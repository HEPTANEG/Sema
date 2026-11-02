import json
import sqlite3
from uuid import uuid4

import memory_engine.event_stream as event_stream

from .common import (
    BOOTSTRAP_SUMMARY,
    BOOTSTRAP_TITLE,
    FOCUS_RESIDENCY_SEMANTICS_VERSION,
    FULL_MAINTAINER_LOW_CONFIDENCE_THRESHOLD,
    VALID_STATUSES,
    _now,
    _single_line,
)

from .ranges import (
    _normalize_ranges,
)

def _ensure_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS focus_segments (
            segment_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL,
            slot_ranges TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_touched_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS focus_segment_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS focus_assignments (
            assignment_id TEXT PRIMARY KEY,
            segment_id TEXT,
            slot_start INTEGER NOT NULL,
            slot_end INTEGER NOT NULL,
            slot_seqs TEXT NOT NULL,
            status TEXT NOT NULL,
            lifecycle_status TEXT NOT NULL DEFAULT 'live',
            retired_at TEXT,
            confidence REAL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(slot_start, slot_end)
        )
        """
    )
    assignment_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(focus_assignments)").fetchall()
    }
    if "lifecycle_status" not in assignment_columns:
        connection.execute(
            "ALTER TABLE focus_assignments ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'live'"
        )
    if "retired_at" not in assignment_columns:
        connection.execute("ALTER TABLE focus_assignments ADD COLUMN retired_at TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS focus_ingest_hints (
            start_slot INTEGER PRIMARY KEY,
            segment_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS focus_assignment_repairs (
            repair_id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            previous_status TEXT NOT NULL,
            previous_segment_id TEXT,
            previous_confidence REAL,
            previous_reason TEXT NOT NULL,
            target_segment_id TEXT NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_focus_assignments_segment ON focus_assignments(segment_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_focus_assignments_status ON focus_assignments(status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_focus_assignments_lifecycle "
        "ON focus_assignments(lifecycle_status, status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_focus_assignment_repairs_assignment "
        "ON focus_assignment_repairs(assignment_id, created_at)"
    )
    version_row = connection.execute(
        "SELECT value FROM focus_segment_state WHERE key = 'focus_residency_semantics_version'"
    ).fetchone()
    if version_row is None:
        legacy_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM focus_assignments AS assignments
                LEFT JOIN focus_segments AS segments ON segments.segment_id = assignments.segment_id
                WHERE assignments.lifecycle_status = 'retired'
                  AND (
                    assignments.status = 'unassigned'
                    OR segments.status IN ('active', 'inactive')
                  )
                """
            ).fetchone()["count"]
        )
        if legacy_count == 0:
            connection.execute(
                "INSERT INTO focus_segment_state(key, value) VALUES ('focus_residency_semantics_version', ?)",
                (str(FOCUS_RESIDENCY_SEMANTICS_VERSION),),
            )
    connection.commit()

