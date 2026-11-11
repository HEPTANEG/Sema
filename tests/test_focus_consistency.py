import json

import pytest

import memory_engine.event_stream as event_stream
from memory_engine.focus import lifecycle as focus_lifecycle
from memory_engine.focus import maintainer as focus_maintainer
from memory_engine.focus import store as focus_store


def _message(role, content):
    return {"role": role, "content": content}


def _messages():
    return [
        _message("system", "system"),
        _message("user", "first user"),
        _message("assistant", "first assistant"),
        _message("user", "second user"),
        _message("assistant", "second assistant"),
    ]


def _patch_wm_preview(monkeypatch, selected_slots, wm_slots=None):
    import memory_engine.observer as observer

    wm_slots = list(selected_slots if wm_slots is None else wm_slots)
    omitted = [
        {"start_index": slot, "end_index": slot}
        for slot in wm_slots
    ]
    monkeypatch.setattr(
        observer,
        "context_build_preview",
        lambda *_args, **_kwargs: {
            "ok": True,
            "working_memory": {"selected_slot_seqs": list(selected_slots)},
            "short_term_memory": {"omitted_units": omitted},
        },
    )


def _record_assignment(connection, focus, slots, segment_id=None, status="assigned"):
    focus_store._record_assignment(
        connection,
        {
            "slot_range": {"start": min(slots), "end": max(slots)},
            "slot_seqs": list(slots),
        },
        {"confidence": 0.9, "reason": "test assignment"},
        {
            "assigned": status == "assigned",
            "target_segment_id": segment_id or "",
            "reason": "test assignment",
        },
    )


def _issue(report, code):
    return next(item for item in report["issues"] if item["code"] == code)


def test_focus_consistency_report_is_ok_for_valid_state(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [1, 2])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current topic", "Current summary", "active")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)

    assert report["status"] == "ok"
    assert report["error_count"] == 0
    assert report["warning_count"] == 0
    assert report["repair_fingerprint"] == ""
    assert report["issues"] == []


def test_focus_consistency_reports_structural_cross_table_failures(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [1, 2, 3, 4])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Topic A", "Summary A", "active")
        inactive_id = focus_store._create_segment(connection, "Topic B", "Summary B", "inactive")
        focus_store._update_segment(connection, active_id, ranges=[{"start": 1, "end": 2}])
        focus_store._update_segment(connection, inactive_id, ranges=[{"start": 2, "end": 3}])
        _record_assignment(connection, focus, [1], status="unassigned")
        _record_assignment(connection, focus, [3], active_id)
        _record_assignment(connection, focus, [4], "seg_missing")
        focus_store._record_focus_ingest_hint(connection, "seg_missing", 99)
        focus_store._state_set(
            connection,
            "last_focus_prepare_result",
            json.dumps(
                {
                    "detector_recorded": True,
                    "target_segment_id": "seg_detector_missing",
                }
            ),
        )
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    codes = {item["code"] for item in report["issues"]}

    assert report["status"] == "error"
    assert report["should_trigger_full_maintainer"] is True
    assert report["repair_fingerprint"]
    assert {
        "segment_range_overlap",
        "unassigned_slots_covered",
        "assigned_slots_not_covered",
        "assigned_segment_missing",
        "pending_hint_invalid_slot",
        "pending_hint_segment_missing",
        "detector_active_target_lost",
    }.issubset(codes)
    assert _issue(report, "segment_range_overlap")["repairable"] is True
    assert _issue(report, "assigned_segment_missing")["repairable"] is False


def test_focus_consistency_reports_assignment_overlap_and_invalid_status(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [1, 2, 3])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current topic", "Summary", "active")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 3}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        _record_assignment(connection, focus, [2, 3], segment_id)
        connection.execute(
            "UPDATE focus_assignments SET status = 'broken', slot_start = 2.5 "
            "WHERE slot_start = 2 AND slot_end = 3"
        )
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    codes = {item["code"] for item in report["issues"]}

    assert "assignment_slot_overlap" in codes
    assert "invalid_assignment_status" in codes
    assert "invalid_assignment_slots" in codes


