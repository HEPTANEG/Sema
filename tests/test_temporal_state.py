from datetime import datetime, timezone

from agents import temporal_state


def test_ensure_process_temporal_state_sets_started_at(monkeypatch):
    runtime_state = {}
    fixed = datetime(2026, 5, 25, 8, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: fixed)

    state = temporal_state.ensure_process_temporal_state(runtime_state)

    assert state["process_started_at"] == "2026-05-25T08:00:00+00:00"
    assert runtime_state["temporal_runtime"] == state


def test_build_runtime_clock_snapshot_uses_utc_and_local_fields():
    fixed = datetime(2026, 5, 25, 8, 1, 0, tzinfo=timezone.utc)

    snapshot = temporal_state.build_runtime_clock_snapshot(now=fixed)

    assert snapshot["utc_now"] == "2026-05-25T08:01:00+00:00"
    assert snapshot["local_now"]
    assert snapshot["local_date"]
    assert snapshot["timezone"]


def test_mark_turn_started_sets_turn_clock(monkeypatch):
    runtime_state = {}
    fixed = datetime(2026, 5, 25, 8, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: fixed)

    state = temporal_state.mark_turn_started(runtime_state)

    assert state["turn_started_at"] == "2026-05-25T08:01:00+00:00"
    assert state["previous_user_turn_at"] == ""
    assert state["last_user_turn_at"] == "2026-05-25T08:01:00+00:00"
    assert state["continuous_session_started_at"] == "2026-05-25T08:01:00+00:00"
    assert state["recent_turn_count"] == 1
    assert state["user_gap_seconds"] is None
    assert state["assistant_gap_seconds"] is None


def test_mark_turn_started_extends_continuous_session(monkeypatch):
    runtime_state = {
        "temporal_runtime": {
            "process_started_at": "2026-05-25T08:00:00+00:00",
            "last_user_turn_at": "2026-05-25T08:01:00+00:00",
            "continuous_session_started_at": "2026-05-25T08:01:00+00:00",
            "recent_turn_count": 1,
        }
    }
    fixed = datetime(2026, 5, 25, 8, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: fixed)

    state = temporal_state.mark_turn_started(runtime_state)
    snapshot = temporal_state.build_temporal_runtime_snapshot(runtime_state, now=fixed)

    assert state["previous_user_turn_at"] == "2026-05-25T08:01:00+00:00"
    assert state["last_user_turn_at"] == "2026-05-25T08:10:00+00:00"
    assert state["continuous_session_started_at"] == "2026-05-25T08:01:00+00:00"
    assert state["recent_turn_count"] == 2
    assert state["user_gap_seconds"] == 540
    assert snapshot["user_gap_seconds"] == 540
    assert snapshot["continuous_session_duration_seconds"] == 540


def test_mark_turn_started_resets_continuous_session_after_gap(monkeypatch):
    runtime_state = {
        "temporal_runtime": {
            "process_started_at": "2026-05-25T08:00:00+00:00",
            "last_user_turn_at": "2026-05-25T08:01:00+00:00",
            "continuous_session_started_at": "2026-05-25T08:01:00+00:00",
            "recent_turn_count": 3,
        }
    }
    fixed = datetime(2026, 5, 25, 8, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: fixed)

    state = temporal_state.mark_turn_started(runtime_state)
    snapshot = temporal_state.build_temporal_runtime_snapshot(runtime_state, now=fixed)

    assert state["previous_user_turn_at"] == "2026-05-25T08:01:00+00:00"
    assert state["continuous_session_started_at"] == "2026-05-25T08:11:01+00:00"
    assert state["recent_turn_count"] == 1
    assert state["user_gap_seconds"] == 601
    assert snapshot["user_gap_seconds"] == 601
    assert snapshot["continuous_session_duration_seconds"] == 0


def test_mark_assistant_turn_finished_sets_visible_reply_clock(monkeypatch):
    runtime_state = {}
    fixed = datetime(2026, 5, 25, 8, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: fixed)

    state = temporal_state.mark_assistant_turn_finished(runtime_state)

    assert state["last_assistant_turn_at"] == "2026-05-25T08:02:00+00:00"


def test_assistant_finish_does_not_rewrite_current_turn_gap(monkeypatch):
    runtime_state = {
        "temporal_runtime": {
            "process_started_at": "2026-05-25T08:00:00+00:00",
            "last_user_turn_at": "2026-05-25T08:01:00+00:00",
            "last_assistant_turn_at": "2026-05-25T08:02:00+00:00",
            "continuous_session_started_at": "2026-05-25T08:01:00+00:00",
            "recent_turn_count": 1,
        }
    }
    turn_start = datetime(2026, 5, 25, 8, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: turn_start)

    temporal_state.mark_turn_started(runtime_state)
    snapshot_before_finish = temporal_state.build_temporal_runtime_snapshot(runtime_state, now=turn_start)

    reply_finished = datetime(2026, 5, 25, 8, 5, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(temporal_state, "_utc_now", lambda: reply_finished)
    temporal_state.mark_assistant_turn_finished(runtime_state)
    snapshot_after_finish = temporal_state.build_temporal_runtime_snapshot(runtime_state, now=reply_finished)

    assert snapshot_before_finish["assistant_gap_seconds"] == 180
    assert snapshot_after_finish["assistant_gap_seconds"] == 180
    assert snapshot_after_finish["last_assistant_turn_at"] == "2026-05-25T08:05:20+00:00"


def test_snapshot_handles_missing_topic_maintenance(isolated_workspace):
    runtime_state = {
        "temporal_runtime": {
            "process_started_at": "2026-05-25T08:00:00+00:00",
            "turn_started_at": "2026-05-25T08:10:00+00:00",
            "last_user_turn_at": "2026-05-25T08:10:00+00:00",
            "last_assistant_turn_at": "2026-05-25T08:09:30+00:00",
        }
    }
    now = datetime(2026, 5, 25, 8, 12, 30, tzinfo=timezone.utc)

    snapshot = temporal_state.build_temporal_runtime_snapshot(runtime_state, now=now)

    assert snapshot["process_age_seconds"] == 750
    assert snapshot["turn_age_seconds"] == 150
    assert snapshot["recent_turn_count"] == 1
    assert snapshot["user_gap_seconds"] is None
    assert snapshot["assistant_gap_seconds"] == 30
    assert "topic_maintenance" not in snapshot


def test_format_temporal_runtime_state_excludes_topic_maintenance(isolated_workspace):
    output = temporal_state.format_temporal_runtime_state({})

    assert "Runtime rhythm:" in output
    assert "recent_turn_count:" in output
    assert "continuous_session_duration:" in output
    assert "user_gap:" in output
    assert "assistant_gap:" in output
    assert "topic_maintenance:" not in output


def test_format_temporal_state_is_runtime_only(isolated_workspace):
    output = temporal_state.format_temporal_state({})

    assert output.startswith("Sema time:\nRuntime rhythm:")
    assert "semantic_temporal" not in output
    assert "time_sensitive" not in output
