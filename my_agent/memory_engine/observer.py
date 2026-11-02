import copy
import json
import sqlite3

from . import context_builder, event_stream, evidence
from .ltm import formation, ltm_store, memory_push_ranking, memorize, memory_retrieval, vector_index
from .dynamic_context_budget import (
    STM_MAX_TOKENS,
    STM_MIN_TOKENS,
    WM_MAX_TOKENS,
    WM_MIN_TOKENS,
    collect_context_pressure_signals,
    compute_dynamic_context_budget,
)
from .state import build_memory_engine_state
from .token_counter import count_message_tokens
from .focus.common import detector_reason_code


OBSERVER_API_VERSION = 1


def memory_engine_snapshot(messages=None, runtime_state=None):
    runtime_copy = _runtime_copy(runtime_state)
    snapshot = build_memory_engine_state(messages=messages, runtime_state=runtime_copy)
    snapshot["observer"] = {
        "api_version": OBSERVER_API_VERSION,
        "read_only": True,
        "scope": "event_stream",
    }
    snapshot["working_memory"].update(_working_memory_persistent_state())
    snapshot["event_stream_slots"] = _slot_projection_snapshot()
    snapshot["focus_segments"] = _focus_segments_snapshot()
    snapshot["ltm"] = ltm_store.ltm_store_snapshot()
    snapshot["ltm"]["vector_index"] = vector_index.vector_index_snapshot()
    snapshot["formation"] = formation.formation_status()
    snapshot["retrieval"] = memory_retrieval.retrieval_observer_snapshot(runtime_state)
    snapshot["memorize"] = memorize.memorize_observer_snapshot(runtime_state)
    snapshot["memory_push"] = memory_push_ranking.memory_push_observer_snapshot(runtime_state)
    snapshot["context"]["budget_stabilizer"] = _budget_stabilizer_state()
    preview = context_build_preview(messages=messages, runtime_state=runtime_state)
    snapshot["context_preview"] = preview
    if preview.get("ok"):
        snapshot["context"]["total_estimated_context_tokens"] = preview.get("total_estimated_tokens", "unavailable")
    snapshot["evidence_audit"] = evidence_audit(runtime_state)
    return snapshot


