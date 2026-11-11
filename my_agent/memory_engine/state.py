import json
import sqlite3

from persistence.session import get_project_info

from . import event_stream
from .stm_projection import STM_TOKEN_BUDGET, build_stm_projection
from .wm.ingress import (
    RUNTIME_STAGED_WATERMARK_KEY,
    RUNTIME_WATERMARK_KEY,
    WATERMARK_KEY,
    effective_wm_ingress_watermark,
)
from .focus.common import detector_reason_code


WM_TOKEN_BUDGET_FALLBACK = 6000


def build_memory_engine_state(messages=None, runtime_state=None):
    project = get_project_info()
    db_path = event_stream.event_stream_db_path()
    storage = _storage_state(db_path)
    pressure = _pressure_state(runtime_state)
    budgets = pressure.get("budgets") or {}
    stm_budget = int(budgets.get("stm") or STM_TOKEN_BUDGET)
    wm_budget = int(budgets.get("wm") or WM_TOKEN_BUDGET_FALLBACK)
    planned = {}
    try:
        from .context_builder import context_status

        planned = context_status(messages or [], runtime_state=runtime_state)
        stm_budget = int(planned.get("stm_budget") or stm_budget)
        wm_budget = int(planned.get("wm_budget") or wm_budget)
    except Exception:
        planned = {}
    watermark = _watermark_state(db_path, runtime_state)
    stm = _stm_state(messages, stm_budget, watermark["effective_watermark"])
    working_memory = _working_memory_state(db_path, stm, wm_budget)
    focus = _focus_state(db_path)
    return {
        "active_conversation_id": project.get("active_conversation_id"),
        "storage": storage,
        "context": {
            "context_order": ["system", "working_memory", "long_term_memory", "short_term_memory"],
            "budget_source": planned.get("budget_source") or pressure.get("budget_source") or "unavailable",
            "stm_budget": stm_budget,
            "stm_raw_budget": planned.get("stm_raw_budget", budgets.get("stm_raw", budgets.get("stm", "unavailable"))),
            "stm_budget_range": _range_text(planned.get("stm_min", budgets.get("stm_min")), planned.get("stm_max", budgets.get("stm_max"))),
            "wm_budget": wm_budget,
            "wm_raw_budget": planned.get("wm_raw_budget", budgets.get("wm_raw", budgets.get("wm", "unavailable"))),
            "wm_budget_range": _range_text(planned.get("wm_min", budgets.get("wm_min")), planned.get("wm_max", budgets.get("wm_max"))),
            "total_estimated_context_tokens": "unavailable",
            "capacity": planned.get("capacity") or pressure.get("capacity") or {},
            "failure_count": planned.get("failure_count", (pressure.get("failure") or {}).get("continuous_session_context_budget_failure_count", 0)),
            "f_failure": planned.get("f_failure", (pressure.get("failure") or {}).get("f_failure", 0)),
        },
        "stm": stm,
        "working_memory": working_memory,
        "focus_segments": focus,
        "wm_ingress": watermark,
    }


