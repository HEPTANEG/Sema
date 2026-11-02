import json

import memory_engine.event_stream as event_stream

from .common import (
    VALID_STATUSES,
    detector_reason_code,
)

from .store import (
    _assignment_health,
    _load_json_state,
    _load_segments,
    _read_only_event_stream_connection,
    _table_exists,
)

from .lifecycle import (
    _empty_focus_consistency_report,
    run_focus_consistency_audit,
)

def focus_segments_status(messages=None, runtime_state=None):
    if not event_stream.event_stream_db_path().exists():
        return {
            "active": False,
            "reason": "no_event_stream",
            "bootstrap_created": False,
            "segment_count": 0,
            "counts": {status: 0 for status in VALID_STATUSES},
            "segments": [],
            "last_detector_decision": {},
            "last_result": {},
            "last_focus_prepare_result": {},
            "last_segment_ingest_result": {},
            "last_full_maintainer_result": {},
            "last_focus_residency_reconciliation_result": {},
            "last_focus_range_retirement_result": {},
            "assignment_health": {},
            "focus_consistency_report": _empty_focus_consistency_report(),
        }
    with _read_only_event_stream_connection() as connection:
        if not _table_exists(connection, "focus_segments"):
            return {
                "active": False,
                "reason": "no_focus_segments",
                "bootstrap_created": False,
                "segment_count": 0,
                "counts": {status: 0 for status in VALID_STATUSES},
                "segments": [],
                "last_detector_decision": {},
                "last_result": {},
                "last_focus_prepare_result": {},
                "last_segment_ingest_result": {},
                "last_full_maintainer_result": {},
                "last_focus_residency_reconciliation_result": {},
                "last_focus_range_retirement_result": {},
                "assignment_health": {},
                "focus_consistency_report": _empty_focus_consistency_report(),
            }
        segments = _load_segments(connection)
        counts = {status: sum(1 for segment in segments if segment.get("status") == status) for status in VALID_STATUSES}
        status = {
            "active": bool(segments),
            "reason": "" if segments else "no_focus_segments",
            "bootstrap_created": False,
            "segment_count": len(segments),
            "counts": counts,
            "segments": segments,
            "last_detector_decision": _load_json_state(connection, "last_detector_decision", {}),
            "last_result": _load_json_state(connection, "last_result", {}),
            "last_focus_prepare_result": _load_json_state(connection, "last_focus_prepare_result", {}),
            "last_segment_ingest_result": _load_json_state(connection, "last_segment_ingest_result", {}),
            "last_full_maintainer_result": _load_json_state(connection, "last_full_maintainer_result", {}),
            "last_focus_residency_reconciliation_result": _load_json_state(
                connection, "last_focus_residency_reconciliation_result", {}
            ),
            "last_focus_range_retirement_result": _load_json_state(
                connection, "last_focus_range_retirement_result", {}
            ),
            "assignment_health": _assignment_health(connection, segments),
        }
    status["focus_consistency_report"] = run_focus_consistency_audit(
        messages,
        runtime_state,
        persist=False,
    )
    return status

def _format_focus_consistency_report(report):
    report = report or _empty_focus_consistency_report()
    metrics = report.get("metrics") or {}
    lines = [
        "focus_consistency_report:",
        f"  status: {report.get('status', 'ok')}",
        f"  checked_at: {report.get('checked_at', '')}",
        f"  error_count: {report.get('error_count', 0)}",
        f"  warning_count: {report.get('warning_count', 0)}",
        f"  repairable_issue_count: {report.get('repairable_issue_count', 0)}",
        f"  should_trigger_full_maintainer: {str(bool(report.get('should_trigger_full_maintainer'))).lower()}",
        f"  repair_fingerprint: {report.get('repair_fingerprint', '')}",
        f"  residency_evaluated: {str(bool(metrics.get('residency_evaluated'))).lower()}",
        f"  wm_injected_slot_count: {metrics.get('wm_injected_slot_count', 0)}",
        f"  wm_resident_slot_count: {metrics.get('wm_resident_slot_count', 0)}",
        f"  wm_resident_not_injected_slot_count: {metrics.get('wm_resident_not_injected_slot_count', 0)}",
        f"  wm_resident_raw_token_count: {metrics.get('wm_resident_raw_token_count', 0)}",
        f"  resolved_retirement_backlog_slot_count: {metrics.get('resolved_retirement_backlog_slot_count', 0)}",
        f"  focus_residency_semantics_version: {metrics.get('focus_residency_semantics_version', 0)}",
        "  issues:",
    ]
    for issue in report.get("issues") or []:
        lines.append(
            "  - "
            f"{issue.get('severity', '')} {issue.get('code', '')} "
            f"count={issue.get('count', 0)} repairable={str(bool(issue.get('repairable'))).lower()} "
            f"samples={json.dumps(issue.get('samples') or {}, ensure_ascii=False, sort_keys=True)}"
        )
    return lines