def _state_get(connection, key, default=None):
    row = connection.execute(
        "SELECT value FROM focus_segment_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    return row["value"]

def _state_set(connection, key, value):
    connection.execute(
        """
        INSERT INTO focus_segment_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )

def _assignment_from_row(row):
    try:
        slot_seqs = json.loads(row["slot_seqs"] or "[]")
    except json.JSONDecodeError:
        slot_seqs = []
    keys = set(row.keys())
    return {
        "assignment_id": row["assignment_id"],
        "segment_id": row["segment_id"] or "",
        "slot_start": int(row["slot_start"]),
        "slot_end": int(row["slot_end"]),
        "slot_seqs": [int(slot) for slot in slot_seqs],
        "status": row["status"],
        "lifecycle_status": row["lifecycle_status"] if "lifecycle_status" in keys else "live",
        "retired_at": row["retired_at"] if "retired_at" in keys else None,
        "confidence": None if row["confidence"] is None else float(row["confidence"]),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

def _load_focus_ingest_hints(connection):
    rows = connection.execute(
        "SELECT start_slot, segment_id, created_at FROM focus_ingest_hints ORDER BY start_slot ASC"
    ).fetchall()
    return [
        {
            "start_slot": int(row["start_slot"]),
            "segment_id": row["segment_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]

def _record_focus_ingest_hint(connection, segment_id, start_slot):
    if start_slot is None:
        return False
    connection.execute(
        """
        INSERT INTO focus_ingest_hints (start_slot, segment_id, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(start_slot) DO UPDATE SET
            segment_id = excluded.segment_id
        """,
        (int(start_slot), str(segment_id), _now()),
    )
    return True

def _pending_focus_ingest_hint_starts(connection):
    rows = connection.execute(
        """
        SELECT hint.start_slot
        FROM focus_ingest_hints AS hint
        LEFT JOIN focus_assignments AS assignment
          ON assignment.slot_start = hint.start_slot
        WHERE assignment.assignment_id IS NULL
        """
    ).fetchall()
    return {int(row["start_slot"]) for row in rows}

def _focus_ingest_hint_for_block(connection, block):
    slot_range = block.get("slot_range") or {}
    try:
        start_slot = int(slot_range.get("start"))
    except (TypeError, ValueError):
        return None
    row = connection.execute(
        """
        SELECT hint.start_slot, hint.segment_id, hint.created_at
        FROM focus_ingest_hints AS hint
        JOIN focus_segments AS segment ON segment.segment_id = hint.segment_id
        WHERE hint.start_slot = ?
          AND segment.status IN ('active', 'inactive')
        """,
        (start_slot,),
    ).fetchone()
    if row is None:
        return None
    return {
        "start_slot": int(row["start_slot"]),
        "segment_id": row["segment_id"],
        "created_at": row["created_at"],
    }

def _record_assignment(connection, block, decision, applied):
    slot_seqs = [int(slot) for slot in block.get("slot_seqs", [])]
    if not slot_seqs:
        slot_range = block.get("slot_range") or {}
        try:
            slot_seqs = list(range(int(slot_range.get("start")), int(slot_range.get("end")) + 1))
        except (TypeError, ValueError):
            return
    slot_start = min(slot_seqs)
    slot_end = max(slot_seqs)
    assigned = bool(applied.get("assigned"))
    segment_id = applied.get("target_segment_id") if assigned else None
    status = "assigned" if assigned else "unassigned"
    confidence = decision.get("confidence")
    reason = decision.get("reason") or applied.get("reason") or ""
    timestamp = _now()
    connection.execute(
        """
        INSERT INTO focus_assignments
            (assignment_id, segment_id, slot_start, slot_end, slot_seqs, status, confidence, reason, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slot_start, slot_end) DO UPDATE SET
            segment_id = excluded.segment_id,
            slot_seqs = excluded.slot_seqs,
            status = excluded.status,
            confidence = excluded.confidence,
            reason = excluded.reason,
            updated_at = excluded.updated_at
        """,
        (
            f"asg_{uuid4().hex}",
            segment_id,
            slot_start,
            slot_end,
            json.dumps(slot_seqs, ensure_ascii=False),
            status,
            None if confidence is None else float(confidence),
            _single_line(reason),
            timestamp,
            timestamp,
        ),
    )

def _load_assignments(connection, segment_id=None, status=None, lifecycle_status=None, low_confidence=False):
    clauses = []
    args = []
    if segment_id:
        clauses.append("segment_id = ?")
        args.append(segment_id)
    if status:
        clauses.append("status = ?")
        args.append(status)
    if lifecycle_status:
        clauses.append("lifecycle_status = ?")
        args.append(lifecycle_status)
    if low_confidence:
        clauses.append(
            "status = 'assigned' AND lifecycle_status = 'live' "
            "AND confidence IS NOT NULL AND confidence < ?"
        )
        args.append(FULL_MAINTAINER_LOW_CONFIDENCE_THRESHOLD)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"""
        SELECT * FROM focus_assignments
        {where}
        ORDER BY slot_start ASC, slot_end ASC, assignment_id ASC
        """,
        tuple(args),
    ).fetchall()
    return [_assignment_from_row(row) for row in rows]

def _assignment_ranges(connection):
    rows = connection.execute("SELECT slot_start, slot_end FROM focus_assignments").fetchall()
    return [(int(row["slot_start"]), int(row["slot_end"])) for row in rows]

def _assignment_health(connection, segments=None):
    segments = segments if segments is not None else _load_segments(connection)
    rows = [_assignment_from_row(row) for row in connection.execute("SELECT * FROM focus_assignments").fetchall()]
    live = [row for row in rows if row["lifecycle_status"] == "live"]
    retired = [row for row in rows if row["lifecycle_status"] == "retired"]
    assigned = [row for row in live if row["status"] == "assigned"]
    unassigned = [row for row in live if row["status"] == "unassigned"]
    retired_assigned = [row for row in retired if row["status"] == "assigned"]
    retired_unassigned = [row for row in retired if row["status"] == "unassigned"]
    low = [
        row
        for row in assigned
        if row.get("confidence") is not None and float(row["confidence"]) < FULL_MAINTAINER_LOW_CONFIDENCE_THRESHOLD
    ]
    per_segment = {}
    for segment in segments or []:
        segment_rows = [row for row in assigned if row.get("segment_id") == segment["segment_id"]]
        segment_low = [
            row
            for row in segment_rows
            if row.get("confidence") is not None and float(row["confidence"]) < FULL_MAINTAINER_LOW_CONFIDENCE_THRESHOLD
        ]
        per_segment[segment["segment_id"]] = {
            "assignment_count": len(segment_rows),
            "retired_assignment_count": sum(
                1
                for row in retired_assigned
                if row.get("segment_id") == segment["segment_id"]
            ),
            "low_confidence_assignment_count": len(segment_low),
        }
    return {
        "assignment_count": len(rows),
        "live_assignment_count": len(live),
        "retired_assignment_count": len(retired),
        "assigned_count": len(assigned),
        "unassigned_block_count": len(unassigned),
        "retired_assigned_count": len(retired_assigned),
        "retired_unassigned_count": len(retired_unassigned),
        "low_confidence_assignment_count": len(low),
        "per_segment": per_segment,
    }

def _table_exists(connection, table):
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (str(table),),
    ).fetchone()
    return row is not None

def _read_only_event_stream_connection():
    db_path = event_stream.event_stream_db_path()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection

def _load_segments(connection):
    rows = connection.execute(
        """
        SELECT * FROM focus_segments
        ORDER BY created_at ASC, segment_id ASC
        """
    ).fetchall()
    return [_segment_from_row(row) for row in rows]

def _segment_from_row(row):
    try:
        ranges = json.loads(row["slot_ranges"] or "[]")
    except json.JSONDecodeError:
        ranges = []
    return {
        "segment_id": row["segment_id"],
        "title": row["title"],
        "summary": row["summary"],
        "status": row["status"],
        "slot_ranges": _normalize_ranges(ranges),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_touched_at": row["last_touched_at"],
    }

def _bootstrap_if_missing(connection):
    if _load_segments(connection):
        return False
    created_at = _now()
    connection.execute(
        """
        INSERT INTO focus_segments
            (segment_id, title, summary, status, slot_ranges, created_at, updated_at, last_touched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"seg_{uuid4().hex}",
            BOOTSTRAP_TITLE,
            BOOTSTRAP_SUMMARY,
            "active",
            "[]",
            created_at,
            created_at,
            created_at,
        ),
    )
    connection.commit()
    return True

def _active_segment(connection):
    row = connection.execute(
        """
        SELECT * FROM focus_segments
        WHERE status = 'active'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return _segment_from_row(row) if row is not None else None

def _segment_by_id(connection, segment_id):
    row = connection.execute(
        """
        SELECT * FROM focus_segments
        WHERE segment_id = ?
        """,
        (segment_id,),
    ).fetchone()
    return _segment_from_row(row) if row is not None else None

def _active_segments(connection):
    rows = connection.execute(
        """
        SELECT * FROM focus_segments
        WHERE status = 'active'
        ORDER BY updated_at ASC, created_at ASC
        """
    ).fetchall()
    return [_segment_from_row(row) for row in rows]

def _set_segment_status(connection, segment_id, status):
    timestamp = _now()
    connection.execute(
        """
        UPDATE focus_segments
        SET status = ?, updated_at = ?, last_touched_at = ?
        WHERE segment_id = ?
        """,
        (status, timestamp, timestamp, segment_id),
    )

def _create_segment(connection, title, summary, status="active"):
    created_at = _now()
    segment_id = f"seg_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO focus_segments
            (segment_id, title, summary, status, slot_ranges, created_at, updated_at, last_touched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            segment_id,
            _single_line(title) or f"F{len(_load_segments(connection)) + 1}",
            _single_line(summary) or "Focus created by Focus Detector.",
            status,
            "[]",
            created_at,
            created_at,
            created_at,
        ),
    )
    return segment_id

def _update_segment_ranges(connection, segment_id, ranges):
    timestamp = _now()
    connection.execute(
        """
        UPDATE focus_segments
        SET slot_ranges = ?, updated_at = ?, last_touched_at = ?
        WHERE segment_id = ?
        """,
        (
            json.dumps(_normalize_ranges(ranges), ensure_ascii=False, sort_keys=True),
            timestamp,
            timestamp,
            segment_id,
        ),
    )
    connection.commit()

def _update_segment(connection, segment_id, ranges=None, title=None, summary=None, status=None):
    current = _segment_by_id(connection, segment_id)
    if not current:
        return False
    timestamp = _now()
    next_title = _single_line(title) if title is not None and _single_line(title) else current["title"]
    next_summary = _single_line(summary) if summary is not None and _single_line(summary) else current["summary"]
    next_status = status if status in VALID_STATUSES else current["status"]
    next_ranges = current["slot_ranges"] if ranges is None else _normalize_ranges(ranges)
    connection.execute(
        """
        UPDATE focus_segments
        SET title = ?, summary = ?, status = ?, slot_ranges = ?, updated_at = ?, last_touched_at = ?
        WHERE segment_id = ?
        """,
        (
            next_title,
            next_summary,
            next_status,
            json.dumps(next_ranges, ensure_ascii=False, sort_keys=True),
            timestamp,
            timestamp,
            segment_id,
        ),
    )
    return True

def _load_json_state(connection, key, default=None):
    value = _state_get(connection, key)
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def ensure_schema(connection):
    return _ensure_tables(connection)


def load_segments(connection):
    return _load_segments(connection)
