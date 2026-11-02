import json
import threading
import time

import pytest


def _messages():
    return [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "I prefer concise technical answers."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Please remember that I prefer concise technical answers."},
    ]


def _tool_call(name="ltm_select_objects", arguments=None, call_id="formation-call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {"projection": "title_summary"})},
    }


def _write_plan(tag_key="user_model"):
    from memory_engine.ltm import meta_memory

    return {
        "action": "write_plan",
        "objects": [
            {
                "object_type": "assertion",
                "title": "Concise response preference",
                "summary": None,
                "payload": {
                    "schema_version": 1,
                    "body": "The user prefers concise technical answers.",
                    "structured_data": None,
                },
                "outgoing_links": [
                    {
                        "relation_type": "tagged_with",
                        "target_object_id": meta_memory.system_tag_object_id(tag_key),
                    }
                ],
            }
        ],
    }


def test_memorize_is_exposed_without_private_retrieval_tools():
    from tools.tool_registry import TOOL_SCHEMAS, tool_schema_names

    names = tool_schema_names()
    assert "memorize" in names
    assert "form_memory" not in names
    assert {"ltm_select_objects", "ltm_search_objects", "ltm_traverse_objects"}.isdisjoint(names)
    schema = next(item for item in TOOL_SCHEMAS if item["function"]["name"] == "memorize")
    assert schema["function"]["parameters"]["required"] == ["instruction"]
    assert set(schema["function"]["parameters"]["properties"]) == {"instruction"}


