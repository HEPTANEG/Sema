import hashlib
import json

import memory_engine.event_stream as event_stream

from .common import (
    FOCUS_CONSISTENCY_SAMPLE_LIMIT,
    FOCUS_RESIDENCY_SEMANTICS_VERSION,
    REPAIRABLE_CONSISTENCY_CODES,
    RETIREMENT_BLOCKING_ISSUE_CODES,
    VALID_ASSIGNMENT_LIFECYCLE_STATUSES,
    VALID_ASSIGNMENT_STATUSES,
    VALID_STATUSES,
    _now,
    _single_line,
)

from .ranges import (
    _assert_no_overlapping_segments,
    _assignment_slots,
    _normalize_ranges,
    _ranges_from_slots,
    _remove_slots_from_ranges,
    _slots_from_ranges,
    _strict_assignment_slots,
    _strict_ranges,
)

from .store import (
    _assignment_from_row,
    _ensure_tables,
    _load_json_state,
    _read_only_event_stream_connection,
    _segment_from_row,
    _state_get,
    _state_set,
    _table_exists,
)

def _consistency_issue(
    issues,
    code,
    severity,
    *,
    count=1,
    segment_ids=None,
    assignment_ids=None,
    slot_seqs=None,
):
    issue = issues.setdefault(
        code,
        {
            "code": code,
            "severity": severity,
            "repairable": code in REPAIRABLE_CONSISTENCY_CODES,
            "count": 0,
            "segment_ids": set(),
            "assignment_ids": set(),
            "slot_seqs": set(),
        },
    )
    issue["count"] += max(1, int(count or 1))
    issue["segment_ids"].update(str(value) for value in segment_ids or [] if value)
    issue["assignment_ids"].update(str(value) for value in assignment_ids or [] if value)
    issue["slot_seqs"].update(int(value) for value in slot_seqs or [])