def test_focus_consistency_reports_corrupt_ranges_and_active_cardinality(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        first_id = focus_store._create_segment(connection, "Topic A", "Summary", "active")
        second_id = focus_store._create_segment(connection, "Topic B", "Summary", "active")
        connection.execute(
            "UPDATE focus_segments SET slot_ranges = '{broken' WHERE segment_id = ?",
            (second_id,),
        )
        connection.commit()

    multiple_active = focus.run_focus_consistency_audit(messages)
    with event_stream.ensure_event_stream() as connection:
        connection.execute(
            "UPDATE focus_segments SET status = 'inactive' WHERE segment_id IN (?, ?)",
            (first_id, second_id),
        )
        connection.commit()
    no_active = focus.run_focus_consistency_audit(messages)

    assert _issue(multiple_active, "invalid_segment_ranges")["severity"] == "error"
    assert _issue(multiple_active, "multiple_active_segments")["severity"] == "warning"
    assert _issue(no_active, "no_active_segment")["severity"] == "warning"


@pytest.mark.parametrize("work_item_status", ["pending", "processing", "completed", "failed"])
def test_focus_consistency_reports_resident_slots_independently_of_injection(
    isolated_workspace,
    monkeypatch,
    work_item_status,
):
    import memory_engine.focus as focus
    import memory_engine.ltm.formation as formation
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [], wm_slots=[1, 2])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        formation._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Historical topic", "Summary", "active")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        segment = focus_store._segment_by_id(connection, segment_id)
        formation._create_work_item(
            connection,
            "segment",
            segment,
            [{"start": 1, "end": 1}],
            formation.FORMATION_PRIORITIES["active"],
        )
        connection.execute("UPDATE formation_work_items SET status = ?", (work_item_status,))
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    metrics = report["metrics"]

    assert metrics["residency_evaluated"] is True
    assert metrics["wm_injected_slot_count"] == 0
    assert metrics["wm_resident_slot_count"] == 2
    assert metrics["wm_resident_not_injected_slot_count"] == 2
    assert metrics["wm_resident_raw_token_count"] > 0
    assert "cold_formation_captured_slots" not in {issue["code"] for issue in report["issues"]}
    assert "cold_formation_uncaptured_slots" not in {issue["code"] for issue in report["issues"]}


def test_focus_consistency_reports_resolved_retirement_backlog_without_full_trigger(
    isolated_workspace,
    monkeypatch,
):
    import memory_engine.focus as focus
    import memory_engine.ltm.formation as formation
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [], wm_slots=[1, 2])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        formation._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closed", "Summary", "resolved")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        formation._create_work_item(
            connection,
            "segment",
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 2}],
            formation.FORMATION_PRIORITIES["resolved"],
        )
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)

    assert _issue(report, "resolved_retirement_backlog")["count"] == 2
    assert report["metrics"]["resolved_retirement_backlog_slot_count"] == 2
    assert report["should_trigger_full_maintainer"] is False


def test_focus_consistency_rejects_nonresolved_and_unassigned_retired_rows(
    isolated_workspace,
    monkeypatch,
):
    import memory_engine.focus as focus
    import memory_engine.ltm.formation as formation
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [], wm_slots=[1, 2])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        formation._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current", "Summary", "active")
        _record_assignment(connection, focus, [1], segment_id)
        _record_assignment(connection, focus, [2], status="unassigned")
        formation._create_work_item(
            connection,
            "segment",
            focus_store._segment_by_id(connection, segment_id),
            [{"start": 1, "end": 1}],
            formation.FORMATION_PRIORITIES["active"],
        )
        formation._create_work_item(
            connection,
            "unassigned",
            None,
            [{"start": 2, "end": 2}],
            formation.FORMATION_PRIORITIES["unassigned"],
        )
        connection.execute(
            "UPDATE focus_assignments SET lifecycle_status = 'retired', retired_at = 'old'"
        )
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    codes = {issue["code"] for issue in report["issues"]}

    assert "nonresolved_retired_assignment" in codes
    assert "retired_unassigned_assignment" in codes
    assert report["should_trigger_full_maintainer"] is False


def test_focus_consistency_bounds_samples_and_excludes_message_content(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    secret = "SECRET_MESSAGE_BODY_MUST_NOT_APPEAR"
    messages = [_message("system", "system"), _message("user", secret)]
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Duplicate title", "Summary", "active")
        for _index in range(11):
            focus_store._create_segment(connection, "Duplicate title", "Summary", "inactive")
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)
    rendered = json.dumps(report, ensure_ascii=False)

    assert len(_issue(report, "duplicate_segment_title")["samples"]["segment_ids"]) == 10
    assert secret not in rendered


def test_focus_status_refresh_does_not_replace_persisted_consistency_report(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    import memory_engine.observer as observer
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])
    persisted = {"status": "error", "checked_at": "persisted-sentinel"}
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._state_set(
            connection,
            "last_consistency_report",
            json.dumps(persisted, sort_keys=True),
        )
        connection.commit()

    status = focus.focus_segments_status(messages)
    snapshot = observer._focus_segments_snapshot()
    with event_stream.ensure_event_stream() as connection:
        stored = focus_store._load_json_state(connection, "last_consistency_report", {})

    assert status["focus_consistency_report"]["checked_at"] != "persisted-sentinel"
    assert snapshot["focus_consistency_report"] == persisted
    assert stored == persisted


