import json

import pytest
from persistence import trajectory


OBJECT_ID = "mem_00000000000000000000000000000001"


def test_retrieval_prompt_requires_terminal_feedback_before_continuing():
    from shared.prompt_loader import load_prompt

    prompt = load_prompt("memory_retrieval")
    compact = " ".join(prompt.split())
    assert "return feedback immediately as a terminal JSON response" in compact
    assert "Do not put tool_calls in the same response as feedback" in compact
    assert "Runtime will deliver that feedback and resume this Session automatically" in compact
    assert "Candidate hits alone are not source observations" in prompt


def test_retrieval_prompt_requires_bounded_memory_recall_scope():
    from shared.prompt_loader import load_prompt

    prompt = load_prompt("memory_retrieval")
    compact = " ".join(prompt.split())
    assert "sole responsibility is to recall and reconstruct information" in compact
    assert "strictly inspect the original Query" in compact
    assert "not a general-purpose agent" in compact
    assert "reject it immediately without using any retrieval tool" in compact
    assert 'kind: "technical"' in compact
    assert 'error.code: "unsupported_query"' in compact


def test_unsupported_retrieval_query_is_rejected_before_tools(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval

    state = {
        "eval_retrieve_memory_responses": [{
            "kind": "technical",
            "message": "This request is outside the responsibility of the Memory Retrieval Agent.",
            "error": {"code": "unsupported_query"},
        }],
    }
    called = []
    monkeypatch.setattr(memory_retrieval, "_run_internal_tool", lambda *args: called.append(args) or {})
    result = memory_retrieval.run_retrieve_memory({"query": "Study the entire memory engine and prepare for a test."}, runtime_state=state)
    assert result["kind"] == "technical"
    assert result["error"] == {"code": "unsupported_query"}
    assert called == []


def _response(value):
    return value if isinstance(value, dict) and ("content" in value or "tool_calls" in value) else {"kind": "checkpoint", "message": "verified", **value}


def test_retrieve_memory_creates_resumable_session_and_accepts_free_text(isolated_workspace):
    from memory_engine.ltm import memory_retrieval

    runtime_state = {"eval_retrieve_memory_responses": [{"kind": "need_clue", "message": "When did it happen?"}, {"kind": "checkpoint", "message": "Done"}]}
    first = memory_retrieval.run_retrieve_memory({"query": "the event"}, runtime_state=runtime_state)
    assert first["kind"] == "need_clue"
    continuation_id = first["continuation_id"]
    second = memory_retrieval.run_retrieve_memory({"continuation_id": continuation_id, "message": "last week"}, runtime_state=runtime_state)
    assert second == {"kind": "checkpoint", "message": "Done", "continuation_id": continuation_id}
    assert runtime_state["memory_retrieval_session"]["query"] == "the event"
    roots = trajectory.list_trajectories(process_type="memory_retriever")
    assert len(roots) == 2
    assert len({root["external_id"] for root in roots}) == 2
    assert all(root["external_id"].startswith(f"{continuation_id}:invocation:") for root in roots)
    invocation_sequences = []
    for root in roots:
        metadata = json.loads(root["metadata_json"])
        assert metadata["session_id"] == continuation_id
        invocation_sequences.append(metadata["invocation_seq"])
        trace = trajectory.get_trajectory(root["trajectory_id"])
        assert len(trace["spans"]) == 1
        process_events = [event for event in trace["events"] if event["event_type"].startswith("process.")]
        assert [event["event_type"] for event in process_events] == ["process.started", "process.completed"]
    assert sorted(invocation_sequences) == [1, 2]


def test_retrieve_memory_rejects_second_query_until_close(isolated_workspace):
    from memory_engine.ltm import memory_retrieval

    state = {"eval_retrieve_memory_responses": [{"kind": "checkpoint", "message": "Done"}]}
    result = memory_retrieval.run_retrieve_memory({"query": "one"}, runtime_state=state)
    rejected = memory_retrieval.run_retrieve_memory({"query": "two"}, runtime_state=state)
    assert rejected["kind"] == "technical"
    assert rejected["error"]["code"] == "session_active"
    closed = memory_retrieval.run_retrieve_memory({"continuation_id": result["continuation_id"], "close": True}, runtime_state=state)
    assert closed["kind"] == "closed"
    assert "memory_retrieval_session" not in state
    assert memory_retrieval.run_retrieve_memory({"continuation_id": result["continuation_id"], "close": True}, runtime_state=state) == closed


def test_retrieve_memory_idempotent_repeated_continuation(isolated_workspace):
    from memory_engine.ltm import memory_retrieval

    state = {"eval_retrieve_memory_responses": [{"kind": "checkpoint", "message": "Done"}]}
    first = memory_retrieval.run_retrieve_memory({"query": "one"}, runtime_state=state)
    state["eval_retrieve_memory_responses"].append({"kind": "checkpoint", "message": "Should not be consumed"})
    repeat = memory_retrieval.run_retrieve_memory({"query": "one"}, runtime_state=state)
    assert repeat["kind"] == "technical"
    same = memory_retrieval.run_retrieve_memory({"continuation_id": first["continuation_id"]}, runtime_state=state)
    same_again = memory_retrieval.run_retrieve_memory({"continuation_id": first["continuation_id"]}, runtime_state=state)
    assert same == same_again


def test_message_does_not_close_session_without_explicit_close_flag(isolated_workspace):
    from memory_engine.ltm import memory_retrieval

    state = {"eval_retrieve_memory_responses": [
        {"kind": "checkpoint", "message": "Initial result"},
        {"kind": "checkpoint", "message": "Guidance result"},
    ]}
    first = memory_retrieval.run_retrieve_memory({"query": "search"}, runtime_state=state)
    continuation = first["continuation_id"]

    guided = memory_retrieval.run_retrieve_memory(
        {"continuation_id": continuation, "message": "Close this retrieval session now."},
        runtime_state=state,
    )
    assert guided["kind"] == "checkpoint"
    assert state["memory_retrieval_session"]["status"] != "closed"

    rejected = memory_retrieval.run_retrieve_memory(
        {"continuation_id": continuation, "close": True, "message": "Close it."},
        runtime_state=state,
    )
    assert rejected["kind"] == "technical"
    assert rejected["error"]["code"] == "invalid_arguments"
    assert state["memory_retrieval_session"]["status"] != "closed"

    closed = memory_retrieval.run_retrieve_memory(
        {"continuation_id": continuation, "close": True},
        runtime_state=state,
    )
    assert closed["kind"] == "closed"


def test_candidate_hit_is_not_evidence_or_successful_use(isolated_workspace, monkeypatch):
    from memory_engine.ltm import candidate_generation, memory_retrieval

    monkeypatch.setattr(candidate_generation, "build_candidate_pool", lambda *_args, **_kwargs: {"status": "ok", "items": [{"object_id": OBJECT_ID, "title": "Candidate"}], "query_evidence": [], "errors": []})
    state = {
        "eval_retrieve_memory_responses": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "search_candidates", "arguments": json.dumps({"query": "x", "service": "lexical"})}}]},
            {"kind": "checkpoint", "message": "Candidates only"},
        ]
    }
    result = memory_retrieval.run_retrieve_memory({"query": "x"}, runtime_state=state)
    assert result["kind"] == "checkpoint"
    assert "evidence" not in result
    assert state["memory_retrieval_session"]["successful_use_object_ids"] == set()


