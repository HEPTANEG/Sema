import json
from datetime import datetime, timezone

FOCUS_DETECTOR_TIMEOUT_SECONDS = 30

FOCUS_DETECTOR_AGENT = "focus-detector-v1"

SEGMENT_INGEST_TIMEOUT_SECONDS = 45

FOCUS_INGESTER_AGENT = "focus-ingester-v1"

SEGMENT_INGEST_ASSIGN_THRESHOLD = 0.6

FULL_MAINTAINER_TIMEOUT_SECONDS = 60

FOCUS_MAINTAINER_AGENT = "focus-maintainer-v1"

FULL_MAINTAINER_MAX_TOOL_ROUNDS = 20

FULL_MAINTAINER_ANOMALY_THRESHOLD = 20

FULL_MAINTAINER_LOW_CONFIDENCE_THRESHOLD = 0.75

FULL_MAINTAINER_UNASSIGNED_CONFIDENCE_THRESHOLD = 0.75

FULL_MAINTAINER_UNASSIGNED_INSPECT_LIMIT = 8

FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_ACTION = 8

FULL_MAINTAINER_MAX_ASSIGNMENTS_PER_RUN = 16

FULL_MAINTAINER_UNASSIGNED_COOLDOWN_TURNS = 10

FULL_MAINTAINER_CONSISTENCY_COOLDOWN_TURNS = 10

FOCUS_CONSISTENCY_SAMPLE_LIMIT = 10

RESOLVED_CONFIDENCE_THRESHOLD = 0.85

FOCUS_METADATA_BUDGET_RATIO = 0.20

FOCUS_METADATA_BUDGET_MAX = 10000

FOCUS_UNOWNED_DETAIL_RATIO = 0.10

FOCUS_RESIDENCY_SEMANTICS_VERSION = 2

BOOTSTRAP_TITLE = "F1"

BOOTSTRAP_SUMMARY = "Primary focus for Phase 1 Focus Segment testing."

NEW_FOCUS_PENDING_SUMMARY = "New focus awaiting working-memory ingress."

VALID_STATUSES = {"active", "inactive", "resolved"}

VALID_TARGET_KINDS = {"active", "inactive", "new", "unclear"}

VALID_DETECTOR_REASON_CODES = {
    "no_shift",
    "target_inactive",
    "new_focus",
    "unclear",
    "validation_failed",
    "protocol_error",
}

VALID_INGEST_ACTIONS = {"assign", "leave_unassigned"}

VALID_ASSIGNMENT_STATUSES = {"assigned", "unassigned"}

VALID_ASSIGNMENT_LIFECYCLE_STATUSES = {"live", "retired"}

RETIREMENT_BLOCKING_ISSUE_CODES = {
    "assigned_segment_missing",
    "assigned_slots_not_covered",
    "assignment_missing_slot",
    "assignment_slot_overlap",
    "assignment_system_slot",
    "invalid_assignment_lifecycle_status",
    "invalid_assignment_slots",
    "invalid_assignment_status",
    "invalid_segment_ranges",
    "invalid_segment_status",
    "retired_assignment_not_formation_captured",
    "retired_assignment_still_live",
    "segment_range_missing_slot",
    "segment_range_overlap",
    "segment_range_system_slot",
    "unassigned_has_segment",
    "unassigned_slots_covered",
}

REPAIRABLE_CONSISTENCY_CODES = {
    "duplicate_segment_title",
    "orphan_empty_inactive_segment",
    "segment_range_overlap",
}

VALID_FULL_ACTIONS = {
    "rename_segment",
    "update_summary",
    "move_range",
    "merge_segments",
    "assign_unassigned",
    "create_segment_from_unassigned",
    "mark_resolved",
    "delete_empty_segment",
    "leave_unchanged",
}

def _now():
    return datetime.now(timezone.utc).isoformat()

def _extract_json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Focus Detector did not return a JSON object.")
    return json.loads(text[start : end + 1])

def detector_reason_code(decision):
    decision = decision or {}
    if not decision:
        return ""
    explicit = str(decision.get("reason_code") or "").strip()
    if explicit in VALID_DETECTOR_REASON_CODES:
        return explicit
    if decision.get("validation_failed"):
        return "validation_failed"
    return {
        "active": "no_shift",
        "inactive": "target_inactive",
        "new": "new_focus",
        "unclear": "unclear",
    }.get(decision.get("target_kind"), "protocol_error")

def _generic_title(title):
    normalized = _single_line(title).casefold().replace("-", " ").replace("_", " ")
    normalized = " ".join(normalized.split())
    if not normalized:
        return True
    generic_titles = {
        "new topic",
        "current topic",
        "general discussion",
        "discussion",
        "topic",
        "chat",
        "conversation",
        "misc",
        "miscellaneous",
        "???",
        "????",
        "????",
        "??",
        "??",
        "??",
        "??",
    }
    return normalized in generic_titles

def _valid_new_title(title):
    title = _single_line(title)
    if _generic_title(title):
        return False
    visible_chars = len(title)
    words = [word for word in title.split() if word]
    if any(ord(char) > 127 for char in title):
        return 4 <= visible_chars <= 20
    return 2 <= len(words) <= 6 or 4 <= visible_chars <= 20

def _single_line(value):
    return " ".join(str(value or "").split())
