from bridge.ui_events import emit_ui_event


def test_emit_ui_event_returns_none_without_runtime_state():
    assert emit_ui_event(None, "turn_start") is None


def test_emit_ui_event_sends_event_to_sink():
    events = []
    runtime_state = {"ui_event_sink": events.append}

    event = emit_ui_event(runtime_state, "tool_started", tool="read_file")

    assert event == {"type": "tool_started", "tool": "read_file"}
    assert events == [event]


def test_emit_ui_event_sink_error_does_not_raise():
    def broken_sink(_event):
        raise RuntimeError("ui failed")

    runtime_state = {"ui_event_sink": broken_sink}

    event = emit_ui_event(runtime_state, "model_thinking", path="structured")

    assert event == {"type": "model_thinking", "path": "structured"}
    assert runtime_state["ui_event_errors"] == ["ui failed"]