def test_evidence_requires_observation_and_records_use_once(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store, maintenance_observation, memory_retrieval

    calls = []
    monkeypatch.setattr(ltm_store, "get_object", lambda *args, **kwargs: {"object_id": OBJECT_ID})
    monkeypatch.setattr(ltm_store, "record_successful_uses", lambda ids, key, **kwargs: calls.append((ids, key)) or {"status": "committed"})
    monkeypatch.setattr(maintenance_observation, "get_object", lambda args=None: {"status": "ok", "items": [{"object_id": OBJECT_ID, "title": "Observed", "body": "Exact"}]})
    state = {
        "eval_retrieve_memory_responses": [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "o1", "function": {"name": "get_object", "arguments": json.dumps({"object_id": OBJECT_ID})}}]},
            {"kind": "checkpoint", "message": "Verified", "evidence": [{"object_id": OBJECT_ID, "title": "Observed", "body": "Exact"}]},
        ]
    }
    result = memory_retrieval.run_retrieve_memory({"query": "x"}, runtime_state=state)
    assert result["evidence"][0]["object_id"] == OBJECT_ID
    assert len(calls) == 1
    continuation = result["continuation_id"]
    state["eval_retrieve_memory_responses"].append({"kind": "checkpoint", "message": "Still verified", "evidence": [{"object_id": OBJECT_ID, "title": "Observed", "body": "Exact"}]})
    memory_retrieval.run_retrieve_memory({"continuation_id": continuation}, runtime_state=state)
    assert len(calls) == 1