def context_build_preview(messages=None, runtime_state=None):
    if not event_stream.event_stream_db_path().exists():
        return {
            "ok": False,
            "error": "event stream database not found",
            "context_order": [],
            "sections": [],
        }
    runtime_copy = _runtime_copy(runtime_state)
    source_messages = messages or event_stream.load_messages()
    try:
        pressure = _observer_pressure(source_messages, runtime_copy)
        budgets = pressure.get("budgets") or {}
        stm_budget = int(budgets.get("stm") or 10000)
        wm_budget = int(budgets.get("wm") or 6000)
        watermark = _read_only_wm_ingress_watermark(runtime_copy)
        stm = context_builder.build_stm_projection(
            source_messages,
            budget=stm_budget,
            wm_ingress_watermark=watermark,
        )
        wm = context_builder._build_working_memory_projection(stm, budget=wm_budget)
    except Exception as error:
        return {
            "ok": False,
            "error": str(error),
            "context_order": [],
            "sections": [],
        }

    system_tokens = sum(count_message_tokens(message) for message in stm.get("messages", []) if message.get("role") == "system")
    total_tokens = system_tokens + int(wm.get("selected_tokens", 0)) + int(stm.get("selected_tokens", 0))
    wm_records = wm.get("selected_records") or []
    return {
        "ok": True,
        "context_order": ["system", "working_memory", "long_term_memory", "short_term_memory"],
        "budget_source": pressure.get("budget_source", ""),
        "total_estimated_tokens": total_tokens,
        "sections": [
            {"name": "system", "token_count": system_tokens},
            {"name": "working_memory", "token_count": wm.get("selected_tokens", 0)},
            {"name": "long_term_memory", "token_count": 0, "status": "not_run"},
            {"name": "short_term_memory", "token_count": stm.get("selected_tokens", 0)},
        ],
        "working_memory": {
            "budget": wm.get("budget", 0),
            "selected_tokens": wm.get("selected_tokens", 0),
            "selected_count": len(wm_records),
            "selected_slot_seqs": wm.get("selected_slot_seqs")
            or [int(record["slot_seq"]) for record in wm_records],
            "selected_range": _slot_range(wm_records),
            "candidate_count": wm.get("candidate_count", 0),
            "skipped_count": wm.get("skipped_count", 0),
            "projection_mode": wm.get("projection_mode", "legacy"),
            "metadata_tokens": wm.get("metadata_tokens", 0),
            "active_detail_tokens": wm.get("active_detail_tokens", 0),
            "unowned_detail_tokens": wm.get("unowned_detail_tokens", 0),
            "active_detail_count": wm.get("active_detail_count", 0),
            "unowned_detail_count": wm.get("unowned_detail_count", 0),
            "inactive_summary_count": wm.get("inactive_summary_count", 0),
            "resolved_title_count": wm.get("resolved_title_count", 0),
            "resident_slot_count": wm.get("resident_slot_count", 0),
            "resident_raw_token_count": wm.get("resident_raw_token_count", 0),
        },
        "short_term_memory": {
            "budget": stm.get("budget", 0),
            "selected_tokens": stm.get("selected_tokens", 0),
            "selected_blocks": [_block_metadata(block) for block in stm.get("selected_blocks") or []],
            "omitted_blocks": [_block_metadata(block) for block in stm.get("omitted_blocks") or []],
            "latest_block_tokens": stm.get("latest_block_tokens", 0),
            "watermark_locked_omitted_block_count": stm.get("watermark_locked_omitted_block_count", 0),
            "wm_ingress_watermark": stm.get("wm_ingress_watermark", -1),
        },
        "context_pressure": {
            "scores": pressure.get("scores") or {},
            "budgets": pressure.get("budgets") or {},
            "stabilizer": pressure.get("stabilizer") or {},
        },
    }


def evidence_audit(runtime_state=None):
    return evidence.evidence_audit(runtime_state)


