import json
import threading
import time

import pytest

from memory_engine.ltm import memory_retrieval as retrieval, maintenance_runner as runner
from persistence import trajectory


def call(request, state, root):
    return retrieval.run_retrieve_memory(request, runtime_state=state,
        caller_profile=retrieval.MAINTAINER_CALLER, root=root)


def test_waits_without_wake_or_usage(isolated_workspace, monkeypatch):
    monkeypatch.setattr(retrieval.ltm_store, "get_object", lambda *a, **k: {"object_id": "mem_a"})
    state = {"wake_callback": lambda *a, **k: pytest.fail("wake")}
    monkeypatch.setattr(retrieval, "RETRIEVAL_SYNC_WINDOW_SECONDS", .001)
    monkeypatch.setattr(retrieval, "_record_successful_uses", lambda *a: pytest.fail("usage"))
    def agent(*args):
        time.sleep(.03)
        return {"kind": "checkpoint", "message": "result", "evidence": [{"object_id": "mem_a", "title": "a"}]}, {}
    monkeypatch.setattr(retrieval, "_run_agent", agent)
    try:
        result = call({"query": "any topic"}, state, isolated_workspace)
        assert result["kind"] == "checkpoint"
        assert call({"continuation_id": result["continuation_id"], "message": "more"}, state, isolated_workspace)["kind"] == "checkpoint"
        assert "memory_retrieval_wake_results" not in state
    finally:
        retrieval.close_retrieval_session(state)


@pytest.mark.parametrize("kind", ["feedback", "need_context"])
def test_profile_corrects_disallowed_results(isolated_workspace, kind):
    state = {"eval_retrieve_memory_responses": [
        {"kind": kind, "message": "not allowed"},
        {"kind": "need_clue", "message": "Which topic?"},
        {"kind": "checkpoint", "message": "No candidates"},
    ]}
    try:
        first = call({"query": "search"}, state, isolated_workspace)
        assert first["kind"] == "need_clue"
        history = state[retrieval.SESSION_KEY]["history"]
        assert "Your caller is Maintainer" in history[0]["content"]
        assert call({"continuation_id": first["continuation_id"], "message": "topic"}, state, isolated_workspace)["kind"] == "checkpoint"
        assert call({"continuation_id": first["continuation_id"], "include_context": True}, state, isolated_workspace)["kind"] == "technical"
    finally:
        retrieval.close_retrieval_session(state)


def test_close_wakes_waiter_and_blocks_late_delivery(isolated_workspace, monkeypatch):
    state, results = {}, []
    started, release = threading.Event(), threading.Event()
    def agent(*args):
        started.set()
        release.wait(2)
        return {"kind": "checkpoint", "message": "late"}, {}
    monkeypatch.setattr(retrieval, "_run_agent", agent)
    thread = threading.Thread(target=lambda: results.append(call({"query": "x"}, state, isolated_workspace)))
    thread.start()
    try:
        assert started.wait(2)
        retrieval.close_retrieval_session(state)
        thread.join(2)
        assert not thread.is_alive()
        assert results[0]["kind"] == "technical"
    finally:
        release.set()
        thread.join(2)
    assert "memory_retrieval_wake_results" not in state


