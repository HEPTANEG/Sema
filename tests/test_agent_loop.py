import subprocess
import time
from contextlib import nullcontext
import json

import pytest

from persistence.trajectory import read_event_projection
from runtime import agent


def _messages(text="hello"):
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": text},
    ]


def _patch_rendering(monkeypatch):

    monkeypatch.setattr(agent, "render_markdown", lambda _text: None)
    monkeypatch.setattr(agent, "render_tool_progress", lambda _tool, _args, _text, verbose=False: None)
    monkeypatch.setattr(agent, "tool_status", lambda _tool, _args: nullcontext())
    monkeypatch.setattr(agent, "model_status", lambda: nullcontext())


def _patch_structured(monkeypatch, replies):
    queue = list(replies)

    def fake_call(_messages, **_kwargs):
        if not queue:
            raise AssertionError("No fake structured replies left.")
        return queue.pop(0)

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    return queue


def test_answer_with_structured_tools_delegates_to_structured_loop(monkeypatch):
    messages = _messages()
    permissions = {"allow_all": True}
    runtime_state = {"trace_id": "trace-1"}
    captured = {}

    def fake_run(current_messages, current_permissions=None, current_runtime_state=None, dependency_source=None):
        captured["messages"] = current_messages
        captured["permissions"] = current_permissions
        captured["runtime_state"] = current_runtime_state
        captured["dependency_source"] = dependency_source
        current_messages.append({"role": "assistant", "content": "delegated"})

    monkeypatch.setattr(agent.structured_loop, "run_structured_loop", fake_run)

    agent.answer_with_structured_tools(messages, permissions, runtime_state)

    assert captured["messages"] is messages
    assert captured["permissions"] is permissions
    assert captured["runtime_state"] is runtime_state
    assert captured["dependency_source"]["run_requested_tool"] is agent.run_requested_tool
    assert messages[-1] == {"role": "assistant", "content": "delegated"}


def test_structured_plain_reply_is_appended_and_saved(monkeypatch):
    messages = _messages()
    saved = []
    order = []
    _patch_rendering(monkeypatch)
    _patch_structured(monkeypatch, [{"content": "Sema final answer.", "tool_calls": []}])

    def fake_save(current):
        order.append("save")
        saved.append(list(current))

    def fake_mark(_runtime_state):
        order.append("mark")

    monkeypatch.setattr(agent, "save_last_session", fake_save)
    monkeypatch.setattr(agent, "mark_assistant_turn_finished", fake_mark)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda _messages, _runtime_state=None: None)

    agent.answer_with_tools(messages)

    assert messages[-1] == {"role": "assistant", "content": "Sema final answer."}
    assert saved[-1] == messages
    assert order == ["save", "save", "mark"]


def test_structured_tool_call_intermediate_reply_does_not_mark_assistant_finished(monkeypatch):
    messages = _messages()
    marks = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "I will inspect it.", "tool_calls": tool_calls},
            {"content": "Done.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "file content")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "mark_assistant_turn_finished", lambda _runtime_state: marks.append(True))
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda _messages, _runtime_state=None: None)

    agent.answer_with_tools(messages)

    assert marks == [True]
    assert messages[-3] == {"role": "assistant", "content": "I will inspect it.", "tool_calls": tool_calls}
    assert messages[-2] == {"role": "tool", "tool_call_id": "call_1", "content": "file content"}
    assert messages[-1] == {"role": "assistant", "content": "Done."}


def test_structured_tool_chain_is_persisted_in_message_order(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.event_stream as event_stream

    session.ensure_project()
    messages = agent.create_initial_messages()
    session.save_last_session(messages)
    _patch_rendering(monkeypatch)
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'},
        }
    ]
    _patch_structured(
        monkeypatch,
        [
            {"content": "Inspecting.", "tool_calls": tool_calls},
            {"content": "Finished.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "file output")
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda *_args, **_kwargs: None)

    agent.answer_with_tools(messages)

    persisted = event_stream.load_messages()
    assert [message["role"] for message in persisted] == ["system", "assistant", "tool", "assistant"]
    assert persisted == messages


def test_provider_failure_keeps_already_persisted_tool_tail(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.event_stream as event_stream

    session.ensure_project()
    messages = agent.create_initial_messages()
    session.save_last_session(messages)
    _patch_rendering(monkeypatch)
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'},
        }
    ]
    replies = iter([{"content": "Inspecting.", "tool_calls": tool_calls}])

    def fake_call(_messages, **_kwargs):
        try:
            return next(replies)
        except StopIteration:
            raise RuntimeError("provider failed")

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "file output")

    with pytest.raises(RuntimeError, match="provider failed"):
        agent.answer_with_tools(messages)

    assert event_stream.load_messages() == messages
    assert [message["role"] for message in messages] == ["system", "assistant", "tool"]


def test_runtime_payload_includes_current_clock_directive(monkeypatch):
    messages = _messages("research this year")
    runtime_state = {
        "intent": {"mode": "coordinator", "capabilities": ["web"], "confidence": 0.9},
    }

    payload = agent.build_model_tools_payload(messages, runtime_state=runtime_state)

    contents = [message["content"] for message in payload["messages"] if message["role"] == "system"]
    assert any("Current clock for this request only." in content for content in contents)
    assert any("This is deterministic runtime context, not a Temporal Agent decision." in content for content in contents)
    assert messages == _messages("research this year")


def test_runtime_directive_trace_records_clock_injection(monkeypatch):
    from runtime.directives import messages_with_runtime_directives

    records = []
    monkeypatch.setattr(agent, "trace_event", lambda state, event, **fields: records.append((state, event, fields)))
    runtime_state = {"trace_id": "trace-1"}

    output = messages_with_runtime_directives(
        _messages("2026啊当然是"),
        runtime_state,
        trace_writer=lambda _trace_id, event_type, **fields: records.append((runtime_state, event_type, fields)),
    )

    assert any(message["role"] == "system" and "Current clock for this request only." in message["content"] for message in output)
    assert len(records) == 1
    assert records[0][0] is runtime_state
    assert records[0][1] == "runtime_directives"
    assert records[0][2] == {
        "status": "ok",
        "model_request_index": 1,
        "clock_message": True,
        "directive_count": 1,
        "clock_local_date": records[0][2]["clock_local_date"],
        "clock_timezone": records[0][2]["clock_timezone"],
    }
