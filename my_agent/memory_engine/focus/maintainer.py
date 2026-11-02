import json
from uuid import uuid4

import memory_engine.event_stream as event_stream
from ..dynamic_context_budget import compute_and_store_dynamic_context_budget
from model.provider import call_chat_completion
from shared.prompt_loader import load_prompt

from .common import (
    FULL_MAINTAINER_ANOMALY_THRESHOLD,
    FULL_MAINTAINER_CONSISTENCY_COOLDOWN_TURNS,
    FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_ACTION,
    FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_RUN,
    FULL_MAINTAINER_MAX_TOOL_ROUNDS,
    FULL_MAINTAINER_TIMEOUT_SECONDS,
    FULL_MAINTAINER_UNASSIGNED_CONFIDENCE_THRESHOLD,
    FULL_MAINTAINER_UNASSIGNED_COOLDOWN_TURNS,
    FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT,
    VALID_FULL_ACTIONS,
    _extract_json_object,
    _now,
    _single_line,
    _valid_new_title,
)
from .context import (
    _protected_active_segment_id,
    _record_previews_for_slots,
    _records_by_slot,
    _stm_evidence,
    _user_turn_count,
)
from .lifecycle import _formation_work_item_segment_ids
from .ranges import (
    _assert_move_preserves_assignment_units,
    _assert_no_overlapping_segments,
    _assert_protected_active_segment,
    _assignment_slots,
    _normalize_ranges,
    _ranges_from_slots,
    _remove_slots_from_ranges,
    _slots_from_ranges,
    _validate_slot_range,
)
from .store import (
    _assignment_health,
    _ensure_tables,
    _load_assignments,
    _load_focus_ingest_hints,
    _load_json_state,
    _load_segments,
    _pending_focus_ingest_hint_starts,
    _segment_by_id,
    _state_get,
    _state_set,
)


def _full_segment_capacity(messages=None, runtime_state=None):
    pressure = compute_and_store_dynamic_context_budget(
        messages or [],
        runtime_state,
        emit_observer=False,
        stabilize=False,
    )
    wm_budget = int((pressure.get("budgets") or {}).get("wm") or 10000)
    return max(1, int(wm_budget * 10 / 5000))


def _consistency_maintenance_trigger(connection, user_turn_count):
    report = _load_json_state(connection, "last_consistency_report", {}) or {}
    fingerprint = str(report.get("repair_fingerprint") or "")
    if not report.get("should_trigger_full_maintainer") or not fingerprint:
        return {"scheduled": False, "fingerprint": ""}
    last_fingerprint = str(_state_get(connection, "last_full_consistency_attempt_fingerprint", "") or "")
    last_turn = int(_state_get(connection, "last_full_consistency_attempt_turn", "-1") or -1)
    if (
        fingerprint == last_fingerprint
        and last_turn >= 0
        and user_turn_count - last_turn < FULL_MAINTAINER_CONSISTENCY_COOLDOWN_TURNS
    ):
        return {"scheduled": False, "fingerprint": fingerprint, "cooldown": True}
    return {"scheduled": True, "fingerprint": fingerprint, "cooldown": False}


def _orphan_empty_inactive_segment_ids(connection, segments=None):
    segments = segments if segments is not None else _load_segments(connection)
    assignment_segment_ids = {
        assignment.get("segment_id")
        for assignment in _load_assignments(connection)
        if assignment.get("segment_id")
    }
    hint_segment_ids = {hint["segment_id"] for hint in _load_focus_ingest_hints(connection)}
    formation_segment_ids = _formation_work_item_segment_ids(connection)
    return {
        segment["segment_id"]
        for segment in segments
        if segment.get("status") == "inactive"
        and not segment.get("slot_ranges")
        and segment["segment_id"] not in assignment_segment_ids
        and segment["segment_id"] not in hint_segment_ids
        and segment["segment_id"] not in formation_segment_ids
    }


