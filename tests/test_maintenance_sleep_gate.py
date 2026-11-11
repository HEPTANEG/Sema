import json

import pytest

from bridge.ui_bridge_server import BridgeServer
from memory_engine.ltm import maintenance_pressure
from runtime import agent
from runtime.maintenance_sleep import (
    MaintenanceControlRequested,
    clear_request_decision,
    mandatory_latch,
    prepare_request_decision,
    release_mandatory_latch,
    request_decision,
)


def snapshot(pressure, cursor=10, frontier=20, latest=25):
    return {
        "level": maintenance_pressure.maintenance_pressure_level(pressure),
        "blocked": pressure >= 1.0,
        "pressure": pressure,
        "unmaintained_slot_count": max(0, frontier - cursor),
        "unmaintained_maintainable_slot_count": max(0, frontier - cursor),
        "maintenance_coverage_cursor": cursor,
        "maintainable_frontier": frontier,
        "coverage_target_slot_seq": max(cursor, frontier),
        "latest_event_stream_slot_seq": latest,
    }


def test_request_decision_uses_soft_and_mandatory_thresholds():
    assert prepare_request_decision({}, snapshot(0.49))["mode"] == "normal"
    assert prepare_request_decision({}, snapshot(0.5))["mode"] == "soft"
    state = {}
    decision = prepare_request_decision(state, snapshot(1.0))
    assert decision["mode"] == "mandatory"
    assert decision["latched_now"] is True
    assert mandatory_latch(state)["snapshot"]["pressure"] == 1.0


def test_mandatory_gate_survives_pressure_drop_and_context_reports_both_states():
    state = {}
    prepare_request_decision(state, snapshot(1.0, cursor=10, frontier=30, latest=35))
    decision = prepare_request_decision(state, snapshot(0.0, cursor=10, frontier=-1, latest=36))

    assert decision["mode"] == "mandatory"
    assert decision["snapshot"]["pressure"] == 0.0
    assert decision["latch"]["snapshot"]["pressure"] == 1.0

    notice = maintenance_pressure.maintenance_pressure_context_notice(runtime_state=state)
    assert "pressure: 0.00" in notice
    assert "mandatory_gate_latched: true" in notice
    assert "gate_trigger_pressure: 1.00" in notice
    assert "Maintenance is mandatory" in notice


def test_tool_schema_keeps_only_sleep_while_gate_is_latched(monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(1.0))
    prepare_request_decision(state, snapshot(0.0))

    names = {item["function"]["name"] for item in agent.tool_schemas_for_model(state)}

    assert names == {"sleep_for_maintenance"}


def test_gate_and_authorization_transitions_are_traced(monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(1.0))
    events = []
    monkeypatch.setattr(agent, "trace_event", lambda _state, event_type, **fields: events.append((event_type, fields)))

    agent.tool_schemas_for_model(state)

    assert [event_type for event_type, _fields in events] == [
        "maintenance.gate_latched",
        "maintenance.sleep_authorized",
    ]
    assert events[0][1]["pressure"] == 1.0
    assert events[1][1]["mode"] == "mandatory"


def test_model_payload_uses_one_pressure_snapshot_for_notice_and_tools(isolated_workspace, monkeypatch):
    calls = []

    def pressure_snapshot(_root=None):
        calls.append(True)
        if len(calls) > 1:
            raise AssertionError("pressure was recomputed within one model request")
        return snapshot(0.5, cursor=10, frontier=20, latest=25)

    monkeypatch.setattr(maintenance_pressure, "maintenance_pressure_snapshot", pressure_snapshot)
    payload = agent.build_model_tools_payload(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "continue"}],
        runtime_state={},
    )

    tool_names = {item["function"]["name"] for item in payload["tools"]}
    system_content = "\n".join(item.get("content") or "" for item in payload["messages"] if item.get("role") == "system")
    assert calls == [True]
    assert "sleep_for_maintenance" in tool_names
    assert "pressure: 0.50" in system_content


