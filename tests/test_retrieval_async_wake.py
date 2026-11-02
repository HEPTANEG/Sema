import threading
import time

import pytest


def test_retrieval_returns_running_after_sync_window_and_publishes_wake(monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    events = []
    state = {"ui_event_sink": events.append}
    monkeypatch.setattr(retrieval, "RETRIEVAL_SYNC_WINDOW_SECONDS", 0.02)

    def slow_agent(session, runtime_state, context, message, check_cancel=None, trajectory_context=None):
        time.sleep(0.08)
        return {"kind": "checkpoint", "message": "verified"}, {"tool_call_count": 1, "model_round_count": 1}

    monkeypatch.setattr(retrieval, "_run_agent", slow_agent)
    result = retrieval.run_retrieve_memory({"query": "target"}, runtime_state=state)
    assert result["kind"] == "running"
    assert [event["type"] for event in events] == ["retriever_started"]
    deadline = time.time() + 1
    while not state.get("memory_retrieval_wake_results") and time.time() < deadline:
        time.sleep(0.01)
    assert state["memory_retrieval_wake_results"][0]["kind"] == "checkpoint"
    assert [event["type"] for event in events] == ["retriever_started", "retriever_finished"]


def test_retrieval_close_stops_background_worker(monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    events = []
    state = {"ui_event_sink": events.append}
    monkeypatch.setattr(retrieval, "RETRIEVAL_SYNC_WINDOW_SECONDS", 0.01)
    started = threading.Event()
    stopped = threading.Event()

    def slow_agent(session, runtime_state, context, message, check_cancel=None, trajectory_context=None):
        started.set()
        while True:
            try:
                check_cancel()
            except Exception:
                stopped.set()
                raise
            time.sleep(0.01)

    monkeypatch.setattr(retrieval, "_run_agent", slow_agent)
    result = retrieval.run_retrieve_memory({"query": "target"}, runtime_state=state)
    assert result["kind"] == "running"
    assert started.wait(1)
    closed = retrieval.run_retrieve_memory({"continuation_id": result["continuation_id"], "close": True}, runtime_state=state)
    assert closed["kind"] == "closed"
    assert stopped.wait(1)
    assert [event["type"] for event in events] == ["retriever_started", "retriever_finished"]


def test_feedback_keeps_retriever_status_until_background_search_finishes(monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    events = []
    state = {"ui_event_sink": events.append}
    calls = []
    monkeypatch.setattr(retrieval, "RETRIEVAL_SYNC_WINDOW_SECONDS", 0.01)

    def agent(session, runtime_state, context, message, check_cancel=None, trajectory_context=None):
        calls.append(1)
        time.sleep(0.04)
        if len(calls) == 1:
            return {"kind": "feedback", "message": "possible match", "evidence": []}, {}
        return {"kind": "checkpoint", "message": "verified"}, {}

    monkeypatch.setattr(retrieval, "_run_agent", agent)
    result = retrieval.run_retrieve_memory({"query": "target"}, runtime_state=state)
    assert result["kind"] == "running"
    assert [event["type"] for event in events] == ["retriever_started"]

    deadline = time.time() + 1
    while len(calls) < 2 or len(events) < 2:
        if time.time() >= deadline:
            break
        time.sleep(0.01)

    assert len(calls) == 2
    assert [event["type"] for event in events] == ["retriever_started", "retriever_finished"]


@pytest.mark.parametrize("kind", ["need_clue", "need_context", "technical"])
def test_background_retriever_clears_status_for_terminal_kinds(monkeypatch, kind):
    from memory_engine.ltm import memory_retrieval as retrieval

    events = []
    state = {"ui_event_sink": events.append}
    monkeypatch.setattr(retrieval, "RETRIEVAL_SYNC_WINDOW_SECONDS", 0.01)

    def agent(session, runtime_state, context, message, check_cancel=None, trajectory_context=None):
        time.sleep(0.04)
        return {"kind": kind, "message": kind}, {}

    monkeypatch.setattr(retrieval, "_run_agent", agent)
    result = retrieval.run_retrieve_memory({"query": "target"}, runtime_state=state)
    assert result["kind"] == "running"
    assert [event["type"] for event in events] == ["retriever_started"]

    deadline = time.time() + 1
    while len(events) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert [event["type"] for event in events] == ["retriever_started", "retriever_finished"]


def test_wake_queue_deduplicates_feedback_per_session():
    from runtime.wake import WakeQueue, wake_event

    queue = WakeQueue()
    first = wake_event("retriever", "feedback", {"message": "one"}, session_key="ret_1")
    second = wake_event("retriever", "feedback", {"message": "two"}, session_key="ret_1")
    assert queue.put(first) is True
    assert queue.put(first) is False
    assert queue.put(second) is False
    delivered = queue.get(0)
    queue.mark_delivered(delivered)
    assert queue.put(second) is True


def test_retriever_wake_is_tool_result_like(monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    state = {}
    received = []
    state["wake_callback"] = lambda *args, **kwargs: received.append((args, kwargs)) or True
    manager = retrieval.RetrievalSessionManager(state)
    session = manager.create("target")
    worker = retrieval.RetrievalWorker(manager, session, state)
    try:
        worker._wake({"kind": "feedback", "message": "possible match"})
    finally:
        worker.close()

    assert received
    assert received[0][1]["processing_class"] == "tool_result_like"


def test_wake_delivery_forwards_processing_class(monkeypatch):
    from bridge.ui_bridge_server import BridgeServer
    from runtime.wake import wake_event

    server = BridgeServer()
    captured = {}
    monkeypatch.setattr(server, "_ensure_initialized", lambda: setattr(server, "messages", []))
    monkeypatch.setattr(server, "_run_message_turn", lambda *args, **kwargs: captured.update(kwargs) or {"ok": True})
    monkeypatch.setattr(server, "send_event", lambda *_args, **_kwargs: None)
    event = wake_event("worker", "feedback", {"message": "candidate"}, processing_class="tool_result_like")

    assert server._deliver_wake(event) is True
    assert captured["processing_class"] == "tool_result_like"


def test_feedback_is_returned_first_then_worker_continues(monkeypatch):
    from memory_engine.ltm import memory_retrieval as retrieval

    state = {}
    calls = []

    def agent(session, runtime_state, context, message, check_cancel=None, trajectory_context=None):
        calls.append(1)
        if len(calls) == 1:
            return {"kind": "feedback", "message": "possible match", "evidence": []}, {}
        return {"kind": "checkpoint", "message": "verified"}, {}

    monkeypatch.setattr(retrieval, "_run_agent", agent)
    result = retrieval.run_retrieve_memory({"query": "target"}, runtime_state=state)
    assert result["kind"] == "feedback"
    deadline = time.time() + 1
    while (len(calls) < 2 or not state.get("memory_retrieval_wake_results")) and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2
    assert state["memory_retrieval_wake_results"][0]["kind"] == "checkpoint"


def test_wake_waits_until_message_response_is_delivered():
    from bridge.ui_bridge_server import BridgeServer
    from runtime.wake import wake_event

    server = BridgeServer()
    server._pending_message_responses = 1
    event = wake_event("retriever", "checkpoint", {"continuation_id": "ret_x"}, session_key="ret_x")
    assert server._deliver_wake(event) is False