def test_get_object_tool_uses_shared_read_only_observation_hook(monkeypatch):
    captured = {}

    def fake_get_object(args):
        captured["args"] = args
        return {"status": "ok", "items": [{"object_id": "mem_a", "title": "Desktop files"}]}

    monkeypatch.setattr(agent, "run_get_object", fake_get_object)
    result = agent.run_requested_tool({
        "tool": "get_object",
        "args": {"query": "desktop files", "projection": "title_summary", "limit": 1},
    })

    assert captured["args"] == {"query": "desktop files", "projection": "title_summary", "limit": 1}
    assert json.loads(result)["items"][0]["object_id"] == "mem_a"


def test_get_object_tool_does_not_create_retrieval_session(monkeypatch):
    monkeypatch.setattr(
        agent,
        "run_get_object",
        lambda _args: {"status": "ok", "items": []},
    )
    runtime_state = {}

    result = agent.run_requested_tool(
        {"tool": "get_object", "args": {"query": "explicit lookup"}},
        runtime_state=runtime_state,
    )

    assert json.loads(result) == {"status": "ok", "items": []}
    assert "memory_retrieval_session" not in runtime_state


def test_image_context_expires_after_final_answer(monkeypatch, tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Image context added: one.png. Use this image."},
                {"type": "sema_image", "path": str(image), "filename": "one.png", "mime_type": "image/png"},
            ],
        },
        {"role": "user", "content": "What is in this image?"},
    ]
    seen_messages = []
    saved = []
    _patch_rendering(monkeypatch)

    def fake_call(current, **_kwargs):

        seen_messages.append(json.loads(json.dumps(current)))
        return {"content": "It is an image.", "tool_calls": []}

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "save_last_session", lambda current: saved.append(json.loads(json.dumps(current))))

    agent.answer_with_tools(messages)

    assert seen_messages[0][1]["content"][1]["type"] == "sema_image"
    assert messages[1]["content"] == [
        {
            "type": "text",
            "text": "Expired image context, not sent again: one.png.",
        }
    ]
    assert saved[-1] == messages


def test_structured_plain_reply_emits_ui_events(monkeypatch):
    messages = _messages()
    events = []
    runtime_state = {"ui_event_sink": events.append}
    _patch_rendering(monkeypatch)
    _patch_structured(monkeypatch, [{"content": "Sema final answer.", "tool_calls": []}])
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda *_args, **_kwargs: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    assert [event["type"] for event in events] == [
        "turn_start",
        "model_thinking",
        "model_finished",
        "final_answer",
    ]
    assert events[-1]["content"] == "Sema final answer."


def test_streaming_plain_reply_emits_delta_and_final_with_same_message_id(monkeypatch):
    messages = _messages()
    events = []
    runtime_state = {"ui_event_sink": events.append, "streaming_enabled": True}
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    def fake_stream(_messages, **kwargs):
        kwargs["on_delta"]("Hel")
        kwargs["on_delta"]("lo")
        return {"content": "Hello", "tool_calls": []}

    monkeypatch.setattr(agent, "call_chat_completion_stream", fake_stream)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    delta_events = [event for event in events if event["type"] == "assistant_delta"]
    final_events = [event for event in events if event["type"] == "final_answer"]
    assert [event["delta"] for event in delta_events] == ["Hel", "lo"]
    assert final_events[-1]["content"] == "Hello"
    assert final_events[-1]["message_id"] == delta_events[0]["message_id"]
    assert messages[-1] == {"role": "assistant", "content": "Hello"}


def test_main_agent_provider_call_passes_trajectory_exchange_metadata(monkeypatch):
    messages = _messages()
    runtime_state = {"trace_id": "trace-1"}
    recorded = []
    provider_context = [{"role": "user", "content": "projected context"}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    monkeypatch.setattr(agent, "messages_for_model", lambda *_args, **_kwargs: provider_context)
    monkeypatch.setattr(agent, "tool_schemas_for_model", lambda _runtime_state=None: tools)
    class Observer:
        def on_request(self, _raw, payload, metadata):
            recorded.append((payload, metadata))
    monkeypatch.setattr(agent.model_runtime, "provider_exchange_observer", lambda *_args, **_kwargs: Observer())

    def fake_call(current, **kwargs):
        kwargs["exchange_observer"].on_request(
            b"{}",
            {"model": "test-model", "messages": current, "stream": False, "tools": kwargs["tools"]},
            kwargs["exchange_metadata"],
        )
        return {"choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": []}}]}

    monkeypatch.setattr(agent, "call_chat_completion", fake_call)

    result = agent.call_model_with_tools(messages, runtime_state=runtime_state)

    assert result["content"] == "ok"
    assert recorded[0][0]["messages"] == provider_context
    assert recorded[0][0]["tools"] == tools
    assert recorded[0][1]["purpose"] == "tool calling"
    assert recorded[0][1]["streaming"] is False
    assert recorded[0][1]["trajectory_id"] == ""
    assert recorded[0][1]["assistant_message_id"].startswith("assistant-")