def test_soft_authorization_accepts_sleep_after_live_pressure_drops(monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(0.5, cursor=10, frontier=20, latest=25))
    monkeypatch.setattr(
        maintenance_pressure,
        "maintenance_pressure_snapshot",
        lambda: snapshot(0.0, cursor=10, frontier=-1, latest=26),
    )
    events = []
    monkeypatch.setattr(agent, "trace_event", lambda _state, event_type, **fields: events.append((event_type, fields)))

    result = agent.run_requested_tool(
        {
            "tool": "sleep_for_maintenance",
            "args": {"wake_prompt": "Resume the document study from section eight."},
        },
        runtime_state=state,
    )

    assert json.loads(result)["status"] == "sleep_requested"
    assert state["_maintenance_control_action"]["action"] == "sleep"
    assert any(event_type == "maintenance.pressure_changed_before_sleep" for event_type, _fields in events)
    assert any(event_type == "maintenance.sleep_request_accepted" for event_type, _fields in events)


def test_invalid_wake_prompt_does_not_release_mandatory_gate(monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(1.0))
    monkeypatch.setattr(maintenance_pressure, "maintenance_pressure_snapshot", lambda: snapshot(0.0))

    with pytest.raises(RuntimeError, match="wake_prompt"):
        agent.run_requested_tool(
            {"tool": "sleep_for_maintenance", "args": {"wake_prompt": "short"}},
            runtime_state=state,
        )

    assert mandatory_latch(state) is not None


def test_mandatory_gate_survives_rejected_plain_reply(isolated_workspace, monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(1.0))
    replies = iter([
        {"content": "I will continue without maintenance.", "tool_calls": []},
        {
            "content": "",
            "tool_calls": [{
                "id": "sleep-call",
                "type": "function",
                "function": {
                    "name": "sleep_for_maintenance",
                    "arguments": json.dumps({"wake_prompt": "Resume the document study from section eight."}),
                },
            }],
        },
    ])
    monkeypatch.setattr(agent, "call_model_with_tools", lambda *_args, **_kwargs: next(replies))
    monkeypatch.setattr(agent, "append_message_and_persist", lambda messages, message: messages.append(message))
    monkeypatch.setattr(agent, "render_markdown", lambda _content: None)
    monkeypatch.setattr(agent, "render_tool_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "should_show_tool_status", lambda _tool: False)
    monkeypatch.setattr(agent, "ui_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "trace_event", lambda *_args, **_kwargs: None)

    def run_tool(_request, **_kwargs):
        state["_maintenance_control_action"] = {
            "action": "sleep",
            "wake_prompt": "Resume the document study from section eight.",
        }
        return json.dumps({"status": "sleep_requested"})

    monkeypatch.setattr(agent, "run_requested_tool", run_tool)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "continue"}]

    with pytest.raises(MaintenanceControlRequested):
        agent.answer_with_structured_tools(messages, runtime_state=state)

    assert any(
        message.get("source") == "runtime" and "Maintenance is mandatory now" in message.get("content", "")
        for message in messages
    )
    assert mandatory_latch(state) is not None


def test_soft_authorization_expires_with_request_decision(monkeypatch):
    state = {}
    prepare_request_decision(state, snapshot(0.5))
    clear_request_decision(state)
    monkeypatch.setattr(maintenance_pressure, "maintenance_pressure_snapshot", lambda: snapshot(0.0))

    with pytest.raises(RuntimeError, match="available only at pressure 0.5"):
        agent.run_requested_tool(
            {
                "tool": "sleep_for_maintenance",
                "args": {"wake_prompt": "Resume the document study from section eight."},
            },
            runtime_state=state,
        )