def _full_maintenance_plan(connection, messages=None, runtime_state=None, force=False):
    messages = messages or []
    segments = _load_segments(connection)
    health = _assignment_health(connection, segments)
    user_turn_count = _user_turn_count(messages)
    max_segments = _full_segment_capacity(messages, runtime_state)
    empty_first_seen = _load_json_state(connection, "empty_segment_first_seen_turns", {}) or {}
    next_empty_first_seen = {}
    empty_too_old = False
    for segment_id in _orphan_empty_inactive_segment_ids(connection, segments):
        first_seen = int(empty_first_seen.get(segment_id, user_turn_count))
        next_empty_first_seen[segment_id] = first_seen
        if user_turn_count - first_seen >= FULL_MAINTAINER_ANOMALY_THRESHOLD:
            empty_too_old = True
    _state_set(connection, "empty_segment_first_seen_turns", json.dumps(next_empty_first_seen, ensure_ascii=False, sort_keys=True))

    if force:
        return {
            "scheduled": True,
            "reason": "manual",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
        }
    if not segments:
        return {"scheduled": False, "reason": "no_segments", "user_turn_count": user_turn_count}

    unassigned_cooldown = False
    if health["unassigned_block_count"] >= FULL_MAINTAINER_ANOMALY_THRESHOLD:
        last_no_progress_turn = int(
            _state_get(connection, "last_full_maintainer_unassigned_no_progress_turn", "-1") or -1
        )
        last_no_progress_count = int(
            _state_get(connection, "last_full_maintainer_unassigned_no_progress_count", "-1") or -1
        )
        unassigned_cooldown = (
            last_no_progress_turn >= 0
            and user_turn_count - last_no_progress_turn < FULL_MAINTAINER_UNASSIGNED_COOLDOWN_TURNS
            and health["unassigned_block_count"] <= last_no_progress_count
        )
        if not unassigned_cooldown:
            return {
                "scheduled": True,
                "reason": "unassigned_threshold",
                "user_turn_count": user_turn_count,
                "max_segments": max_segments,
            }
    if health["low_confidence_assignment_count"] >= FULL_MAINTAINER_ANOMALY_THRESHOLD:
        return {
            "scheduled": True,
            "reason": "low_confidence_threshold",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
        }
    if empty_too_old:
        return {
            "scheduled": True,
            "reason": "empty_segment_age_threshold",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
        }

    consistency = _consistency_maintenance_trigger(connection, user_turn_count)
    if consistency.get("scheduled"):
        return {
            "scheduled": True,
            "reason": "consistency_report",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
            "consistency_repair_fingerprint": consistency["fingerprint"],
        }
    if unassigned_cooldown:
        return {
            "scheduled": False,
            "reason": "unassigned_no_progress_cooldown_pending",
            "user_turn_count": user_turn_count,
        }

    segment_count = len(segments)
    if segment_count < max_segments / 4:
        return {
            "scheduled": False,
            "reason": (
                "unassigned_no_progress_cooldown_pending"
                if unassigned_cooldown
                else "consistency_report_cooldown_pending"
                if consistency.get("cooldown")
                else "below_capacity_threshold"
            ),
            "user_turn_count": user_turn_count,
        }

    last_turn = int(_state_get(connection, "last_full_maintainer_capacity_turn_count", "0") or 0)
    if segment_count > max_segments:
        return {
            "scheduled": True,
            "reason": "over_capacity",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
        }
    if segment_count < max_segments / 2:
        if user_turn_count - last_turn >= 20:
            return {
                "scheduled": True,
                "reason": "capacity_quarter_cooldown",
                "user_turn_count": user_turn_count,
                "max_segments": max_segments,
            }
        return {
            "scheduled": False,
            "reason": (
                "unassigned_no_progress_cooldown_pending"
                if unassigned_cooldown
                else "consistency_report_cooldown_pending"
                if consistency.get("cooldown")
                else "capacity_quarter_cooldown_pending"
            ),
            "user_turn_count": user_turn_count,
        }
    if user_turn_count - last_turn >= 10:
        return {
            "scheduled": True,
            "reason": "capacity_half_cooldown",
            "user_turn_count": user_turn_count,
            "max_segments": max_segments,
        }
    return {
        "scheduled": False,
        "reason": (
            "unassigned_no_progress_cooldown_pending"
            if unassigned_cooldown
            else "consistency_report_cooldown_pending"
            if consistency.get("cooldown")
            else "capacity_half_cooldown_pending"
        ),
        "user_turn_count": user_turn_count,
    }


def _segment_health(connection, segment):
    health = _assignment_health(connection, [segment])
    per_segment = health["per_segment"].get(segment["segment_id"], {})
    pending_hint_starts = _pending_focus_ingest_hint_starts(connection)
    pending_hint_count = sum(
        1
        for hint in _load_focus_ingest_hints(connection)
        if hint["segment_id"] == segment["segment_id"] and hint["start_slot"] in pending_hint_starts
    )
    return {
        "is_empty": not bool(segment.get("slot_ranges")),
        "assignment_count": per_segment.get("assignment_count", 0),
        "low_confidence_assignment_count": per_segment.get("low_confidence_assignment_count", 0),
        "pending_ingest_hint_count": pending_hint_count,
    }


def _full_maintainer_initial_payload(connection):
    segments = _load_segments(connection)
    health = _assignment_health(connection, segments)
    return {
        "global_health": {
            "empty_segment_count": len(_orphan_empty_inactive_segment_ids(connection, segments)),
            "unassigned_block_count": health["unassigned_block_count"],
            "low_confidence_assignment_count": health["low_confidence_assignment_count"],
        },
        "unassigned_repair_limits": {
            "inspect_page_size": FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT,
            "max_assignments_per_action": FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_ACTION,
            "max_assignments_per_run": FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_RUN,
            "minimum_confidence": FULL_MAINTAINER_UNASSIGNED_CONFIDENCE_THRESHOLD,
        },
        "consistency_report": _load_json_state(connection, "last_consistency_report", {}),
        "last_focus_range_retirement_result": _load_json_state(
            connection, "last_focus_range_retirement_result", {}
        ),
        "segments": [
            {
                "segment_id": segment["segment_id"],
                "title": segment["title"],
                "summary": segment["summary"],
                "status": segment["status"],
                "ranges": segment["slot_ranges"],
                **_segment_health(connection, segment),
            }
            for segment in segments
        ],
        "tools": [
            {"name": "inspect_segment", "args": {"segment_id": "seg_x"}},
            {"name": "inspect_ranges", "args": {"segment_id": "seg_x"}},
            {"name": "inspect_assignments", "args": {"segment_id": "optional", "filter": "all | low_confidence | unassigned"}},
            {"name": "inspect_unassigned_blocks", "args": {"cursor": "optional", "limit": 8}},
            {"name": "get_stm_evidence", "args": {}},
        ],
        "output_schema": {
            "tool": "optional read-only tool name",
            "args": "tool arguments",
            "repair_plan": "final list of repair actions",
        },
    }