def format_memory_engine_state(state):
    state = state or {}
    storage = state.get("storage") or {}
    context = state.get("context") or {}
    stm = state.get("stm") or {}
    wm = state.get("working_memory") or {}
    focus = state.get("focus_segments") or {}
    watermark = state.get("wm_ingress") or {}
    lines = [
        "Sema memory state:",
        f"- active_conversation: {state.get('active_conversation_id') or '(none)'}",
        "- Event Stream:",
        f"  memory_root: {storage.get('memory_root', '')}",
        f"  event_stream_dir: {storage.get('event_stream_dir', '')}",
        f"  db_path: {storage.get('db_path', '')}",
        f"  slot_count: {storage.get('slot_count', 0)}",
        f"  event_count: {storage.get('event_count', 0)}",
        f"  raw_event_count: {storage.get('raw_event_count', 0)}",
        f"  ref_event_count: {storage.get('ref_event_count', 0)}",
        f"  sum_event_count: {storage.get('sum_event_count', 0)}",
        f"  artifact_count: {storage.get('artifact_count', 0)}",
        "- Context Builder:",
        f"  context_order: {' -> '.join(context.get('context_order') or [])}",
        f"  budget_source: {context.get('budget_source', 'unavailable')}",
        f"  stm_budget: {context.get('stm_budget', 0)}",
        f"  stm_raw_budget: {context.get('stm_raw_budget', 'unavailable')}",
        f"  stm_budget_range: {context.get('stm_budget_range', 'unavailable')}",
        f"  wm_budget: {context.get('wm_budget', 0)}",
        f"  wm_raw_budget: {context.get('wm_raw_budget', 'unavailable')}",
        f"  wm_budget_range: {context.get('wm_budget_range', 'unavailable')}",
        f"  total_estimated_context_tokens: {context.get('total_estimated_context_tokens', 'unavailable')}",
        f"  failure_count: {context.get('failure_count', 0)}",
        f"  f_failure: {context.get('f_failure', 0)}",
        f"  capacity_recovery_action: {(context.get('capacity') or {}).get('capacity_recovery_action', 'none')}",
        "- Short-Term Memory:",
        f"  selected_unit_count: {stm.get('selected_unit_count', 'unavailable')}",
        f"  omitted_unit_count: {stm.get('omitted_unit_count', 'unavailable')}",
        f"  selected_tokens: {stm.get('selected_tokens', 'unavailable')}",
        f"  latest_unit_tokens: {stm.get('latest_unit_tokens', 'unavailable')}",
        f"  wm_ingress_watermark: {watermark.get('effective_watermark', -1)}",
        f"  watermark_locked_omitted_unit_count: {stm.get('watermark_locked_omitted_unit_count', 'unavailable')}",
        "- Working Memory:",
        f"  active_raw_count: {wm.get('active_raw_count', 0)}",
        f"  active_ref_count: {wm.get('active_ref_count', 0)}",
        f"  sum_event_count: {wm.get('sum_event_count', 0)}",
        f"  active_raw_tokens: {wm.get('active_raw_tokens', 0)}",
        f"  active_sum_tokens: {wm.get('active_sum_tokens', 0)}",
        f"  candidate_count: {wm.get('candidate_count', 'unavailable')}",
        "- Focus Segments:",
        f"  active: {str(bool(focus.get('active'))).lower()}",
        f"  segment_count: {focus.get('segment_count', 0)}",
        f"  active_count: {focus.get('active_count', 0)}",
        f"  inactive_count: {focus.get('inactive_count', 0)}",
        f"  resolved_count: {focus.get('resolved_count', 0)}",
        f"  live_assignment_count: {focus.get('live_assignment_count', 0)}",
        f"  retired_assignment_count: {focus.get('retired_assignment_count', 0)}",
        f"  unassigned_assignment_count: {focus.get('unassigned_assignment_count', 0)}",
        f"  low_confidence_assignment_count: {focus.get('low_confidence_assignment_count', 0)}",
        f"  last_detector_target_kind: {focus.get('last_detector_target_kind', '')}",
        f"  last_ingest_assigned_count: {focus.get('last_ingest_assigned_count', '')}",
        f"  last_full_maintainer_triggered: {focus.get('last_full_maintainer_triggered', '')}",
    ]
    return "\n".join(lines)


def _storage_state(db_path):
    base = {
        "memory_root": str(event_stream.memory_root()),
        "event_stream_dir": str(event_stream.event_stream_dir()),
        "db_path": str(db_path),
        "slot_count": 0,
        "event_count": 0,
        "raw_event_count": 0,
        "ref_event_count": 0,
        "sum_event_count": 0,
        "artifact_count": 0,
    }
    with _read_connection(db_path) as connection:
        if connection is None:
            return base
        base["slot_count"] = _count(connection, "slots")
        base["event_count"] = _count(connection, "events")
        base["artifact_count"] = _count(connection, "artifacts")
        for kind, count in _event_kind_counts(connection).items():
            base[f"{kind}_event_count"] = count
    return base


def _pressure_state(runtime_state):
    status = (runtime_state or {}).get("context_pressure")
    return status if isinstance(status, dict) else {}


def _stm_state(messages, budget, watermark):
    try:
        projection = build_stm_projection(messages or [], budget=budget, wm_ingress_watermark=watermark)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "selected_unit_count": len(projection.get("selected_units") or []),
        "omitted_unit_count": len(projection.get("omitted_units") or []),
        "selected_tokens": projection.get("selected_tokens", 0),
        "latest_unit_tokens": projection.get("latest_unit_tokens", 0),
        "watermark_locked_omitted_unit_count": projection.get("watermark_locked_omitted_unit_count", 0),
        "omitted_units": projection.get("omitted_units") or [],
    }


def _working_memory_state(db_path, stm, budget):
    state = {
        "budget": budget,
        "active_raw_count": 0,
        "active_ref_count": 0,
        "sum_event_count": 0,
        "active_raw_tokens": 0,
        "active_ref_tokens": 0,
        "active_sum_tokens": 0,
        "candidate_count": 0,
    }
    try:
        from .wm.pins import pin_status

        state["current_turn_pin"] = pin_status()
    except Exception:
        state["current_turn_pin"] = {"active": False}
    omitted_ranges = [(int(unit["start_index"]), int(unit["end_index"])) for unit in stm.get("omitted_units") or []]
    with _read_connection(db_path) as connection:
        if connection is None or not _has_table(connection, "slots") or not _has_table(connection, "events"):
            return state
        rows = connection.execute(
            """
            SELECT slots.slot_seq, slots.source, events.kind, events.token_count
            FROM slots
            JOIN events ON events.event_id = slots.active_event_id
            WHERE slots.source != 'system'
            """
        ).fetchall()
        for row in rows:
            kind = row["kind"]
            tokens = int(row["token_count"] or 0)
            if kind == "raw":
                state["active_raw_count"] += 1
                state["active_raw_tokens"] += tokens
            elif kind == "ref":
                state["active_ref_count"] += 1
                state["active_ref_tokens"] += tokens
            elif kind == "sum":
                state["sum_event_count"] += 1
                state["active_sum_tokens"] += tokens
            if kind in {"raw", "ref"} and tokens > 400 and _slot_in_ranges(int(row["slot_seq"]), omitted_ranges):
                state["candidate_count"] += 1
    return state


