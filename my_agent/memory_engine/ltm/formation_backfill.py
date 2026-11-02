"""Formation Backfill scheduling for Event Stream projects.

The backfill deliberately reuses Formation WorkItems and the existing
Formation Agent path. It only discovers history strictly older than the current buffer
and processes one batch per invocation.
"""

import json
import sqlite3
import threading
from persistence.trajectory import complete_trajectory, fail_trajectory, record_event, start_trajectory

from .. import event_stream
from . import formation, ltm_store, meta_memory
from ..focus import store as focus_store



STATE_STATUS = "historical_backfill_status"
STATE_CURSOR = "historical_backfill_cursor_slot"
STATE_END = "historical_backfill_end_slot"
STATE_ERROR = "historical_backfill_last_error"
STATE_UPDATED = "historical_backfill_updated_at"
_RUN_LOCK = threading.Lock()


def _now():
    return formation._now()


def _state(connection, key, default=None):
    return formation._state_get(connection, key, default)


def _set_state(connection, key, value):
    formation._state_set(connection, key, value)


def _known_provenance_slots():
    path = ltm_store.ltm_db_path()
    if not path.exists():
        return set()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_links'"
        ).fetchone()
        if not table:
            return set()
        rows = connection.execute(
            """
            SELECT slot_seq_start, slot_seq_end
            FROM memory_links
            WHERE relation_type = 'sourced_from'
              AND target_kind = 'event_slot'
            """
        ).fetchall()
        if ltm_store._table_exists(connection, "ltm_forgetting_batches"):
            for (result_json,) in connection.execute("SELECT result_json FROM ltm_forgetting_batches"):
                rows.extend(json.loads(result_json).get("provenance_ranges", []))
        slots = set()
        for start, end in rows:
            if start is None or end is None:
                continue
            slots.update(range(int(start), int(end) + 1))
        return slots
    finally:
        connection.close()


def _current_buffer(messages, runtime_state):
    slots, budget = formation._buffer_slots(messages, runtime_state)
    return set(slots), int(budget or 0)


def _history_boundary(records, buffer_slots):
    if not buffer_slots:
        return None
    ordered = sorted(int(slot) for slot in records if int(slot) < min(buffer_slots))
    if not ordered:
        return -1
    return ordered[-1]


def _historical_slots(records, buffer_slots, end_slot):
    if not buffer_slots or end_slot is None:
        return set()
    return {
        int(slot)
        for slot in records
        if int(slot) <= int(end_slot) and int(slot) < min(buffer_slots)
    }


def _existing_coverage(rows):
    coverage = {}
    for row in rows:
        key = row.get("segment_id") or "__unassigned__"
        coverage.setdefault(key, set()).update(formation._slots_from_ranges(formation._row_ranges(row)))
    return coverage


