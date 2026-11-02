import math
from datetime import datetime, timezone

import memory_engine.dynamic_context_budget as context_budget


def test_context_budget_matches_sporadic_simple_qa_fixture(monkeypatch):
    monkeypatch.setattr(context_budget, "_continuous_session_token_stats", lambda: context_budget._empty_token_stats())
    signals = {
        "recent_tool_count": 0,
        "recent_turn_count": 1,
        "continuous_session_duration_seconds": 60,
        "user_gap_seconds": None,
        "current_user_message_tokens": 0,
    }

    result = context_budget.compute_dynamic_context_budget(signals)

    expected_i = round(
        0.30 * (1 - math.exp(-1 / 4))
        + 0.30 * (1 - math.exp(-60 / 1200)),
        4,
    )
    assert result["scores"]["I"] == expected_i
    assert result["scores"]["S"] == 0.0
    assert "R" not in result["scores"]
    assert result["budgets"]["stm_min"] == 1000
    assert result["budgets"]["wm_min"] == 1000
    assert result["budgets"]["stm"] == 2587
    assert result["budgets"]["wm"] == 4968


def test_context_budget_uses_temporal_task_and_failure_signals(monkeypatch):
    monkeypatch.setattr(context_budget, "_continuous_session_token_stats", lambda: context_budget._empty_token_stats())
    monkeypatch.setattr(context_budget, "count_message_tokens", lambda message: len(message.get("content", "")))
    runtime_state = {
        "last_run": [
            {"tool": "run_command", "args": "python -m pytest", "result": "exit code 1\nfailed"},
            {"tool": "run_command", "args": "python -m pytest", "result": "exit code 1\nfailed"},
            {"tool": "write_file", "args": "app.py", "result": "Wrote file: app.py"},
        ],
    }
    fixed = datetime(2026, 5, 25, 8, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        context_budget,
        "_build_temporal_runtime_snapshot",
        lambda _runtime_state: {
            "recent_turn_count": 5,
            "continuous_session_duration_seconds": 1200,
            "user_gap_seconds": 30,
            "utc_now": fixed.isoformat(),
        },
    )

    result = context_budget.compute_and_store_dynamic_context_budget(
        [{"role": "user", "content": "still wrong, retry"}],
        runtime_state,
        emit_observer=False,
    )

    signals = result["signals"]
    assert signals["recent_tool_count"] == 3
    assert signals["current_user_message_tokens"] > 0
    assert "assistant_gap_seconds" not in signals
    assert "tool_failure_count" not in signals
    assert "repeated_command_count" not in signals
    assert "recent_error_message_count" not in signals
    assert "recent_test_count" not in signals
    assert "test_failure_count" not in signals
    assert "user_retry_phrase_count" not in signals
    assert result["budgets"]["stm"] <= context_budget.STM_MAX_TOKENS
    assert result["budgets"]["wm"] <= context_budget.WM_MAX_TOKENS
    assert runtime_state["context_pressure"] == result


def test_token_load_score_raises_budget_floor():
    base_signals = {
        "recent_tool_count": 0,
        "recent_turn_count": 1,
        "continuous_session_duration_seconds": 60,
        "user_gap_seconds": None,
        "current_user_message_tokens": 0,
    }

    low = context_budget.compute_dynamic_context_budget(base_signals)
    high = context_budget.compute_dynamic_context_budget(
        {
            **base_signals,
            "recent_session_token_total": 50000,
            "recent_session_token_average": 10000,
            "recent_session_token_max": 30000,
        }
    )

    assert high["scores"]["L"] > low["scores"]["L"]
    assert high["budgets"]["stm"] > low["budgets"]["stm"]
    assert high["budgets"]["wm"] > low["budgets"]["wm"]


def test_removed_retry_signals_do_not_affect_budget():
    baseline = context_budget.compute_dynamic_context_budget({"recent_tool_count": 0})
    with_removed_signals = context_budget.compute_dynamic_context_budget(
        {
            "recent_tool_count": 0,
            "tool_failure_count": 5,
            "repeated_command_count": 7,
            "recent_error_message_count": 9,
        }
    )

    assert with_removed_signals["budgets"] == baseline["budgets"]
    assert "R" not in with_removed_signals["scores"]