def _focus_state(db_path):
    state = {
        "active": False,
        "segment_count": 0,
        "active_count": 0,
        "inactive_count": 0,
        "resolved_count": 0,
        "unassigned_assignment_count": 0,
        "low_confidence_assignment_count": 0,
        "live_assignment_count": 0,
        "retired_assignment_count": 0,
        "last_detector_reason_code": "",
    }
    with _read_connection(db_path) as connection:
        if connection is None:
            return state
        if _has_table(connection, "focus_segments"):
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM focus_segments GROUP BY status").fetchall()
            for row in rows:
                state[f"{row['status']}_count"] = int(row["count"])
            state["segment_count"] = sum(int(row["count"]) for row in rows)
            state["active"] = state["segment_count"] > 0
        if _has_table(connection, "focus_assignments"):
            assignment_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(focus_assignments)").fetchall()
            }
            live_clause = "lifecycle_status = 'live'" if "lifecycle_status" in assignment_columns else "1 = 1"
            state["live_assignment_count"] = _scalar(
                connection,
                f"SELECT COUNT(*) AS count FROM focus_assignments WHERE {live_clause}",
            )
            if "lifecycle_status" in assignment_columns:
                state["retired_assignment_count"] = _scalar(
                    connection,
                    "SELECT COUNT(*) AS count FROM focus_assignments WHERE lifecycle_status = 'retired'",
                )
            state["unassigned_assignment_count"] = _scalar(
                connection,
                f"SELECT COUNT(*) AS count FROM focus_assignments WHERE status = 'unassigned' AND {live_clause}",
            )
            state["low_confidence_assignment_count"] = _scalar(
                connection,
                f"SELECT COUNT(*) AS count FROM focus_assignments WHERE status = 'assigned' AND confidence < 0.75 AND {live_clause}",
            )
        if _has_table(connection, "focus_segment_state"):
            detector = _json_state(connection, "last_detector_decision")
            ingest = _json_state(connection, "last_segment_ingest_result")
            full = _json_state(connection, "last_full_maintainer_result")
            state["last_detector_target_kind"] = detector.get("target_kind", "")
            state["last_detector_reason_code"] = detector_reason_code(detector)
            state["last_ingest_assigned_count"] = ingest.get("assigned_count", "")
            state["last_full_maintainer_triggered"] = full.get("triggered", "")
    return state


def _watermark_state(db_path, runtime_state):
    persisted = -1
    runtime = -1
    staged = -1
    with _read_connection(db_path) as connection:
        if connection is not None and _has_table(connection, "event_stream_state"):
            value = _state_value(connection, WATERMARK_KEY)
            try:
                persisted = int(json.loads(value)) if value is not None else -1
            except (TypeError, ValueError, json.JSONDecodeError):
                persisted = -1
    if isinstance(runtime_state, dict):
        try:
            runtime = int(runtime_state.get(RUNTIME_WATERMARK_KEY, -1))
        except (TypeError, ValueError):
            runtime = -1
        try:
            staged = int(runtime_state.get(RUNTIME_STAGED_WATERMARK_KEY, -1))
        except (TypeError, ValueError):
            staged = -1
    return {
        "persisted_watermark": persisted,
        "runtime_watermark": runtime,
        "staged_watermark": staged,
        "effective_watermark": effective_wm_ingress_watermark(runtime_state),
    }


def _read_connection(db_path):
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


def _count(connection, table):
    if not _has_table(connection, table):
        return 0
    return _scalar(connection, f"SELECT COUNT(*) AS count FROM {table}")


def _scalar(connection, query):
    row = connection.execute(query).fetchone()
    return int(row["count"] or 0) if row else 0


def _event_kind_counts(connection):
    if not _has_table(connection, "events"):
        return {}
    rows = connection.execute("SELECT kind, COUNT(*) AS count FROM events GROUP BY kind").fetchall()
    return {row["kind"]: int(row["count"]) for row in rows}


def _state_value(connection, key):
    row = connection.execute(
        "SELECT value FROM event_stream_state WHERE key = ?",
        (key,),
    ).fetchone()
    return row["value"] if row else None


def _json_state(connection, key):
    row = connection.execute(
        "SELECT value FROM focus_segment_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return {}
    try:
        data = json.loads(row["value"] or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _slot_in_ranges(slot_seq, ranges):
    return any(start <= slot_seq <= end for start, end in ranges)


def _range_text(minimum, maximum):
    if minimum is None or maximum is None:
        return "unavailable"
    return f"{minimum}..{maximum}"