def format_memory_engine_snapshot(snapshot):
    snapshot = snapshot or {}
    storage = snapshot.get("storage") or {}
    preview = snapshot.get("context_preview") or {}
    focus = snapshot.get("focus_segments") or {}
    wm = snapshot.get("working_memory") or {}
    ltm = snapshot.get("ltm") or {}
    formation_state = snapshot.get("formation") or {}
    lines = [
        "Memory Engine observer snapshot:",
        f"api_version: {(snapshot.get('observer') or {}).get('api_version', OBSERVER_API_VERSION)}",
        f"slot_count: {storage.get('slot_count', 0)}",
        f"event_count: {storage.get('event_count', 0)}",
        f"artifact_count: {storage.get('artifact_count', 0)}",
        f"context_preview_ok: {str(bool(preview.get('ok'))).lower()}",
        f"total_estimated_tokens: {preview.get('total_estimated_tokens', 'unavailable')}",
        f"stm_selected_blocks: {len(((preview.get('short_term_memory') or {}).get('selected_blocks') or []))}",
        f"stm_omitted_blocks: {len(((preview.get('short_term_memory') or {}).get('omitted_blocks') or []))}",
        f"wm_selected_count: {(preview.get('working_memory') or {}).get('selected_count', 0)}",
        f"wm_projection_mode: {(preview.get('working_memory') or {}).get('projection_mode', 'legacy')}",
        f"wm_metadata_tokens: {(preview.get('working_memory') or {}).get('metadata_tokens', 0)}",
        f"wm_active_detail_tokens: {(preview.get('working_memory') or {}).get('active_detail_tokens', 0)}",
        f"wm_unowned_detail_tokens: {(preview.get('working_memory') or {}).get('unowned_detail_tokens', 0)}",
        f"wm_resident_slot_count: {(preview.get('working_memory') or {}).get('resident_slot_count', 0)}",
        f"wm_resident_raw_token_count: {(preview.get('working_memory') or {}).get('resident_raw_token_count', 0)}",
        f"wm_sum_event_count: {wm.get('sum_event_count', 0)}",
        f"focus_segment_count: {focus.get('segment_count', 0)}",
        f"focus_live_assignment_count: {(focus.get('assignment_health') or {}).get('live_assignment_count', 0)}",
        f"focus_retired_assignment_count: {(focus.get('assignment_health') or {}).get('retired_assignment_count', 0)}",
        f"focus_unassigned_count: {(focus.get('assignment_health') or {}).get('unassigned_block_count', 0)}",
        f"focus_last_reconciliation_reason: {(focus.get('last_focus_residency_reconciliation_result') or {}).get('reason', '')}",
        f"focus_last_restored_slot_count: {(focus.get('last_focus_residency_reconciliation_result') or {}).get('restored_slot_count', 0)}",
        f"focus_last_retirement_reason: {(focus.get('last_focus_range_retirement_result') or {}).get('reason', '')}",
        f"focus_last_retired_slot_count: {(focus.get('last_focus_range_retirement_result') or {}).get('retired_slot_count', 0)}",
        f"focus_last_detector_reason_code: {focus.get('last_detector_reason_code', '')}",
        f"focus_consistency_status: {(focus.get('focus_consistency_report') or {}).get('status', 'not_run')}",
        f"ltm_status: {ltm.get('status', 'unavailable')}",
        f"ltm_object_count: {ltm.get('object_count', 0)}",
        f"ltm_link_count: {ltm.get('link_count', 0)}",
        f"formation_status: {formation_state.get('status', 'not_initialized')}",
        f"formation_work_items: {formation_state.get('work_item_count', 0)}",
        f"formation_failed: {formation_state.get('failed_count', 0)}",
        f"memory_push_requests: {(snapshot.get('memory_push') or {}).get('memory_push_request_count', 0)}",
        f"evidence_audit_count: {len(snapshot.get('evidence_audit') or [])}",
    ]
    return "\n".join(lines)


def _runtime_copy(runtime_state):
    if not isinstance(runtime_state, dict):
        return {}
    try:
        return copy.deepcopy(runtime_state)
    except Exception:
        return dict(runtime_state)


def _read_connection():
    db_path = event_stream.event_stream_db_path()
    if not db_path.exists():
        return _NullConnection()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


class _NullConnection:
    def __enter__(self):
        return None

    def __exit__(self, *_args):
        return False


def _has_table(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _json_loads(value, default):
    try:
        data = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


def _json_state(connection, table, key, default=None):
    if connection is None or not _has_table(connection, table):
        return default if default is not None else {}
    row = connection.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default if default is not None else {}
    return _json_loads(row["value"], default if default is not None else {})


def _working_memory_persistent_state():
    state = {
        "status_counts": {},
        "processed_slot_checkpoint": None,
        "last_result": {},
    }
    with _read_connection() as connection:
        if connection is None:
            return state
        if _has_table(connection, "working_memory_slot_state"):
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM working_memory_slot_state GROUP BY status"
            ).fetchall()
            state["status_counts"] = {row["status"]: int(row["count"]) for row in rows}
            row = connection.execute("SELECT MAX(slot_seq) AS slot_seq FROM working_memory_slot_state").fetchone()
            state["processed_slot_checkpoint"] = row["slot_seq"] if row else None
        state["last_result"] = _json_state(connection, "working_memory_state", "last_result", {})
    return state