def test_streaming_fallback_records_each_actual_provider_exchange(monkeypatch):
    messages = _messages()
    runtime_state = {"streaming_enabled": True}
    recorded = []
    provider_context = [{"role": "user", "content": "projected context"}]

    monkeypatch.setattr(agent, "messages_for_model", lambda *_args, **_kwargs: provider_context)
    monkeypatch.setattr(agent, "tool_schemas_for_model", lambda _runtime_state=None: [])
    monkeypatch.setattr(agent, "ui_event", lambda *_args, **_kwargs: None)
    class Observer:
        def on_request(self, _raw, payload, metadata):
            recorded.append((payload, metadata))
    monkeypatch.setattr(agent.model_runtime, "provider_exchange_observer", lambda *_args, **_kwargs: Observer())

    def fake_stream(current, **kwargs):
        kwargs["exchange_observer"].on_request(
            b"{}",
            {"model": "test-model", "messages": current, "stream": True, "tools": kwargs["tools"]},
            kwargs["exchange_metadata"],
        )
        raise RuntimeError("stream unsupported")

    def fake_call(current, **kwargs):
        kwargs["exchange_observer"].on_request(
            b"{}",
            {"model": "test-model", "messages": current, "stream": False, "tools": kwargs["tools"]},
            kwargs["exchange_metadata"],
        )
        return {"choices": [{"message": {"role": "assistant", "content": "fallback", "tool_calls": []}}]}

    monkeypatch.setattr(agent, "call_chat_completion_stream", fake_stream)
    monkeypatch.setattr(agent, "call_chat_completion", fake_call)

    result = agent.call_model_with_tools(messages, runtime_state=runtime_state)

    assert result["content"] == "fallback"
    assert [payload["stream"] for payload, _metadata in recorded] == [True, False]
    assert [metadata["streaming"] for _payload, metadata in recorded] == [True, False]


def test_streaming_tool_call_buffers_tool_until_complete(monkeypatch):
    messages = _messages()
    events = []
    runtime_state = {"ui_event_sink": events.append, "streaming_enabled": True, "last_run": []}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"main.py"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "file content")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    replies = iter([
        {"content": "I will inspect it.", "tool_calls": tool_calls},
        {"content": "Done.", "tool_calls": []},
    ])

    def fake_stream(_messages, **kwargs):
        reply = next(replies)
        if reply["content"]:
            kwargs["on_delta"](reply["content"])
        return reply

    monkeypatch.setattr(agent, "call_chat_completion_stream", fake_stream)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    assert any(event["type"] == "assistant_delta" and event["delta"] == "I will inspect it." for event in events)
    assert any(event["type"] == "tool_started" and event["tool"] == "read_file" for event in events)
    assert not any(event["type"] == "assistant_content" and event.get("content") == "I will inspect it." for event in events)
    assert messages[-3] == {"role": "assistant", "content": "I will inspect it.", "tool_calls": tool_calls}
    assert messages[-2] == {"role": "tool", "tool_call_id": "call_1", "content": "file content"}
    assert messages[-1] == {"role": "assistant", "content": "Done."}


def test_event_stream_final_answer_background_tasks_use_working_memory(monkeypatch):
    messages = _messages()
    calls = []
    runtime_state = {"trace_id": "trace-1"}

    monkeypatch.setattr(agent, "schedule_event_stream_memory_maintenance", lambda current, state=None: calls.append(("event_stream_memory", list(current), state)))

    result = agent.schedule_final_answer_background_tasks(messages, runtime_state)

    assert result == [None]
    assert calls == [("event_stream_memory", messages, runtime_state)]


