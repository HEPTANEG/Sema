"""Auxiliary semantic digest cache for LTM Maintenance runs.

The cache is intentionally outside the canonical LTM Store graph. It helps the
next Maintainer run orient itself, but it is not evidence and must not be used
as a substitute for observation-tool verification.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .. import event_stream
from . import ltm_store
from ..token_counter import count_text_tokens


CACHE_DIR_NAME = "maintenance_cache"
PENDING_DIGEST_FILE = "pending_digest.json"
COMPLETED_DIGESTS_FILE = "semantic_digests.jsonl"
STORED_DIGEST_WINDOW_SIZE = 5
STAGE_SUMMARY_FORMAT = "maintenance_summary_v1"
PARTIAL_STAGE_SUMMARY_FORMAT = "maintenance_summary_v1_partial"
MAINTENANCE_STAGES = (
    "recent_review",
    "global_duplicate_review",
    "abstraction",
    "episode_review",
)
BASE_DIGEST_INJECTION_BUDGET_TOKENS = 3000
TOKENS_PER_100_PRESSURE_SLOTS = 500
_LOCK = threading.RLock()


def cache_dir(root=None):
    return event_stream.memory_root(root) / ltm_store.LTM_DIR_NAME / CACHE_DIR_NAME


def pending_digest_path(root=None):
    return cache_dir(root) / PENDING_DIGEST_FILE


def completed_digests_path(root=None):
    return cache_dir(root) / COMPLETED_DIGESTS_FILE


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def single_digest_max_tokens(pressure_threshold_slots=None):
    threshold = (
        ltm_store.MAINTENANCE_PRESSURE_THRESHOLD
        if pressure_threshold_slots is None
        else int(pressure_threshold_slots)
    )
    return max(1, round(threshold / 100 * TOKENS_PER_100_PRESSURE_SLOTS))


def digest_injection_budget_tokens(pressure_threshold_slots=None):
    return max(BASE_DIGEST_INJECTION_BUDGET_TOKENS, single_digest_max_tokens(pressure_threshold_slots))


def injected_digest_count(pressure_threshold_slots=None):
    per_digest = single_digest_max_tokens(pressure_threshold_slots)
    budget = digest_injection_budget_tokens(pressure_threshold_slots)
    return max(1, min(STORED_DIGEST_WINDOW_SIZE, budget // per_digest))


def _normalize_references(references):
    references = dict(references or {})
    normalized = {}
    for key in ("object_ids", "link_ids"):
        values = references.get(key) or []
        if values:
            normalized[key] = [str(value) for value in values]
    ranges = []
    for item in references.get("event_slot_ranges") or []:
        if not isinstance(item, dict):
            continue
        ranges.append({
            "start": int(item.get("start", item.get("start_slot_seq", 0))),
            "end": int(item.get("end", item.get("end_slot_seq", item.get("start", 0)))),
        })
    if ranges:
        normalized["event_slot_ranges"] = ranges
    return normalized


def build_digest(run_id, content, references=None, root=None, digest_id=None, created_at=None):
    token_count = count_text_tokens(content)
    return {
        "digest_id": digest_id or f"maintenance_digest_{uuid4().hex}",
        "maintenance_run_id": str(run_id),
        "created_at": created_at or _now(),
        "coverage_cursor": ltm_store.get_maintenance_runtime_state(root).get("maintenance_coverage_cursor"),
        "token_count": token_count,
        "content": str(content or ""),
        "references": _normalize_references(references),
    }


def write_pending_digest(run_id, content, references=None, root=None):
    content = str(content or "").strip()
    if not content:
        return {"status": "rejected", "error": {"code": "empty_digest", "message": "digest content is required."}}
    token_count = count_text_tokens(content)
    max_tokens = single_digest_max_tokens()
    if token_count > max_tokens:
        return {
            "status": "rejected",
            "error": {
                "code": "digest_too_large",
                "message": f"digest has {token_count} tokens; maximum is {max_tokens}.",
            },
            "token_count": token_count,
            "max_tokens": max_tokens,
        }
    try:
        with _LOCK:
            digest = build_digest(run_id, content, references, root)
            _atomic_write_text(pending_digest_path(root), json.dumps(digest, ensure_ascii=False, indent=2))
            return {
                "status": "ok",
                "digest_id": digest["digest_id"],
                "token_count": digest["token_count"],
                "max_tokens": max_tokens,
            }
    except Exception as error:
        return {"status": "rejected", "error": {"code": "digest_write_failed", "message": str(error)}}


def load_pending_digest(root=None):
    try:
        data = json.loads(pending_digest_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_pending_digest(root=None):
    try:
        pending_digest_path(root).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _read_completed_rows(root=None):
    rows = []
    try:
        text = completed_digests_path(root).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    except json.JSONDecodeError:
        return []
    return rows


def load_recent_digests(root=None, pressure_threshold_slots=None):
    with _LOCK:
        rows = _read_completed_rows(root)
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:injected_digest_count(pressure_threshold_slots)]


def is_staged_summary(row):
    """Return whether a cached row is a current staged maintenance summary."""
    if not isinstance(row, dict) or row.get("format") not in {STAGE_SUMMARY_FORMAT, PARTIAL_STAGE_SUMMARY_FORMAT}:
        return False
    summaries = row.get("stage_summaries")
    return isinstance(summaries, dict) and bool(summaries) and all(
        stage in MAINTENANCE_STAGES for stage in summaries
    )


def load_latest_staged_summary(root=None):
    with _LOCK:
        rows = [row for row in _read_completed_rows(root) if is_staged_summary(row)]
        rows.sort(key=lambda row: str(row.get("completed_at") or row.get("created_at") or ""), reverse=True)
        return rows[0] if rows else None


def load_stage_summary(stage, root=None):
    if stage not in MAINTENANCE_STAGES:
        return None
    row = load_latest_staged_summary(root)
    if not row:
        return None
    value = row.get("stage_summaries", {}).get(stage)
    return value if isinstance(value, str) and value.strip() else None


def build_maintenance_summary(stage_summaries, stages=None):
    """Mechanically join stage summaries without rewriting their content."""
    parts = []
    for stage in tuple(stages or MAINTENANCE_STAGES):
        content = str((stage_summaries or {}).get(stage) or "").strip()
        parts.append(f"[{stage}]\n{content}")
    return "\n\n".join(parts)


def persist_stage_summaries(run_id, stage_summaries, references=None, root=None, required_stages=None, max_tokens=None):
    """Persist a completed staged summary; failures are deliberately non-fatal."""
    required = tuple(required_stages or MAINTENANCE_STAGES)
    summary_max_tokens = int(max_tokens or single_digest_max_tokens())
    if not required or any(stage not in MAINTENANCE_STAGES for stage in required):
        return {"status": "failed", "error": "unknown required maintenance stage"}
    normalized = {}
    for stage in required:
        content = str((stage_summaries or {}).get(stage) or "").strip()
        if not content:
            return {"status": "missing", "token_count": 0}
        if count_text_tokens(content) > summary_max_tokens:
            return {"status": "failed", "error": f"{stage} summary exceeds {summary_max_tokens} token limit"}
        normalized[stage] = content
    summary = build_maintenance_summary(normalized, required)
    try:
        with _LOCK:
            rows = _read_completed_rows(root)
            row = {
                "format": STAGE_SUMMARY_FORMAT if set(required) == set(MAINTENANCE_STAGES) else PARTIAL_STAGE_SUMMARY_FORMAT,
                "digest_id": f"maintenance_summary_{uuid4().hex}",
                "maintenance_run_id": str(run_id),
                "created_at": _now(),
                "completed_at": _now(),
                "token_count": count_text_tokens(summary),
                "content": summary,
                "stage_summaries": normalized,
                "required_stages": list(required),
                "references": _normalize_references(references),
            }
            rows = [item for item in rows if item.get("maintenance_run_id") != str(run_id)]
            rows.insert(0, row)
            rows.sort(key=lambda item: str(item.get("completed_at") or item.get("created_at") or ""), reverse=True)
            _atomic_write_text(
                completed_digests_path(root),
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows[:STORED_DIGEST_WINDOW_SIZE]),
            )
            return {"status": "written", "digest_id": row["digest_id"], "token_count": row["token_count"]}
    except Exception as error:
        return {"status": "failed", "error": str(error)}


def persist_pending_digest(run_id, root=None):
    """Persist the pending digest as completed. Returns a non-throwing status."""
    with _LOCK:
        pending = load_pending_digest(root)
        if not pending or pending.get("maintenance_run_id") != run_id:
            clear_pending_digest(root)
            return {"status": "missing"}
        try:
            rows = _read_completed_rows(root)
            pending = dict(pending)
            pending["completed_at"] = _now()
            rows = [row for row in rows if row.get("digest_id") != pending.get("digest_id")]
            rows.insert(0, pending)
            rows.sort(key=lambda row: str(row.get("completed_at") or row.get("created_at") or ""), reverse=True)
            kept = rows[:STORED_DIGEST_WINDOW_SIZE]
            text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in kept)
            _atomic_write_text(completed_digests_path(root), text)
            clear_pending_digest(root)
            return {
                "status": "written",
                "digest_id": pending.get("digest_id"),
                "token_count": int(pending.get("token_count") or 0),
            }
        except Exception as error:
            clear_pending_digest(root)
            return {"status": "failed", "error": str(error)}