def _discover(messages, runtime_state, connection):
    buffer_slots, wm_budget = _current_buffer(messages, runtime_state)
    records_by_slot = formation._active_records_by_slot()
    all_slots = set(records_by_slot)
    all_slots = {slot for slot in all_slots if records_by_slot[slot].get("source") != "system"}
    saved_end = _state(connection, STATE_END)
    if saved_end is None:
        end_slot = _history_boundary(all_slots, buffer_slots)
        if end_slot is not None:
            _set_state(connection, STATE_END, end_slot)
    else:
        end_slot = int(saved_end)
    historical_slots = _historical_slots(all_slots, buffer_slots, end_slot)
    from runtime.setup_capabilities import skipped_slots
    historical_slots -= skipped_slots(connection)
    historical_slots -= _known_provenance_slots()
    rows = formation._work_item_rows(connection)
    coverage = _existing_coverage(rows)
    segments = []
    focus_store.ensure_schema(connection)
    segments = focus_store.load_segments(connection)
    covered_by_segment = set()
    created_count = 0

    for segment in segments:
        segment_id = segment["segment_id"]
        segment_slots = formation._slots_from_ranges(segment.get("slot_ranges", []))
        pending = (segment_slots & historical_slots) - coverage.get(segment_id, set())
        if not pending:
            continue
        covered_by_segment.update(pending)
        for chunk in formation._split_slots(pending, wm_budget, records_by_slot):
            _item_id, created = formation._create_work_item(
                connection,
                "segment",
                segment,
                formation._ranges_from_slots(chunk),
                formation.FORMATION_PRIORITIES.get(segment.get("status"), 2),
            )
            created_count += int(bool(created))

    unassigned = historical_slots - covered_by_segment - coverage.get("__unassigned__", set())
    for chunk in formation._split_slots(unassigned, wm_budget, records_by_slot):
        _item_id, created = formation._create_work_item(
            connection,
            "unassigned",
            None,
            formation._ranges_from_slots(chunk),
            formation.FORMATION_PRIORITIES["unassigned"],
        )
        created_count += int(bool(created))
    return {
        "historical_end_slot": end_slot if end_slot is not None else -1,
        "buffer_slot_count": len(buffer_slots),
        "historical_slot_count": len(historical_slots),
        "created_count": created_count,
        "wm_budget": wm_budget,
    }


def _eligible_rows(connection, end_slot, buffer_slots):
    if not buffer_slots:
        return []
    rows = formation._work_item_rows(connection, statuses=("pending", "failed"))
    result = []
    for row in rows:
        slots = formation._slots_from_ranges(formation._row_ranges(row))
        if slots and max(slots) <= int(end_slot) and max(slots) < min(buffer_slots):
            result.append(row)
    return result


def _run_formation_backfill_impl(messages=None, runtime_state=None):
    if not _RUN_LOCK.acquire(blocking=False):
        return {
            "triggered": False,
            "reason": "historical_backfill_already_running",
            "status": "running",
            "processed_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "created_count": 0,
            "cursor_slot_seq": -1,
            "historical_backfill_end_slot": -1,
            "error": "",
        }
    messages = messages or event_stream.load_messages()
    result = {
        "triggered": False,
        "reason": "no_historical_work_items",
        "status": "completed",
        "processed_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "created_count": 0,
        "cursor_slot_seq": -1,
        "historical_backfill_end_slot": -1,
        "error": "",
    }
    try:
        with event_stream.ensure_event_stream() as connection:
            formation._ensure_tables(connection)
            current_status = _state(connection, STATE_STATUS)
            if current_status == "running":
                _set_state(connection, STATE_STATUS, "pending")
                current_status = "pending"
            _set_state(connection, STATE_STATUS, "running")
            _set_state(connection, STATE_UPDATED, _now())
            connection.commit()

            discovery = _discover(messages, runtime_state, connection)
            buffer_slots, _budget = _current_buffer(messages, runtime_state)
            if not buffer_slots:
                _set_state(connection, STATE_STATUS, "pending")
                _set_state(connection, STATE_ERROR, "")
                connection.commit()
                result.update(status="deferred", reason="no_stable_formation_boundary",
                              historical_backfill_end_slot=discovery["historical_end_slot"])
                return result
            rows = _eligible_rows(connection, discovery["historical_end_slot"], buffer_slots)
            result.update(
                {
                    "historical_backfill_end_slot": discovery["historical_end_slot"],
                    "cursor_slot_seq": int(_state(connection, STATE_CURSOR, -1) or -1),
                    "created_count": discovery["created_count"],
                }
            )
            if not rows:
                _set_state(connection, STATE_STATUS, "completed")
                _set_state(connection, STATE_ERROR, "")
                _set_state(connection, STATE_UPDATED, _now())
                connection.commit()
                return result
            item_id = rows[0]["work_item_id"]

        bootstrap = meta_memory.bootstrap_system_tags()
        if bootstrap.get("status") == "failed":
            raise RuntimeError(bootstrap.get("error") or "Meta-Memory Bootstrap failed.")
        system_root_tags = meta_memory.system_owned_tags()
        processed = formation._process_item(item_id, system_root_tags, runtime_state=runtime_state)
        slots = formation._slots_from_ranges(formation._row_ranges(rows[0]))
        cursor = max(slots) if slots else int(result["cursor_slot_seq"])
        with event_stream.ensure_event_stream() as connection:
            formation._ensure_tables(connection)
            buffer_slots, _budget = _current_buffer(messages, runtime_state)
            remaining = _eligible_rows(connection, result["historical_backfill_end_slot"], buffer_slots)
            status = "pending" if remaining else "completed"
            _set_state(connection, STATE_STATUS, status)
            _set_state(connection, STATE_CURSOR, cursor)
            _set_state(connection, STATE_ERROR, processed.get("error") or "")
            _set_state(connection, STATE_UPDATED, _now())
            connection.commit()
        result.update(
            {
                "triggered": True,
                "reason": "processed_historical_batch",
                "status": status,
                "processed_count": 1,
                "completed_count": int(processed.get("status") == "completed"),
                "failed_count": int(processed.get("status") == "failed"),
                "cursor_slot_seq": cursor,
                "error": processed.get("error") or "",
            }
        )
        return result
    except Exception as error:
        result.update({"reason": "historical_backfill_failed", "status": "pending", "error": str(error)})
        try:
            with event_stream.ensure_event_stream() as connection:
                formation._ensure_tables(connection)
                _set_state(connection, STATE_STATUS, "pending")
                _set_state(connection, STATE_ERROR, str(error))
                _set_state(connection, STATE_UPDATED, _now())
                connection.commit()
        except Exception:
            pass
        return result
    finally:
        _RUN_LOCK.release()