def test_structured_read_file_tool_result_uses_tool_role(monkeypatch):
    messages = _messages()
    runtime_state = {"last_run": []}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"main.py"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "", "tool_calls": tool_calls},
            {"content": "I read the file.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "--- main.py ---\nprint(1)\n--- end ---")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    assert messages[-3] == {"role": "assistant", "content": "", "tool_calls": tool_calls}
    assert messages[-2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "--- main.py ---\nprint(1)\n--- end ---",
    }
    assert messages[-1] == {"role": "assistant", "content": "I read the file."}
    assert runtime_state["last_run"] == [
        {
            "tool": "read_file",
            "args": {"path": "main.py"},
            "result": "--- main.py ---\nprint(1)\n--- end ---",
        }
    ]


def test_run_requested_tool_lists_images(monkeypatch):
    monkeypatch.setattr(agent, "list_images_for_context", lambda path, recursive=False, allow_external=False: f"images {path} {recursive} {allow_external}")

    result = agent.run_requested_tool(
        {
            "tool": "list_images",
            "args": {"path": "shots", "recursive": True},
        }
    )

    assert result == "images shots True False"


def test_run_requested_tool_analyzes_images(monkeypatch):
    monkeypatch.setattr(
        agent,
        "analyze_images_for_context",
        lambda paths, instruction, allow_external=False: f"analyzed {len(paths)} {instruction} {allow_external}",
    )
    entered_status = []

    class FakeStatus:
        def __enter__(self):
            entered_status.append(True)

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    monkeypatch.setattr(agent, "tool_status", lambda _tool, _args: FakeStatus())

    result = agent.run_requested_tool(
        {
            "tool": "vision_analyze_images",
            "args": {"paths": ["one.png", "two.jpg"], "instruction": "Convert."},
        }
    )

    assert result == "analyzed 2 Convert. False"
    assert entered_status == [True]


def test_run_requested_tool_confirms_external_image_directory(monkeypatch):
    confirmations = []
    monkeypatch.setattr(agent, "path_outside_workspace", lambda path: path.startswith("D:"))
    monkeypatch.setattr(
        agent,
        "confirm_external_path_access",
        lambda action, details, _permissions=None: confirmations.append((action, details)) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(agent, "list_images_for_context", lambda path, recursive=False, allow_external=False: f"{path} {allow_external}")

    result = agent.run_requested_tool(
        {
            "tool": "list_images",
            "args": {"path": "D:\\Pictures", "recursive": False},
        }
    )

    assert result == "D:\\Pictures True"
    assert confirmations == [("read image directory", "D:\\Pictures")]


def test_allow_all_bypasses_external_image_confirmation(monkeypatch):
    def fail_if_called(_action, _details, _permissions=None):
        raise AssertionError("external path confirmation should not be called")

    monkeypatch.setattr(agent, "path_outside_workspace", lambda _path: True)
    monkeypatch.setattr(agent, "confirm_external_path_access", fail_if_called)
    monkeypatch.setattr(agent, "list_images_for_context", lambda path, recursive=False, allow_external=False: f"{path} {allow_external}")

    result = agent.run_requested_tool(
        {
            "tool": "list_images",
            "args": {"path": "D:\\Pictures", "recursive": False},
        },
        permissions={"allow_all": True},
    )

    assert result == "D:\\Pictures True"


def test_tool_failure_key_accepts_dict_args():
    key = agent.tool_failure_key(
        {
            "tool": "list_images",
            "args": {"path": "D:\\Pictures", "recursive": True},
        },
        RuntimeError("Cannot list directories outside the current working directory."),
    )

    assert key[0] == "list_images"
    assert '"recursive": true' in key[1]
    assert "Cannot list directories" in key[2]


def test_structured_tool_call_preserves_reasoning_content(monkeypatch):
    messages = _messages()
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "show_memory_state",
                "arguments": "{}",
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {
                "content": "",
                "reasoning_content": "Need to inspect Memory Engine status.",
                "tool_calls": tool_calls,
            },
            {"content": "I checked memory.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "No memory.")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert messages[-3]["role"] == "assistant"
    assert messages[-3]["tool_calls"] == tool_calls
    assert messages[-3]["reasoning_content"] == "Need to inspect Memory Engine status."


def test_malformed_tool_arguments_are_returned_as_tool_result(monkeypatch):
    messages = _messages("inspect")
    tool_calls = [
        {
            "id": "call_bad",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"unterminated',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "I will inspect it.", "tool_calls": tool_calls},
            {"content": "The tool call was malformed.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert messages[-3] == {"role": "assistant", "content": "I will inspect it.", "tool_calls": tool_calls}
    assert messages[-2]["role"] == "tool"
    assert messages[-2]["tool_call_id"] == "call_bad"
    assert "Invalid tool arguments JSON" in messages[-2]["content"]
    assert messages[-1] == {"role": "assistant", "content": "The tool call was malformed."}


def test_structured_tool_call_emits_ui_events(monkeypatch):
    messages = _messages()
    events = []
    runtime_state = {"last_run": [], "ui_event_sink": events.append}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"main.py"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "I will inspect the file.", "tool_calls": tool_calls},
            {"content": "I read the file.", "tool_calls": []},
        ],
    )
    def fake_run_requested_tool(_request, **kwargs):
        kwargs["runtime_state"]["_last_edit_stats"] = {"tool": "write_file", "added": 3, "removed": 1, "files": ["main.py"]}
        return "tool result"

    monkeypatch.setattr(agent, "run_requested_tool", fake_run_requested_tool)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    event_types = [event["type"] for event in events]
    assert "assistant_content" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert events[event_types.index("tool_started")]["tool"] == "read_file"
    assert events[event_types.index("tool_finished")]["result"] == "tool result"
    assert events[event_types.index("tool_finished")]["edit_stats"] == {"tool": "write_file", "added": 3, "removed": 1, "files": ["main.py"]}
    assert "_last_edit_stats" not in runtime_state


def test_recall_current_receives_actual_pre_call_provider_context(monkeypatch):
    messages = _messages("recall a childhood detail")
    provider_context = [
        {"role": "system", "content": "actual projected system"},
        {"role": "user", "content": "projected user context"},
    ]
    recall_call = {
        "id": "recall-1",
        "type": "function",
        "function": {
            "name": "retrieve_memory",
            "arguments": json.dumps(
                {"query": "childhood detail", "include_context": True}
            ),
        },
    }
    replies = [
        {
            "content": "",
            "tool_calls": [recall_call],
            "_provider_context_messages": provider_context,
        },
        {"content": "Done.", "tool_calls": [], "_provider_context_messages": provider_context},
    ]
    captured = {}
    _patch_rendering(monkeypatch)

    def fake_call(_messages, **_kwargs):
        return replies.pop(0)

    def fake_tool(_request, **kwargs):
        captured["snapshot"] = kwargs["context_snapshot"]
        return json.dumps({"status": "not_found", "recall_material": [], "context_used": True})

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "run_requested_tool", fake_tool)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda *_args: None)

    agent.answer_with_tools(messages)

    assert captured["snapshot"] == tuple(provider_context)
    assert captured["snapshot"] is not provider_context
    assert all("retrieve_memory" not in str(message) for message in captured["snapshot"])


def test_recall_none_does_not_capture_provider_context(monkeypatch):
    messages = _messages("recall a preference")
    recall_call = {
        "id": "recall-1",
        "type": "function",
        "function": {
            "name": "retrieve_memory",
            "arguments": json.dumps(
                {"query": "preference"}
            ),
        },
    }
    replies = [
        {"content": "", "tool_calls": [recall_call], "_provider_context_messages": [{"role": "user", "content": "actual"}]},
        {"content": "Done.", "tool_calls": [], "_provider_context_messages": []},
    ]
    captured = {}
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "call_model_with_tools", lambda _messages, **_kwargs: replies.pop(0))
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **kwargs: captured.setdefault("snapshot", kwargs["context_snapshot"]) or "ok")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda *_args: None)

    agent.answer_with_tools(messages)

    assert captured["snapshot"] is None


def test_multiple_current_recalls_share_one_pre_call_snapshot(monkeypatch):
    messages = _messages("recall two details")
    provider_context = [{"role": "user", "content": "projected clues"}]
    tool_calls = [
        {
            "id": f"recall-{index}",
            "type": "function",
            "function": {
                "name": "retrieve_memory",
                "arguments": json.dumps(
                    {"query": f"detail {index}", "include_context": True}
                ),
            },
        }
        for index in (1, 2)
    ]
    replies = [
        {"content": "", "tool_calls": tool_calls, "_provider_context_messages": provider_context},
        {"content": "Done.", "tool_calls": [], "_provider_context_messages": provider_context},
    ]
    snapshots = []
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "call_model_with_tools", lambda _messages, **_kwargs: replies.pop(0))
    monkeypatch.setattr(
        agent,
        "run_requested_tool",
        lambda _request, **kwargs: snapshots.append(kwargs["context_snapshot"]) or "ok",
    )
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda *_args: None)

    agent.answer_with_tools(messages)

    assert len(snapshots) == 2
    assert snapshots[0] is snapshots[1]
    assert snapshots[0] == tuple(provider_context)


