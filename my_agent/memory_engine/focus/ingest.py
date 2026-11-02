import json

import memory_engine.event_stream as event_stream
from model.provider import call_chat_completion
from shared.prompt_loader import load_prompt

from .common import (
    RESOLVED_CONFIDENCE_THRESHOLD,
    FOCUS_INGESTER_AGENT,
    SEGMENT_INGEST_ASSIGN_THRESHOLD,
    SEGMENT_INGEST_TIMEOUT_SECONDS,
    VALID_INGEST_ACTIONS,
    _extract_json_object,
    _now,
    _valid_new_title,
)

from .ranges import (
    _append_slots_to_ranges,
)

from .store import (
    _bootstrap_if_missing,
    _ensure_tables,
    _focus_ingest_hint_for_block,
    _load_segments,
    _record_assignment,
    _segment_by_id,
    _state_set,
    _update_segment,
)

from .context import (
    _protected_active_segment_id,
    _stm_evidence,
    _uncovered_wm_interaction_blocks,
)

def _ingest_segment_list(segments):
    return [
        {
            "segment_id": segment["segment_id"],
            "status": segment["status"],
            "title": segment["title"],
            "summary": segment["summary"],
        }
        for segment in segments
        if segment.get("status") in {"active", "inactive"}
    ]

def _segment_ingest_payload(block, segments, detector_hint=None):
    ingest_segments = _ingest_segment_list(segments)
    block_start = (block.get("slot_range") or {}).get("start")
    if detector_hint and block_start is not None and int(detector_hint.get("start_slot")) == int(block_start):
        hinted_segment_id = str(detector_hint.get("segment_id") or "")
        hinted_start_slot = detector_hint.get("start_slot")
        for segment in ingest_segments:
            if segment.get("segment_id") == hinted_segment_id:
                segment["new_focus_start_slot"] = int(hinted_start_slot)
                break
    return {
        "ingress_block": {
            "slot_range": block.get("slot_range", {}),
            "slot_seqs": block.get("slot_seqs", []),
            "token_count": int(block.get("token_count") or 0),
            "records": block.get("records", []),
        },
        "segments": ingest_segments,
        "assignment_threshold": SEGMENT_INGEST_ASSIGN_THRESHOLD,
        "output_schema": {
            "action": "assign or leave_unassigned",
            "target_segment_id": "existing active/inactive segment id only when action is assign",
            "confidence": "0.0 to 1.0",
            "reason": "short explanation",
            "updates": {
                "title": "optional replacement title or null",
                "summary": "optional replacement summary or null",
                "mark_resolved": "boolean",
            },
        },
    }

def _segment_ingest_prompt(block, segments, detector_hint=None):
    return [
        {"role": "system", "content": load_prompt("segment_ingest")},
        {
            "role": "user",
            "content": json.dumps(
                _segment_ingest_payload(block, segments, detector_hint),
                ensure_ascii=False,
            ),
        },
    ]

def _call_segment_ingest_json(prompt):
    data = call_chat_completion(
        prompt,
        temperature=0,
        timeout_seconds=SEGMENT_INGEST_TIMEOUT_SECONDS,
        purpose="focus ingester",
    )
    return data["choices"][0]["message"].get("content", "")

def _normalize_segment_ingest_decision(raw):
    raw = raw or {}
    action = str(raw.get("action", "")).strip()
    if action not in VALID_INGEST_ACTIONS:
        action = "leave_unassigned"
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    updates = raw.get("updates") if isinstance(raw.get("updates"), dict) else {}
    proposed_title = updates.get("title") if updates.get("title") is not None else None
    if proposed_title is not None and not _valid_new_title(proposed_title):
        proposed_title = None
    target_segment_id = str(raw.get("target_segment_id") or "").strip()
    if action != "assign":
        target_segment_id = ""
    return {
        "action": action,
        "target_segment_id": target_segment_id,
        "confidence": confidence,
        "reason": str(raw.get("reason") or "").strip(),
        "updates": {
            "title": proposed_title,
            "summary": updates.get("summary") if updates.get("summary") is not None else None,
            "mark_resolved": bool(updates.get("mark_resolved", False)),
        },
        "created_by": FOCUS_INGESTER_AGENT,
        "created_at": _now(),
    }

