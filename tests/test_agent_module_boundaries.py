from runtime import agent
import pytest

from runtime import background_tasks, model_runtime, structured_loop
from runtime.dependency_validation import RuntimeDependencyError
from tools import tool_runtime


def test_agent_structured_loop_wrapper_forwards_live_dependency_source(monkeypatch):
    captured = {}

    def fake_run(messages, permissions=None, runtime_state=None, dependency_source=None):
        captured.update(
            messages=messages,
            permissions=permissions,
            runtime_state=runtime_state,
            dependencies=dependency_source,
        )
        return "ok"

    monkeypatch.setattr(structured_loop, "run_structured_loop", fake_run)
    messages = [{"role": "user", "content": "hello"}]
    runtime_state = {"trace_id": "trace"}

    assert agent.answer_with_structured_tools(messages, {"x": True}, runtime_state) == "ok"
    assert captured["messages"] == messages
    assert captured["permissions"] == {"x": True}
    assert captured["runtime_state"] is runtime_state
    assert captured["dependencies"]["run_requested_tool"] is agent.run_requested_tool


def test_agent_model_runtime_wrappers_forward_live_dependency_source(monkeypatch):
    calls = []

    def fake_payload(*args, **kwargs):
        calls.append(("payload", args, kwargs))
        return "payload"

    def fake_call(*args, **kwargs):
        calls.append(("call", args, kwargs))
        return "call"

    def fake_retry(*args, **kwargs):
        calls.append(("retry", args, kwargs))
        return "retry"

    monkeypatch.setattr(model_runtime, "build_model_tools_payload", fake_payload)
    monkeypatch.setattr(model_runtime, "call_model_with_tools", fake_call)
    monkeypatch.setattr(model_runtime, "call_model_with_structured_retries", fake_retry)

    assert agent.build_model_tools_payload([]) == "payload"
    assert agent.call_model_with_tools([]) == "call"
    assert agent.call_model_with_structured_retries([]) == "retry"
    assert len(calls) == 3
    assert all(call[2]["dependency_source"]["model_status"] is agent.model_status for call in calls)


def test_agent_background_wrappers_forward_live_dependency_source(monkeypatch):
    calls = []

    def fake_schedule(*args, **kwargs):
        calls.append((args, kwargs))
        return "scheduled"

    monkeypatch.setattr(background_tasks, "schedule_working_memory_maintenance", fake_schedule)
    monkeypatch.setattr(background_tasks, "schedule_focus_segment_maintenance", fake_schedule)
    monkeypatch.setattr(background_tasks, "schedule_event_stream_memory_maintenance", fake_schedule)
    monkeypatch.setattr(background_tasks, "schedule_final_answer_background_tasks", fake_schedule)

    assert agent.schedule_working_memory_maintenance([]) == "scheduled"
    assert agent.schedule_focus_segment_maintenance([]) == "scheduled"
    assert agent.schedule_event_stream_memory_maintenance([]) == "scheduled"
    assert agent.schedule_final_answer_background_tasks([]) == "scheduled"
    assert len(calls) == 4
    assert all(call[1]["dependency_source"]["ui_event"] is agent.ui_event for call in calls)


@pytest.mark.parametrize(
    "entrypoint, expected",
    [
        (structured_loop.run_structured_loop, "structured_loop.run_structured_loop"),
        (model_runtime.build_model_tools_payload, "model_runtime.build_model_tools_payload"),
        (tool_runtime.build_tool_execution_hooks, "tool_runtime.build_tool_execution_hooks"),
        (tool_runtime.run_tool_request, "tool_runtime.run_tool_request"),
    ],
)
def test_runtime_boundaries_report_missing_dependencies(entrypoint, expected):
    with pytest.raises(RuntimeDependencyError, match=expected):
        if entrypoint is tool_runtime.run_tool_request:
            entrypoint({"tool": "read_file", "args": {}}, dependency_source={})
        elif entrypoint is tool_runtime.build_tool_execution_hooks:
            entrypoint({})
        else:
            entrypoint([], dependency_source={})