def test_recall_tool_executor_passes_snapshot_and_returns_json(monkeypatch):
    captured = {}
    snapshot = ({"role": "user", "content": "projected context"},)

    def fake_recall(request, **kwargs):
        captured["request"] = request
        captured["snapshot"] = kwargs["context_snapshot"]
        return {"status": "not_found", "recall_material": [], "context_used": True}

    monkeypatch.setattr(agent, "run_retrieve_memory", fake_recall)
    result = agent.run_requested_tool(
        {
            "tool": "retrieve_memory",
            "args": {
                "query": "childhood detail",
                "include_context": True,
            },
        },
        runtime_state={},
        context_snapshot=snapshot,
    )

    assert captured["request"]["query"] == "childhood detail"
    assert captured["snapshot"] is snapshot
    assert json.loads(result) == {
        "status": "not_found",
        "recall_material": [],
        "context_used": True,
    }


def test_memorize_trace_does_not_conflict_with_result_status(monkeypatch):
    trace_records = []

    def fake_memorize(_instruction, **kwargs):
        kwargs["runtime_state"]["memory_engine_memorize_observer"] = {
            "last_result": {
                "status": "committed",
                "reason_code": "stored",
                "commit_status": "committed",
            }
        }
        return {"status": "committed", "reason_code": "stored"}

    monkeypatch.setattr(agent, "run_memorize", fake_memorize)
    monkeypatch.setattr(agent, "trace_event", lambda _state, event, **fields: trace_records.append((event, fields)))
    runtime_state = {}

    result = agent.run_requested_tool(
        {"tool": "memorize", "args": {"instruction": "Remember this."}},
        runtime_state=runtime_state,
    )

    assert json.loads(result) == {"status": "committed", "reason_code": "stored"}
    assert trace_records == [
        (
            "ltm_memorize",
            {"status": "ok", "path": "tool", "reason_code": "stored", "commit_status": "committed"},
        )
    ]


def test_structured_tool_success_writes_trace(monkeypatch, isolated_workspace):
    messages = _messages()
    runtime_state = {"last_run": []}
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"main.py"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "", "tool_calls": tool_calls},
            {"content": "I read the file.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "--- main.py ---\nprint(1)\n--- end ---")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    records = read_event_projection(runtime_state["trajectory_context"]["trajectory_id"], limit=100)
    tool_records = [record for record in records if record["event"] == "tool"]
    assert tool_records[-1]["tool"] == "read_file"
    assert tool_records[-1]["status"] == "ok"
    assert tool_records[-1]["args_summary"] == {"path": "main.py"}
    assert tool_records[-1]["captured_chars"] == len("--- main.py ---\nprint(1)\n--- end ---")
    assert "--- main.py ---" not in json.dumps(tool_records[-1], ensure_ascii=False)


def test_structured_tool_call_renders_intermediate_content(monkeypatch):
    messages = _messages()
    rendered = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"main.py"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "render_markdown", lambda text: rendered.append(text))
    _patch_structured(
        monkeypatch,
        [
            {"content": "我先读取相关文件。", "tool_calls": tool_calls},
            {"content": "文件读完了。", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "--- main.py ---\nprint(1)\n--- end ---")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert rendered == ["我先读取相关文件。", "文件读完了。"]
    assert messages[-3]["content"] == "我先读取相关文件。"


def test_structured_loop_uses_model_status(monkeypatch):
    messages = _messages()
    status_count = 0

    class CountingStatus:
        def __enter__(self):
            nonlocal status_count
            status_count += 1

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "model_status", lambda: CountingStatus())
    _patch_structured(monkeypatch, [{"content": "Done.", "tool_calls": []}])
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert status_count == 1


def test_structured_replace_block_success_adds_final_answer_instruction(monkeypatch):
    messages = _messages()
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "replace_block",
                "arguments": '{"path":"app.py","old":"old","new":"new"}',
            },
        }
    ]
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "", "tool_calls": tool_calls},
            {"content": "Changed app.py.", "tool_calls": []},
        ],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "Replaced block in: app.py")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert messages[-2]["role"] == "tool"
    assert "edit/write tool succeeded" in messages[-2]["content"]
    assert "do not request another tool" in messages[-2]["content"]
    assert messages[-1]["content"] == "Changed app.py."


def test_confirmation_tools_do_not_use_spinner():
    assert not agent.should_show_tool_status("write_file")
    assert not agent.should_show_tool_status("run_command")
    assert not agent.should_show_tool_status("insert_after")
    assert not agent.should_show_tool_status("replace_block")
    assert not agent.should_show_tool_status("read_file")
    assert not agent.should_show_tool_status("list_images")


def test_structured_same_failure_point_gets_two_retries_then_forces_explanation(monkeypatch):
    messages = _messages()
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"missing.txt"}',
        },
    }
    _patch_rendering(monkeypatch)
    _patch_structured(
        monkeypatch,
        [
            {"content": "", "tool_calls": [tool_call]},
            {"content": "", "tool_calls": [tool_call]},
            {"content": "", "tool_calls": [tool_call]},
            {"content": "", "tool_calls": [tool_call]},
            {"content": "I could not read the file.", "tool_calls": []},
        ],
    )

    def fail_same_way(_request, **_kwargs):
        raise RuntimeError("missing")

    monkeypatch.setattr(agent, "run_requested_tool", fail_same_way)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert messages[-2]["role"] == "tool"
    assert "already been retried two times" in messages[-2]["content"]
    assert messages[-1]["content"] == "I could not read the file."