def test_root_forwarded_to_search_and_observation(isolated_workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(retrieval.candidate_generation, "build_candidate_pool",
        lambda *a, **kw: seen.append(kw) or {"items": []})
    monkeypatch.setattr(retrieval.retrieval_observation, "run",
        lambda *a, **kw: seen.append(kw) or {"items": []})
    session = {"root": isolated_workspace}
    retrieval._run_internal_tool("search_candidates", {"query": "topic"}, session)
    retrieval._run_internal_tool("get_object", {}, session)
    assert all(item["root"] == isolated_workspace for item in seen)
    assert seen[0]["profile"] is retrieval.ltm_retrieval.MEMORY_RETRIEVAL_PROFILE


def test_runner_technical_is_nonfatal_and_isolated(isolated_workspace, monkeypatch):
    events, owned = [], []
    main_state = {"sentinel": True}
    runtime = {retrieval.SESSION_KEY: main_state, "ui_event_sink": events.append}
    def retrieve(request, **kwargs):
        owned.append(kwargs["runtime_state"])
        assert kwargs["root"] == isolated_workspace
        assert kwargs["caller_profile"] is retrieval.MAINTAINER_CALLER
        raise ConnectionError("offline")
    monkeypatch.setattr(retrieval, "run_retrieve_memory", retrieve)
    monkeypatch.setattr(runner.maintenance_observation, "get_overall_health", lambda *_args, **_kwargs: {"status": "ok"})
    def response(name, args):
        return {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
            {"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
    responses = [
        response("retrieve_memory", {"query": "ordinary search"}),
        response("get_object", {"limit": 1}),
        response("complete_maintenance_stage", {"stage": "recent_review", "status": "no_applicable_work", "stage_summary": "The retrieved material was unavailable; no local change was made."}),
    ]
    for stage in ("global_duplicate_review", "abstraction", "episode_review"):
        responses.extend([response("get_object", {"limit": 1}), response("complete_maintenance_stage", {"stage": stage, "status": "no_applicable_work", "stage_summary": "No additional high-confidence change."})])
    responses.append(response("finish_maintenance", {}))
    responses = iter(responses)
    monkeypatch.setattr(runner, "call_chat_completion", lambda *a, **k: next(responses))
    result = runner.run_ltm_maintenance(runtime, root=isolated_workspace)
    assert result["status"] == "completed"
    assert runtime[retrieval.SESSION_KEY] is main_state
    assert owned[0] is not runtime
    failed_tool = [e for e in events if e["type"] == "ltm_maintenance_tool_finished" and e.get("tool") == "retrieve_memory"]
    assert failed_tool and failed_tool[0]["status"] == "error"


def test_schema_and_prompt():
    schema = next(t["function"]["parameters"] for t in runner.MAINTAINER_TOOL_SCHEMAS if t["function"]["name"] == "retrieve_memory")
    assert set(schema["properties"]) == {"query", "continuation_id", "message", "close"}
    prompt = runner.load_prompt("ltm_maintainer")
    assert "global duplicate review and consolidation" in prompt
    assert "when broader review" not in prompt
    assert "general-purpose" not in prompt.lower()
    assert "more powerful semantic memory-retrieval tool" in prompt
    assert "unbounded objective" in prompt
    description = next(t["function"]["description"] for t in runner.MAINTAINER_TOOL_SCHEMAS if t["function"]["name"] == "retrieve_memory")
    assert "independent Retriever Agent" in description
    assert "more powerful than get_object" in description
    assert "expressions or languages" in description
    assert "specific follow-up" in description
    assert "not guarantee exhaustive coverage" in description
    assert "Calling Retriever is optional" in prompt


def test_maintainer_auto_batches_share_overall_deadline(isolated_workspace, monkeypatch):
    clock = {"now": 0.0}
    provider_calls = []
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(retrieval, "RETRIEVAL_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(retrieval, "RETRIEVAL_MAX_TOOL_CALLS", 1)

    def provider(_history, timeout, **_kwargs):
        provider_calls.append(timeout)
        if len(provider_calls) <= 3:
            clock["now"] += 4
            return {"role": "assistant", "tool_calls": [{
                "id": f"lookup_{len(provider_calls)}",
                "function": {"name": "get_object", "arguments": "{}"},
            }]}
        return {"role": "assistant", "content": '{"kind":"checkpoint","message":"Resumed successfully"}'}

    monkeypatch.setattr(retrieval, "_call_agent", provider)
    monkeypatch.setattr(retrieval, "_run_internal_tool", lambda *_args: {"status": "ok", "items": []})
    state = {}
    result = call({"query": "bounded topic"}, state, isolated_workspace)

    assert result["kind"] == "technical"
    assert result["error"] == {"code": "retrieval_timeout", "batch_count": 2, "tool_call_count": 1}
    assert result["continuation_id"].startswith("ret_")
    assert len(provider_calls) == 3
    assert provider_calls == [10, 6, 2]
    trace = trajectory.list_trajectories(process_type="memory_retriever", root=isolated_workspace)[0]
    events = trajectory.get_trajectory(trace["trajectory_id"], root=isolated_workspace)["events"]
    assert any(event["event_type"] == "retriever.timeout" for event in events)

    resumed = call({"continuation_id": result["continuation_id"], "message": "Return the verified result"}, state, isolated_workspace)
    assert resumed["kind"] == "checkpoint"
    assert provider_calls[-1] == 10


@pytest.mark.parametrize("compiled_ok,commit_fails", [(False, False), (True, False), (True, True)])
def test_close_only_after_compile_before_commit(isolated_workspace, monkeypatch, compiled_ok, commit_fails):
    state, order = {}, []
    manager = retrieval.RetrievalSessionManager(state)
    manager.create("task")
    token = runner._RETRIEVAL_STATE.set(state)
    monkeypatch.setattr(runner.ltm_store, "get_maintenance_runtime_state", lambda *a: {})
    monkeypatch.setattr(runner.maintenance_intent_compiler, "compile_maintenance_action_plan",
        lambda *a: {"status": "compiled" if compiled_ok else "rejected", "idempotency_key": "key"})
    def commit(*a):
        assert manager.current() is None
        order.append("commit")
        if commit_fails:
            raise RuntimeError("store offline")
        return {"status": "rejected"}
    monkeypatch.setattr(runner.ltm_store, "commit_maintenance_plan", commit)
    try:
        if commit_fails:
            with pytest.raises(RuntimeError):
                runner._execute_tool("submit_maintenance_plan", {}, "run", {}, isolated_workspace)
        else:
            runner._execute_tool("submit_maintenance_plan", {}, "run", {}, isolated_workspace)
        assert bool(order) == compiled_ok
        assert (manager.current() is None) == compiled_ok
    finally:
        retrieval.close_retrieval_session(state)
        runner._RETRIEVAL_STATE.reset(token)


def test_explicit_root_reads_real_evidence(isolated_workspace):
    root = isolated_workspace / "other"
    root.mkdir()
    object_id = "mem_" + "a" * 32
    body = "A concise reply preference; a separate detailed-report preference is not equivalent."
    result = retrieval.ltm_store.commit_write_plan({"objects": [{
        "object_id": object_id, "object_type": "assertion", "title": "Concise replies",
        "payload": {"schema_version": 1, "body": body},
    }], "links": []}, "seed", root=root)
    assert result["status"] == "committed"
    state = {"eval_retrieve_memory_responses": [
        {"tool_calls": [{"id": "read", "function": {"name": "get_object", "arguments": json.dumps({"object_id": object_id, "projection": "payload"})}}]},
        {"kind": "checkpoint", "message": "Candidate for comparison", "evidence": [{"object_id": object_id, "title": "Concise replies", "body": body}]},
    ]}
    try:
        answer = call({"query": "回复简短"}, state, root)
        assert answer["evidence"][0]["body"] == body
        assert answer["evidence"][0]["object_id"] == object_id
        tool_messages = [m for m in state[retrieval.SESSION_KEY]["history"] if m["role"] == "tool"]
        assert body in tool_messages[0]["content"]
        assert retrieval.ltm_store.get_object(object_id, root=isolated_workspace) is None
    finally:
        retrieval.close_retrieval_session(state)


@pytest.mark.parametrize("fail", [False, True])
def test_invocation_cleanup_and_fresh_resume_state(monkeypatch, fail):
    states = []
    def run(*args):
        state = runner._RETRIEVAL_STATE.get()
        states.append(state)
        retrieval.RetrievalSessionManager(state).create("task")
        if fail:
            raise RuntimeError("maintenance failure")
        return {"status": "completed"}
    monkeypatch.setattr(runner, "_run_ltm_maintenance", run)
    for _ in range(2):
        if fail:
            with pytest.raises(RuntimeError):
                runner.run_ltm_maintenance()
        else:
            assert runner.run_ltm_maintenance()["status"] == "completed"
    assert states[0] is not states[1]
    assert all(retrieval.SESSION_KEY not in state for state in states)
    assert runner._RETRIEVAL_STATE.get() is None


def test_unrecoverable_retrieval_format_returns_technical(isolated_workspace):
    state = {"eval_retrieve_memory_responses": [
        {"kind": "feedback", "message": "wrong"},
        {"kind": "need_context", "message": "still wrong"},
    ]}
    try:
        assert call({"query": "search"}, state, isolated_workspace)["kind"] == "technical"
        assert "memory_retrieval_wake_results" not in state
    finally:
        retrieval.close_retrieval_session(state)
