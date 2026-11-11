import pytest


def _message(role, content, **extra):
    return {"role": role, "content": content, **extra}


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
    assert (blocks[0]["start_index"], blocks[0]["end_index"]) == (1, 3)
    assert (blocks[1]["start_index"], blocks[1]["end_index"]) == (4, 5)


def test_projection_units_keep_tool_call_and_all_matching_results_atomic():
    from memory_engine.stm_projection import build_projection_units

    messages = [
        _message("user", "task"),
        _message(
            "assistant",
            "",
            tool_calls=[
                {"id": "call-1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                {"id": "call-2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
            ],
        ),
        _message("tool", "first", tool_call_id="call-1"),
        _message("tool", "second", tool_call_id="call-2"),
        _message("assistant", "done"),
    ]

    units = build_projection_units(messages)

    assert [unit["kind"] for unit in units] == ["user", "tool_bundle", "assistant"]
    assert (units[1]["start_index"], units[1]["end_index"], units[1]["message_count"]) == (1, 3, 3)


def test_projection_units_keep_unmatched_tool_as_orphan():
    from memory_engine.stm_projection import build_projection_units

    units = build_projection_units([_message("tool", "legacy", tool_call_id="missing")])

    assert len(units) == 1
    assert units[0]["kind"] == "orphan_tool"


def test_stm_projection_selects_continuous_recent_slot_units():
    from memory_engine.stm_projection import build_projection_units, build_stm_projection

    messages = [
        _message("system", "system"),
        _message("user", "older"),
        _message("assistant", "older answer"),
        _message("user", "latest"),
        _message("assistant", "latest answer"),
    ]
    units = build_projection_units(messages)
    budget = units[-2]["token_count"] + units[-1]["token_count"]

    projection = build_stm_projection(messages, budget=budget)

    assert projection["messages"] == [
        _message("system", "system"),
        _message("user", "latest"),
        _message("assistant", "latest answer"),
    ]
    assert projection["selected_tokens"] == budget
    assert len(projection["selected_units"]) == 2
    assert len(projection["omitted_units"]) == 2


def test_stm_projection_stops_at_first_unit_that_does_not_fit():
    from memory_engine.stm_projection import build_projection_units, build_stm_projection

    messages = [
        _message("user", "tiny old"),
        _message("assistant", "large middle " * 200),
        _message("user", "latest"),
    ]
    units = build_projection_units(messages)
    budget = units[0]["token_count"] + units[2]["token_count"]

    projection = build_stm_projection(messages, budget=budget)

    assert projection["messages"] == [_message("user", "latest")]
    assert [(unit["start_index"], unit["end_index"]) for unit in projection["omitted_units"]] == [(0, 0), (1, 1)]


def test_stm_projection_locks_units_that_intersect_watermark():
    from memory_engine.stm_projection import build_projection_units, build_stm_projection

    messages = [
        _message("assistant", "", tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]),
        _message("tool", "result", tool_call_id="call-1"),
        _message("user", "eligible"),
    ]
    budget = sum(unit["token_count"] for unit in build_projection_units(messages))

    projection = build_stm_projection(messages, budget=budget, wm_ingress_watermark=0)

    assert projection["messages"] == [_message("user", "eligible")]
    assert projection["omitted_units"][0]["kind"] == "tool_bundle"
    assert projection["watermark_locked_omitted_unit_count"] == 1


def test_leading_system_messages_are_outside_stm_budget():
    from memory_engine.stm_projection import build_projection_units, build_stm_projection

    messages = [_message("system", "system " * 1000), _message("user", "latest")]
    budget = build_projection_units(messages)[0]["token_count"]

    projection = build_stm_projection(messages, budget=budget)

    assert projection["messages"] == messages
    assert projection["selected_tokens"] == budget


def test_stm_projection_omission_metadata_contains_no_bodies():
    from memory_engine.stm_projection import build_projection_units, build_stm_projection

    secret = "SECRET_OLDER_BODY"
    messages = [_message("user", secret), _message("assistant", "latest")]
    budget = build_projection_units(messages)[-1]["token_count"]

    projection = build_stm_projection(messages, budget=budget)

    assert "messages" not in projection["omitted_units"][0]
    assert secret not in str(projection["omitted_units"])


def test_stm_projection_rejects_newest_atomic_unit_over_budget():
    from memory_engine.stm_projection import build_stm_projection

    with pytest.raises(RuntimeError, match="STM newest atomic unit exceeds budget"):
        build_stm_projection([_message("user", "latest")], budget=1)


def test_stm_status_is_metadata_only():
    from memory_engine.stm_projection import stm_projection_status

    messages = [_message("system", "system"), _message("user", "secret user body")]

    status = stm_projection_status(messages)

    assert status["selected_unit_count"] == 1
    assert status["selected_units"][0]["start_index"] == 1
    assert status["total_message_count"] == 2
    assert "total_unit_tokens" in status
    assert "largest_unit_tokens" in status
    assert "secret user body" not in str(status)