def test_current_user_input_has_proposed_token_load_weight():
    small = context_budget.compute_dynamic_context_budget(
        {"current_user_message_tokens": 0}
    )
    large = context_budget.compute_dynamic_context_budget(
        {"current_user_message_tokens": 4000}
    )

    assert large["features"]["f_current_user_input"] == round(1 - math.exp(-1), 4)
    assert large["scores"]["L"] > small["scores"]["L"]
    assert large["budgets"]["stm"] > small["budgets"]["stm"]
    assert large["budgets"]["wm"] > small["budgets"]["wm"]


def test_proposed_i_l_and_budget_aggregation_weights():
    result = context_budget.compute_dynamic_context_budget(
        {
            "recent_turn_count": 4,
            "continuous_session_duration_seconds": 1200,
            "user_gap_seconds": 0,
            "recent_tool_count": 4,
            "recent_session_token_total": 20000,
            "recent_session_token_average": 4000,
            "recent_session_token_max": 10000,
            "current_user_message_tokens": 4000,
        }
    )
    features = result["features"]
    expected_i = round(
        0.30 * features["f_turns"]
        + 0.30 * features["f_session"]
        + 0.40 * features["f_user_gap"],
        4,
    )
    expected_l = round(
        0.15 * features["f_token_total"]
        + 0.30 * features["f_token_average"]
        + 0.15 * features["f_token_max"]
        + 0.40 * features["f_current_user_input"],
        4,
    )
    expected_stm = 0.40 * expected_i + 0.20 * result["scores"]["S"] + 0.40 * expected_l
    expected_wm = max(expected_i, result["scores"]["S"], expected_l)

    assert result["scores"]["I"] == expected_i
    assert result["scores"]["L"] == expected_l
    assert result["scores"]["F_STM"] == round(expected_stm, 4)
    assert result["scores"]["F_WM"] == round(expected_wm, 4)


def test_empty_signals_use_proposed_one_k_minimum():
    result = context_budget.compute_dynamic_context_budget({})

    assert result["budgets"]["stm"] == 1000
    assert result["budgets"]["wm"] == 1000


def test_context_build_failure_forces_target_budget_to_max():
    result = context_budget.compute_dynamic_context_budget(
        {
            "context_build_failure": 1.0,
            "failure_target": "wm",
            "failure_required_tokens": 12000,
            "failure_failed_budget": 10000,
        }
    )

    assert result["scores"]["P_context"] == 1.0
    assert result["budgets"]["wm"] == context_budget.WM_MAX_TOKENS
    assert result["budgets"]["stm"] < context_budget.STM_MAX_TOKENS
    assert result["failure"]["budget_action"] == "force_wm_max"


def test_context_build_failure_details_parse_stm_and_wm_errors():
    from runtime import model_runtime

    stm = model_runtime.context_build_failure_details(
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000.")
    )
    wm = model_runtime.context_build_failure_details(
        RuntimeError("Context Builder WM slot exceeds budget: slot_seq=1, tokens=60000, budget=10000.")
    )

    assert stm["target"] == "stm"
    assert stm["required_tokens"] == 12000
    assert stm["needs_preflight_recovery"] is False
    assert wm["target"] == "wm"
    assert wm["required_tokens"] == 60000
    assert wm["needs_preflight_recovery"] is True


def test_budget_stabilizer_raises_immediately_and_decays_slowly(isolated_workspace):
    import memory_engine.budget_stabilizer as budget_stabilizer
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    budget_stabilizer.reset_budget_stabilizer_state()

    high = {
        "budget_source": "dynamic",
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 39000, "wm_min": 1000, "wm_max": 50000, "wm": 20000},
        "failure": {"context_build_failure": 0.0, "target": ""},
    }
    low = {
        "budget_source": "dynamic",
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 20000, "wm_min": 1000, "wm_max": 50000, "wm": 10000},
        "failure": {"context_build_failure": 0.0, "target": ""},
    }

    first = budget_stabilizer.apply_budget_stabilizer(high, persist=True)
    second = budget_stabilizer.apply_budget_stabilizer(low, persist=True)

    assert first["budgets"]["stm"] == 39000
    assert second["budgets"]["stm"] == 31200
    assert second["budgets"]["stm_raw"] == 20000
    assert second["stabilizer"]["context_build_seq"] == 2