def test_structured_loop_allows_more_than_previous_tool_request_limit(monkeypatch):
    messages = _messages()
    events = []
    runtime_state = {"ui_event_sink": events.append}
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "list_files",
            "arguments": '{"path":"."}',
        },
    }
    _patch_rendering(monkeypatch)
    tool_rounds = 105
    _patch_structured(
        monkeypatch,
        [{"content": "", "tool_calls": [tool_call]}] * tool_rounds + [{"content": "Done.", "tool_calls": []}],
    )
    monkeypatch.setattr(agent, "run_requested_tool", lambda _request, **_kwargs: "--- directory: . ---\napp.py\n--- end ---")
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    assert not [event for event in events if event.get("type") == "loop_limit"]
    assert len([message for message in messages if message["role"] == "tool"]) == tool_rounds
    assert messages[-1]["content"] == "Done."


def test_structured_path_error_does_not_fallback_to_text_loop(monkeypatch):
    messages = _messages()
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "call_model_with_tools", lambda _messages, **_kwargs: (_ for _ in ()).throw(RuntimeError("Unknown structured tool call: bad_tool")))
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    with pytest.raises(RuntimeError, match="Unknown structured tool call"):
        agent.answer_with_tools(messages)

    assert messages == _messages()


def test_structured_path_error_writes_agent_error_trace(monkeypatch, isolated_workspace):
    messages = _messages()
    runtime_state = {"last_run": []}
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(agent, "call_model_with_tools", lambda _messages, **_kwargs: (_ for _ in ()).throw(RuntimeError("Unknown structured tool call: bad_tool")))
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    with pytest.raises(RuntimeError, match="Unknown structured tool call"):
        agent.answer_with_tools(messages, runtime_state=runtime_state)

    records = read_event_projection(runtime_state["trajectory_context"]["trajectory_id"], limit=100)
    error_records = [record for record in records if record["event"] == "agent_error"]
    assert error_records
    assert error_records[-1]["path"] == "structured"
    assert "Unknown structured tool call" in error_records[-1]["error"]


def test_api_timeout_becomes_final_recoverable_message(monkeypatch):
    messages = _messages()
    saved = []
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(
        agent,
        "call_model_with_tools",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(agent.model_timeout_message())
        ),
    )
    monkeypatch.setattr(agent, "save_last_session", lambda current: saved.append(json.loads(json.dumps(current))))

    agent.answer_with_tools(messages)

    assert "Model request failed:" in messages[-1]["content"]
    assert "request timed out" in messages[-1]["content"]
    assert saved[-1] == messages


def test_incomplete_provider_response_becomes_final_recoverable_message(monkeypatch):
    messages = _messages()
    _patch_rendering(monkeypatch)
    monkeypatch.setattr(
        agent,
        "call_model_with_tools",
        lambda _messages, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Could not complete deepseek:deepseek-chat tool calling API request: incomplete response: IncompleteRead(0 bytes read)")
        ),
    )
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert "Model request failed:" in messages[-1]["content"]
    assert "IncompleteRead(0 bytes read)" in messages[-1]["content"]


def test_api_timeout_retries_before_final_answer(monkeypatch):
    messages = _messages()
    calls = []
    _patch_rendering(monkeypatch)

    def fake_call(_messages, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError(agent.model_timeout_message())
        return {"content": "Recovered.", "tool_calls": []}

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert len(calls) == 2
    assert messages[-1]["content"] == "Recovered."


def test_context_limit_error_does_not_retry(monkeypatch):
    messages = _messages()
    calls = []
    _patch_rendering(monkeypatch)

    def fake_call(_messages, **_kwargs):
        calls.append(True)
        raise RuntimeError("kimi:kimi-k2.6 tool calling API error 400: request exceeded model token limit")

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert len(calls) == 1
    assert "model token limit" in messages[-1]["content"]


def test_stm_context_build_error_becomes_final_answer(monkeypatch):
    messages = _messages()
    calls = []
    _patch_rendering(monkeypatch)

    def fake_call(_messages, **_kwargs):
        calls.append(True)
        raise RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000.")

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert len(calls) == 1
    assert "Context build failed:" in messages[-1]["content"]
    assert "STM V1 keeps complete interaction blocks only" in messages[-1]["content"]


def test_context_builder_error_becomes_final_answer(monkeypatch):
    messages = _messages()
    calls = []
    _patch_rendering(monkeypatch)

    def fake_call(_messages, **_kwargs):
        calls.append(True)
        raise RuntimeError("Context Builder WM slot exceeds budget: slot_seq=1, tokens=7000, budget=6000.")

    monkeypatch.setattr(agent, "call_model_with_tools", fake_call)
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)

    agent.answer_with_tools(messages)

    assert len(calls) == 1
    assert "Context build failed:" in messages[-1]["content"]
    assert "overlarge working-memory slot" in messages[-1]["content"]


def test_context_build_failure_saturates_budget_before_rebuild(monkeypatch):
    messages = _messages("oversized")
    attempts = []
    recoveries = []
    runtime_state = {}
    _patch_rendering(monkeypatch)

    def fake_messages_for_model(current, retrieval_plan=None, runtime_state=None):
        attempts.append(list(current))
        if len(attempts) == 1:
            raise RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000.")
        return current

    monkeypatch.setattr(agent, "messages_for_model", fake_messages_for_model)
    monkeypatch.setattr(agent, "attempt_preflight_working_memory_recovery", lambda error, current, state=None: recoveries.append((str(error), list(current))) or {"triggered": True, "updated": True, "reason": "preflight_recovery"})
    monkeypatch.setattr(agent, "call_chat_completion", lambda *_args, **_kwargs: {"choices": [{"message": {"content": "Recovered.", "tool_calls": []}}]})
    monkeypatch.setattr(agent, "parse_chat_completion_message", lambda data: data["choices"][0]["message"])
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "mark_assistant_turn_finished", lambda _runtime_state: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda _messages, _runtime_state=None: None)

    agent.answer_with_tools(messages, runtime_state=runtime_state)

    assert len(attempts) == 2
    assert recoveries == []
    assert "context_build_failure" not in runtime_state
    assert messages[-1]["content"] == "Recovered."