def _run_segment_ingest_agent(block, segments, detector_hint=None):
    raw_text = _call_segment_ingest_json(_segment_ingest_prompt(block, segments, detector_hint))
    return _normalize_segment_ingest_decision(_extract_json_object(raw_text))

def _confirm_segment_ingest_resolved_decision(
    block,
    segments,
    decision,
    messages=None,
    runtime_state=None,
    detector_hint=None,
):
    if not (decision.get("updates") or {}).get("mark_resolved"):
        return decision
    prompt = _segment_ingest_prompt(block, segments, detector_hint)
    prompt.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
    prompt.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "tool_result": {"get_stm_evidence": _stm_evidence(messages, runtime_state)},
                    "instruction": (
                        "You proposed mark_resolved. Using this STM evidence, return the final same-schema JSON. "
                        "Keep mark_resolved true only if the evidence confirms clear closure. "
                        "If the recent STM still continues this segment or evidence is insufficient, keep the assignment "
                        "but set updates.mark_resolved to false."
                    ),
                },
                ensure_ascii=False,
            ),
        }
    )
    try:
        raw_text = _call_segment_ingest_json(prompt)
        confirmed = _normalize_segment_ingest_decision(_extract_json_object(raw_text))
    except Exception:
        confirmed = dict(decision)
        confirmed["updates"] = {**(decision.get("updates") or {}), "mark_resolved": False}
        confirmed["resolved_evidence_error"] = "segment_ingest_resolved_confirmation_failed"
        return confirmed

    same_target = confirmed.get("target_segment_id") == decision.get("target_segment_id")
    strong_confidence = float(confirmed.get("confidence") or 0.0) >= RESOLVED_CONFIDENCE_THRESHOLD
    if (
        confirmed.get("action") == "assign"
        and same_target
        and strong_confidence
        and (confirmed.get("updates") or {}).get("mark_resolved")
    ):
        confirmed["resolved_evidence_checked"] = True
        return confirmed

    downgraded = dict(decision)
    downgraded["updates"] = {**(decision.get("updates") or {}), "mark_resolved": False}
    downgraded["resolved_evidence_checked"] = True
    downgraded["resolved_evidence_downgraded"] = True
    return downgraded

def _apply_segment_ingest_decision(connection, block, decision, runtime_state=None):
    target_id = decision.get("target_segment_id", "")
    target = _segment_by_id(connection, target_id) if target_id else None
    if (
        decision.get("action") != "assign"
        or float(decision.get("confidence") or 0.0) < SEGMENT_INGEST_ASSIGN_THRESHOLD
        or not target
        or target.get("status") not in {"active", "inactive"}
    ):
        return {
            "assigned": False,
            "resolved": False,
            "target_segment_id": target_id,
            "reason": "leave_unassigned",
        }

    updates = decision.get("updates") or {}
    next_ranges = _append_slots_to_ranges(target.get("slot_ranges", []), block.get("slot_seqs", []))
    protected_active_segment_id = _protected_active_segment_id(runtime_state)
    mark_resolved = bool(updates.get("mark_resolved", False)) and target_id != protected_active_segment_id
    _update_segment(
        connection,
        target_id,
        ranges=next_ranges,
        title=updates.get("title"),
        summary=updates.get("summary"),
        status="resolved" if mark_resolved else target.get("status"),
    )
    return {
        "assigned": True,
        "resolved": mark_resolved,
        "target_segment_id": target_id,
        "reason": "assigned",
    }