def test_budget_stabilizer_holds_high_watermark_and_failure_max(isolated_workspace):
    import memory_engine.budget_stabilizer as budget_stabilizer
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    budget_stabilizer.reset_budget_stabilizer_state()

    high = {
        "budget_source": "dynamic",
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 45000, "wm_min": 1000, "wm_max": 50000, "wm": 10000},
        "failure": {"context_build_failure": 0.0, "target": ""},
    }
    low = {
        "budget_source": "dynamic",
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 12000, "wm_min": 1000, "wm_max": 50000, "wm": 10000},
        "failure": {"context_build_failure": 0.0, "target": ""},
    }
    failure = {
        "budget_source": "dynamic",
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 12000, "wm_min": 1000, "wm_max": 50000, "wm": 12000},
        "failure": {"context_build_failure": 1.0, "target": "wm"},
    }

    budget_stabilizer.apply_budget_stabilizer(high, persist=True)
    held = budget_stabilizer.apply_budget_stabilizer(low, persist=True)
    forced = budget_stabilizer.apply_budget_stabilizer(failure, persist=True)

    assert held["budgets"]["stm"] == 45000
    assert held["stabilizer"]["stm_hold_until_seq"] >= 4
    assert forced["budgets"]["wm"] == 50000
    assert forced["stabilizer"]["wm_hold_until_seq"] >= 8


def test_continuous_session_token_stats_walks_adjacent_gaps(isolated_workspace):
    import sqlite3
    import memory_engine.event_stream as event_stream
    from persistence import session

    session.ensure_project()
    session.save_last_session(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old break"},
            {"role": "assistant", "content": "recent one " * 100},
            {"role": "user", "content": "recent two " * 200},
        ]
    )
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        connection.execute("UPDATE slots SET created_at = ? WHERE slot_seq = 1", ("2026-06-24T00:00:00+00:00",))
        connection.execute("UPDATE slots SET created_at = ? WHERE slot_seq = 2", ("2026-06-24T00:20:00+00:00",))
        connection.execute("UPDATE slots SET created_at = ? WHERE slot_seq = 3", ("2026-06-24T00:25:00+00:00",))
        connection.commit()

    stats = event_stream.continuous_session_token_stats(gap_seconds=600)

    assert stats["recent_session_slot_count"] == 2
    assert stats["recent_session_token_total"] > 0
    assert stats["recent_session_token_max"] >= stats["recent_session_token_average"]


def test_context_budget_observer_is_metadata_only():
    events = []
    runtime_state = {
        "ui_event_sink": events.append,
        "intent": {"mode": "chat"},
    }

    result = context_budget.compute_and_store_dynamic_context_budget(
        [{"role": "user", "content": "secret body text should not appear"}],
        runtime_state,
        emit_observer=True,
    )

    assert events[-1]["type"] == "context_pressure"
    assert events[-1]["stm"] == result["budgets"]["stm"]
    assert "secret body text" not in str(events[-1])


def test_manual_budget_override_sets_effective_max_range(isolated_workspace, monkeypatch):
    import memory_engine.context_budget_override as context_budget_override
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    monkeypatch.setattr(context_budget, "_continuous_session_token_stats", lambda: context_budget._empty_token_stats())

    context_budget_override.set_budget_override("stm", 3000)
    result = context_budget.compute_and_store_dynamic_context_budget(
        [{"role": "user", "content": "hello"}],
        {},
        emit_observer=False,
    )

    assert result["budget_source"] == "manual_max_override+stabilized_preview"
    assert result["budgets"]["stm_max"] == 3000
    assert result["budgets"]["stm"] <= 3000
    assert result["budgets"]["wm"] != 3000
    assert result["budgets"]["stm_max_override"] == 3000
    assert result["budgets"]["manual_min"] == 2000
    assert result["scores"]["I"] >= 0

    context_budget_override.reset_budget_override()
    reset = context_budget.compute_and_store_dynamic_context_budget(
        [{"role": "user", "content": "hello"}],
        {},
        emit_observer=False,
    )

    assert reset["budget_source"] == "dynamic+stabilized_preview"
    assert reset["budgets"]["stm"] != 3000


def test_manual_budget_override_rejects_out_of_range(isolated_workspace):
    import memory_engine.context_budget_override as context_budget_override
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])

    try:
        context_budget_override.set_budget_override("stm", 1999)
    except RuntimeError as error:
        assert "between 2000 and 200000" in str(error)
    else:
        raise AssertionError("Expected out-of-range override to fail")