def test_need_context_and_include_context_are_resumable(isolated_workspace):
    from memory_engine.ltm import memory_retrieval

    state = {"eval_retrieve_memory_responses": [{"kind": "need_context", "message": "I need the complete context."}, {"kind": "checkpoint", "message": "Context used"}]}
    first = memory_retrieval.run_retrieve_memory({"query": "that thing"}, runtime_state=state)
    second = memory_retrieval.run_retrieve_memory({"continuation_id": first["continuation_id"], "include_context": True}, context_snapshot=({"role": "user", "content": "full context"},), runtime_state=state)
    assert first["kind"] == "need_context"
    assert second["kind"] == "checkpoint"


def test_rank_2_retrieval_rejects_over_cap():
    from memory_engine.ltm import reranker_adapter

    result = reranker_adapter.rank_2_candidates_for_target("target", [{"object_id": f"mem_{index}"} for index in range(401)])
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "rank_2_input_over_cap"


def test_timeout_is_a_resumable_technical_result(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval

    clock = {"now": 0.0}
    monkeypatch.setattr(memory_retrieval.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(memory_retrieval, "_call_agent", lambda *_args, **_kwargs: (clock.__setitem__("now", memory_retrieval.RETRIEVAL_TIMEOUT_SECONDS + 1) or {"role": "assistant", "content": '{"kind":"checkpoint","message":"late"}'}))
    result = memory_retrieval.run_retrieve_memory({"query": "late"}, runtime_state={})
    assert result["kind"] == "technical"
    assert result["error"]["code"] == "timeout"
    assert result["continuation_id"].startswith("ret_")


def _assert_complete_provider_history(history):
    pending = []
    for message in history:
        if message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
        else:
            assert not pending, "Another message interrupts an unanswered tool batch"
            pending = [call["id"] for call in message.get("tool_calls", [])]
    assert not pending


@pytest.mark.parametrize("reason,completed", [("tool_limit", 20), ("cancelled", 0), ("cancelled", 1), ("timeout", 1), ("timeout", 3)])
def test_interrupted_batch_resumes_with_valid_history(isolated_workspace, monkeypatch, reason, completed):
    from memory_engine.ltm import memory_retrieval as retrieval

    clock = {"now": 0.0}
    executed, provider_calls = [], []
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])
    batch_size = 22 if reason == "tool_limit" else 3
    def provider(history, _timeout):
        _assert_complete_provider_history(history)
        provider_calls.append(True)
        if len(provider_calls) == 1:
            return {"content": "", "tool_calls": [
                {"id": f"read_{i}", "function": {"name": "get_object", "arguments": "{}"}}
                for i in range(batch_size)
            ]}
        return {"content": '{"kind":"checkpoint","message":"Resumed successfully"}'}

    def execute(*_args):
        executed.append(True)
        if reason == "timeout" and len(executed) == completed:
            clock["now"] += retrieval.RETRIEVAL_TIMEOUT_SECONDS + 1
        return {"status": "ok", "items": [], "marker": len(executed)}

    def cancel():
        if reason == "cancelled" and provider_calls and len(executed) == completed:

            if state[retrieval.SESSION_KEY]["history"][-1].get("tool_calls") or executed:
                raise RuntimeError("cancel requested")

    monkeypatch.setattr(retrieval, "_call_agent", provider)
    monkeypatch.setattr(retrieval, "_run_internal_tool", execute)
    state = {}
    first = retrieval.run_retrieve_memory({"query": "search"}, runtime_state=state, check_cancel=cancel)
    if reason == "tool_limit":
        assert first["kind"] == "checkpoint"
    else:
        assert first["kind"] == "technical"
        assert first["error"]["code"] == reason
    history = state[retrieval.SESSION_KEY]["history"]
    _assert_complete_provider_history(history)
    results = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
    assert len(executed) == completed
    assert [r["marker"] for r in results[:completed]] == list(range(1, completed + 1))
    assert all(r["status"] == "not_executed" and r["reason"] == reason for r in results[completed:])
    second = retrieval.run_retrieve_memory({"continuation_id": first["continuation_id"], "message": "Continue from these observations"}, runtime_state=state)
    assert second["kind"] == "checkpoint"
    assert len(executed) == completed
    assert len(provider_calls) == (3 if reason == "tool_limit" else 2)