def test_resuming_continuation_can_request_next_maintenance(monkeypatch):
    state = {
        "maintenance_continuation": {
            "status": "resuming",
            "continuation_id": "continuation-1",
            "run_id": "maintenance_run_1",
        }
    }
    prepare_request_decision(state, snapshot(0.5))
    monkeypatch.setattr(maintenance_pressure, "maintenance_pressure_snapshot", lambda: snapshot(0.5))
    events = []
    monkeypatch.setattr(agent, "trace_event", lambda _state, event_type, **fields: events.append((event_type, fields)))

    result = agent.run_requested_tool(
        {
            "tool": "sleep_for_maintenance",
            "args": {"wake_prompt": "Continue the long task from the next pending step."},
        },
        runtime_state=state,
    )

    assert json.loads(result)["status"] == "sleep_requested"
    assert state["_maintenance_control_action"]["action"] == "sleep"
    assert any(
        event_type == "maintenance.resuming_continuation_reaccepted"
        and fields["continuation_id"] == "continuation-1"
        for event_type, fields in events
    )


def test_running_continuation_still_rejects_next_maintenance(monkeypatch):
    state = {
        "maintenance_continuation": {
            "status": "running",
            "continuation_id": "continuation-1",
        }
    }
    prepare_request_decision(state, snapshot(0.5))
    monkeypatch.setattr(maintenance_pressure, "maintenance_pressure_snapshot", lambda: snapshot(0.5))

    with pytest.raises(RuntimeError, match="A maintenance continuation is already active"):
        agent.run_requested_tool(
            {
                "tool": "sleep_for_maintenance",
                "args": {"wake_prompt": "Continue the long task from the next pending step."},
            },
            runtime_state=state,
        )


def test_bridge_releases_gate_after_continuation_is_running(monkeypatch):
    server = BridgeServer()
    prepare_request_decision(server.runtime_state, snapshot(1.0))
    events = []
    monkeypatch.setattr(server, "_start_maintenance_continuation", lambda: True)
    monkeypatch.setattr(agent, "trace_event", lambda _state, event_type, **fields: events.append((event_type, fields)))

    server._dispatch_maintenance_control(
        {"action": "sleep", "wake_prompt": "Resume the document study from section eight."}
    )

    assert server.runtime_state["maintenance_continuation"]["status"] == "running"
    assert mandatory_latch(server.runtime_state) is None
    assert any(event_type == "maintenance.gate_released" for event_type, _fields in events)


def test_resuming_continuation_can_chain_three_maintenance_cycles(monkeypatch):
    server = BridgeServer()
    starts = []
    monkeypatch.setattr(server, "_start_maintenance_continuation", lambda: starts.append(True) or True)
    continuation_id = "continuation-1"

    for index in range(3):
        server.runtime_state["maintenance_continuation"] = {
            "status": "resuming",
            "continuation_id": continuation_id,
            "failure_count": 0,
            "run_id": f"maintenance_run_{index}",
        }
        server._dispatch_maintenance_control(
            {"action": "sleep", "wake_prompt": f"Continue after maintenance cycle {index + 1}."}
        )
        continuation = server.runtime_state["maintenance_continuation"]
        assert continuation["status"] == "running"
        assert continuation["continuation_id"] == continuation_id
        assert continuation["run_id"] is None

    assert len(starts) == 3


def test_bridge_start_failure_moves_to_failure_controls(monkeypatch):
    server = BridgeServer()
    prepare_request_decision(server.runtime_state, snapshot(1.0))
    monkeypatch.setattr(server, "_start_maintenance_continuation", lambda: False)
    monkeypatch.setattr(server, "enqueue_wake", lambda *_args, **_kwargs: True)

    server._dispatch_maintenance_control(
        {"action": "sleep", "wake_prompt": "Resume the document study from section eight."}
    )

    assert mandatory_latch(server.runtime_state) is None
    assert server.runtime_state["maintenance_continuation"]["status"] == "failed"
    names = {item["function"]["name"] for item in agent.tool_schemas_for_model(server.runtime_state)}
    assert names == {"retry_ltm_maintenance", "return_maintenance_failure"}


def test_release_is_idempotent():
    state = {}
    prepare_request_decision(state, snapshot(1.0))
    assert release_mandatory_latch(state) is not None
    assert release_mandatory_latch(state) is None
    assert request_decision(state)["mode"] == "mandatory"
