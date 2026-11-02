def test_token_counter_counts_text_and_messages():
    from memory_engine.token_counter import count_message_tokens, count_messages_tokens, count_text_tokens

    message = {"role": "user", "content": "hello 中文"}

    assert count_text_tokens("hello 中文") > 0
    assert count_message_tokens(message) >= count_text_tokens("hello 中文")
    assert count_messages_tokens([message, message]) == count_message_tokens(message) * 2
