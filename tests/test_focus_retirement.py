import json

import pytest

import memory_engine.event_stream as event_stream
from memory_engine.focus import lifecycle as focus_lifecycle
from memory_engine.focus import maintainer as focus_maintainer
from memory_engine.focus import ranges as focus_ranges
from memory_engine.focus import store as focus_store
from memory_engine.focus.common import FOCUS_RESIDENCY_SEMANTICS_VERSION


def _messages():
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "user", "content": "second user"},
        {"role": "assistant", "content": "second assistant"},
    ]


def _patch_wm_preview(monkeypatch, selected_slots=(), wm_slots=(1, 2, 3, 4)):
    import memory_engine.observer as observer

    monkeypatch.setattr(
        observer,
        "context_build_preview",
        lambda *_args, **_kwargs: {
            "ok": True,
            "working_memory": {"selected_slot_seqs": list(selected_slots)},
            "short_term_memory": {
                "omitted_units": [
                    {"start_index": slot, "end_index": slot} for slot in wm_slots
                ]
            },
        },
    )


def _record_assignment(connection, focus, slots, segment_id=None, status="assigned"):
    focus_store._record_assignment(
        connection,
        {"slot_range": {"start": min(slots), "end": max(slots)}, "slot_seqs": list(slots)},
        {"confidence": 0.9, "reason": "test assignment"},
        {
            "assigned": status == "assigned",
            "target_segment_id": segment_id or "",
            "reason": "test assignment",
        },
    )


def _create_work_item(connection, formation, segment, ranges, status="pending", source_kind="segment"):
    formation._ensure_tables(connection)
    formation._create_work_item(
        connection,
        source_kind,
        segment,
        ranges,
        formation.FORMATION_PRIORITIES[source_kind if source_kind == "unassigned" else segment["status"]],
    )
    connection.execute("UPDATE formation_work_items SET status = ?", (status,))


def _seed_project(monkeypatch, selected_slots=(), wm_slots=(1, 2, 3, 4)):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, selected_slots, wm_slots)
    return focus, messages


@pytest.mark.parametrize("work_item_status", ["pending", "processing", "completed", "failed"])
def test_assigned_unit_retires_for_every_formation_status(
    isolated_workspace, monkeypatch, work_item_status
):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        segment = focus_store._segment_by_id(connection, segment_id)
        _create_work_item(connection, formation, segment, [{"start": 1, "end": 2}], work_item_status)
        original = focus_store._segment_by_id(connection, segment_id)
        connection.commit()

    result = focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        segment = focus_store._segment_by_id(connection, segment_id)
        assignment = focus_store._load_assignments(connection)[0]
    assert result["updated"] is True
    assert result["retired_assignment_count"] == 1
    assert result["retired_slot_count"] == 2
    assert segment["slot_ranges"] == []
    assert segment["title"] == original["title"]
    assert segment["summary"] == original["summary"]
    assert segment["status"] == original["status"]
    assert segment["last_touched_at"] == original["last_touched_at"]
    assert assignment["lifecycle_status"] == "retired"
    assert assignment["retired_at"]
    report = focus.run_focus_consistency_audit(messages)
    assert report["error_count"] == 0


def test_unassigned_unit_remains_live_for_full_maintenance_retry(isolated_workspace, monkeypatch):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current", "Summary", "active")
        _record_assignment(connection, focus, [1, 2], status="unassigned")
        _create_work_item(
            connection,
            formation,
            None,
            [{"start": 1, "end": 2}],
            source_kind="unassigned",
        )
        connection.commit()

    result = focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        health = focus_store._assignment_health(connection)
        inspected = focus_maintainer._inspect_unassigned_blocks(connection)
        assignment = focus_store._load_assignments(connection)[0]
    assert result["retired_unassigned_count"] == 0
    assert result["retired_slot_count"] == 0
    assert assignment["status"] == "unassigned"
    assert assignment["lifecycle_status"] == "live"
    assert health["unassigned_block_count"] == 1
    assert health["retired_unassigned_count"] == 0
    assert len(inspected["assignments"]) == 1