def _focus_segments_snapshot():
    snapshot = {
        "active": False,
        "segment_count": 0,
        "counts": {"active": 0, "inactive": 0, "resolved": 0},
        "segments": [],
        "assignment_health": {},
        "last_detector_decision": {},
        "last_detector_reason_code": "",
        "last_focus_prepare_result": {},
        "last_segment_ingest_result": {},
        "last_full_maintainer_result": {},
        "last_focus_residency_reconciliation_result": {},
        "last_focus_range_retirement_result": {},
        "focus_consistency_report": {},
    }
    with _read_connection() as connection:
        if connection is None:
            return snapshot
        if _has_table(connection, "focus_segments"):
            rows = connection.execute(
                """
                SELECT segment_id, title, summary, status, slot_ranges, created_at, updated_at, last_touched_at
                FROM focus_segments
                ORDER BY created_at ASC
                """
            ).fetchall()
            segments = []
            for row in rows:
                status = row["status"]
                if status in snapshot["counts"]:
                    snapshot["counts"][status] += 1
                segments.append(
                    {
                        "segment_id": row["segment_id"],
                        "title": row["title"],
                        "summary": row["summary"],
                        "status": status,
                        "slot_ranges": _json_loads(row["slot_ranges"], []),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "last_touched_at": row["last_touched_at"],
                    }
                )
            snapshot["segments"] = segments
            snapshot["segment_count"] = len(segments)
            snapshot["active"] = bool(segments)
        snapshot["assignment_health"] = _assignment_health(connection)
        snapshot["last_detector_decision"] = _json_state(connection, "focus_segment_state", "last_detector_decision", {})
        snapshot["last_detector_reason_code"] = detector_reason_code(snapshot["last_detector_decision"])
        snapshot["last_focus_prepare_result"] = _json_state(connection, "focus_segment_state", "last_focus_prepare_result", {})
        snapshot["last_segment_ingest_result"] = _json_state(connection, "focus_segment_state", "last_segment_ingest_result", {})
        snapshot["last_full_maintainer_result"] = _json_state(connection, "focus_segment_state", "last_full_maintainer_result", {})
        snapshot["last_focus_residency_reconciliation_result"] = _json_state(
            connection,
            "focus_segment_state",
            "last_focus_residency_reconciliation_result",
            {},
        )
        snapshot["last_focus_range_retirement_result"] = _json_state(
            connection, "focus_segment_state", "last_focus_range_retirement_result", {}
        )
        snapshot["focus_consistency_report"] = _json_state(
            connection,
            "focus_segment_state",
            "last_consistency_report",
            {},
        )
    return snapshot


def _slot_projection_snapshot():
    slots = []
    with _read_connection() as connection:
        if connection is None or not _has_table(connection, "slots") or not _has_table(connection, "events"):
            return slots
        rows = connection.execute(
            """
            SELECT
                slots.slot_seq,
                slots.source,
                slots.active_event_id,
                slots.original_event_id,
                active_events.kind AS active_kind,
                active_events.content_ref AS active_content_ref,
                active_events.token_count AS active_token_count,
                original_events.kind AS original_kind,
                original_events.content_ref AS original_content_ref,
                original_events.token_count AS original_token_count
            FROM slots
            JOIN events AS active_events ON active_events.event_id = slots.active_event_id
            JOIN events AS original_events ON original_events.event_id = slots.original_event_id
            ORDER BY slots.slot_seq ASC
            """
        ).fetchall()
        for row in rows:
            active_event_id = row["active_event_id"]
            original_event_id = row["original_event_id"]
            slots.append(
                {
                    "slot_seq": int(row["slot_seq"]),
                    "source": row["source"],
                    "active_event_id": active_event_id,
                    "original_event_id": original_event_id,
                    "active_event_id_equals_original": active_event_id == original_event_id,
                    "active_kind": row["active_kind"],
                    "original_kind": row["original_kind"],
                    "active_content_ref": row["active_content_ref"],
                    "original_content_ref": row["original_content_ref"],
                    "active_token_count": int(row["active_token_count"] or 0),
                    "original_token_count": int(row["original_token_count"] or 0),
                }
            )
    return slots


