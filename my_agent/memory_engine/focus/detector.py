import json

import memory_engine.event_stream as event_stream
from model.provider import call_chat_completion
from shared.prompt_loader import load_prompt

from .common import (
    FOCUS_DETECTOR_AGENT,
    FOCUS_DETECTOR_TIMEOUT_SECONDS,
    NEW_FOCUS_PENDING_SUMMARY,
    VALID_TARGET_KINDS,
    detector_reason_code,
    _extract_json_object,
    _now,
    _valid_new_title,
)

from .store import (
    _active_segment,
    _active_segments,
    _create_segment,
    _ensure_tables,
    _load_segments,
    _record_focus_ingest_hint,
    _segment_by_id,
    _set_segment_status,
    _state_set,
)

from .context import (
    _latest_focus_context,
    _latest_user_slot_seq,
)

def _focus_list(segments, status):
    return [
        {
            "segment_id": segment["segment_id"],
            "title": segment["title"],
            "summary": segment["summary"],
        }
        for segment in segments
        if segment.get("status") == status
    ]

def _detector_segment_list(segments):
    return [
        {
            "segment_id": segment["segment_id"],
            "status": segment["status"],
            "title": segment["title"],
        }
        for segment in segments
        if segment.get("status") in {"active", "inactive"}
    ]

def _detector_payload(messages, segments):
    return {
        "focus_context": _latest_focus_context(messages),
        "segments": _detector_segment_list(segments),
    }