@pytest.mark.parametrize("segment_status", ["active", "inactive"])
def test_nonresolved_assignment_remains_live_when_cold_and_captured(
    isolated_workspace,
    monkeypatch,
    segment_status,
):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(
            connection,
            "Resident",
            "Summary",
            segment_status,
        )
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 2}],
        )
        connection.commit()

    result = focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        assignment = focus_store._load_assignments(connection)[0]
        segment = focus_store._segment_by_id(connection, segment_id)
    assert result["retired_assignment_count"] == 0
    assert assignment["lifecycle_status"] == "live"
    assert segment["slot_ranges"] == [{"start": 1, "end": 2}]


@pytest.mark.parametrize(
    ("wm_slots", "formation_ranges"),
    [([2], [{"start": 1, "end": 2}]), ([1, 2], [{"start": 1, "end": 1}])],
)
def test_partial_assignment_unit_is_deferred(
    isolated_workspace, monkeypatch, wm_slots, formation_ranges
):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch, wm_slots=wm_slots)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            formation_ranges,
        )
        connection.commit()

    result = focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        segment = focus_store._segment_by_id(connection, segment_id)
        assignment = focus_store._load_assignments(connection)[0]
    assert result["updated"] is False
    assert result["deferred_partial_assignment_count"] == 1
    assert segment["slot_ranges"] == [{"start": 1, "end": 2}]
    assert assignment["lifecycle_status"] == "live"
    assert assignment["retired_at"] is None


def test_legacy_slots_retire_with_assignment_unit_and_operation_is_idempotent(
    isolated_workspace, monkeypatch
):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 3}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 3}],
        )
        connection.commit()

    first = focus.run_focus_range_retirement(messages)
    with event_stream.ensure_event_stream() as connection:
        first_retired_at = focus_store._load_assignments(connection)[0]["retired_at"]
    second = focus.run_focus_range_retirement(messages)
    with event_stream.ensure_event_stream() as connection:
        assignment = focus_store._load_assignments(connection)[0]
        segment = focus_store._segment_by_id(connection, segment_id)
        assignment_ranges = focus_store._assignment_ranges(connection)

    assert first["retired_slot_count"] == 3
    assert segment["slot_ranges"] == []
    assert second["updated"] is False
    assert second["retired_assignment_count"] == 0
    assert assignment["retired_at"] == first_retired_at
    assert focus_ranges._assignment_range_exists(assignment_ranges, 1, 2)


def test_retirement_blocks_on_assignment_range_inconsistency(isolated_workspace, monkeypatch):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 1}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 2}],
        )
        connection.commit()

    result = focus.run_focus_range_retirement(messages)

    assert result["reason"] == "consistency_blocked"
    assert "assigned_slots_not_covered" in result["blocking_issue_codes"]
    with event_stream.ensure_event_stream() as connection:
        assert focus_store._load_assignments(connection)[0]["lifecycle_status"] == "live"


def test_retirement_failure_rolls_back_ranges_and_assignment_lifecycle(isolated_workspace, monkeypatch):
    import memory_engine.ltm.formation as formation

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 2}],
        )
        connection.commit()

    monkeypatch.setattr(
        focus_lifecycle,
        "_assert_no_overlapping_segments",
        lambda _segments: (_ for _ in ()).throw(RuntimeError("simulation failed")),
    )
    result = focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        segment = focus_store._segment_by_id(connection, segment_id)
        assignment = focus_store._load_assignments(connection)[0]
    assert result["reason"] == "focus_range_retirement_failed"
    assert "simulation failed" in result["error"]
    assert segment["slot_ranges"] == [{"start": 1, "end": 2}]
    assert assignment["lifecycle_status"] == "live"
    assert assignment["retired_at"] is None


def test_audit_validates_retired_lifecycle_invariants(isolated_workspace, monkeypatch):
    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'now'"
        )
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    codes = {issue["code"] for issue in report["issues"]}
    assert "retired_assignment_still_live" in codes
    assert "retired_assignment_not_formation_captured" in codes

    with event_stream.ensure_event_stream() as connection:
        connection.execute("UPDATE focus_assignments SET lifecycle_status = 'broken'")
        connection.commit()
    report = focus.run_focus_consistency_audit(messages)
    assert "invalid_assignment_lifecycle_status" in {issue["code"] for issue in report["issues"]}