def test_provider_failure_retry_cache_does_not_block_new_message(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    state = {"eval_retrieve_memory_responses": [{"kind": "need_clue", "message": "Which event?"}]}
    initial = retrieval.run_retrieve_memory({"query": "search"}, runtime_state=state)
    state.pop("eval_retrieve_memory_responses")
    calls = []
    def provider(history, _timeout):
        _assert_complete_provider_history(history)
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        return {"content": '{"kind":"checkpoint","message":"Recovered"}'}
    monkeypatch.setattr(retrieval, "_call_agent", provider)
    request = {"continuation_id": initial["continuation_id"], "message": "Last week's event"}
    failed = retrieval.run_retrieve_memory(request, runtime_state=state)
    assert failed["kind"] == "technical"
    assert "new message" in failed["message"]
    assert retrieval.run_retrieve_memory(request, runtime_state=state) == failed
    assert len(calls) == 1
    resumed = retrieval.run_retrieve_memory({**request, "message": "Continue the interrupted search"}, runtime_state=state)
    assert resumed["kind"] == "checkpoint"
    assert len(calls) == 2


def test_limit_across_batches_and_cached_technical_retry(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    state = {"eval_retrieve_memory_responses": [{"kind": "need_clue", "message": "Which event?"}]}
    first = retrieval.run_retrieve_memory({"query": "search"}, runtime_state=state)
    state.pop("eval_retrieve_memory_responses")
    executed, provider_calls = [], []
    def provider(history, _timeout):
        _assert_complete_provider_history(history)
        provider_calls.append(True)
        batch = len(provider_calls)
        if batch <= 2:
            return {"tool_calls": [
                {"id": f"batch_{batch}_{i}", "function": {"name": "get_object", "arguments": "{}"}}
                for i in range(19 if batch == 1 else 3)
            ]}
        return {"content": '{"kind":"checkpoint","message":"Recovered"}'}
    monkeypatch.setattr(retrieval, "_call_agent", provider)
    monkeypatch.setattr(retrieval, "_run_internal_tool", lambda *args: executed.append(True) or {"status": "ok", "items": []})
    request = {"continuation_id": first["continuation_id"], "message": "Find last week's event"}
    result = retrieval.run_retrieve_memory(request, runtime_state=state)
    assert result["kind"] == "checkpoint"
    assert len(executed) == 20
    assert retrieval.run_retrieve_memory(request, runtime_state=state) == result
    assert len(provider_calls) == 3
    _assert_complete_provider_history(state[retrieval.SESSION_KEY]["history"])
    resumed = retrieval.run_retrieve_memory({**request, "message": "Continue with the observations already obtained"}, runtime_state=state)
    assert resumed["kind"] == "checkpoint"
    assert len(provider_calls) == 4 and len(executed) == 20


def test_repeated_tool_limit_batches_stop_after_no_progress(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    provider_calls = []

    def provider(history, _timeout):
        provider_calls.append(True)
        return {"tool_calls": [{
            "id": f"lookup_{len(provider_calls)}",
            "function": {"name": "search_candidates", "arguments": json.dumps({"query": "missing memory", "service": "embedding"})},
        }]}

    monkeypatch.setattr(retrieval, "_call_agent", provider)
    monkeypatch.setattr(retrieval.candidate_generation, "build_candidate_pool", lambda *_args, **_kwargs: {"status": "ok", "items": [], "query_evidence": [], "errors": []})
    result = retrieval.run_retrieve_memory({"query": "recall the missing object"}, runtime_state={})
    assert result["kind"] == "technical"
    assert result["error"] == {"code": "no_progress"}
    assert len(provider_calls) == 63


def test_exactly_twenty_calls_can_return_normal_checkpoint(isolated_workspace, monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    state = {"eval_retrieve_memory_responses": [
        {"tool_calls": [{"id": f"o{i}", "function": {"name": "get_object", "arguments": "{}"}} for i in range(20)]},
        {"kind": "checkpoint", "message": "Finished this run"},
    ]}
    monkeypatch.setattr(retrieval, "_run_internal_tool", lambda *args: {"status": "ok", "items": []})
    result = retrieval.run_retrieve_memory({"query": "search"}, runtime_state=state)
    assert result["kind"] == "checkpoint"
    _assert_complete_provider_history(state[retrieval.SESSION_KEY]["history"])


def test_resumed_evidence_side_effects_are_idempotent(isolated_workspace, monkeypatch):
    from types import SimpleNamespace
    from memory_engine.ltm import memory_retrieval as retrieval, ltm_store, maintenance_observation
    from tools import tool_executor

    uses, refreshes = [], []
    monkeypatch.setattr(ltm_store, "get_object", lambda *args, **kwargs: {"object_id": OBJECT_ID})
    obj = {"object_id": OBJECT_ID, "title": "Observed"}
    monkeypatch.setattr(maintenance_observation, "get_object", lambda _args: {"status": "ok", "items": [obj]})
    monkeypatch.setattr(ltm_store, "record_successful_uses", lambda *args, **kwargs: uses.append(args) or {"status": "committed"})
    monkeypatch.setattr(tool_executor.context_builder, "schedule_memory_push_refresh", lambda *args, **kwargs: refreshes.append(True))
    state = {"eval_retrieve_memory_responses": [
        {"tool_calls": [{"id": f"o{i}", "function": {"name": "get_object", "arguments": "{}"}} for i in range(21)]},
        {"kind": "checkpoint", "message": "Verified", "evidence": [obj]},
        {"kind": "checkpoint", "message": "Still verified", "evidence": [obj]},
    ]}
    context = SimpleNamespace(runtime_state=state, messages=[], context_snapshot=None, hooks=SimpleNamespace(
        run_retrieve_memory=retrieval.run_retrieve_memory,
        check_cancel_requested=lambda _state: None, trace_event=lambda *args, **kwargs: None, json_dumps=json.dumps,
    ))
    def run(request):
        return json.loads(tool_executor._run_retrieve_memory({"args": request}, context))
    first = run({"query": "search"})
    assert first["kind"] == "checkpoint"
    assert len(uses) == len(refreshes) == 1
    request = {"continuation_id": first["continuation_id"], "message": "Return the verified evidence"}
    result = run(request)
    assert result["kind"] == "checkpoint"
    assert run(request) == result
    assert run({**request, "message": "Verify again"})["kind"] == "technical"
    assert len(uses) == len(refreshes) == 1