def test_context_build_failure_over_max_runs_preflight_wm(monkeypatch):
    messages = _messages("oversized")
    attempts = []
    recoveries = []
    _patch_rendering(monkeypatch)

    def fake_messages_for_model(current, retrieval_plan=None, runtime_state=None):
        attempts.append(list(current))
        if len(attempts) == 1:
            raise RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=60000, budget=10000.")
        return current

    monkeypatch.setattr(agent, "messages_for_model", fake_messages_for_model)
    monkeypatch.setattr(agent, "attempt_preflight_working_memory_recovery", lambda error, current, state=None: recoveries.append((str(error), list(current))) or {"triggered": True, "updated": True, "reason": "preflight_recovery"})
    monkeypatch.setattr(agent, "call_chat_completion", lambda *_args, **_kwargs: {"choices": [{"message": {"content": "Recovered.", "tool_calls": []}}]})
    monkeypatch.setattr(agent, "parse_chat_completion_message", lambda data: data["choices"][0]["message"])
    monkeypatch.setattr(agent, "save_last_session", lambda _messages: None)
    monkeypatch.setattr(agent, "mark_assistant_turn_finished", lambda _runtime_state: None)
    monkeypatch.setattr(agent, "schedule_final_answer_background_tasks", lambda _messages, _runtime_state=None: None)

    agent.answer_with_tools(messages, runtime_state={})

    assert len(attempts) == 2
    assert len(recoveries) == 1
    assert recoveries[0][1] == _messages("oversized")
    assert messages[-1]["content"] == "Recovered."


def test_outbound_context_error_preflight_recovers(monkeypatch):
    messages = _messages("oversized")
    attempts = []
    recoveries = []

    def fake_messages_for_model(current, runtime_state=None):
        attempts.append(list(current))
        if len(attempts) == 1:
            raise RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=60000, budget=10000.")
        return current

    monkeypatch.setattr(agent, "messages_for_model", fake_messages_for_model)
    monkeypatch.setattr(agent, "attempt_preflight_working_memory_recovery", lambda error, current, state=None: recoveries.append((str(error), list(current))) or {"triggered": True, "updated": True, "reason": "preflight_recovery"})

    result = agent.outbound_context_error_message(messages, runtime_state={})

    assert result == ""
    assert len(attempts) == 2
    assert len(recoveries) == 1


def test_outbound_context_error_marks_preflight_and_restores_state(monkeypatch):
    phases = []

    def fake_messages_for_model(_current, runtime_state=None):
        phases.append(runtime_state.get("_context_build_phase"))
        return []

    monkeypatch.setattr(agent, "messages_for_model", fake_messages_for_model)
    runtime_state = {}

    assert agent.outbound_context_error_message(_messages(), runtime_state) == ""
    assert phases == ["preflight"]
    assert "_context_build_phase" not in runtime_state


def test_outbound_context_error_restores_preexisting_phase_on_error(monkeypatch):
    def fail(_current, runtime_state=None):
        assert runtime_state["_context_build_phase"] == "preflight"
        raise ValueError("context check failed")

    monkeypatch.setattr(agent, "messages_for_model", fail)
    runtime_state = {"_context_build_phase": "outer"}

    with pytest.raises(ValueError, match="context check failed"):
        agent.outbound_context_error_message(_messages(), runtime_state)

    assert runtime_state["_context_build_phase"] == "outer"