def _formation_work_item_slots(connection):
    from runtime.setup_capabilities import skipped_slots
    explicitly_skipped = skipped_slots(connection)
    if not _table_exists(connection, "formation_work_items"):
        return explicitly_skipped, 0
    rows = connection.execute("SELECT pending_slot_ranges FROM formation_work_items").fetchall()
    slots = set()
    for row in rows:
        try:
            ranges = json.loads(row["pending_slot_ranges"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(ranges, list):
            slots.update(_slots_from_ranges(_normalize_ranges(ranges)))
    return slots | explicitly_skipped, len(rows)

def _formation_work_item_segment_ids(connection):
    if not _table_exists(connection, "formation_work_items"):
        return set()
    rows = connection.execute(
        "SELECT DISTINCT segment_id FROM formation_work_items WHERE segment_id IS NOT NULL"
    ).fetchall()
    return {str(row["segment_id"]) for row in rows if row["segment_id"]}

def _working_memory_selected_slots(messages=None, runtime_state=None):
    projection = _focus_lifecycle_projection(messages, runtime_state)
    if projection is None:
        return None
    return projection["injected_slots"]

def _focus_lifecycle_projection(messages=None, runtime_state=None):
    from .. import observer as memory_observer

    preview = memory_observer.context_build_preview(messages, runtime_state)
    if not preview.get("ok"):
        return None
    injected_slots = {
        int(slot)
        for slot in ((preview.get("working_memory") or {}).get("selected_slot_seqs") or [])
    }
    wm_slots = set()
    for block in ((preview.get("short_term_memory") or {}).get("omitted_units") or []):
        try:
            start = int(block.get("start_index"))
            end = int(block.get("end_index"))
        except (TypeError, ValueError):
            continue
        if start >= 0 and end >= start:
            wm_slots.update(range(start, end + 1))
    return {
        "injected_slots": injected_slots,
        "wm_slots": wm_slots,
        "working_memory": dict(preview.get("working_memory") or {}),
    }

def _finalize_consistency_issues(issues):
    severity_order = {"error": 0, "warning": 1}
    finalized = []
    for issue in sorted(
        issues.values(),
        key=lambda item: (severity_order.get(item["severity"], 2), item["code"]),
    ):
        finalized.append(
            {
                "code": issue["code"],
                "severity": issue["severity"],
                "repairable": bool(issue["repairable"]),
                "count": int(issue["count"]),
                "samples": {
                    "segment_ids": sorted(issue["segment_ids"])[:FOCUS_CONSISTENCY_SAMPLE_LIMIT],
                    "assignment_ids": sorted(issue["assignment_ids"])[:FOCUS_CONSISTENCY_SAMPLE_LIMIT],
                    "slot_seqs": sorted(issue["slot_seqs"])[:FOCUS_CONSISTENCY_SAMPLE_LIMIT],
                },
            }
        )
    return finalized

def _consistency_repair_fingerprint(issues):
    repairable = []
    for issue in sorted(issues.values(), key=lambda item: item["code"]):
        if not issue["repairable"]:
            continue
        repairable.append(
            {
                "code": issue["code"],
                "segment_ids": sorted(issue["segment_ids"]),
                "assignment_ids": sorted(issue["assignment_ids"]),
                "slot_seqs": sorted(issue["slot_seqs"]),
            }
        )
    if not repairable:
        return ""
    payload = json.dumps(repairable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def _build_focus_consistency_report(connection, messages=None, runtime_state=None):
    from runtime.setup_capabilities import skipped_slots
    issues = {}
    slot_rows = connection.execute(
        """
        SELECT slots.slot_seq, slots.source, events.token_count AS active_token_count
        FROM slots
        JOIN events ON events.event_id = slots.active_event_id
        ORDER BY slots.slot_seq ASC
        """
    ).fetchall()
    slots_by_id = {int(row["slot_seq"]): row["source"] for row in slot_rows}
    slot_tokens = {int(row["slot_seq"]): int(row["active_token_count"] or 0) for row in slot_rows}
    segment_rows = connection.execute(
        "SELECT segment_id, title, status, slot_ranges FROM focus_segments ORDER BY segment_id ASC"
    ).fetchall()
    assignment_rows = connection.execute(
        "SELECT * FROM focus_assignments ORDER BY slot_start ASC, slot_end ASC, assignment_id ASC"
    ).fetchall()
    hint_rows = connection.execute(
        "SELECT start_slot, segment_id FROM focus_ingest_hints ORDER BY start_slot ASC"
    ).fetchall()
    formation_slots, formation_work_item_count = _formation_work_item_slots(connection)
    formation_segment_ids = _formation_work_item_segment_ids(connection)

    segments_by_id = {row["segment_id"]: row for row in segment_rows}
    segment_slots = {}
    slot_owner = {}
    active_ids = []
    titles = {}
    for row in segment_rows:
        segment_id = row["segment_id"]
        status = row["status"]
        if status not in VALID_STATUSES:
            _consistency_issue(issues, "invalid_segment_status", "error", segment_ids=[segment_id])
        if status == "active":
            active_ids.append(segment_id)
        title_key = _single_line(row["title"])
        if title_key:
            titles.setdefault(title_key, []).append(segment_id)
        ranges, invalid_count = _strict_ranges(row["slot_ranges"])
        if invalid_count:
            _consistency_issue(
                issues,
                "invalid_segment_ranges",
                "error",
                count=invalid_count,
                segment_ids=[segment_id],
            )
        owned_slots = _slots_from_ranges(ranges)
        segment_slots[segment_id] = owned_slots
        missing_slots = {slot for slot in owned_slots if slot not in slots_by_id}
        system_slots = {slot for slot in owned_slots if slots_by_id.get(slot) == "system"}
        if missing_slots:
            _consistency_issue(
                issues,
                "segment_range_missing_slot",
                "error",
                count=len(missing_slots),
                segment_ids=[segment_id],
                slot_seqs=missing_slots,
            )
        if system_slots:
            _consistency_issue(
                issues,
                "segment_range_system_slot",
                "error",
                count=len(system_slots),
                segment_ids=[segment_id],
                slot_seqs=system_slots,
            )
        for slot in owned_slots:
            previous_owner = slot_owner.get(slot)
            if previous_owner and previous_owner != segment_id:
                _consistency_issue(
                    issues,
                    "segment_range_overlap",
                    "error",
                    segment_ids=[previous_owner, segment_id],
                    slot_seqs=[slot],
                )
            else:
                slot_owner[slot] = segment_id

    for duplicate_ids in titles.values():
        if len(duplicate_ids) > 1:
            _consistency_issue(
                issues,
                "duplicate_segment_title",
                "warning",
                count=len(duplicate_ids) - 1,
                segment_ids=duplicate_ids,
            )

    assignment_owner = {}
    segment_assignment_ids = set()
    live_assignment_count = 0
    retired_assignment_count = 0
    live_unassigned_slots = set()
    retired_slots = set()
    live_assigned_slots = set()
    for row in assignment_rows:
        assignment_id = row["assignment_id"]
        status = row["status"]
        lifecycle_status = (
            row["lifecycle_status"] if "lifecycle_status" in set(row.keys()) else "live"
        )
        segment_id = row["segment_id"] or ""
        if lifecycle_status == "live":
            live_assignment_count += 1
        elif lifecycle_status == "retired":
            retired_assignment_count += 1
        elif lifecycle_status not in VALID_ASSIGNMENT_LIFECYCLE_STATUSES:
            _consistency_issue(
                issues,
                "invalid_assignment_lifecycle_status",
                "error",
                assignment_ids=[assignment_id],
            )
        if segment_id:
            segment_assignment_ids.add(segment_id)
        if status not in VALID_ASSIGNMENT_STATUSES:
            _consistency_issue(issues, "invalid_assignment_status", "error", assignment_ids=[assignment_id])
        slots, invalid_count = _strict_assignment_slots(row)
        if invalid_count:
            _consistency_issue(
                issues,
                "invalid_assignment_slots",
                "error",
                count=invalid_count,
                assignment_ids=[assignment_id],
            )
        missing_slots = {slot for slot in slots if slot not in slots_by_id}
        system_slots = {slot for slot in slots if slots_by_id.get(slot) == "system"}
        if missing_slots:
            _consistency_issue(
                issues,
                "assignment_missing_slot",
                "error",
                count=len(missing_slots),
                assignment_ids=[assignment_id],
                slot_seqs=missing_slots,
            )
        if system_slots:
            _consistency_issue(
                issues,
                "assignment_system_slot",
                "error",
                count=len(system_slots),
                assignment_ids=[assignment_id],
                slot_seqs=system_slots,
            )
        for slot in slots:
            previous_assignment = assignment_owner.get(slot)
            if previous_assignment and previous_assignment != assignment_id:
                _consistency_issue(
                    issues,
                    "assignment_slot_overlap",
                    "error",
                    assignment_ids=[previous_assignment, assignment_id],
                    slot_seqs=[slot],
                )
            else:
                assignment_owner[slot] = assignment_id
        if status == "assigned":
            if not segment_id or segment_id not in segments_by_id:
                _consistency_issue(
                    issues,
                    "assigned_segment_missing",
                    "error",
                    segment_ids=[segment_id],
                    assignment_ids=[assignment_id],
                )
            elif lifecycle_status == "live":
                live_assigned_slots.update(slots)
                uncovered = slots - segment_slots.get(segment_id, set())
                if uncovered:
                    _consistency_issue(
                        issues,
                        "assigned_slots_not_covered",
                        "error",
                        count=len(uncovered),
                        segment_ids=[segment_id],
                        assignment_ids=[assignment_id],
                        slot_seqs=uncovered,
                    )
        elif status == "unassigned":
            if segment_id:
                _consistency_issue(
                    issues,
                    "unassigned_has_segment",
                    "error",
                    segment_ids=[segment_id],
                    assignment_ids=[assignment_id],
                )
            covered = slots & set(slot_owner)
            if covered and lifecycle_status == "live":
                _consistency_issue(
                    issues,
                    "unassigned_slots_covered",
                    "error",
                    count=len(covered),
                    segment_ids={slot_owner[slot] for slot in covered},
                    assignment_ids=[assignment_id],
                    slot_seqs=covered,
                )
            if lifecycle_status == "live":
                live_unassigned_slots.update(slots)
        if lifecycle_status == "retired":
            retired_slots.update(slots)
            covered = slots & set(slot_owner)
            if covered:
                _consistency_issue(
                    issues,
                    "retired_assignment_still_live",
                    "error",
                    count=len(covered),
                    segment_ids={slot_owner[slot] for slot in covered},
                    assignment_ids=[assignment_id],
                    slot_seqs=covered,
                )
            uncaptured = slots - formation_slots
            if uncaptured:
                _consistency_issue(
                    issues,
                    "retired_assignment_not_formation_captured",
                    "error",
                    count=len(uncaptured),
                    assignment_ids=[assignment_id],
                    slot_seqs=uncaptured,
                )
            target = segments_by_id.get(segment_id) if segment_id else None
            if status == "unassigned":
                _consistency_issue(
                    issues,
                    "retired_unassigned_assignment",
                    "error",
                    assignment_ids=[assignment_id],
                    slot_seqs=slots,
                )
            elif target is not None and target["status"] != "resolved":
                _consistency_issue(
                    issues,
                    "nonresolved_retired_assignment",
                    "error",
                    segment_ids=[segment_id],
                    assignment_ids=[assignment_id],
                    slot_seqs=slots,
                )

    pending_hint_targets = set()
    for row in hint_rows:
        segment_id = row["segment_id"]
        raw_start_slot = row["start_slot"]
        if not isinstance(raw_start_slot, int) or isinstance(raw_start_slot, bool):
            start_slot = None
            _consistency_issue(
                issues,
                "pending_hint_invalid_slot",
                "error",
                segment_ids=[segment_id],
            )
        else:
            start_slot = raw_start_slot
        if start_slot is not None and start_slot in assignment_owner:
            _consistency_issue(
                issues,
                "consumed_ingest_hint",
                "warning",
                segment_ids=[segment_id],
                slot_seqs=[start_slot],
            )
            continue
        pending_hint_targets.add(segment_id)
        if start_slot is not None and (
            start_slot not in slots_by_id or slots_by_id.get(start_slot) != "user"
        ):
            _consistency_issue(
                issues,
                "pending_hint_invalid_slot",
                "error",
                segment_ids=[segment_id],
                slot_seqs=[start_slot],
            )
        target = segments_by_id.get(segment_id)
        if target is None:
            _consistency_issue(issues, "pending_hint_segment_missing", "error", segment_ids=[segment_id])
        elif target["status"] not in {"active", "inactive"}:
            _consistency_issue(issues, "pending_hint_segment_not_assignable", "error", segment_ids=[segment_id])

    prepare = _load_json_state(connection, "last_focus_prepare_result", {}) or {}
    expected_active_id = str(prepare.get("target_segment_id") or "") if prepare.get("detector_recorded") else ""
    if expected_active_id:
        target = segments_by_id.get(expected_active_id)
        if target is None or target["status"] != "active":
            _consistency_issue(
                issues,
                "detector_active_target_lost",
                "error",
                segment_ids=[expected_active_id],
            )
    elif not active_ids:
        _consistency_issue(issues, "no_active_segment", "warning")
    if len(active_ids) > 1:
        _consistency_issue(
            issues,
            "multiple_active_segments",
            "warning",
            count=len(active_ids) - 1,
            segment_ids=active_ids,
        )

    for row in segment_rows:
        segment_id = row["segment_id"]
        if (
            row["status"] == "inactive"
            and not segment_slots.get(segment_id)
            and segment_id not in segment_assignment_ids
            and segment_id not in pending_hint_targets
            and segment_id not in formation_segment_ids
        ):
            _consistency_issue(
                issues,
                "orphan_empty_inactive_segment",
                "warning",
                segment_ids=[segment_id],
            )

    residency_evaluated = False
    wm_injected_slots = set()
    wm_slots = set()
    resident_slots = set()
    resolved_retirement_backlog = set()
    try:
        projection = _focus_lifecycle_projection(messages, runtime_state)
        if projection is not None:
            residency_evaluated = True
            wm_injected_slots = projection["injected_slots"]
            wm_slots = {
                slot
                for slot in projection["wm_slots"]
                if slot in slots_by_id and slots_by_id.get(slot) != "system"
            }
            unaudited_slots = wm_slots - set(assignment_owner)
            valid_range_slots = {
                slot
                for slots in segment_slots.values()
                for slot in slots
                if slot in wm_slots and slot in slots_by_id and slots_by_id.get(slot) != "system"
            }
            resident_slots = (valid_range_slots | (live_unassigned_slots & wm_slots) | unaudited_slots) - retired_slots

            for row in assignment_rows:
                if row["status"] != "assigned" or row["lifecycle_status"] != "live":
                    continue
                segment = segments_by_id.get(row["segment_id"] or "")
                if segment is None or segment["status"] != "resolved":
                    continue
                slots, invalid_count = _strict_assignment_slots(row)
                if not invalid_count and slots and slots.issubset(wm_slots & formation_slots):
                    resolved_retirement_backlog.update(slots)
            for segment_id, slots in segment_slots.items():
                segment = segments_by_id.get(segment_id)
                if segment is None or segment["status"] != "resolved":
                    continue
                legacy = slots - live_assigned_slots - retired_slots
                resolved_retirement_backlog.update(legacy & wm_slots & formation_slots)
            if resolved_retirement_backlog:
                _consistency_issue(
                    issues,
                    "resolved_retirement_backlog",
                    "warning",
                    count=len(resolved_retirement_backlog),
                    slot_seqs=resolved_retirement_backlog,
                )
        else:
            _consistency_issue(issues, "residency_projection_unavailable", "warning")
    except Exception:
        _consistency_issue(issues, "residency_projection_unavailable", "warning")

    try:
        reconciliation_version = int(
            _state_get(connection, "focus_residency_semantics_version", "0") or 0
        )
    except (TypeError, ValueError):
        reconciliation_version = 0
    reconciliation = _load_json_state(connection, "last_focus_residency_reconciliation_result", {}) or {}
    if reconciliation_version < 2:
        _consistency_issue(issues, "residency_reconciliation_pending", "warning")
    if int(reconciliation.get("skipped_assignment_count") or 0) > 0:
        _consistency_issue(
            issues,
            "residency_reconciliation_conflict",
            "error",
            count=int(reconciliation.get("skipped_assignment_count") or 0),
            assignment_ids=reconciliation.get("skipped_assignment_samples") or [],
        )

    finalized = _finalize_consistency_issues(issues)
    error_count = sum(issue["count"] for issue in finalized if issue["severity"] == "error")
    warning_count = sum(issue["count"] for issue in finalized if issue["severity"] == "warning")
    repairable_issue_count = sum(issue["count"] for issue in finalized if issue["repairable"])
    all_segment_slots = set().union(*segment_slots.values()) if segment_slots else set()
    return {
        "status": "error" if error_count else "warning" if warning_count else "ok",
        "checked_at": _now(),
        "error_count": error_count,
        "warning_count": warning_count,
        "repairable_issue_count": repairable_issue_count,
        "should_trigger_full_maintainer": bool(repairable_issue_count),
        "repair_fingerprint": _consistency_repair_fingerprint(issues),
        "metrics": {
            "segment_count": len(segment_rows),
            "active_segment_count": len(active_ids),
            "segment_range_slot_count": len(all_segment_slots),
            "assignment_count": len(assignment_rows),
            "live_assignment_count": live_assignment_count,
            "retired_assignment_count": retired_assignment_count,
            "ingest_hint_count": len(hint_rows),
            "formation_work_item_count": formation_work_item_count,
            "formation_captured_slot_count": len(formation_slots - skipped_slots(connection)),
            "formation_explicitly_skipped_slot_count": len(skipped_slots(connection)),
            "residency_evaluated": residency_evaluated,
            "wm_injected_slot_count": len(wm_injected_slots),
            "wm_resident_slot_count": len(resident_slots),
            "wm_resident_not_injected_slot_count": len(resident_slots - wm_injected_slots),
            "wm_resident_raw_token_count": sum(slot_tokens.get(slot, 0) for slot in resident_slots),
            "resolved_retirement_backlog_slot_count": len(resolved_retirement_backlog),
            "focus_residency_semantics_version": reconciliation_version,
        },
        "issues": finalized,
    }

def _empty_focus_consistency_report():
    return {
        "status": "ok",
        "checked_at": _now(),
        "error_count": 0,
        "warning_count": 0,
        "repairable_issue_count": 0,
        "should_trigger_full_maintainer": False,
        "repair_fingerprint": "",
        "metrics": {
            "segment_count": 0,
            "active_segment_count": 0,
            "segment_range_slot_count": 0,
            "assignment_count": 0,
            "live_assignment_count": 0,
            "retired_assignment_count": 0,
            "ingest_hint_count": 0,
            "formation_work_item_count": 0,
            "formation_captured_slot_count": 0,
            "residency_evaluated": False,
            "wm_injected_slot_count": 0,
            "wm_resident_slot_count": 0,
            "wm_resident_not_injected_slot_count": 0,
            "wm_resident_raw_token_count": 0,
            "resolved_retirement_backlog_slot_count": 0,
            "focus_residency_semantics_version": 0,
        },
        "issues": [],
    }

def run_focus_consistency_audit(messages=None, runtime_state=None, persist=False):
    if not event_stream.event_stream_db_path().exists():
        return _empty_focus_consistency_report()
    if persist:
        connection = event_stream.ensure_event_stream()
        _ensure_tables(connection)
    else:
        connection = _read_only_event_stream_connection()
    with connection:
        required_tables = {
            "slots",
            "focus_segments",
            "focus_segment_state",
            "focus_assignments",
            "focus_ingest_hints",
        }
        if not all(_table_exists(connection, table) for table in required_tables):
            return _empty_focus_consistency_report()
        report = _build_focus_consistency_report(connection, messages, runtime_state)
        if persist:
            _state_set(
                connection,
                "last_consistency_report",
                json.dumps(report, ensure_ascii=False, sort_keys=True),
            )
            connection.commit()
        return report

def _empty_focus_residency_reconciliation_result(reason="already_reconciled", error=""):
    return {
        "triggered": False,
        "updated": False,
        "reason": reason,
        "from_version": 0,
        "to_version": 0,
        "restored_assignment_count": 0,
        "restored_assigned_count": 0,
        "restored_unassigned_count": 0,
        "restored_slot_count": 0,
        "updated_segment_count": 0,
        "skipped_assignment_count": 0,
        "restored_assignment_samples": [],
        "skipped_assignment_samples": [],
        "error": str(error or ""),
    }

def _write_focus_residency_reconciliation_result(connection, result):
    _state_set(
        connection,
        "last_focus_residency_reconciliation_result",
        json.dumps(result, ensure_ascii=False, sort_keys=True),
    )

def _persist_focus_residency_reconciliation_result(result):
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        _write_focus_residency_reconciliation_result(connection, result)
        connection.commit()
    return result

def run_focus_residency_reconciliation(messages=None, runtime_state=None):
    del messages, runtime_state
    if not event_stream.event_stream_db_path().exists():
        return _empty_focus_residency_reconciliation_result("no_event_stream")

    connection = event_stream.ensure_event_stream()
    try:
        _ensure_tables(connection)
        try:
            current_version = int(
                _state_get(connection, "focus_residency_semantics_version", "0") or 0
            )
        except (TypeError, ValueError):
            current_version = 0
        if current_version >= FOCUS_RESIDENCY_SEMANTICS_VERSION:
            result = _empty_focus_residency_reconciliation_result("already_reconciled")
            result.update({"from_version": current_version, "to_version": current_version})
            return result

        connection.execute("BEGIN IMMEDIATE")
        slot_rows = connection.execute("SELECT slot_seq, source FROM slots").fetchall()
        slots_by_id = {int(row["slot_seq"]): row["source"] for row in slot_rows}
        segments = {}
        segment_slots = {}
        for row in connection.execute("SELECT * FROM focus_segments").fetchall():
            ranges, invalid_count = _strict_ranges(row["slot_ranges"])
            if invalid_count or row["status"] not in VALID_STATUSES:
                raise RuntimeError("Residency reconciliation found invalid segment state.")
            segment = _segment_from_row(row)
            segment["slot_ranges"] = ranges
            segments[segment["segment_id"]] = segment
            segment_slots[segment["segment_id"]] = _slots_from_ranges(ranges)
        slot_owner = {}
        for segment_id, slots in segment_slots.items():
            if any(slot not in slots_by_id or slots_by_id.get(slot) == "system" for slot in slots):
                raise RuntimeError("Residency reconciliation found invalid segment slots.")
            if slots & set(slot_owner):
                raise RuntimeError("Residency reconciliation found overlapping segment ranges.")
            slot_owner.update({slot: segment_id for slot in slots})

        retired_rows = connection.execute(
            "SELECT * FROM focus_assignments WHERE lifecycle_status = 'retired' "
            "ORDER BY slot_start, slot_end, assignment_id"
        ).fetchall()
        restored = []
        skipped = []
        restored_slots = set()
        updated_segment_ids = set()
        for row in retired_rows:
            assignment_slots, invalid_count = _strict_assignment_slots(row)
            assignment_id = str(row["assignment_id"])
            if (
                invalid_count
                or not assignment_slots
                or any(slot not in slots_by_id or slots_by_id.get(slot) == "system" for slot in assignment_slots)
            ):
                skipped.append(assignment_id)
                continue
            assignment = _assignment_from_row(row)
            if assignment["status"] == "assigned":
                segment_id = assignment.get("segment_id") or ""
                target = segments.get(segment_id)
                if target is None:
                    skipped.append(assignment_id)
                    continue
                if target.get("status") == "resolved":
                    continue
                if target.get("status") not in {"active", "inactive"}:
                    skipped.append(assignment_id)
                    continue
                conflicting = {
                    slot for slot in assignment_slots if slot_owner.get(slot) not in {None, segment_id}
                }
                if conflicting:
                    skipped.append(assignment_id)
                    continue
                segment_slots[segment_id].update(assignment_slots)
                slot_owner.update({slot: segment_id for slot in assignment_slots})
                updated_segment_ids.add(segment_id)
            elif assignment["status"] == "unassigned":
                if assignment.get("segment_id") or assignment_slots & set(slot_owner):
                    skipped.append(assignment_id)
                    continue
            else:
                skipped.append(assignment_id)
                continue
            restored.append(assignment)
            restored_slots.update(assignment_slots)

        _assert_no_overlapping_segments(
            {
                segment_id: {
                    **segment,
                    "slot_ranges": _ranges_from_slots(segment_slots[segment_id]),
                }
                for segment_id, segment in segments.items()
            }
        )
        timestamp = _now()
        for segment_id in sorted(updated_segment_ids):
            connection.execute(
                "UPDATE focus_segments SET slot_ranges = ?, updated_at = ? WHERE segment_id = ?",
                (
                    json.dumps(_ranges_from_slots(segment_slots[segment_id]), ensure_ascii=False),
                    timestamp,
                    segment_id,
                ),
            )
        for assignment in restored:
            connection.execute(
                """
                UPDATE focus_assignments
                SET lifecycle_status = 'live', retired_at = NULL, updated_at = ?
                WHERE assignment_id = ? AND lifecycle_status = 'retired'
                """,
                (timestamp, assignment["assignment_id"]),
            )

        next_version = current_version
        if not skipped:
            next_version = FOCUS_RESIDENCY_SEMANTICS_VERSION
            _state_set(connection, "focus_residency_semantics_version", str(next_version))
        result = {
            "triggered": True,
            "updated": bool(restored),
            "reason": "residency_reconciled" if not skipped else "residency_reconciliation_incomplete",
            "from_version": current_version,
            "to_version": next_version,
            "restored_assignment_count": len(restored),
            "restored_assigned_count": sum(1 for row in restored if row["status"] == "assigned"),
            "restored_unassigned_count": sum(1 for row in restored if row["status"] == "unassigned"),
            "restored_slot_count": len(restored_slots),
            "updated_segment_count": len(updated_segment_ids),
            "skipped_assignment_count": len(skipped),
            "restored_assignment_samples": [
                row["assignment_id"] for row in restored[:FOCUS_CONSISTENCY_SAMPLE_LIMIT]
            ],
            "skipped_assignment_samples": skipped[:FOCUS_CONSISTENCY_SAMPLE_LIMIT],
            "error": "",
        }
        _write_focus_residency_reconciliation_result(connection, result)
        connection.commit()
        return result
    except Exception as error:
        connection.rollback()
        return _persist_focus_residency_reconciliation_result(
            _empty_focus_residency_reconciliation_result(
                "focus_residency_reconciliation_failed",
                error,
            )
        )
    finally:
        connection.close()

def _empty_focus_range_retirement_result(reason="no_eligible_focus_ranges", error=""):
    return {
        "triggered": False,
        "updated": False,
        "reason": reason,
        "retired_assignment_count": 0,
        "retired_assigned_count": 0,
        "retired_unassigned_count": 0,
        "retired_slot_count": 0,
        "retired_range_count": 0,
        "updated_segment_count": 0,
        "deferred_partial_assignment_count": 0,
        "segment_samples": [],
        "blocking_issue_codes": [],
        "error": str(error or ""),
    }

def _write_focus_range_retirement_result(connection, result):
    _state_set(
        connection,
        "last_focus_range_retirement_result",
        json.dumps(result, ensure_ascii=False, sort_keys=True),
    )

def _persist_focus_range_retirement_result(result):
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        _write_focus_range_retirement_result(connection, result)
        connection.commit()
    return result

def _validated_retirement_snapshot(connection, slots_by_id, formation_slots):
    segment_rows = connection.execute("SELECT * FROM focus_segments").fetchall()
    segments = {}
    segment_slots = {}
    slot_owner = {}
    for row in segment_rows:
        segment_id = row["segment_id"]
        ranges, invalid_count = _strict_ranges(row["slot_ranges"])
        if invalid_count or row["status"] not in VALID_STATUSES:
            raise RuntimeError("Retirement snapshot contains an invalid segment.")
        slots = _slots_from_ranges(ranges)
        if any(slot not in slots_by_id or slots_by_id.get(slot) == "system" for slot in slots):
            raise RuntimeError("Retirement snapshot contains an invalid segment slot.")
        if slots & set(slot_owner):
            raise RuntimeError("Retirement snapshot contains overlapping segment ranges.")
        segment = _segment_from_row(row)
        segments[segment_id] = segment
        segment_slots[segment_id] = slots
        slot_owner.update({slot: segment_id for slot in slots})

    assignment_rows = connection.execute(
        "SELECT * FROM focus_assignments ORDER BY slot_start, slot_end, assignment_id"
    ).fetchall()
    assignments = []
    assignment_owner = {}
    for row in assignment_rows:
        slots, invalid_count = _strict_assignment_slots(row)
        status = row["status"]
        lifecycle_status = row["lifecycle_status"]
        segment_id = row["segment_id"] or ""
        if invalid_count or status not in VALID_ASSIGNMENT_STATUSES:
            raise RuntimeError("Retirement snapshot contains an invalid assignment.")
        if lifecycle_status not in VALID_ASSIGNMENT_LIFECYCLE_STATUSES:
            raise RuntimeError("Retirement snapshot contains an invalid assignment lifecycle.")
        if any(slot not in slots_by_id or slots_by_id.get(slot) == "system" for slot in slots):
            raise RuntimeError("Retirement snapshot contains an invalid assignment slot.")
        if slots & set(assignment_owner):
            raise RuntimeError("Retirement snapshot contains overlapping assignments.")
        assignment_owner.update({slot: row["assignment_id"] for slot in slots})
        if status == "assigned":
            if segment_id not in segments:
                raise RuntimeError("Retirement snapshot contains a dangling assigned row.")
            if lifecycle_status == "live" and not slots.issubset(segment_slots[segment_id]):
                raise RuntimeError("Retirement snapshot contains uncovered live assignment slots.")
        elif segment_id:
            raise RuntimeError("Retirement snapshot contains a mapped unassigned row.")
        if lifecycle_status == "live" and status == "unassigned" and slots & set(slot_owner):
            raise RuntimeError("Retirement snapshot contains covered live unassigned slots.")
        if lifecycle_status == "retired":
            if slots & set(slot_owner):
                raise RuntimeError("Retirement snapshot contains retired slots in live ranges.")
            if not slots.issubset(formation_slots):
                raise RuntimeError("Retirement snapshot contains uncaptured retired slots.")
        assignments.append(_assignment_from_row(row))
    return segments, assignments, segment_slots, slot_owner

def run_focus_range_retirement(messages=None, runtime_state=None, pre_audit_report=None):
    if not event_stream.event_stream_db_path().exists():
        return _empty_focus_range_retirement_result("no_event_stream")


    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)

    pre_audit_report = pre_audit_report or run_focus_consistency_audit(
        messages,
        runtime_state,
        persist=False,
    )
    if pre_audit_report.get("error"):
        return _persist_focus_range_retirement_result(
            _empty_focus_range_retirement_result(
                "consistency_audit_unavailable",
                pre_audit_report.get("error"),
            )
        )
    blocking_codes = sorted(
        {
            str(issue.get("code") or "")
            for issue in (pre_audit_report.get("issues") or [])
            if issue.get("severity") == "error"
            and str(issue.get("code") or "") in RETIREMENT_BLOCKING_ISSUE_CODES
        }
    )
    if blocking_codes:
        result = _empty_focus_range_retirement_result("consistency_blocked")
        result["blocking_issue_codes"] = blocking_codes
        return _persist_focus_range_retirement_result(result)

    try:
        lifecycle_projection = _focus_lifecycle_projection(messages, runtime_state)
    except Exception as error:
        return _persist_focus_range_retirement_result(
            _empty_focus_range_retirement_result("residency_projection_unavailable", error)
        )
    if lifecycle_projection is None:
        return _persist_focus_range_retirement_result(
            _empty_focus_range_retirement_result("residency_projection_unavailable")
        )
    wm_slots = set(lifecycle_projection["wm_slots"])

    connection = event_stream.ensure_event_stream()
    try:
        _ensure_tables(connection)
        connection.execute("BEGIN IMMEDIATE")
        slot_rows = connection.execute("SELECT slot_seq, source FROM slots").fetchall()
        slots_by_id = {int(row["slot_seq"]): row["source"] for row in slot_rows}
        formation_slots, _work_item_count = _formation_work_item_slots(connection)
        segments, assignments, segment_slots, slot_owner = _validated_retirement_snapshot(
            connection,
            slots_by_id,
            formation_slots,
        )
        live_assignments = [row for row in assignments if row["lifecycle_status"] == "live"]

        retired_assignments = []
        deferred_partial_assignment_count = 0
        remove_by_segment = {segment_id: set() for segment_id in segments}
        for assignment in live_assignments:
            assignment_slots = _assignment_slots(assignment)
            segment_id = assignment.get("segment_id") or ""
            target = segments.get(segment_id)
            if assignment["status"] != "assigned" or not target or target.get("status") != "resolved":
                continue
            per_slot_eligible = {
                slot
                for slot in assignment_slots
                if slot in wm_slots and slot in formation_slots
            }
            if per_slot_eligible and per_slot_eligible != assignment_slots:
                deferred_partial_assignment_count += 1
                continue
            if not assignment_slots or per_slot_eligible != assignment_slots:
                continue
            if any(slot not in slots_by_id or slots_by_id.get(slot) == "system" for slot in assignment_slots):
                raise RuntimeError("Retirement encountered an invalid assignment slot after pre-audit.")
            if not assignment_slots.issubset(segment_slots[segment_id]):
                raise RuntimeError("Retirement encountered inconsistent assigned ownership after pre-audit.")
            remove_by_segment[segment_id].update(assignment_slots)
            retired_assignments.append(assignment)

        live_assigned_slots = {
            slot
            for assignment in live_assignments
            if assignment["status"] == "assigned"
            for slot in _assignment_slots(assignment)
        }
        legacy_slots = {
            slot
            for segment_id, slots in segment_slots.items()
            if segments[segment_id].get("status") == "resolved"
            for slot in slots
            if slot not in live_assigned_slots
            and slot in wm_slots
            and slot in formation_slots
        }
        for slot in legacy_slots:
            remove_by_segment[slot_owner[slot]].add(slot)

        next_segments = {
            segment_id: {
                **segment,
                "slot_ranges": _remove_slots_from_ranges(
                    segment.get("slot_ranges", []),
                    remove_by_segment[segment_id],
                ),
            }
            for segment_id, segment in segments.items()
        }
        retired_assignment_ids = {row["assignment_id"] for row in retired_assignments}
        for assignment in live_assignments:
            if assignment["assignment_id"] in retired_assignment_ids:
                continue
            slots = _assignment_slots(assignment)
            if assignment["status"] == "assigned":
                target_slots = _slots_from_ranges(
                    next_segments[assignment["segment_id"]].get("slot_ranges", [])
                )
                if not slots.issubset(target_slots):
                    raise RuntimeError("Retirement would split a live assignment unit.")
            elif slots & {
                slot
                for segment in next_segments.values()
                for slot in _slots_from_ranges(segment.get("slot_ranges", []))
            }:
                raise RuntimeError("Retirement would cover a live unassigned unit.")
        _assert_no_overlapping_segments(next_segments)

        removed_slots = set().union(*remove_by_segment.values()) if remove_by_segment else set()
        retired_slots = removed_slots | {
            slot for assignment in retired_assignments for slot in _assignment_slots(assignment)
        }
        if removed_slots:
            timestamp = _now()
            for segment_id, slots in remove_by_segment.items():
                if not slots:
                    continue
                connection.execute(
                    "UPDATE focus_segments SET slot_ranges = ?, updated_at = ? WHERE segment_id = ?",
                    (
                        json.dumps(next_segments[segment_id]["slot_ranges"], ensure_ascii=False),
                        timestamp,
                        segment_id,
                    ),
                )
        else:
            timestamp = _now()
        for assignment in retired_assignments:
            connection.execute(
                """
                UPDATE focus_assignments
                SET lifecycle_status = 'retired', retired_at = ?, updated_at = ?
                WHERE assignment_id = ? AND lifecycle_status = 'live'
                """,
                (timestamp, timestamp, assignment["assignment_id"]),
            )

        updated_segment_ids = sorted(segment_id for segment_id, slots in remove_by_segment.items() if slots)
        result = {
            "triggered": True,
            "updated": bool(retired_assignments or removed_slots),
            "reason": "retired_focus_ranges" if retired_assignments or removed_slots else "no_eligible_focus_ranges",
            "retired_assignment_count": len(retired_assignments),
            "retired_assigned_count": sum(1 for row in retired_assignments if row["status"] == "assigned"),
            "retired_unassigned_count": sum(1 for row in retired_assignments if row["status"] == "unassigned"),
            "retired_slot_count": len(retired_slots),
            "retired_range_count": len(_ranges_from_slots(retired_slots)),
            "updated_segment_count": len(updated_segment_ids),
            "deferred_partial_assignment_count": deferred_partial_assignment_count,
            "segment_samples": updated_segment_ids[:FOCUS_CONSISTENCY_SAMPLE_LIMIT],
            "blocking_issue_codes": [],
            "error": "",
        }
        _write_focus_range_retirement_result(connection, result)
        connection.commit()
        return result
    except Exception as error:
        connection.rollback()
        return _persist_focus_range_retirement_result(
            _empty_focus_range_retirement_result("focus_range_retirement_failed", error)
        )
    finally:
        connection.close()