def test_read_only_consistency_audit_does_not_run_schema_writes(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current topic", "Summary", "active")

    monkeypatch.setattr(
        focus_lifecycle,
        "_ensure_tables",
        lambda _connection: pytest.fail("read-only audit attempted a schema write"),
    )

    report = focus.run_focus_consistency_audit(messages, persist=False)
    status = focus.focus_segments_status(messages)

    assert report["status"] == "ok"
    assert status["focus_consistency_report"]["status"] == "ok"


def test_focus_consistency_detects_consumed_hint_inside_assignment_span(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [1, 2])
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current topic", "Summary", "active")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 2}])
        _record_assignment(connection, focus, [1, 2], segment_id)
        focus_store._record_focus_ingest_hint(connection, segment_id, 2)
        connection.commit()

    report = focus.run_focus_consistency_audit(messages)

    assert _issue(report, "consumed_ingest_hint")["samples"]["slot_seqs"] == [2]


def test_focus_consistency_persists_only_when_requested(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])

    report = focus.run_focus_consistency_audit(messages, persist=True)
    with event_stream.ensure_event_stream() as connection:
        stored = focus_store._load_json_state(connection, "last_consistency_report", {})

    assert stored == report


def test_full_maintainer_uses_consistency_fingerprint_and_cooldown(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(focus_maintainer, "_full_segment_capacity", lambda *_args, **_kwargs: 20)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current topic", "Summary", "active")
        report = {
            "should_trigger_full_maintainer": True,
            "repair_fingerprint": "fingerprint-a",
            "issues": [],
        }
        focus_store._state_set(connection, "last_consistency_report", json.dumps(report))
        connection.commit()
        first = focus_maintainer._full_maintenance_plan(connection, messages)
        focus_store._state_set(connection, "last_full_consistency_attempt_fingerprint", "fingerprint-a")
        focus_store._state_set(connection, "last_full_consistency_attempt_turn", "2")
        connection.commit()
        cooled = focus_maintainer._full_maintenance_plan(connection, messages)
        report["repair_fingerprint"] = "fingerprint-b"
        focus_store._state_set(connection, "last_consistency_report", json.dumps(report))
        connection.commit()
        changed = focus_maintainer._full_maintenance_plan(connection, messages)

    assert first["reason"] == "consistency_report"
    assert first["consistency_repair_fingerprint"] == "fingerprint-a"
    assert cooled["scheduled"] is False
    assert cooled["reason"] == "consistency_report_cooldown_pending"
    assert changed["reason"] == "consistency_report"
    assert changed["consistency_repair_fingerprint"] == "fingerprint-b"


def test_nonrepairable_consistency_error_does_not_trigger_full_maintainer(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(focus_maintainer, "_full_segment_capacity", lambda *_args, **_kwargs: 20)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current topic", "Summary", "active")
        focus_store._state_set(
            connection,
            "last_consistency_report",
            json.dumps(
                {
                    "should_trigger_full_maintainer": False,
                    "repair_fingerprint": "",
                    "issues": [{"code": "assigned_segment_missing", "severity": "error"}],
                }
            ),
        )
        connection.commit()
        plan = focus_maintainer._full_maintenance_plan(connection, messages)

    assert plan["scheduled"] is False
    assert plan["reason"] == "below_capacity_threshold"


def test_full_maintainer_records_consistency_attempt_before_cooldown(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _patch_wm_preview(monkeypatch, [])
    monkeypatch.setattr(focus_maintainer, "_full_segment_capacity", lambda *_args, **_kwargs: 20)
    monkeypatch.setattr(
        focus_maintainer,
        "_call_full_maintainer_json",
        lambda _prompt: json.dumps(
            {"repair_plan": [{"action": "leave_unchanged", "reason": "insufficient evidence"}]}
        ),
    )
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Duplicate title", "Summary", "active")
        focus_store._create_segment(connection, "Duplicate title", "Summary", "inactive")
        connection.commit()

    report = focus.run_focus_consistency_audit(messages, persist=True)
    first = focus.run_full_segment_maintenance(messages)
    second = focus.run_full_segment_maintenance(messages)
    with event_stream.ensure_event_stream() as connection:
        attempted_fingerprint = focus_store._state_get(
            connection,
            "last_full_consistency_attempt_fingerprint",
            "",
        )

    assert first["triggered"] is True
    assert first["reason"] == "consistency_report"
    assert first["consistency_repair_fingerprint"] == report["repair_fingerprint"]
    assert attempted_fingerprint == report["repair_fingerprint"]
    assert second["triggered"] is False
    assert second["reason"] == "consistency_report_cooldown_pending"