def _assignment_health(connection):
    if connection is None or not _has_table(connection, "focus_assignments"):
        return {
            "assignment_count": 0,
            "live_assignment_count": 0,
            "retired_assignment_count": 0,
            "assigned_count": 0,
            "unassigned_block_count": 0,
            "low_confidence_assignment_count": 0,
        }
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(focus_assignments)").fetchall()
    }
    live_clause = "lifecycle_status = 'live'" if "lifecycle_status" in columns else "1 = 1"
    return {
        "assignment_count": _count_where(connection, "focus_assignments", "1 = 1"),
        "live_assignment_count": _count_where(connection, "focus_assignments", live_clause),
        "retired_assignment_count": (
            _count_where(connection, "focus_assignments", "lifecycle_status = 'retired'")
            if "lifecycle_status" in columns
            else 0
        ),
        "assigned_count": _count_where(connection, "focus_assignments", f"status = 'assigned' AND {live_clause}"),
        "unassigned_block_count": _count_where(connection, "focus_assignments", f"status = 'unassigned' AND {live_clause}"),
        "low_confidence_assignment_count": _count_where(
            connection,
            "focus_assignments",
            f"status = 'assigned' AND confidence < 0.75 AND {live_clause}",
        ),
    }


def _count_where(connection, table, where):
    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}").fetchone()
    return int(row["count"] or 0) if row else 0


def _budget_stabilizer_state():
    return _json_state_from_table("context_budget_state", "budget_stabilizer", {})


def _read_only_wm_ingress_watermark(runtime_state=None):
    values = [-1]
    if isinstance(runtime_state, dict):
        for key in ("wm_ingress_watermark_slot_seq", "staged_wm_ingress_watermark_slot_seq"):
            try:
                values.append(int(runtime_state.get(key, -1)))
            except (TypeError, ValueError):
                pass
    persisted = _json_state_from_table("event_stream_state", "wm_ingress_watermark_slot_seq", -1)
    try:
        values.append(int(persisted))
    except (TypeError, ValueError):
        pass
    return max(values)


def _observer_pressure(messages, runtime_state):
    existing = (runtime_state or {}).get("context_pressure")
    if isinstance(existing, dict) and existing.get("budgets"):
        return copy.deepcopy(existing)
    signals = collect_context_pressure_signals(messages, runtime_state or {})
    return compute_dynamic_context_budget(signals, ranges=_read_only_budget_ranges())


def _read_only_budget_ranges():
    defaults = {
        "stm_min": STM_MIN_TOKENS,
        "stm_max": STM_MAX_TOKENS,
        "wm_min": WM_MIN_TOKENS,
        "wm_max": WM_MAX_TOKENS,
    }
    override = _json_state_from_table("context_budget_state", "context_budget_max_override", {})
    stm_max = int(override.get("stm") or defaults["stm_max"]) if isinstance(override, dict) else defaults["stm_max"]
    wm_max = int(override.get("wm") or defaults["wm_max"]) if isinstance(override, dict) else defaults["wm_max"]
    return {
        "stm_min": min(defaults["stm_min"], stm_max),
        "stm_max": stm_max,
        "wm_min": min(defaults["wm_min"], wm_max),
        "wm_max": wm_max,
        "stm_max_override": override.get("stm") if isinstance(override, dict) else None,
        "wm_max_override": override.get("wm") if isinstance(override, dict) else None,
        "manual_min": 2000,
        "manual_max": 200000,
    }


def _json_state_from_table(table, key, default):
    with _read_connection() as connection:
        return _json_state(connection, table, key, default)


def _slot_range(records):
    if not records:
        return ""
    return f"{records[0]['slot_seq']}..{records[-1]['slot_seq']}"


def _block_metadata(block):
    return {
        "kind": block.get("kind", "interaction"),
        "start_index": int(block.get("start_index", 0)),
        "end_index": int(block.get("end_index", 0)),
        "message_count": int(block.get("message_count", 0)),
        "token_count": int(block.get("token_count", 0)),
    }