def test_empty_segment_with_retired_assignment_or_formation_reference_is_not_deletable(
    isolated_workspace, monkeypatch
):
    import memory_engine.ltm.formation as formation

    focus, _messages_value = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        source_id = focus_store._create_segment(connection, "Historical", "Summary", "inactive")
        target_id = focus_store._create_segment(connection, "Current", "Summary", "active")
        _record_assignment(connection, focus, [1, 2], source_id)
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'now'"
        )
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, source_id),
            [{"start": 1, "end": 2}],
        )
        connection.commit()

        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "delete_empty_segment", "segment_id": source_id}],
        )
        merge = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "merge_segments", "target_segment_id": target_id, "source_segment_ids": [source_id]}],
        )
        focus_maintainer._apply_repair_simulation(connection, merge)
        assignment = focus_store._load_assignments(connection)[0]

    assert simulation["applied_actions"] == []
    assert "assignment audit history" in simulation["rejected_actions"][0]["error"]
    assert assignment["segment_id"] == target_id
    assert assignment["lifecycle_status"] == "retired"


def test_assignment_schema_migration_and_new_rows_default_to_live(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())
    with event_stream.ensure_event_stream() as connection:
        connection.execute(
            """
            CREATE TABLE focus_assignments (
                assignment_id TEXT PRIMARY KEY,
                segment_id TEXT,
                slot_start INTEGER NOT NULL,
                slot_end INTEGER NOT NULL,
                slot_seqs TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(slot_start, slot_end)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO focus_assignments(
                assignment_id, segment_id, slot_start, slot_end, slot_seqs,
                status, confidence, reason, created_at, updated_at
            ) VALUES ('asg_old', NULL, 1, 2, '[1, 2]', 'unassigned', 0.9, 'old', 'now', 'now')
            """
        )
        focus_store._ensure_tables(connection)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(focus_assignments)")}
        _record_assignment(connection, focus, [3, 4], status="unassigned")
        assignments = focus_store._load_assignments(connection)

    assert {"lifecycle_status", "retired_at"}.issubset(columns)
    assert [assignment["lifecycle_status"] for assignment in assignments] == ["live", "live"]
    assert all(assignment["retired_at"] is None for assignment in assignments)


def test_residency_reconciliation_restores_nonresolved_and_unassigned_units(isolated_workspace):
    import memory_engine.focus as focus
    import memory_engine.ltm.formation as formation
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        inactive_id = focus_store._create_segment(connection, "Parked", "Summary", "inactive")
        resolved_id = focus_store._create_segment(connection, "Closed", "Summary", "resolved")
        _record_assignment(connection, focus, [1], inactive_id)
        _record_assignment(connection, focus, [2], status="unassigned")
        _record_assignment(connection, focus, [3], resolved_id)
        focus_store._update_segment(connection, inactive_id, ranges=[])
        focus_store._update_segment(connection, resolved_id, ranges=[])
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, inactive_id),
            [{"start": 1, "end": 1}],
        )
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'old'"
        )
        connection.execute(
            "DELETE FROM focus_segment_state WHERE key = 'focus_residency_semantics_version'"
        )
        connection.commit()

    first = focus.run_focus_residency_reconciliation()
    second = focus.run_focus_residency_reconciliation()

    with event_stream.ensure_event_stream() as connection:
        assignments = focus_store._load_assignments(connection)
        inactive = focus_store._segment_by_id(connection, inactive_id)
        version = focus_store._state_get(connection, "focus_residency_semantics_version")

    assert first["restored_assignment_count"] == 2
    assert first["restored_assigned_count"] == 1
    assert first["restored_unassigned_count"] == 1
    assert first["restored_slot_count"] == 2
    assert inactive["slot_ranges"] == [{"start": 1, "end": 1}]
    assert [row["lifecycle_status"] for row in assignments] == ["live", "live", "retired"]
    assert assignments[0]["retired_at"] is None
    assert assignments[1]["retired_at"] is None
    assert assignments[2]["retired_at"] == "old"
    assert version == str(FOCUS_RESIDENCY_SEMANTICS_VERSION)
    assert second["reason"] == "already_reconciled"
    assert second["updated"] is False