def format_focus_segments_status(status):
    status = status or {}
    if not status.get("active", True):
        counts = status.get("counts") or {}
        return "\n".join(
            [
                "Focus Segment status:",
                "active: false",
                f"reason: {status.get('reason') or 'inactive'}",
                f"segment_count: {status.get('segment_count', 0)}",
                f"active_count: {counts.get('active', 0)}",
                f"inactive_count: {counts.get('inactive', 0)}",
                f"resolved_count: {counts.get('resolved', 0)}",
                *_format_focus_consistency_report(status.get("focus_consistency_report")),
            ]
        )
    counts = status.get("counts") or {}
    lines = [
        "Focus Segment status:",
        f"segment_count: {status.get('segment_count', 0)}",
        f"active_count: {counts.get('active', 0)}",
        f"inactive_count: {counts.get('inactive', 0)}",
        f"resolved_count: {counts.get('resolved', 0)}",
        f"bootstrap_created: {str(bool(status.get('bootstrap_created'))).lower()}",
        "segments:",
    ]
    for segment in status.get("segments") or []:
        lines.append(
            f"- {segment['segment_id']} {segment['status']} title={segment['title']} ranges={json.dumps(segment.get('slot_ranges', []), ensure_ascii=False)}"
        )
    decision = status.get("last_detector_decision") or {}
    lines.extend(
        [
            "last_detector_decision:",
            f"  should_shift: {decision.get('should_shift', '')}",
            f"  target_kind: {decision.get('target_kind', '')}",
            f"  target_segment_id: {decision.get('target_segment_id', '')}",
            f"  reason_code: {detector_reason_code(decision) if decision else ''}",
            f"  retry_count: {decision.get('retry_count', '')}",
            f"  validation_failed: {decision.get('validation_failed', '')}",
        ]
    )
    result = status.get("last_result") or {}
    if result:
        lines.extend(
            [
                "last_result:",
                f"  range_updated: {result.get('range_updated', '')}",
                f"  detector_recorded: {result.get('detector_recorded', '')}",
                f"  detector_error: {result.get('detector_error', '')}",
            ]
        )
    ingest = status.get("last_segment_ingest_result") or {}
    if ingest:
        lines.extend(
            [
                "last_segment_ingest_result:",
                f"  processed_block_count: {ingest.get('processed_block_count', 0)}",
                f"  assigned_count: {ingest.get('assigned_count', 0)}",
                f"  unassigned_count: {ingest.get('unassigned_count', 0)}",
                f"  resolved_count: {ingest.get('resolved_count', 0)}",
                f"  resolved_evidence_checked_count: {ingest.get('resolved_evidence_checked_count', 0)}",
                f"  last_target_segment_id: {ingest.get('last_target_segment_id', '')}",
                f"  last_reason: {ingest.get('last_reason', '')}",
                f"  error: {ingest.get('error', '')}",
            ]
        )
    health = status.get("assignment_health") or {}
    if health:
        lines.extend(
            [
                "assignment_health:",
                f"  assignment_count: {health.get('assignment_count', 0)}",
                f"  live_assignment_count: {health.get('live_assignment_count', health.get('assignment_count', 0))}",
                f"  retired_assignment_count: {health.get('retired_assignment_count', 0)}",
                f"  assigned_count: {health.get('assigned_count', 0)}",
                f"  unassigned_block_count: {health.get('unassigned_block_count', 0)}",
                f"  low_confidence_assignment_count: {health.get('low_confidence_assignment_count', 0)}",
            ]
        )
    full = status.get("last_full_maintainer_result") or {}
    if full:
        lines.extend(
            [
                "last_full_maintainer_result:",
                f"  triggered: {full.get('triggered', '')}",
                f"  reason: {full.get('reason', '')}",
                f"  tool_round_count: {full.get('tool_round_count', 0)}",
                f"  proposed_action_count: {full.get('proposed_action_count', 0)}",
                f"  applied_action_count: {full.get('applied_action_count', 0)}",
                f"  rejected_action_count: {full.get('rejected_action_count', 0)}",
                f"  reassigned_unassigned_count: {full.get('reassigned_unassigned_count', 0)}",
                f"  created_from_unassigned_segment_count: {full.get('created_from_unassigned_segment_count', 0)}",
                f"  remaining_unassigned_count: {full.get('remaining_unassigned_count', 0)}",
                f"  error: {full.get('error', '')}",
            ]
        )
    prepare_result = status.get("last_focus_prepare_result") or {}
    if prepare_result:
        lines.extend(
            [
                "last_focus_prepare_result:",
                f"  action: {prepare_result.get('action', '')}",
                f"  target_segment_id: {prepare_result.get('target_segment_id', '')}",
                f"  detector_recorded: {prepare_result.get('detector_recorded', '')}",
                f"  detector_error: {prepare_result.get('detector_error', '')}",
            ]
        )
    retirement = status.get("last_focus_range_retirement_result") or {}
    reconciliation = status.get("last_focus_residency_reconciliation_result") or {}
    if reconciliation:
        lines.extend(
            [
                "last_focus_residency_reconciliation_result:",
                f"  triggered: {reconciliation.get('triggered', '')}",
                f"  updated: {reconciliation.get('updated', '')}",
                f"  reason: {reconciliation.get('reason', '')}",
                f"  from_version: {reconciliation.get('from_version', 0)}",
                f"  to_version: {reconciliation.get('to_version', 0)}",
                f"  restored_assignment_count: {reconciliation.get('restored_assignment_count', 0)}",
                f"  restored_slot_count: {reconciliation.get('restored_slot_count', 0)}",
                f"  skipped_assignment_count: {reconciliation.get('skipped_assignment_count', 0)}",
                f"  error: {reconciliation.get('error', '')}",
            ]
        )
    if retirement:
        lines.extend(
            [
                "last_focus_range_retirement_result:",
                f"  triggered: {retirement.get('triggered', '')}",
                f"  updated: {retirement.get('updated', '')}",
                f"  reason: {retirement.get('reason', '')}",
                f"  retired_assignment_count: {retirement.get('retired_assignment_count', 0)}",
                f"  retired_slot_count: {retirement.get('retired_slot_count', 0)}",
                f"  retired_range_count: {retirement.get('retired_range_count', 0)}",
                f"  updated_segment_count: {retirement.get('updated_segment_count', 0)}",
                f"  deferred_partial_assignment_count: {retirement.get('deferred_partial_assignment_count', 0)}",
                f"  error: {retirement.get('error', '')}",
            ]
        )
    lines.extend(_format_focus_consistency_report(status.get("focus_consistency_report")))
    return "\n".join(lines)