def _inspect_assignments(connection, segment_id=None, filter_name="all"):
    status = "unassigned" if filter_name == "unassigned" else None
    low_confidence = filter_name == "low_confidence"
    rows = _load_assignments(
        connection,
        segment_id=segment_id,
        status=status,
        lifecycle_status="live",
        low_confidence=low_confidence,
    )
    return {
        "assignments": [
            {
                **row,
                "records": _record_previews_for_slots(row.get("slot_seqs", []), limit=600),
            }
            for row in rows
        ]
    }


def _assignment_cursor(assignment):
    return f"{int(assignment['slot_start'])}:{int(assignment['slot_end'])}:{assignment['assignment_id']}"


def _assignment_cursor_key(cursor):
    if not cursor:
        return None
    try:
        slot_start, slot_end, assignment_id = str(cursor).split(":", 2)
        return int(slot_start), int(slot_end), assignment_id
    except (TypeError, ValueError):
        return None


def _inspect_unassigned_blocks(connection, cursor=None, limit=FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT
    limit = max(1, min(FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT, limit))
    cursor_key = _assignment_cursor_key(cursor)
    rows = _load_assignments(connection, status="unassigned", lifecycle_status="live")
    if cursor_key is not None:
        rows = [
            row
            for row in rows
            if (int(row["slot_start"]), int(row["slot_end"]), row["assignment_id"]) > cursor_key
        ]
    selected = rows[:limit]
    hints_by_start = {hint["start_slot"]: hint for hint in _load_focus_ingest_hints(connection)}
    assignments = []
    for row in selected:
        item = {
            **row,
            "records": _record_previews_for_slots(row.get("slot_seqs", []), limit=600),
        }
        hint = hints_by_start.get(int(row["slot_start"]))
        if hint:
            item["detector_hint"] = {
                "segment_id": hint["segment_id"],
                "new_focus_start_slot": hint["start_slot"],
            }
        assignments.append(item)
    has_more = len(rows) > len(selected)
    return {
        "assignments": assignments,
        "has_more": has_more,
        "next_cursor": _assignment_cursor(selected[-1]) if has_more and selected else None,
        "limit": limit,
    }


def _inspect_segment(connection, segment_id):
    segment = _segment_by_id(connection, segment_id)
    if not segment:
        return {"error": f"segment not found: {segment_id}"}
    slots = sorted(_slots_from_ranges(segment.get("slot_ranges", [])))
    return {
        "segment": {
            **segment,
            **_segment_health(connection, segment),
        },
        "records": _record_previews_for_slots(slots, limit=600),
        "assignments": _inspect_assignments(connection, segment_id=segment_id).get("assignments", []),
    }


def _inspect_ranges(connection, segment_id):
    segment = _segment_by_id(connection, segment_id)
    if not segment:
        return {"error": f"segment not found: {segment_id}"}
    ranges = []
    for item in segment.get("slot_ranges", []):
        slots = list(range(int(item["start"]), int(item["end"]) + 1))
        ranges.append(
            {
                "range": dict(item),
                "records": _record_previews_for_slots(slots, limit=600),
            }
        )
    return {"segment_id": segment_id, "ranges": ranges}


def _run_full_maintainer_tool(connection, tool, args, messages=None, runtime_state=None):
    args = args if isinstance(args, dict) else {}
    if tool == "inspect_segment":
        return _inspect_segment(connection, str(args.get("segment_id") or ""))
    if tool == "inspect_ranges":
        return _inspect_ranges(connection, str(args.get("segment_id") or ""))
    if tool == "inspect_assignments":
        segment_id = str(args.get("segment_id") or "").strip() or None
        filter_name = str(args.get("filter") or "all").strip()
        return _inspect_assignments(connection, segment_id=segment_id, filter_name=filter_name)
    if tool == "inspect_unassigned_blocks":
        cursor = args.get("cursor")
        if cursor is None or cursor == "":
            cursor = _state_get(connection, "full_unassigned_scan_cursor", "") or None
        return _inspect_unassigned_blocks(connection, cursor=cursor, limit=args.get("limit"))
    if tool == "get_stm_evidence":
        return _stm_evidence(messages, runtime_state)
    return {"error": f"unknown tool: {tool}"}


def _full_maintainer_prompt(connection):
    return [
        {"role": "system", "content": load_prompt("full_segment_maintainer")},
        {"role": "user", "content": json.dumps(_full_maintainer_initial_payload(connection), ensure_ascii=False)},
    ]


def _call_full_maintainer_json(prompt):
    data = call_chat_completion(
        prompt,
        temperature=0,
        timeout_seconds=FULL_MAINTAINER_TIMEOUT_SECONDS,
        purpose="focus maintainer",
    )
    return data["choices"][0]["message"].get("content", "")


def _normalize_repair_plan(raw):
    raw = raw or {}
    actions = raw.get("repair_plan", raw.get("actions", []))
    if not isinstance(actions, list):
        actions = []
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = str(action.get("action") or "").strip()
        if name not in VALID_FULL_ACTIONS:
            continue
        normalized.append({**action, "action": name, "reason": _single_line(action.get("reason", ""))})
    if not normalized:
        normalized = [{"action": "leave_unchanged", "reason": "No valid repair actions were proposed."}]
    return normalized


def _run_full_maintainer_agent(connection, messages=None, runtime_state=None):
    prompt = _full_maintainer_prompt(connection)
    tool_round_count = 0
    estimated_context_tokens = 0
    stm_evidence_requested = False
    inspected_segment_ids = set()
    inspected_assignment_ids = set()
    unassigned_scan_cursor = None
    for _round in range(FULL_MAINTAINER_MAX_TOOL_ROUNDS + 1):
        estimated_context_tokens = sum(max(1, len(str(message.get("content", ""))) // 4) for message in prompt)
        raw_text = _call_full_maintainer_json(prompt)
        parsed = _extract_json_object(raw_text)
        if isinstance(parsed.get("repair_plan", parsed.get("actions")), list):
            return {
                "repair_plan": _normalize_repair_plan(parsed),
                "tool_round_count": tool_round_count,
                "estimated_context_tokens": estimated_context_tokens,
                "stm_evidence_requested": stm_evidence_requested,
                "inspected_segment_ids": sorted(inspected_segment_ids),
                "inspected_assignment_ids": sorted(inspected_assignment_ids),
                "unassigned_scan_cursor": unassigned_scan_cursor,
                "reason": "repair_plan",
            }
        tool = str(parsed.get("tool") or "").strip()
        if not tool:
            return {
                "repair_plan": [{"action": "leave_unchanged", "reason": "Maintainer returned no tool or repair plan."}],
                "tool_round_count": tool_round_count,
                "estimated_context_tokens": estimated_context_tokens,
                "stm_evidence_requested": stm_evidence_requested,
                "inspected_segment_ids": sorted(inspected_segment_ids),
                "inspected_assignment_ids": sorted(inspected_assignment_ids),
                "unassigned_scan_cursor": unassigned_scan_cursor,
                "reason": "no_tool_or_plan",
            }
        if tool_round_count >= FULL_MAINTAINER_MAX_TOOL_ROUNDS:
            return {
                "repair_plan": [{"action": "leave_unchanged", "reason": "Max tool rounds reached."}],
                "tool_round_count": tool_round_count,
                "estimated_context_tokens": estimated_context_tokens,
                "stm_evidence_requested": stm_evidence_requested,
                "inspected_segment_ids": sorted(inspected_segment_ids),
                "inspected_assignment_ids": sorted(inspected_assignment_ids),
                "unassigned_scan_cursor": unassigned_scan_cursor,
                "reason": "max_tool_rounds_reached",
            }
        tool_round_count += 1
        args = parsed.get("args") or {}
        result = _run_full_maintainer_tool(connection, tool, args, messages, runtime_state)
        if tool == "get_stm_evidence":
            stm_evidence_requested = stm_evidence_requested or bool(result.get("available"))
        elif tool == "inspect_segment" and not result.get("error"):
            segment_id = str(args.get("segment_id") or "").strip()
            if segment_id:
                inspected_segment_ids.add(segment_id)
        elif tool == "inspect_unassigned_blocks" and not result.get("error"):
            inspected_assignment_ids.update(
                str(assignment.get("assignment_id"))
                for assignment in result.get("assignments", [])
                if assignment.get("assignment_id")
            )
            unassigned_scan_cursor = result.get("next_cursor") or ""
        prompt.append({"role": "assistant", "content": json.dumps({"tool": tool, "args": args}, ensure_ascii=False)})
        prompt.append({"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False)})
    return {
        "repair_plan": [{"action": "leave_unchanged", "reason": "Max tool rounds reached."}],
        "tool_round_count": tool_round_count,
        "estimated_context_tokens": estimated_context_tokens,
        "stm_evidence_requested": stm_evidence_requested,
        "inspected_segment_ids": sorted(inspected_segment_ids),
        "inspected_assignment_ids": sorted(inspected_assignment_ids),
        "unassigned_scan_cursor": unassigned_scan_cursor,
        "reason": "max_tool_rounds_reached",
    }












def _repair_confidence(action):
    try:
        confidence = float(action.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < FULL_MAINTAINER_UNASSIGNED_CONFIDENCE_THRESHOLD or confidence > 1.0:
        raise RuntimeError(
            "unassigned repair requires confidence between "
            f"{FULL_MAINTAINER_UNASSIGNED_CONFIDENCE_THRESHOLD} and 1.0."
        )
    return confidence


def _repair_assignment_ids(action):
    raw_ids = action.get("assignment_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise RuntimeError("unassigned repair requires a non-empty assignment_ids list.")
    assignment_ids = [str(assignment_id or "").strip() for assignment_id in raw_ids]
    if any(not assignment_id for assignment_id in assignment_ids):
        raise RuntimeError("unassigned repair assignment_ids must be non-empty strings.")
    if len(set(assignment_ids)) != len(assignment_ids):
        raise RuntimeError("unassigned repair assignment_ids must not contain duplicates.")
    if len(assignment_ids) > FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_ACTION:
        raise RuntimeError(
            "unassigned repair exceeds the per-action assignment limit of "
            f"{FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_ACTION}."
        )
    return assignment_ids


def _unassigned_repair_batch(
    action,
    assignments_by_id,
    assignment_statuses,
    assignment_lifecycle_statuses,
    inspected_assignment_ids,
    repaired_assignment_ids,
    records,
):
    assignment_ids = _repair_assignment_ids(action)
    if len(repaired_assignment_ids) + len(assignment_ids) > FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_RUN:
        raise RuntimeError(
            "unassigned repair exceeds the per-run assignment limit of "
            f"{FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_RUN}."
        )
    selected = []
    slots = set()
    for assignment_id in assignment_ids:
        assignment = assignments_by_id.get(assignment_id)
        if not assignment:
            raise RuntimeError(f"unassigned repair assignment does not exist: {assignment_id}.")
        if assignment_id not in inspected_assignment_ids:
            raise RuntimeError(
                "unassigned repair requires inspect_unassigned_blocks for every assignment in the same maintainer run."
            )
        if assignment_statuses.get(assignment_id) != "unassigned":
            raise RuntimeError(f"unassigned repair assignment is no longer unassigned: {assignment_id}.")
        if assignment_lifecycle_statuses.get(assignment_id) != "live":
            raise RuntimeError(f"unassigned repair assignment is no longer live: {assignment_id}.")
        assignment_slots = _assignment_slots(assignment)
        for slot in assignment_slots:
            record = records.get(slot)
            if not record:
                raise RuntimeError(f"unassigned repair slot does not exist: {slot}.")
            if record.get("source") == "system":
                raise RuntimeError(f"unassigned repair cannot assign system slot: {slot}.")
        selected.append(assignment)
        slots.update(assignment_slots)
    return assignment_ids, selected, slots


def _validate_and_simulate_repair_plan(
    connection,
    actions,
    allow_mark_resolved=False,
    mark_resolved_segment_ids=None,
    protected_active_segment_id="",
    inspected_assignment_ids=None,
    inspected_segment_ids=None,
    max_segments=None,
):
    records = _records_by_slot()
    segments = {segment["segment_id"]: dict(segment) for segment in _load_segments(connection)}
    assignments = _load_assignments(connection)
    assignments_by_id = {assignment["assignment_id"]: assignment for assignment in assignments}
    assignment_segment_ids = {assignment["assignment_id"]: assignment["segment_id"] for assignment in assignments}
    assignment_statuses = {assignment["assignment_id"]: assignment["status"] for assignment in assignments}
    assignment_lifecycle_statuses = {
        assignment["assignment_id"]: assignment["lifecycle_status"] for assignment in assignments
    }
    hints = _load_focus_ingest_hints(connection)
    hint_segment_ids = {hint["start_slot"]: hint["segment_id"] for hint in hints}
    pending_hint_starts = _pending_focus_ingest_hint_starts(connection)
    formation_segment_ids = _formation_work_item_segment_ids(connection)
    mark_resolved_segment_ids = {str(segment_id) for segment_id in mark_resolved_segment_ids or []}
    inspected_assignment_ids = {str(assignment_id) for assignment_id in inspected_assignment_ids or []}
    inspected_segment_ids = {str(segment_id) for segment_id in inspected_segment_ids or []}
    assignment_updates = []
    hint_updates = []
    deleted_segments = set()
    created_segment_ids = set()
    repaired_assignment_ids = set()
    changed_segment_ids = set()
    applied_actions = []
    rejected_actions = []

    for action in actions:
        name = action.get("action")
        applied_action = dict(action)
        segments_before = {segment_id: {**segment, "slot_ranges": [dict(item) for item in segment.get("slot_ranges", [])]} for segment_id, segment in segments.items()}
        assignment_segment_ids_before = dict(assignment_segment_ids)
        assignment_statuses_before = dict(assignment_statuses)
        assignment_updates_before = list(assignment_updates)
        hint_segment_ids_before = dict(hint_segment_ids)
        hint_updates_before = list(hint_updates)
        deleted_segments_before = set(deleted_segments)
        created_segment_ids_before = set(created_segment_ids)
        repaired_assignment_ids_before = set(repaired_assignment_ids)
        changed_segment_ids_before = set(changed_segment_ids)
        try:
            if name == "leave_unchanged":
                continue
            if name == "rename_segment":
                segment = segments[str(action.get("segment_id") or "")]
                title = _single_line(action.get("title"))
                if not title:
                    raise RuntimeError("rename_segment requires a non-empty title.")
                segment["title"] = title
                changed_segment_ids.add(segment["segment_id"])
            elif name == "update_summary":
                segment = segments[str(action.get("segment_id") or "")]
                summary = _single_line(action.get("summary"))
                if not summary:
                    raise RuntimeError("update_summary requires a non-empty summary.")
                segment["summary"] = summary
                changed_segment_ids.add(segment["segment_id"])
            elif name in {"assign_unassigned", "create_segment_from_unassigned"}:
                confidence = _repair_confidence(action)
                reason = _single_line(action.get("reason"))
                if not reason:
                    raise RuntimeError("unassigned repair requires a non-empty reason.")
                assignment_ids, selected_assignments, repair_slots = _unassigned_repair_batch(
                    action,
                    assignments_by_id,
                    assignment_statuses,
                    assignment_lifecycle_statuses,
                    inspected_assignment_ids,
                    repaired_assignment_ids,
                    records,
                )
                overlapping_segment_ids = [
                    segment_id
                    for segment_id, segment in segments.items()
                    if repair_slots & _slots_from_ranges(segment.get("slot_ranges", []))
                ]
                if overlapping_segment_ids:
                    raise RuntimeError(
                        "unassigned repair slots are already covered by segment(s): "
                        + ", ".join(sorted(overlapping_segment_ids))
                    )

                if name == "assign_unassigned":
                    target_id = str(action.get("target_segment_id") or "")
                    target = segments[target_id]
                    if target.get("status") not in {"active", "inactive"}:
                        raise RuntimeError("assign_unassigned target must be active or inactive.")
                    if target_id not in inspected_segment_ids:
                        raise RuntimeError(
                            "assign_unassigned requires inspect_segment for the target in the same maintainer run."
                        )
                    target["slot_ranges"] = _ranges_from_slots(
                        _slots_from_ranges(target.get("slot_ranges", [])) | repair_slots
                    )
                    changed_segment_ids.add(target_id)
                else:
                    title = _single_line(action.get("title"))
                    summary = _single_line(action.get("summary"))
                    if not _valid_new_title(title):
                        raise RuntimeError("create_segment_from_unassigned requires a valid specific title.")
                    if not summary:
                        raise RuntimeError("create_segment_from_unassigned requires a non-empty summary.")
                    if any(_single_line(segment.get("title")).casefold() == title.casefold() for segment in segments.values()):
                        raise RuntimeError("create_segment_from_unassigned title duplicates an existing segment title.")
                    if max_segments is not None and len(segments) >= int(max_segments):
                        raise RuntimeError("create_segment_from_unassigned would exceed the Focus Segment capacity.")
                    target_id = f"seg_{uuid4().hex}"
                    segments[target_id] = {
                        "segment_id": target_id,
                        "title": title,
                        "summary": summary,
                        "status": "inactive",
                        "slot_ranges": _ranges_from_slots(repair_slots),
                        "created_at": "",
                        "updated_at": "",
                        "last_touched_at": "",
                    }
                    created_segment_ids.add(target_id)
                    applied_action["created_segment_id"] = target_id

                assignment_updates.append(
                    {
                        "kind": "repair_unassigned",
                        "assignments": [dict(assignment) for assignment in selected_assignments],
                        "assignment_ids": assignment_ids,
                        "target": target_id,
                        "confidence": confidence,
                        "reason": reason,
                    }
                )
                for assignment_id in assignment_ids:
                    assignment_statuses[assignment_id] = "assigned"
                    assignment_segment_ids[assignment_id] = target_id
                repaired_assignment_ids.update(assignment_ids)
            elif name == "mark_resolved":
                segment_id = str(action.get("segment_id") or "")
                if not allow_mark_resolved:
                    raise RuntimeError("mark_resolved requires get_stm_evidence confirmation in the same maintainer run.")
                if segment_id not in mark_resolved_segment_ids:
                    raise RuntimeError("mark_resolved requires inspect_segment for the target segment in the same maintainer run.")
                if any(
                    start_slot in pending_hint_starts and owner_id == segment_id
                    for start_slot, owner_id in hint_segment_ids.items()
                ):
                    raise RuntimeError("mark_resolved cannot resolve a segment with a pending Detector ingest hint.")
                segment = segments[segment_id]
                segment["status"] = "resolved"
                changed_segment_ids.add(segment_id)
            elif name == "delete_empty_segment":
                segment_id = str(action.get("segment_id") or "")
                segment = segments[segment_id]
                if segment.get("slot_ranges"):
                    raise RuntimeError("delete_empty_segment requires an empty segment.")
                if segment.get("status") != "inactive":
                    raise RuntimeError("delete_empty_segment only permits inactive segments.")
                if any(
                    start_slot in pending_hint_starts and owner_id == segment_id
                    for start_slot, owner_id in hint_segment_ids.items()
                ):
                    raise RuntimeError("delete_empty_segment cannot delete a segment with a pending Detector ingest hint.")
                if any(
                    assignment_segment_ids.get(assignment["assignment_id"]) == segment_id
                    for assignment in assignments
                ):
                    raise RuntimeError("delete_empty_segment cannot delete a segment with assignment audit history.")
                if segment_id in formation_segment_ids:
                    raise RuntimeError("delete_empty_segment cannot delete a segment referenced by Formation.")
                deleted_segments.add(segment_id)
                del segments[segment_id]
            elif name == "move_range":
                source_id = str(action.get("source_segment_id") or "")
                target_id = str(action.get("target_segment_id") or "")
                source = segments[source_id]
                target = segments[target_id]
                start, end = _validate_slot_range(int(action.get("start")), int(action.get("end")), records)
                move_slots = set(range(start, end + 1))
                if not move_slots.issubset(_slots_from_ranges(source.get("slot_ranges", []))):
                    raise RuntimeError("move_range must be fully contained in the source segment.")
                source_assignments = [
                    assignment
                    for assignment in assignments
                    if assignment_segment_ids.get(assignment["assignment_id"]) == source_id
                    and assignment_statuses.get(assignment["assignment_id"]) == "assigned"
                    and assignment_lifecycle_statuses.get(assignment["assignment_id"]) == "live"
                ]
                _assert_move_preserves_assignment_units(source_assignments, move_slots)
                source["slot_ranges"] = _remove_slots_from_ranges(source.get("slot_ranges", []), move_slots)
                target["slot_ranges"] = _ranges_from_slots(_slots_from_ranges(target.get("slot_ranges", [])) | move_slots)
                moved_assignment_ids = []
                for assignment in source_assignments:
                    assignment_slots = {int(slot) for slot in assignment.get("slot_seqs", [])}
                    if not assignment_slots:
                        assignment_slots = set(range(int(assignment["slot_start"]), int(assignment["slot_end"]) + 1))
                    if assignment_slots.issubset(move_slots):
                        assignment_segment_ids[assignment["assignment_id"]] = target_id
                        moved_assignment_ids.append(assignment["assignment_id"])
                if moved_assignment_ids:
                    assignment_updates.append({"kind": "assignment_ids", "assignment_ids": moved_assignment_ids, "target": target_id})
                changed_segment_ids.update({source_id, target_id})
            elif name == "merge_segments":
                target_id = str(action.get("target_segment_id") or "")
                target = segments[target_id]
                for source_id in [str(item) for item in action.get("source_segment_ids", [])]:
                    if source_id == target_id:
                        raise RuntimeError("merge_segments source cannot equal target.")
                    source = segments[source_id]
                    target["slot_ranges"] = _ranges_from_slots(_slots_from_ranges(target.get("slot_ranges", [])) | _slots_from_ranges(source.get("slot_ranges", [])))
                    moved_assignment_ids = [
                        assignment["assignment_id"]
                        for assignment in assignments
                        if assignment_segment_ids.get(assignment["assignment_id"]) == source_id
                        and assignment_statuses.get(assignment["assignment_id"]) == "assigned"
                    ]
                    for assignment_id in moved_assignment_ids:
                        assignment_segment_ids[assignment_id] = target_id
                    if moved_assignment_ids:
                        assignment_updates.append({"kind": "assignment_ids", "assignment_ids": moved_assignment_ids, "target": target_id})
                    moved_hint_starts = [
                        start_slot
                        for start_slot, owner_id in hint_segment_ids.items()
                        if owner_id == source_id
                    ]
                    for start_slot in moved_hint_starts:
                        hint_segment_ids[start_slot] = target_id
                    if moved_hint_starts:
                        hint_updates.append({"start_slots": moved_hint_starts, "target": target_id})
                    deleted_segments.add(source_id)
                    del segments[source_id]
                if _single_line(action.get("title")):
                    target["title"] = _single_line(action.get("title"))
                if _single_line(action.get("summary")):
                    target["summary"] = _single_line(action.get("summary"))
                changed_segment_ids.add(target_id)
            else:
                raise RuntimeError(f"Unsupported action: {name}")
            _assert_no_overlapping_segments(segments)
            _assert_protected_active_segment(segments, protected_active_segment_id)
            applied_actions.append(applied_action)
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            segments = segments_before
            assignment_segment_ids = assignment_segment_ids_before
            assignment_statuses = assignment_statuses_before
            assignment_updates = assignment_updates_before
            hint_segment_ids = hint_segment_ids_before
            hint_updates = hint_updates_before
            deleted_segments = deleted_segments_before
            created_segment_ids = created_segment_ids_before
            repaired_assignment_ids = repaired_assignment_ids_before
            changed_segment_ids = changed_segment_ids_before
            rejected_actions.append({**action, "error": str(error)})

    return {
        "segments": segments,
        "deleted_segments": deleted_segments,
        "created_segment_ids": created_segment_ids,
        "changed_segment_ids": changed_segment_ids,
        "assignment_updates": assignment_updates,
        "hint_updates": hint_updates,
        "repaired_assignment_ids": repaired_assignment_ids,
        "applied_actions": applied_actions,
        "rejected_actions": rejected_actions,
    }


def _apply_repair_simulation(connection, simulation):
    timestamp = _now()
    for segment_id in simulation.get("created_segment_ids", set()):
        segment = simulation["segments"].get(segment_id)
        if not segment:
            continue
        connection.execute(
            """
            INSERT INTO focus_segments
                (segment_id, title, summary, status, slot_ranges, created_at, updated_at, last_touched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                _single_line(segment["title"]),
                _single_line(segment["summary"]),
                "inactive",
                json.dumps(_normalize_ranges(segment["slot_ranges"]), ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    for update in simulation.get("hint_updates", []):
        placeholders = ", ".join("?" for _ in update["start_slots"])
        connection.execute(
            f"UPDATE focus_ingest_hints SET segment_id = ? WHERE start_slot IN ({placeholders})",
            (update["target"], *update["start_slots"]),
        )
    for segment_id in simulation["deleted_segments"]:
        connection.execute("DELETE FROM focus_ingest_hints WHERE segment_id = ?", (segment_id,))
        connection.execute("DELETE FROM focus_segments WHERE segment_id = ?", (segment_id,))
    for segment_id in simulation.get("changed_segment_ids", set()):
        segment = simulation["segments"].get(segment_id)
        if not segment:
            continue
        if _segment_by_id(connection, segment["segment_id"]):
            connection.execute(
                """
                UPDATE focus_segments
                SET title = ?, summary = ?, status = ?, slot_ranges = ?, updated_at = ?, last_touched_at = ?
                WHERE segment_id = ?
                """,
                (
                    _single_line(segment["title"]),
                    _single_line(segment["summary"]),
                    segment["status"],
                    json.dumps(_normalize_ranges(segment["slot_ranges"]), ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                    segment["segment_id"],
                ),
            )
    for update in simulation["assignment_updates"]:
        if update["kind"] == "assignment_ids":
            placeholders = ", ".join("?" for _ in update["assignment_ids"])
            connection.execute(
                f"UPDATE focus_assignments SET segment_id = ?, updated_at = ? WHERE assignment_id IN ({placeholders})",
                (update["target"], timestamp, *update["assignment_ids"]),
            )
        elif update["kind"] == "repair_unassigned":
            for assignment in update["assignments"]:
                connection.execute(
                    """
                    INSERT INTO focus_assignment_repairs(
                        repair_id, assignment_id, previous_status, previous_segment_id,
                        previous_confidence, previous_reason, target_segment_id,
                        confidence, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"repair_{uuid4().hex}",
                        assignment["assignment_id"],
                        assignment["status"],
                        assignment.get("segment_id") or None,
                        assignment.get("confidence"),
                        assignment.get("reason") or "",
                        update["target"],
                        update["confidence"],
                        update["reason"],
                        timestamp,
                    ),
                )
            placeholders = ", ".join("?" for _ in update["assignment_ids"])
            connection.execute(
                f"""
                UPDATE focus_assignments
                SET segment_id = ?, status = 'assigned', confidence = ?, reason = ?, updated_at = ?
                WHERE assignment_id IN ({placeholders})
                """,
                (
                    update["target"],
                    update["confidence"],
                    update["reason"],
                    timestamp,
                    *update["assignment_ids"],
                ),
            )
    connection.commit()


def _write_full_maintainer_result(connection, result):
    _state_set(connection, "last_full_maintainer_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
    connection.commit()


def run_full_segment_maintenance(messages=None, runtime_state=None, force=False):
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        plan = _full_maintenance_plan(connection, messages or [], runtime_state, force=force)
        if not plan.get("scheduled"):
            remaining_unassigned_count = _assignment_health(connection)["unassigned_block_count"]
            result = {
                "triggered": False,
                "updated": False,
                "reason": plan.get("reason", "not_scheduled"),
                "tool_round_count": 0,
                "estimated_context_tokens": 0,
                "proposed_action_count": 0,
                "applied_action_count": 0,
                "rejected_action_count": 0,
                "reassigned_unassigned_count": 0,
                "created_from_unassigned_segment_count": 0,
                "remaining_unassigned_count": remaining_unassigned_count,
                "consistency_repair_fingerprint": "",
                "error": "",
            }
            _write_full_maintainer_result(connection, result)
            return result

    try:
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            agent_result = _run_full_maintainer_agent(connection, messages or [], runtime_state)
            repair_plan = agent_result.get("repair_plan", [])
            simulation = _validate_and_simulate_repair_plan(
                connection,
                repair_plan,
                allow_mark_resolved=bool(agent_result.get("stm_evidence_requested")),
                mark_resolved_segment_ids=agent_result.get("inspected_segment_ids") or [],
                protected_active_segment_id=_protected_active_segment_id(runtime_state),
                inspected_assignment_ids=agent_result.get("inspected_assignment_ids") or [],
                inspected_segment_ids=agent_result.get("inspected_segment_ids") or [],
                max_segments=plan.get("max_segments"),
            )
            _apply_repair_simulation(connection, simulation)
            reassigned_unassigned_count = len(simulation.get("repaired_assignment_ids", set()))
            created_from_unassigned_segment_count = len(simulation.get("created_segment_ids", set()))
            remaining_unassigned_count = _assignment_health(connection)["unassigned_block_count"]
            if agent_result.get("unassigned_scan_cursor") is not None:
                _state_set(connection, "full_unassigned_scan_cursor", agent_result.get("unassigned_scan_cursor") or "")
            if plan.get("reason") == "unassigned_threshold":
                if reassigned_unassigned_count:
                    _state_set(connection, "last_full_maintainer_unassigned_no_progress_turn", "-1")
                    _state_set(connection, "last_full_maintainer_unassigned_no_progress_count", "-1")
                else:
                    _state_set(
                        connection,
                        "last_full_maintainer_unassigned_no_progress_turn",
                        str(plan.get("user_turn_count", 0)),
                    )
                    _state_set(
                        connection,
                        "last_full_maintainer_unassigned_no_progress_count",
                        str(remaining_unassigned_count),
                    )
            if plan.get("reason", "").startswith("capacity") or plan.get("reason") == "over_capacity":
                _state_set(connection, "last_full_maintainer_capacity_turn_count", str(plan.get("user_turn_count", 0)))
            if plan.get("reason") == "consistency_report":
                _state_set(
                    connection,
                    "last_full_consistency_attempt_fingerprint",
                    plan.get("consistency_repair_fingerprint", ""),
                )
                _state_set(
                    connection,
                    "last_full_consistency_attempt_turn",
                    str(plan.get("user_turn_count", 0)),
                )
            result = {
                "triggered": True,
                "updated": bool(simulation["applied_actions"]),
                "reason": plan.get("reason", agent_result.get("reason", "")),
                "tool_round_count": agent_result.get("tool_round_count", 0),
                "estimated_context_tokens": agent_result.get("estimated_context_tokens", 0),
                "proposed_action_count": len(repair_plan),
                "applied_action_count": len(simulation["applied_actions"]),
                "rejected_action_count": len(simulation["rejected_actions"]),
                "reassigned_unassigned_count": reassigned_unassigned_count,
                "created_from_unassigned_segment_count": created_from_unassigned_segment_count,
                "remaining_unassigned_count": remaining_unassigned_count,
                "consistency_repair_fingerprint": plan.get("consistency_repair_fingerprint", ""),
                "error": "",
            }
            _write_full_maintainer_result(connection, result)
            return result
    except Exception as error:
        result = {
            "triggered": True,
            "updated": False,
            "reason": plan.get("reason", "full_maintainer_failed"),
            "tool_round_count": 0,
            "estimated_context_tokens": 0,
            "proposed_action_count": 0,
            "applied_action_count": 0,
            "rejected_action_count": 0,
            "reassigned_unassigned_count": 0,
            "created_from_unassigned_segment_count": 0,
            "remaining_unassigned_count": 0,
            "consistency_repair_fingerprint": plan.get("consistency_repair_fingerprint", ""),
            "error": str(error),
        }
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            if plan.get("reason") == "consistency_report":
                _state_set(
                    connection,
                    "last_full_consistency_attempt_fingerprint",
                    plan.get("consistency_repair_fingerprint", ""),
                )
                _state_set(
                    connection,
                    "last_full_consistency_attempt_turn",
                    str(plan.get("user_turn_count", 0)),
                )
            result["remaining_unassigned_count"] = _assignment_health(connection)["unassigned_block_count"]
            _write_full_maintainer_result(connection, result)
        return result