def _write_segment_ingest_result(connection, result):
    _state_set(connection, "last_segment_ingest_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
    _state_set(connection, "last_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
    connection.commit()

def run_segment_ingest_maintenance(messages=None, runtime_state=None):
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        blocks = _uncovered_wm_interaction_blocks(connection, messages or [], runtime_state)
        bootstrap_created = False
        if blocks and not _load_segments(connection):
            bootstrap_created = _bootstrap_if_missing(connection)
        segments = _load_segments(connection)
        if not blocks:
            result = {
                "triggered": False,
                "updated": False,
                "bootstrap_created": bootstrap_created,
                "reason": "no_uncovered_wm_blocks",
                "processed_block_count": 0,
                "assigned_count": 0,
                "unassigned_count": 0,
                "resolved_count": 0,
                "resolved_evidence_checked_count": 0,
                "last_target_segment_id": "",
                "last_reason": "",
                "error": "",
            }
            _write_segment_ingest_result(connection, result)
            return result
        if not _ingest_segment_list(segments):
            for block in blocks:
                _record_assignment(
                    connection,
                    block,
                    {"confidence": None, "reason": "No active or inactive segment was available for assignment."},
                    {"assigned": False, "target_segment_id": "", "reason": "no_assignable_segments"},
                )
            connection.commit()
            result = {
                "triggered": True,
                "updated": bootstrap_created,
                "bootstrap_created": bootstrap_created,
                "reason": "no_assignable_segments",
                "processed_block_count": 0,
                "assigned_count": 0,
                "unassigned_count": len(blocks),
                "resolved_count": 0,
                "resolved_evidence_checked_count": 0,
                "last_target_segment_id": "",
                "last_reason": "",
                "error": "",
            }
            _write_segment_ingest_result(connection, result)
            return result

    processed = []
    for block in blocks:
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            segments = _load_segments(connection)
            detector_hint = _focus_ingest_hint_for_block(connection, block)
            decision = _run_segment_ingest_agent(
                block,
                segments,
                detector_hint,
            )
            decision = _confirm_segment_ingest_resolved_decision(
                block,
                segments,
                decision,
                messages,
                runtime_state,
                detector_hint,
            )
            applied = _apply_segment_ingest_decision(connection, block, decision, runtime_state)
            _record_assignment(connection, block, decision, applied)
            connection.commit()
        processed.append(
            {
                "slot_range": block.get("slot_range", {}),
                "slot_seqs": block.get("slot_seqs", []),
                "decision": decision,
                **applied,
            }
        )

    assigned_count = sum(1 for item in processed if item.get("assigned"))
    resolved_count = sum(1 for item in processed if item.get("resolved"))
    resolved_evidence_checked_count = sum(
        1 for item in processed if (item.get("decision") or {}).get("resolved_evidence_checked")
    )
    unassigned_count = len(processed) - assigned_count
    last = processed[-1] if processed else {}
    last_decision = last.get("decision") or {}
    result = {
        "triggered": True,
        "updated": bool(bootstrap_created or assigned_count or resolved_count),
        "bootstrap_created": bootstrap_created,
        "reason": "processed_ingress_blocks",
        "processed_block_count": len(processed),
        "assigned_count": assigned_count,
        "unassigned_count": unassigned_count,
        "resolved_count": resolved_count,
        "resolved_evidence_checked_count": resolved_evidence_checked_count,
        "last_target_segment_id": last.get("target_segment_id", ""),
        "last_reason": last_decision.get("reason", ""),
        "error": "",
    }
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        _write_segment_ingest_result(connection, result)
    return result

def run_focus_segment_maintenance(messages=None, runtime_state=None):
    try:
        return run_segment_ingest_maintenance(messages, runtime_state)
    except Exception as error:
        result = {
            "triggered": False,
            "updated": False,
            "bootstrap_created": False,
            "reason": "segment_ingest_failed",
            "processed_block_count": 0,
            "assigned_count": 0,
            "unassigned_count": 0,
            "resolved_count": 0,
            "resolved_evidence_checked_count": 0,
            "last_target_segment_id": "",
            "last_reason": "",
            "detector_recorded": False,
            "detector_error": str(error),
            "error": str(error),
        }
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            _write_segment_ingest_result(connection, result)
        return result
