from runtime.context import _drop_incomplete_tool_call_blocks


def test_context_drops_incomplete_tool_call_block():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "user", "content": "new"},
    ]

    assert _drop_incomplete_tool_call_blocks(messages) == [messages[0], messages[2]]


def test_context_keeps_complete_tool_call_block():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]

    assert _drop_incomplete_tool_call_blocks(messages) == messages


def test_context_drops_partial_multi_tool_call_block_and_results():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}, {"id": "call-2"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        {"role": "user", "content": "continue"},
    ]

    assert _drop_incomplete_tool_call_blocks(messages) == [messages[2]]