from .storage_gate import memory_task


@memory_task
def run_formation_backfill(messages=None, runtime_state=None):
    from runtime.setup_capabilities import ltm_enabled, restoration_pending
    if not ltm_enabled():
        return {"triggered": False, "status": "unavailable", "reason": "LTM is unconfigured"}
    if restoration_pending():
        return {"triggered": False, "status": "deferred", "reason": "Waiting for a valid Formation window after setup"}
    caller = (runtime_state or {}).get("trajectory_context")
    context = start_trajectory("formation", caller=caller, metadata={"formation_mode": "backfill"})
    if isinstance(runtime_state, dict):
        runtime_state["_formation_trajectory_context"] = context
    try:
        result = _run_formation_backfill_impl(messages, runtime_state)
        failed = bool(result.get("error"))
        record_event(context, "formation.completed", status="failed" if failed else "completed", summary=result, artifacts={"result": result})
        if failed:
            fail_trajectory(context, RuntimeError(result["error"]), metadata={"formation_mode": "backfill"})
        else:
            complete_trajectory(context, metadata={"formation_mode": "backfill"})
        return result
    except Exception as error:
        fail_trajectory(context, error, metadata={"formation_mode": "backfill"})
        raise
    finally:
        if isinstance(runtime_state, dict):
            runtime_state.pop("_formation_trajectory_context", None)


def formation_backfill_status():
    result = {
        "status": "not_initialized",
        "cursor_slot_seq": -1,
        "historical_backfill_end_slot": -1,
        "last_error": "",
        "updated_at": "",
    }
    path = event_stream.event_stream_db_path()
    if not path.exists():
        return result
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "formation_state" not in tables:
            return result
        values = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT key, value FROM formation_state WHERE key LIKE 'historical_backfill_%'"
            )
        }
    result["status"] = values.get(STATE_STATUS, "not_started")
    result["cursor_slot_seq"] = int(values.get(STATE_CURSOR, -1) or -1)
    result["historical_backfill_end_slot"] = int(values.get(STATE_END, -1) or -1)
    result["last_error"] = values.get(STATE_ERROR, "")
    result["updated_at"] = values.get(STATE_UPDATED, "")
    return result