def _detector_prompt(messages, segments, validation_errors=None):
    payload = _detector_payload(messages, segments)
    if validation_errors:
        payload["previous_output_validation_errors"] = list(validation_errors)
        payload["retry_instruction"] = (
            "Your previous output failed hard-coded consistency validation. "
            "Return a corrected decision that is consistent with the provided segment ids and statuses."
        )
    return [
        {"role": "system", "content": load_prompt("focus_detector")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

def _call_detector_json(prompt):
    data = call_chat_completion(
        prompt,
        temperature=0,
        timeout_seconds=FOCUS_DETECTOR_TIMEOUT_SECONDS,
        purpose="focus detector",
    )
    return data["choices"][0]["message"].get("content", "")

def _normalize_detector_decision(raw):
    target_kind = str((raw or {}).get("target_kind", "")).strip()
    if target_kind not in VALID_TARGET_KINDS:
        target_kind = "unclear"
    target_segment_id = str((raw or {}).get("target_segment_id") or "").strip()
    if target_kind not in {"active", "inactive"}:
        target_segment_id = ""
    return {
        "should_shift": target_kind in {"inactive", "new", "unclear"},
        "target_kind": target_kind,
        "target_segment_id": target_segment_id,
        "proposed_title": str((raw or {}).get("proposed_title") or "").strip(),
        "reason_code": detector_reason_code({"target_kind": target_kind}),
        "created_by": FOCUS_DETECTOR_AGENT,
        "created_at": _now(),
    }

def _no_shift_detector_decision(segments):
    active_segments = [segment for segment in segments if segment.get("status") == "active"]
    if len(active_segments) != 1:
        return None, ["NO_SHIFT requires exactly one active segment."]
    active_segment = active_segments[0]
    return (
        {
            "should_shift": False,
            "target_kind": "active",
            "target_segment_id": active_segment["segment_id"],
            "proposed_title": "",
            "reason_code": "no_shift",
            "created_by": FOCUS_DETECTOR_AGENT,
            "created_at": _now(),
        },
        [],
    )

def _validate_detector_decision(decision, segments):
    errors = []
    target_kind = decision.get("target_kind")
    target_id = decision.get("target_segment_id", "")
    target = next((segment for segment in segments if segment.get("segment_id") == target_id), None) if target_id else None

    if target_kind in {"active", "inactive"}:
        if not target:
            errors.append(f"target_segment_id does not exist for target_kind={target_kind}.")
        elif target.get("status") != target_kind:
            errors.append(
                f"target_kind={target_kind} does not match actual segment status={target.get('status')} "
                f"for target_segment_id={target_id}."
            )
    elif target_id:
        errors.append(f"target_segment_id must be empty for target_kind={target_kind}.")

    if target and target.get("status") == "resolved":
        errors.append("resolved segments cannot be Detector targets.")

    if target_kind == "new" and not _valid_new_title(decision.get("proposed_title", "")):
        errors.append("target_kind=new requires a specific non-generic proposed_title with valid length.")
    if target_kind == "unclear" and target_id:
        errors.append("target_kind=unclear must not include target_segment_id.")
    return errors

def _fallback_detector_decision(reason_code, validation_errors=None, previous_decision=None):
    return {
        "should_shift": True,
        "target_kind": "unclear",
        "target_segment_id": "",
        "proposed_title": "",
        "reason_code": reason_code,
        "created_by": FOCUS_DETECTOR_AGENT,
        "created_at": _now(),
        "validation_errors": list(validation_errors or []),
        "retry_count": 1,
        "validation_failed": True,
        **({"previous_decision": previous_decision} if previous_decision else {}),
    }

def _run_detector(messages, segments):
    validation_errors = []
    last_decision = None
    last_reason_code = "validation_failed"
    for attempt in range(2):
        try:
            raw_text = _call_detector_json(
                _detector_prompt(messages, segments, validation_errors if attempt else None)
            )
            if str(raw_text or "").strip() == "NO_SHIFT":
                decision, errors = _no_shift_detector_decision(segments)
                if errors:
                    decision = _fallback_detector_decision("protocol_error", errors)
                    last_reason_code = "protocol_error"
            else:
                decision = _normalize_detector_decision(_extract_json_object(raw_text))
                errors = _validate_detector_decision(decision, segments)
        except Exception as error:
            errors = [str(error)]
            decision = _fallback_detector_decision("protocol_error", errors)
            last_reason_code = "protocol_error"
        decision["validation_errors"] = errors
        decision["retry_count"] = attempt
        decision["validation_failed"] = bool(errors)
        if errors and decision.get("reason_code") != "protocol_error":
            decision["reason_code"] = detector_reason_code({"validation_failed": True})
            last_reason_code = "validation_failed"
        last_decision = decision
        if not errors:
            return decision
        validation_errors = errors
    return _fallback_detector_decision(last_reason_code, validation_errors, last_decision or {})

def _record_detector_decision(connection, decision):
    _state_set(connection, "last_detector_decision", json.dumps(decision, ensure_ascii=False, sort_keys=True))
    connection.commit()

def _apply_detector_decision(connection, decision, new_focus_start_slot=None):
    target_kind = decision.get("target_kind")
    if target_kind in {"active", "unclear"}:
        return {
            "action": "no_change",
            "reason": "active_or_unclear",
            "target_segment_id": decision.get("target_segment_id", ""),
        }

    if target_kind == "inactive":
        target = _segment_by_id(connection, decision.get("target_segment_id", ""))
        if not target or target.get("status") != "inactive":
            return {
                "action": "no_change",
                "reason": "inactive_target_not_found",
                "target_segment_id": decision.get("target_segment_id", ""),
            }
        for segment in _active_segments(connection):
            if segment["segment_id"] != target["segment_id"]:
                _set_segment_status(connection, segment["segment_id"], "inactive")
        _set_segment_status(connection, target["segment_id"], "active")
        connection.commit()
        return {
            "action": "activated_inactive",
            "reason": "detector_matched_inactive",
            "target_segment_id": target["segment_id"],
        }

    if target_kind == "new":
        for segment in _active_segments(connection):
            _set_segment_status(connection, segment["segment_id"], "inactive")
        segment_id = _create_segment(
            connection,
            decision.get("proposed_title") or "New focus",
            NEW_FOCUS_PENDING_SUMMARY,
            "active",
        )
        _record_focus_ingest_hint(connection, segment_id, new_focus_start_slot)
        connection.commit()
        return {
            "action": "created_active",
            "reason": "detector_found_new_focus",
            "target_segment_id": segment_id,
        }

    return {"action": "no_change", "reason": "unknown_target_kind", "target_segment_id": ""}

def run_focus_detector_before_context(messages=None, runtime_state=None):
    if isinstance(runtime_state, dict):
        runtime_state.pop("_focus_detector_active_segment_id", None)
    if not event_stream.event_stream_db_path().exists():
        return {
            "triggered": False,
            "detector_recorded": False,
            "detector_error": "",
            "action": "skipped",
            "target_segment_id": "",
            "reason": "no_event_stream",
            "reason_code": "",
        }
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        segments = _load_segments(connection)
        try:
            detector = _run_detector(messages or [], segments)
            _record_detector_decision(connection, detector)
            shift = _apply_detector_decision(
                connection,
                detector,
                new_focus_start_slot=_latest_user_slot_seq(connection, messages or []),
            )
            detector_error = "; ".join(detector.get("validation_errors") or []) if detector.get("validation_failed") else ""
        except Exception as error:
            detector = None
            shift = {"action": "no_change", "reason": "detector_failed", "target_segment_id": ""}
            detector_error = str(error)
        result = {
            "triggered": True,
            "detector_recorded": detector is not None,
            "detector_error": detector_error,
            "action": shift.get("action", ""),
            "target_segment_id": shift.get("target_segment_id", ""),
            "reason": shift.get("reason", "focus_prepare"),
            "reason_code": (detector or {}).get("reason_code", "protocol_error"),
            "active_title": "",
        }
        active = _segment_by_id(connection, result["target_segment_id"])
        current_active = active if active and active.get("status") == "active" else _active_segment(connection)
        if isinstance(runtime_state, dict) and current_active:
            runtime_state["_focus_detector_active_segment_id"] = current_active["segment_id"]
        if current_active:
            result["active_title"] = str(current_active.get("title") or "").strip()
        _state_set(connection, "last_focus_prepare_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
        connection.commit()
        return result
