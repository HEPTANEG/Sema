from memory_engine.context_builder import build_event_stream_context


def messages_for_model(messages, retrieval_plan=None, runtime_state=None):
    context = build_event_stream_context(messages, runtime_state=runtime_state)
    return _drop_incomplete_tool_call_blocks(context["messages"])


def _drop_incomplete_tool_call_blocks(messages):
    """Keep provider history valid after an interrupted tool turn.

    A process or Gateway restart can persist an assistant tool-call message
    before its tool result is appended. Providers reject that incomplete block
    on the next request, so omit only the incomplete block from the outbound
    provider context. The Event Stream remains unchanged as the source record.
    """
    repaired = []
    index = 0
    while index < len(messages):
        message = messages[index]
        tool_calls = message.get("tool_calls") or []
        if message.get("role") != "assistant" or not tool_calls:
            repaired.append(message)
            index += 1
            continue

        expected_ids = {
            str(call.get("id"))
            for call in tool_calls
            if isinstance(call, dict) and call.get("id")
        }
        following = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            following.append(messages[cursor])
            cursor += 1
        actual_ids = {
            str(item.get("tool_call_id"))
            for item in following
            if item.get("tool_call_id")
        }
        if expected_ids and expected_ids.issubset(actual_ids):
            repaired.append(message)
            repaired.extend(following)
        index = cursor if following else index + 1
    return repaired
