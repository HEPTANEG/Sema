import pytest


def _message(role, content):
    return {"role": role, "content": content}


def test_stm_builds_user_started_interaction_blocks():
    from memory_engine.stm_projection import build_interaction_blocks

    messages = [
        _message("system", "system"),
        _message("user", "first"),
        _message("assistant", "answer"),
        _message("tool", "tool result"),
        _message("user", "second"),
        _message("assistant", "final"),
    ]

    blocks = build_interaction_blocks(messages)

    assert len(blocks) == 2
    assert blocks[0]["start_index"] == 1
    assert blocks[0]["end_index"] == 3
    assert blocks[0]["message_count"] == 3
    assert blocks[1]["start_index"] == 4
    assert blocks[1]["end_index"] == 5


def test_stm_projection_preserves_system_and_selects_recent_complete_blocks():
    from memory_engine.stm_projection import build_interaction_blocks, build_stm_projection

    messages = [
        _message("system", "system"),
        _message("user", "older"),
        _message("assistant", "older answer"),
        _message("user", "latest"),
        _message("assistant", "latest answer"),
    ]
    blocks = build_interaction_blocks(messages)
    latest_budget = blocks[-1]["token_count"]

    projection = build_stm_projection(messages, budget=latest_budget)

    assert projection["messages"] == [
        _message("system", "system"),
        _message("user", "latest"),
        _message("assistant", "latest answer"),
    ]
    assert projection["selected_tokens"] == latest_budget
    assert len(projection["selected_blocks"]) == 1
    assert len(projection["omitted_blocks"]) == 1


def test_stm_projection_keeps_all_blocks_when_they_fit():
    from memory_engine.stm_projection import build_interaction_blocks, build_stm_projection

    messages = [
        _message("system", "system"),
        _message("user", "first"),
        _message("assistant", "first answer"),
        _message("user", "second"),
        _message("assistant", "second answer"),
    ]
    total_budget = sum(block["token_count"] for block in build_interaction_blocks(messages))

    projection = build_stm_projection(messages, budget=total_budget)

    assert projection["messages"] == messages
    assert len(projection["selected_blocks"]) == 2
    assert projection["omitted_blocks"] == []


def test_stm_projection_does_not_reselect_watermark_locked_blocks():
    from memory_engine.stm_projection import build_interaction_blocks, build_stm_projection

    messages = [
        _message("system", "system"),
        _message("user", "first"),
        _message("assistant", "first answer"),
        _message("user", "second"),
        _message("assistant", "second answer"),
        _message("user", "third"),
        _message("assistant", "third answer"),
    ]
    total_budget = sum(block["token_count"] for block in build_interaction_blocks(messages))

    projection = build_stm_projection(messages, budget=total_budget, wm_ingress_watermark=4)

    assert projection["messages"] == [
        _message("system", "system"),
        _message("user", "third"),
        _message("assistant", "third answer"),
    ]
    assert [block["end_index"] for block in projection["omitted_blocks"]] == [2, 4]
    assert projection["watermark_locked_omitted_block_count"] == 2


def test_stm_projection_keeps_only_complete_blocks_and_metadata_only_omissions():
    from memory_engine.stm_projection import build_interaction_blocks, build_stm_projection

    secret = "SECRET_OLDER_BODY"
    messages = [
        _message("system", "system prompt " * 50),
        _message("user", secret),
        _message("assistant", "older answer " * 20),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
        _message("tool", "latest tool output"),
    ]
    latest_budget = build_interaction_blocks(messages)[-1]["token_count"]

    projection = build_stm_projection(messages, budget=latest_budget)

    assert projection["messages"] == [
        _message("system", "system prompt " * 50),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
        _message("tool", "latest tool output"),
    ]
    assert len(projection["selected_blocks"]) == 1
    assert projection["selected_blocks"][0]["start_index"] == 3
    assert projection["selected_blocks"][0]["end_index"] == 5
    assert len(projection["omitted_blocks"]) == 1
    assert "messages" not in projection["omitted_blocks"][0]
    assert secret not in str(projection["omitted_blocks"])


def test_stm_projection_rejects_latest_block_over_budget():
    from memory_engine.stm_projection import build_stm_projection

    messages = [
        _message("system", "system"),
        _message("user", "latest"),
        _message("assistant", "latest answer"),
    ]

    with pytest.raises(RuntimeError, match="STM latest interaction block exceeds budget"):
        build_stm_projection(messages, budget=1)


def test_stm_status_is_metadata_only():
    from memory_engine.stm_projection import stm_projection_status

    messages = [
        _message("system", "system"),
        _message("user", "secret user body"),
        _message("assistant", "secret assistant body"),
    ]

    status = stm_projection_status(messages)

    assert status["selected_block_count"] == 1
    assert status["selected_blocks"][0]["start_index"] == 1
    assert status["total_message_count"] == 3
    assert status["non_system_message_count"] == 2
    assert "omitted_tokens" in status
    assert "total_block_tokens" in status
    assert "budget_used_ratio" in status
    assert "largest_block_tokens" in status
    assert "secret user body" not in str(status)