def test_run_command_tool_requires_confirmation(monkeypatch):
    confirmations = []
    monkeypatch.setattr(
        agent,
        "confirm_run_command",
        lambda command, permissions: confirmations.append((command, permissions)) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(agent, "run_command_for_context", lambda _command: (0, "command output"))

    result = agent.run_requested_tool(
        {
            "tool": "run_command",
            "args": "python --version",
        },
        permissions={},
    )

    assert result == "command output"
    assert confirmations == [("python --version", {})]


def test_run_command_nonzero_exit_is_a_tool_failure(monkeypatch):
    monkeypatch.setattr(agent, "confirm_run_command", lambda _command, _permissions: {"allowed": True, "reason": ""})
    monkeypatch.setattr(
        agent,
        "run_command_for_context",
        lambda _command: (255, "--- command: missing-tool ---\nexit code: 255\n(no output)\n--- end ---"),
    )

    with pytest.raises(RuntimeError, match="Command exited with code 255"):
        agent.run_requested_tool(
            {"tool": "run_command", "args": "missing-tool"},
            permissions={},
        )


def test_start_background_command_tool_requires_confirmation(monkeypatch):
    confirmations = []
    monkeypatch.setattr(
        agent,
        "confirm_run_command",
        lambda command, permissions: confirmations.append((command, permissions)) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(agent, "start_background_command", lambda command, cwd=None: f"started {command} cwd={cwd}")

    result = agent.run_requested_tool(
        {
            "tool": "start_background_command",
            "args": {"command": "npm run dev", "cwd": "app"},
        },
        permissions={},
    )

    assert result == "started npm run dev cwd=app"
    assert confirmations == [("npm run dev", {})]


def test_run_requested_tool_checks_security_policy_first(monkeypatch):
    calls = []
    original = agent.classify_tool_request

    def recording_policy(tool_request, permissions=None):
        calls.append((tool_request["tool"], permissions))
        return original(tool_request, permissions)

    monkeypatch.setattr(agent, "classify_tool_request", recording_policy)
    monkeypatch.setattr(agent, "list_directory_for_context", lambda _path, allow_external=False: f"listed {allow_external}")

    result = agent.run_requested_tool(
        {
            "tool": "list_files",
            "args": ".",
        },
        permissions={},
    )

    assert result == "listed False"
    assert calls == [("list_files", {})]


def test_run_requested_tool_uses_agent_monkeypatched_runtime_hooks(monkeypatch):
    confirmations = []
    monkeypatch.setattr(
        agent,
        "confirm_run_command",
        lambda command, permissions: confirmations.append((command, permissions)) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(agent, "run_command_for_context", lambda _command: (0, "patched command output"))

    result = agent.run_requested_tool(
        {
            "tool": "run_command",
            "args": "python --version",
        },
        permissions={},
    )

    assert result == "patched command output"
    assert confirmations == [("python --version", {})]


def test_file_mutating_run_command_ignores_allow_all_permission(monkeypatch):
    safe_confirm_called = []
    mutating_confirm_called = []
    monkeypatch.setattr(
        agent,
        "confirm_run_command",
        lambda _command, _permissions: safe_confirm_called.append(True) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(
        agent,
        "confirm_file_mutating_command",
        lambda command, _permissions=None: mutating_confirm_called.append(command) or {"allowed": False, "reason": "Deletion is unsafe."},
    )
    monkeypatch.setattr(agent, "run_command_for_context", lambda _command: (0, "should not run"))

    result = agent.run_requested_tool(
        {
            "tool": "run_command",
            "args": "Remove-Item notes.txt",
        },
        permissions={"allow_run_commands": True},
    )

    assert result == "Command cancelled: Remove-Item notes.txt\nReason: Deletion is unsafe."
    assert mutating_confirm_called == ["Remove-Item notes.txt"]
    assert safe_confirm_called == []


def test_file_mutating_command_detection_covers_delete_and_redirection():
    assert agent.is_file_mutating_command("del notes.txt")
    assert agent.is_file_mutating_command("Remove-Item notes.txt")
    assert agent.is_file_mutating_command("echo hi > notes.txt")
    assert agent.is_file_mutating_command("git checkout -- particles.js")
    assert agent.is_file_mutating_command("git restore index.html")
    assert agent.is_file_mutating_command("git reset --hard HEAD")
    assert not agent.is_file_mutating_command("python --version")


def test_model_requested_git_undo_command_is_blocked_before_confirmation(monkeypatch):
    safe_confirm_called = []
    mutating_confirm_called = []
    monkeypatch.setattr(
        agent,
        "confirm_run_command",
        lambda _command, _permissions: safe_confirm_called.append(True) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(
        agent,
        "confirm_file_mutating_command",
        lambda _command, _permissions=None: mutating_confirm_called.append(True) or {"allowed": True, "reason": ""},
    )
    monkeypatch.setattr(agent, "run_command_for_context", lambda _command: (0, "should not run"))

    result = agent.run_requested_tool(
        {
            "tool": "run_command",
            "args": "git checkout -- index.html game.js",
        },
        permissions={"allow_run_commands": True},
    )

    assert "Command blocked: git checkout -- index.html game.js" in result
    assert "use undo_edit first" in result
    assert safe_confirm_called == []
    assert mutating_confirm_called == []


def test_insert_after_tool_confirm_writes_file(monkeypatch, isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text(
        "def update():\n"
        "    terrain_angle = terrain.get_angle(x)\n"
        "    accel = vx - prev_vx\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent,
        "confirm_insert",
        lambda _path, _anchor, _content, _anchor_context=None, _permissions=None, allow_external=False: {"allowed": True, "reason": ""},
    )

    result = agent.run_requested_tool(
        {
            "tool": "insert_after",
            "args": "car.py",
            "anchor": "terrain_angle = terrain.get_angle(x)",
            "anchor_closed": True,
            "content": "slope_gravity = math.sin(terrain_angle) * 0.3",
            "content_closed": True,
            "patch": None,
            "patch_closed": False,
        },
        _messages(),
    )

    assert result == "Inserted content after anchor in: car.py"
    assert "    slope_gravity = math.sin(terrain_angle) * 0.3" in target.read_text(encoding="utf-8")


def test_write_file_tool_records_edit_stats(monkeypatch, isolated_workspace):
    target = isolated_workspace / "app.py"
    target.write_text("old\nkeep\n", encoding="utf-8")
    runtime_state = {}
    monkeypatch.setattr(agent, "confirm_write", lambda _path, _content, _permissions=None: {"allowed": True, "reason": ""})

    result = agent.run_requested_tool(
        {
            "tool": "write_file",
            "args": "app.py",
            "content": "new\nkeep\nadded\n",
            "content_closed": True,
        },
        _messages(),
        runtime_state=runtime_state,
    )

    assert result == "Wrote file: app.py"
    assert runtime_state["_last_edit_stats"] == {
        "tool": "write_file",
        "added": 2,
        "removed": 1,
        "files": ["app.py"],
    }


def test_replace_block_tool_confirm_writes_file(monkeypatch, isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text(
        "def update():\n"
        "    self.x += self.vx\n"
        "    self.y += self.vy\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent,
        "confirm_replace",
        lambda _path, _old, _new, _permissions=None, allow_external=False: {"allowed": True, "reason": ""},
    )

    result = agent.run_requested_tool(
        {
            "tool": "replace_block",
            "args": "car.py",
            "old_block": "    self.x += self.vx\n    self.y += self.vy",
            "old_block_closed": True,
            "new_block": "    if not self.on_ground:\n        self.x += self.vx\n        self.y += self.vy",
            "new_block_closed": True,
            "patch": None,
            "patch_closed": False,
        },
        _messages(),
    )

    assert result == "Replaced block in: car.py"
    assert "    if not self.on_ground:" in target.read_text(encoding="utf-8")


def test_replace_block_tool_cancel_returns_reason(monkeypatch, isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(
        agent,
        "confirm_replace",
        lambda _path, _old, _new, _permissions=None, allow_external=False: {
            "allowed": False,
            "reason": "Hold on and discuss the plan.",
        },
    )

    result = agent.run_requested_tool(
        {
            "tool": "replace_block",
            "args": "car.py",
            "old_block": "old",
            "old_block_closed": True,
            "new_block": "new",
            "new_block_closed": True,
        },
        _messages(),
    )

    assert result == "Replace cancelled: car.py\nReason: Hold on and discuss the plan."
    assert target.read_text(encoding="utf-8") == "old\n"