def test_residency_reconciliation_skips_conflicts_and_keeps_version_pending(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Current", "Summary", "active")
        inactive_id = focus_store._create_segment(connection, "Parked", "Summary", "inactive")
        focus_store._update_segment(connection, active_id, ranges=[{"start": 1, "end": 1}])
        _record_assignment(connection, focus, [1], inactive_id)
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'old'"
        )
        connection.execute(
            "DELETE FROM focus_segment_state WHERE key = 'focus_residency_semantics_version'"
        )
        connection.commit()

    result = focus.run_focus_residency_reconciliation()

    with event_stream.ensure_event_stream() as connection:
        assignment = focus_store._load_assignments(connection)[0]
        version = focus_store._state_get(connection, "focus_residency_semantics_version")

    assert result["reason"] == "residency_reconciliation_incomplete"
    assert result["restored_assignment_count"] == 0
    assert result["skipped_assignment_count"] == 1
    assert assignment["lifecycle_status"] == "retired"
    assert version is None


def test_residency_reconciliation_rolls_back_and_remains_retryable(
    isolated_workspace,
    monkeypatch,
):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Parked", "Summary", "inactive")
        _record_assignment(connection, focus, [1], segment_id)
        focus_store._update_segment(connection, segment_id, ranges=[])
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'old'"
        )
        connection.execute(
            "DELETE FROM focus_segment_state WHERE key = 'focus_residency_semantics_version'"
        )
        connection.commit()

    original_write = focus_lifecycle._write_focus_residency_reconciliation_result
    calls = {"count": 0}

    def fail_once(connection, result):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("forced reconciliation commit failure")
        return original_write(connection, result)

    monkeypatch.setattr(
        focus_lifecycle,
        "_write_focus_residency_reconciliation_result",
        fail_once,
    )

    failed = focus.run_focus_residency_reconciliation()

    with event_stream.ensure_event_stream() as connection:
        assignment = focus_store._load_assignments(connection)[0]
        segment = focus_store._segment_by_id(connection, segment_id)
        version = focus_store._state_get(connection, "focus_residency_semantics_version")
    assert failed["reason"] == "focus_residency_reconciliation_failed"
    assert "forced reconciliation commit failure" in failed["error"]
    assert assignment["lifecycle_status"] == "retired"
    assert assignment["retired_at"] == "old"
    assert segment["slot_ranges"] == []
    assert version is None

    retried = focus.run_focus_residency_reconciliation()
    assert retried["reason"] == "residency_reconciled"
    assert retried["restored_assignment_count"] == 1


def test_status_and_observer_expose_retirement_without_writing(isolated_workspace, monkeypatch):
    import memory_engine.ltm.formation as formation
    import memory_engine.observer as observer

    focus, messages = _seed_project(monkeypatch)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _create_work_item(
            connection,
            formation,
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 2}],
        )
        connection.commit()
    focus.run_focus_range_retirement(messages)

    with event_stream.ensure_event_stream() as connection:
        before = connection.execute(
            "SELECT key, value FROM focus_segment_state ORDER BY key"
        ).fetchall()
        before_state = [(row["key"], row["value"]) for row in before]
    status = focus.focus_segments_status(messages)
    snapshot = observer._focus_segments_snapshot()
    with event_stream.ensure_event_stream() as connection:
        after = connection.execute(
            "SELECT key, value FROM focus_segment_state ORDER BY key"
        ).fetchall()
        after_state = [(row["key"], row["value"]) for row in after]

    assert before_state == after_state
    assert status["assignment_health"]["live_assignment_count"] == 0
    assert status["assignment_health"]["retired_assignment_count"] == 1
    assert status["last_focus_range_retirement_result"]["retired_slot_count"] == 2
    assert snapshot["assignment_health"]["retired_assignment_count"] == 1
    assert snapshot["last_focus_range_retirement_result"]["reason"] == "retired_focus_ranges"
    rendered = focus.format_focus_segments_status(status)
    assert "retired_assignment_count: 1" in rendered
    assert "last_focus_range_retirement_result:" in rendered