def test_memorize_commits_with_current_user_provenance(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store, memorize

    messages = _messages()
    event_stream.save_messages(messages)
    runtime_state = {
        "eval_memorize_responses": [
            {"content": "", "tool_calls": [_tool_call()]},
            {"content": json.dumps(_write_plan())},
        ]
    }

    result = memorize.run_memorize(
        "Remember this preference.",
        messages=messages,
        context_snapshot=({"role": "system", "content": "hidden"}, {"role": "user", "content": "context"}),
        runtime_state=runtime_state,
    )

    assert result == {"status": "committed", "reason_code": "stored"}
    objects = ltm_store.list_objects("assertion")
    assert len(objects) == 1
    links = ltm_store.get_links(objects[0]["object_id"], "outgoing")
    assert {(link["relation_type"], link["slot_seq_start"], link["slot_seq_end"]) for link in links} >= {
        ("sourced_from", 2, 3),
        ("tagged_with", None, None),
    }
    observer = runtime_state["memory_engine_memorize_observer"]
    assert observer["memorize_request_count"] == 1
    assert observer["last_result"]["context_snapshot_used"] is True
    assert observer["last_result"]["provenance_slot_count"] == 2
    assert observer["last_result"]["retrieval_success_count"] == 1


def test_memorize_no_memory_does_not_require_retrieval(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store, memorize

    messages = _messages()
    event_stream.save_messages(messages)
    result = memorize.run_memorize(
        "Remember this.",
        messages=messages,
        runtime_state={"eval_memorize_responses": [{"content": json.dumps({"action": "no_memory"})}]},
    )

    assert result == {"status": "no_memory", "reason_code": "not_durable"}
    assert ltm_store.list_objects("assertion") == []


def test_write_plan_without_retrieval_is_reprompted(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store, memorize

    messages = _messages()
    event_stream.save_messages(messages)
    runtime_state = {
        "eval_memorize_responses": [
            {"content": json.dumps(_write_plan())},
            {"content": json.dumps({"action": "no_memory"})},
        ]
    }
    result = memorize.run_memorize("Remember this.", messages=messages, runtime_state=runtime_state)
    assert result == {"status": "no_memory", "reason_code": "not_durable"}
    assert ltm_store.list_objects("assertion") == []


def test_provenance_mismatch_does_not_write(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store, memorize

    messages = _messages()
    event_stream.save_messages(messages[:-1])
    result = memorize.run_memorize(
        "Remember this.",
        messages=messages,
        runtime_state={},
    )
    assert result == {"status": "failed", "reason_code": "provenance_unavailable"}
    assert ltm_store.list_objects("assertion") == []


def test_invalid_root_plan_is_atomic_and_retryable(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store, memorize

    messages = _messages()
    event_stream.save_messages(messages)
    plan = _write_plan("others")
    plan["objects"][0]["outgoing_links"].append(
        {"relation_type": "tagged_with", "target_object_id": "mem_" + "a" * 32}
    )
    result = memorize.run_memorize(
        "Remember this.",
        messages=messages,
        runtime_state={
            "eval_memorize_responses": [
                {"content": "", "tool_calls": [_tool_call()]},
                {"content": json.dumps(plan)},
            ]
        },
    )
    assert result == {"status": "rejected", "reason_code": "invalid_plan"}
    assert ltm_store.list_objects("assertion") == []


def test_previous_assistant_must_be_immediately_before_user(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import memorize

    messages = _messages()[:-1] + [{"role": "tool", "content": "tool result"}, {"role": "user", "content": "Remember this."}]
    event_stream.save_messages(messages)
    runtime_state = {"eval_memorize_responses": [{"content": json.dumps({"action": "no_memory"})}]}
    result = memorize.run_memorize("Remember this.", messages=messages, runtime_state=runtime_state)
    assert result["status"] == "no_memory"


def test_memorize_returns_running_and_publishes_completion_wake(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import memorize

    messages = _messages()
    event_stream.save_messages(messages)
    wakes = []
    completed = threading.Event()
    ui_events = []

    def wake_callback(*args, **kwargs):
        wakes.append((args, kwargs))
        completed.set()
        return True

    runtime_state = {
        "wake_callback": wake_callback,
        "ui_event_sink": ui_events.append,
        "eval_memorize_responses": [{"content": json.dumps({"action": "no_memory"})}],
    }
    result = memorize.run_memorize("Remember this.", messages=messages, runtime_state=runtime_state)

    assert result["status"] == "running"
    assert result["memorize_id"].startswith("memorize_task_")
    assert completed.wait(2)
    assert len(wakes) == 1
    args, kwargs = wakes[0]
    assert args[0:2] == ("memorize", "completion")
    assert kwargs["processing_class"] == "tool_result_like"
    assert kwargs["wake_id"] == "wake_" + result["memorize_id"]
    assert set(args[2]) == {"memorize_id", "status", "reason_code"}
    assert args[2]["status"] == "no_memory"
    assert [event["type"] for event in ui_events] == ["memorize_started", "memorize_finished"]
    assert ui_events[1]["status"] == "no_memory"
    observer = runtime_state["memory_engine_memorize_observer"]
    assert observer["memorize_request_count"] == 1
    assert observer["last_result"]["memorize_id"] == result["memorize_id"]
    assert observer["last_result"]["status"] == "no_memory"


def test_memorize_worker_uses_call_time_snapshot(monkeypatch, isolated_workspace):
    from memory_engine.ltm import memorize

    started = threading.Event()
    release = threading.Event()
    captured = {}

    def fake_impl(instruction, messages, context_snapshot, runtime_state, check_cancel, **kwargs):
        captured["instruction"] = instruction
        captured["messages"] = messages
        captured["context_snapshot"] = context_snapshot
        started.set()
        assert release.wait(2)
        return {"status": "no_memory", "reason_code": "not_durable"}

    monkeypatch.setattr(memorize, "_run_memorize_impl", fake_impl)
    messages = _messages()
    context_snapshot = [{"role": "user", "content": "original context"}]
    wakes = []
    runtime_state = {"wake_callback": lambda *args, **kwargs: wakes.append((args, kwargs)) or True}
    result = memorize.run_memorize(
        "Remember the original instruction.",
        messages=messages,
        context_snapshot=context_snapshot,
        runtime_state=runtime_state,
    )
    assert result["status"] == "running"
    assert started.wait(2)

    messages[-1]["content"] = "mutated after dispatch"
    context_snapshot[0]["content"] = "mutated context"
    release.set()
    deadline = time.time() + 2
    while not wakes and time.time() < deadline:
        time.sleep(0.01)

    assert captured["messages"][-1]["content"] == "Please remember that I prefer concise technical answers."
    assert captured["context_snapshot"][0]["content"] == "original context"


@pytest.mark.parametrize(
    ("status", "reason_code"),
    [("committed", "stored"), ("rejected", "invalid_plan"), ("failed", "timeout")],
)
def test_memorize_worker_wakes_for_terminal_status(monkeypatch, status, reason_code):
    from memory_engine.ltm import memorize

    wakes = []

    def fake_impl(*_args, **_kwargs):
        return {"status": status, "reason_code": reason_code}

    monkeypatch.setattr(memorize, "_run_memorize_impl", fake_impl)
    runtime_state = {"wake_callback": lambda *args, **kwargs: wakes.append((args, kwargs)) or True}
    result = memorize.run_memorize("Remember this.", messages=[], runtime_state=runtime_state)

    assert result["status"] == "running"
    deadline = time.time() + 2
    while not wakes and time.time() < deadline:
        time.sleep(0.01)
    assert len(wakes) == 1
    assert wakes[0][0][2] == {
        "memorize_id": result["memorize_id"],
        "status": status,
        "reason_code": reason_code,
    }


def test_memorize_worker_wakes_when_trajectory_initialization_fails(monkeypatch):
    from memory_engine.ltm import memorize

    wakes = []
    monkeypatch.setattr(memorize, "start_trajectory", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("trajectory unavailable")))
    runtime_state = {"wake_callback": lambda *args, **kwargs: wakes.append((args, kwargs)) or True}

    result = memorize.run_memorize("Remember this.", messages=[], runtime_state=runtime_state)

    assert result["status"] == "running"
    deadline = time.time() + 2
    while not wakes and time.time() < deadline:
        time.sleep(0.01)
    assert wakes[0][0][2] == {
        "memorize_id": result["memorize_id"],
        "status": "failed",
        "reason_code": "memorize_error",
    }
