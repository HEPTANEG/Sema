def emit_ui_event(runtime_state, event_type, **fields):

    if runtime_state is None:
        return None

    event = {"type": event_type, **fields}
    sink = runtime_state.get("ui_event_sink")
    if sink is None:
        return event

    try:
        sink(event)
    except Exception as error:

        runtime_state.setdefault("ui_event_errors", []).append(str(error))
    return event
