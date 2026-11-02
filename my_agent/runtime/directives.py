from agents.temporal_state import build_runtime_clock_snapshot


def messages_with_runtime_directives(messages, runtime_state=None, trace_writer=None):
    runtime_messages = []
    clock = build_runtime_clock_snapshot()
    clock_message = build_runtime_clock_message(clock)
    if clock_message:
        runtime_messages.append(clock_message)
    interaction_message = build_interaction_source_directive(
        (runtime_state or {}).get("interaction_source")
    )
    if interaction_message:
        runtime_messages.append(interaction_message)
    trace_runtime_directives(
        runtime_state,
        trace_writer=trace_writer,
        clock_message=bool(clock_message),
        directive_count=len(runtime_messages),
        clock_local_date=clock.get("local_date", ""),
        clock_timezone=clock.get("timezone", ""),
    )
    if not runtime_messages:
        return messages
    if not messages:
        return runtime_messages
    return [messages[0], *runtime_messages, *messages[1:]]


def trace_runtime_directives(runtime_state=None, trace_writer=None, **fields):
    if runtime_state is None or not runtime_state.get("trace_id") or trace_writer is None:
        return
    request_index = int(runtime_state.get("runtime_directive_request_count") or 0) + 1
    runtime_state["runtime_directive_request_count"] = request_index
    try:
        trace_writer(
            runtime_state["trace_id"],
            "runtime_directives",
            status="ok",
            model_request_index=request_index,
            **fields,
        )
    except OSError:
        return


def build_runtime_clock_message(clock=None):
    clock = clock or build_runtime_clock_snapshot()
    lines = [
        "Current clock for this request only.",
        f"local_now: {clock['local_now']}",
        f"local_date: {clock['local_date']}",
        f"timezone: {clock['timezone']}",
        f"utc_now: {clock['utc_now']}",
        "",
        "Use this clock only when it is relevant to the user's task.",
        "This is deterministic runtime context, not a Temporal Agent decision.",
    ]
    return {
        "role": "system",
        "content": "\n".join(lines),
    }

def build_interaction_source_directive(source):
    if source != "telegram":
        return None
    return {
        "role": "system",
        "content": (
            "This turn comes from Telegram Remote.\n"
            "Keep the response concise and mobile-friendly.\n"
            "Prefer plain text and avoid Markdown formatting."
        ),
    }
