"""Unified, best-effort process trajectory storage.

Trajectory metadata stays small and queryable in SQLite. Potentially large or
sensitive diagnostic values are stored verbatim as content-addressed gzip
artifacts. None of this state is runtime truth.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


SCHEMA_VERSION = 2
TERMINAL_RETENTION_PER_PROCESS = 20
STORE_MIGRATION_KEYS = (
    "legacy_trace_and_context_cache_v1",
    "readable_provider_requests_v2",
)
_LOCK = threading.RLock()
_INITIALIZED = set()
_PROVIDER_USAGE_CALLBACK = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def trajectory_project_root(root=None):
    return Path(root) if root is not None else Path.cwd()


def observability_root(root=None):
    base = trajectory_project_root(root)
    return base / ".sema" / "observability"


def trajectory_db_path(root=None):
    return observability_root(root) / "trajectory.sqlite3"


def trajectory_artifact_dir(root=None):
    return observability_root(root) / "artifacts"


def _root_key(root=None):
    return str(trajectory_project_root(root).resolve())


def _connect_raw(root=None):
    path = trajectory_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _create_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS trajectories (
            trajectory_id TEXT PRIMARY KEY,
            process_type TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            initial_caller_trajectory_id TEXT,
            initial_caller_span_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error_type TEXT,
            error_message TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trajectories_external
            ON trajectories(process_type, external_id) WHERE external_id <> '';
        CREATE INDEX IF NOT EXISTS idx_trajectories_recent
            ON trajectories(process_type, status, started_at DESC);

        CREATE TABLE IF NOT EXISTS trajectory_spans (
            span_id TEXT PRIMARY KEY,
            trajectory_id TEXT NOT NULL REFERENCES trajectories(trajectory_id) ON DELETE CASCADE,
            parent_span_id TEXT REFERENCES trajectory_spans(span_id) ON DELETE CASCADE,
            span_type TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error_type TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_spans_trajectory ON trajectory_spans(trajectory_id, started_at);

        CREATE TABLE IF NOT EXISTS trajectory_events (
            event_id TEXT PRIMARY KEY,
            trajectory_id TEXT NOT NULL REFERENCES trajectories(trajectory_id) ON DELETE CASCADE,
            span_id TEXT REFERENCES trajectory_spans(span_id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(trajectory_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_events_trajectory ON trajectory_events(trajectory_id, seq);
        CREATE INDEX IF NOT EXISTS idx_events_status ON trajectory_events(status, ts DESC);

        CREATE TABLE IF NOT EXISTS trajectory_artifacts (
            artifact_id TEXT PRIMARY KEY,
            media_type TEXT NOT NULL,
            compression TEXT NOT NULL,
            raw_bytes INTEGER NOT NULL,
            stored_bytes INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trajectory_artifact_refs (
            event_id TEXT NOT NULL REFERENCES trajectory_events(event_id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            artifact_id TEXT NOT NULL REFERENCES trajectory_artifacts(artifact_id) ON DELETE RESTRICT,
            PRIMARY KEY(event_id, role, artifact_id)
        );

        CREATE TABLE IF NOT EXISTS trajectory_migrations (
            migration_key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """
    )
    from persistence import trajectory_contexts
    trajectory_contexts.create_schema(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _initialize(root=None):
    key = _root_key(root)
    if key in _INITIALIZED:
        return
    with _LOCK:
        if key in _INITIALIZED:
            return
        with _connect_raw(root) as connection:
            _create_schema(connection)
        _migrate_legacy_observability(root)
        _INITIALIZED.add(key)
        from persistence import trajectory_contexts
        trajectory_contexts.migrate(root)


def _connect(root=None):
    _initialize(root)
    return _connect_raw(root)


def _connect_readonly(root=None):
    """Open an existing trajectory database without initialization or writes."""
    path = trajectory_db_path(root)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            timeout=30,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except Exception:
        return None


def _legacy_observability_sources(root=None):
    project_root = trajectory_project_root(root)
    trace_file = project_root / ".sema" / "logs" / "trace.jsonl"
    cache_dir = project_root / ".sema" / "memory" / "context_cache"
    sources = ([trace_file] if trace_file.is_file() else [])
    if cache_dir.is_dir():
        sources.extend(path for path in cache_dir.rglob("*") if path.is_file())
    return sources


def trajectory_store_status(root=None):
    """Inspect store availability without creating or changing any files."""
    database = trajectory_db_path(root)
    sources = _legacy_observability_sources(root)
    result = {
        "state": "uninitialized",
        "database": str(database),
        "database_exists": database.is_file(),
        "legacy_source_count": len(sources),
    }
    if not database.is_file():
        if sources:
            result["state"] = "migration_pending"
        return result

    connection = _connect_readonly(root)
    if connection is None:
        result["state"] = "unavailable"
        return result
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "trajectories",
            "trajectory_spans",
            "trajectory_events",
            "trajectory_artifacts",
            "trajectory_artifact_refs",
            "trajectory_migrations",
            "trajectory_requests",
        }
        missing = sorted(required - tables)
        if missing:
            result["state"] = "unavailable"
            result["missing_tables"] = missing
            return result
        rows = connection.execute(
            "SELECT migration_key, status FROM trajectory_migrations"
        ).fetchall()
        migration_status = {row["migration_key"]: row["status"] for row in rows}
        pending = bool(sources) or any(
            migration_status.get(key) not in (None, "completed")
            for key in STORE_MIGRATION_KEYS
        )
        result["state"] = "migration_pending" if pending else "ready"
        result["migration_status"] = migration_status
        return result
    except Exception:
        result["state"] = "unavailable"
        return result
    finally:
        connection.close()


def format_trajectory_store_status(status):
    if not status:
        return "Trajectory store status unavailable."
    state = status.get("state")
    if state == "ready":
        return ""
    if state == "uninitialized":
        return "Trajectory store is not initialized."
    if state == "migration_pending":
        return (
            "Trajectory store has pending migration work; read-only inspection "
            "did not change it."
        )
    return "Trajectory store is unavailable for read-only inspection."


def _json(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _context(trajectory_id=None, span_id=None, root=None, enabled=True):
    return {
        "enabled": bool(enabled and trajectory_id),
        "trajectory_id": trajectory_id,
        "span_id": span_id,
        "root": str(Path(root).resolve()) if root is not None else None,
    }


def disabled_context(root=None):
    return _context(root=root, enabled=False)


def _context_root(context):
    return context.get("root") if isinstance(context, dict) else None


def _start_trajectory(process_type, external_id=None, caller=None, metadata=None, root=None):
    trajectory_id = f"traj_{uuid4().hex}"
    caller = caller if isinstance(caller, dict) else {}
    now = _now()
    with _connect(root) as connection:
        if external_id:
            existing = connection.execute(
                "SELECT trajectory_id FROM trajectories WHERE process_type = ? AND external_id = ?",
                (str(process_type), str(external_id)),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE trajectories SET status = 'running', ended_at = NULL, error_type = NULL, error_message = NULL WHERE trajectory_id = ?",
                    (existing["trajectory_id"],),
                )
                return _context(existing["trajectory_id"], root=root)
        connection.execute(
            """INSERT INTO trajectories(
                trajectory_id, process_type, external_id, status, started_at,
                initial_caller_trajectory_id, initial_caller_span_id, metadata_json
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)""",
            (
                trajectory_id,
                str(process_type),
                str(external_id or ""),
                now,
                caller.get("trajectory_id"),
                caller.get("span_id"),
                _json(metadata),
            ),
        )
    context = _context(trajectory_id, root=root)
    _record_event(context, "process.started", status="running", summary={"process_type": process_type, "external_id": external_id or ""})
    return context


def start_trajectory(process_type, external_id=None, caller=None, metadata=None, root=None):
    try:
        return _start_trajectory(process_type, external_id, caller, metadata, root)
    except Exception:
        return disabled_context(root)


def resume_trajectory(process_type, external_id, root=None):
    return start_trajectory(process_type, external_id=external_id, root=root)


def _start_span(parent, span_type, name, metadata=None):
    if not isinstance(parent, dict) or not parent.get("enabled"):
        return disabled_context(_context_root(parent))
    span_id = f"span_{uuid4().hex}"
    root = _context_root(parent)
    with _connect(root) as connection:
        connection.execute(
            """INSERT INTO trajectory_spans(
                span_id, trajectory_id, parent_span_id, span_type, name, status, started_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
            (
                span_id,
                parent["trajectory_id"],
                parent.get("span_id"),
                str(span_type),
                str(name),
                _now(),
                _json(metadata),
            ),
        )
    return _context(parent["trajectory_id"], span_id=span_id, root=root)


def start_span(parent, span_type, name, metadata=None):
    try:
        return _start_span(parent, span_type, name, metadata)
    except Exception:
        return disabled_context(_context_root(parent))


def _artifact_bytes(value, media_type=None):
    if isinstance(value, bytes):
        return value, media_type or "application/octet-stream", "bin"
    if isinstance(value, str):
        return value.encode("utf-8"), media_type or "text/plain; charset=utf-8", "txt"
    return _json(value).encode("utf-8"), media_type or "application/json", "json"


def _store_artifact(connection, value, root=None, media_type=None):
    raw, resolved_media_type, extension = _artifact_bytes(value, media_type)
    artifact_id = hashlib.sha256(raw).hexdigest()
    existing = connection.execute(
        "SELECT artifact_id, relative_path, raw_bytes, stored_bytes, media_type, compression FROM trajectory_artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if existing:
        return dict(existing)
    relative = Path(artifact_id[:2]) / f"{artifact_id}.{extension}.gz"
    final_path = trajectory_artifact_dir(root) / relative
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_name(f".{final_path.name}.{uuid4().hex}.tmp")
    with gzip.open(temporary, "wb") as handle:
        handle.write(raw)
    os.replace(temporary, final_path)
    stored_bytes = final_path.stat().st_size
    connection.execute(
        """INSERT INTO trajectory_artifacts(
            artifact_id, media_type, compression, raw_bytes, stored_bytes, relative_path, created_at
        ) VALUES (?, ?, 'gzip', ?, ?, ?, ?)""",
        (artifact_id, resolved_media_type, len(raw), stored_bytes, str(relative), _now()),
    )
    return {
        "artifact_id": artifact_id,
        "media_type": resolved_media_type,
        "compression": "gzip",
        "raw_bytes": len(raw),
        "stored_bytes": stored_bytes,
        "relative_path": str(relative),
    }


def store_artifact(value, root=None, media_type=None):
    try:
        with _connect(root) as connection:
            return {"ok": True, **_store_artifact(connection, value, root, media_type)}
    except Exception as error:
        return {"ok": False, "error": str(error)}


def load_artifact(artifact_id, root=None):
    try:
        connection = _connect_readonly(root)
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM trajectory_artifacts WHERE artifact_id = ?", (str(artifact_id),)).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        path = trajectory_artifact_dir(root) / row["relative_path"]
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
        if hashlib.sha256(raw).hexdigest() != row["artifact_id"]:
            return None
        return {**dict(row), "content": raw}
    except Exception:
        return None


def _record_event(context, event_type, status=None, summary=None, artifacts=None):
    if not isinstance(context, dict) or not context.get("enabled"):
        return None
    root = _context_root(context)
    with _LOCK, _connect(root) as connection:
        connection.execute("BEGIN IMMEDIATE")
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectory_events WHERE trajectory_id = ?",
            (context["trajectory_id"],),
        ).fetchone()[0]
        event_id = f"event_{uuid4().hex}"
        connection.execute(
            """INSERT INTO trajectory_events(
                event_id, trajectory_id, span_id, seq, ts, event_type, status, summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                context["trajectory_id"],
                context.get("span_id"),
                seq,
                _now(),
                str(event_type),
                str(status) if status is not None else None,
                _json(summary),
            ),
        )
        artifact_rows = []
        for role, value in (artifacts or {}).items():
            media_type = None
            content = value
            if isinstance(value, dict) and set(value).issuperset({"content", "media_type"}):
                content = value["content"]
                media_type = value["media_type"]
            artifact = _store_artifact(connection, content, root, media_type)
            connection.execute(
                "INSERT OR IGNORE INTO trajectory_artifact_refs(event_id, role, artifact_id) VALUES (?, ?, ?)",
                (event_id, str(role), artifact["artifact_id"]),
            )
            artifact_rows.append({"role": str(role), **artifact})
    return {"event_id": event_id, "seq": seq, "artifacts": artifact_rows}


def record_event(context, event_type, status=None, summary=None, artifacts=None):
    try:
        return _record_event(context, event_type, status=status, summary=summary, artifacts=artifacts)
    except Exception:
        return None


def _finish_span(context, status, error=None, metadata=None):
    if not isinstance(context, dict) or not context.get("enabled") or not context.get("span_id"):
        return None
    with _connect(_context_root(context)) as connection:
        connection.execute(
            """UPDATE trajectory_spans SET status = ?, ended_at = ?, metadata_json = ?,
               error_type = ?, error_message = ? WHERE span_id = ?""",
            (
                status,
                _now(),
                _json(metadata),
                type(error).__name__ if error is not None else None,
                str(error) if error is not None else None,
                context["span_id"],
            ),
        )
    return context


def complete_span(context, metadata=None):
    try:
        return _finish_span(context, "completed", metadata=metadata)
    except Exception:
        return None


def fail_span(context, error, metadata=None):
    try:
        return _finish_span(context, "failed", error=error, metadata=metadata)
    except Exception:
        return None


def _finish_trajectory(context, status, error=None, metadata=None):
    if not isinstance(context, dict) or not context.get("enabled"):
        return None
    root = _context_root(context)
    with _connect(root) as connection:
        connection.execute(
            """UPDATE trajectories SET status = ?, ended_at = ?, metadata_json = ?,
               error_type = ?, error_message = ? WHERE trajectory_id = ?""",
            (
                status,
                _now(),
                _json(metadata),
                type(error).__name__ if error is not None else None,
                str(error) if error is not None else None,
                context["trajectory_id"],
            ),
        )
    _record_event(context, f"process.{status}", status=status, summary=metadata)
    _enforce_retention(root)
    return context


def complete_trajectory(context, metadata=None):
    try:
        return _finish_trajectory(context, "completed", metadata=metadata)
    except Exception:
        return None


def fail_trajectory(context, error, metadata=None):
    try:
        return _finish_trajectory(context, "failed", error=error, metadata=metadata)
    except Exception:
        return None


def fail_trajectory_by_external_id(process_type, external_id, error, metadata=None, root=None):
    try:
        with _connect(root) as connection:
            row = connection.execute(
                "SELECT trajectory_id, status FROM trajectories WHERE process_type = ? AND external_id = ?",
                (str(process_type), str(external_id)),
            ).fetchone()
        if row is None or row["status"] != "running":
            return False
        context = _context(row["trajectory_id"], root=root)
        return bool(fail_trajectory(context, error, metadata=metadata))
    except Exception:
        return False


def _enforce_retention(root=None):
    with _LOCK, _connect(root) as connection:
        process_types = [row[0] for row in connection.execute("SELECT DISTINCT process_type FROM trajectories")]
        for process_type in process_types:
            rows = connection.execute(
                """SELECT trajectory_id FROM trajectories
                   WHERE process_type = ? AND status IN ('completed', 'failed')
                   ORDER BY COALESCE(ended_at, started_at) DESC""",
                (process_type,),
            ).fetchall()
            for row in rows[TERMINAL_RETENTION_PER_PROCESS:]:
                connection.execute("DELETE FROM trajectories WHERE trajectory_id = ?", (row["trajectory_id"],))
        referenced = {row[0] for row in connection.execute("SELECT DISTINCT artifact_id FROM trajectory_artifact_refs")}
        artifacts = connection.execute("SELECT artifact_id, relative_path FROM trajectory_artifacts").fetchall()
        for artifact in artifacts:
            if artifact["artifact_id"] in referenced:
                continue
            try:
                (trajectory_artifact_dir(root) / artifact["relative_path"]).unlink(missing_ok=True)
            except OSError:
                continue
            connection.execute("DELETE FROM trajectory_artifacts WHERE artifact_id = ?", (artifact["artifact_id"],))
        known_paths = {str(row[0]) for row in connection.execute("SELECT relative_path FROM trajectory_artifacts")}
        artifact_root = trajectory_artifact_dir(root)
        if artifact_root.exists():
            for path in artifact_root.rglob("*.gz"):
                try:
                    relative = str(path.relative_to(artifact_root))
                    if relative not in known_paths:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass


def list_trajectories(limit=20, process_type=None, errors_only=False, root=None):
    try:
        clauses = []
        params = []
        if process_type:
            clauses.append("process_type = ?")
            params.append(str(process_type))
        if errors_only:
            clauses.append("status = 'failed'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, int(limit)))
        connection = _connect_readonly(root)
        if connection is None:
            return []
        try:
            rows = connection.execute(
                f"SELECT * FROM trajectories {where} ORDER BY started_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]
    except Exception:
        return []


def get_trajectory(trajectory_id, root=None):
    try:
        connection = _connect_readonly(root)
        if connection is None:
            return None
        try:
            trajectory = connection.execute("SELECT * FROM trajectories WHERE trajectory_id = ?", (str(trajectory_id),)).fetchone()
            if trajectory:
                spans = connection.execute("SELECT * FROM trajectory_spans WHERE trajectory_id = ? ORDER BY started_at", (str(trajectory_id),)).fetchall()
                events = connection.execute(
                    """SELECT e.*, GROUP_CONCAT(r.role || ':' || r.artifact_id) AS artifact_refs
                       FROM trajectory_events e LEFT JOIN trajectory_artifact_refs r ON r.event_id = e.event_id
                       WHERE e.trajectory_id = ? GROUP BY e.event_id ORDER BY e.seq""",
                    (str(trajectory_id),),
                ).fetchall()
            else:
                spans, events = [], []
        finally:
            connection.close()
        if not trajectory:
            return None
        from persistence.trajectory_contexts import get_request
        projected_events = [dict(row) for row in events]
        for event in projected_events:
            summary = json.loads(event.get("summary_json") or "{}")
            if summary.get("request_id"):
                request = get_request(summary["request_id"], root)
                event["request_snapshot"] = request
        return {"trajectory": dict(trajectory), "spans": [dict(row) for row in spans], "events": projected_events}
    except Exception:
        return None


def latest_trajectory(process_type="main_agent", root=None):
    rows = list_trajectories(limit=1, process_type=process_type, root=root)
    return rows[0] if rows else None


def artifact_metadata(artifact_id, root=None):
    try:
        connection = _connect_readonly(root)
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM trajectory_artifacts WHERE artifact_id = ?", (str(artifact_id),)).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        return {**dict(row), "path": str(trajectory_artifact_dir(root) / row["relative_path"])}
    except Exception:
        return None


def format_trajectory_errors(limit=20, root=None):
    """Format recent failed roots, spans, and events without loading artifacts."""
    try:
        capped = max(1, min(int(limit), 200))
        connection = _connect_readonly(root)
        if connection is None:
            return "No trajectory records."
        try:
            rows = connection.execute(
                """SELECT kind, trajectory_id, item_id, name, status, ts, error_type, error_message FROM (
                    SELECT 'root' AS kind, trajectory_id, trajectory_id AS item_id, process_type AS name,
                           status, COALESCE(ended_at, started_at) AS ts, error_type, error_message
                      FROM trajectories WHERE status = 'failed'
                    UNION ALL
                    SELECT 'span', trajectory_id, span_id, name, status,
                           COALESCE(ended_at, started_at), error_type, error_message
                      FROM trajectory_spans WHERE status = 'failed'
                    UNION ALL
                    SELECT 'event', trajectory_id, event_id, event_type, status, ts, NULL, summary_json
                      FROM trajectory_events WHERE status IN ('failed', 'error', 'rejected')
                ) ORDER BY ts DESC LIMIT ?""",
                (capped,),
            ).fetchall()
        finally:
            connection.close()
        if not rows:
            return "No trajectory errors."
        return "\n".join(
            f"{row['ts']}  {row['kind']}  {row['trajectory_id']}  {row['name']}  {row['status']}"
            + (f"  {row['error_type']}: {row['error_message']}" if row["error_message"] else "")
            for row in rows
        )
    except Exception:
        return "Trajectory error query unavailable."


def read_event_projection(trajectory_id, limit=200, errors_only=False, root=None):
    data = get_trajectory(trajectory_id, root)
    if not data:
        return []
    events = data["events"]
    if errors_only:
        events = [event for event in events if event.get("status") in {"error", "failed", "rejected"}]
    projected = []
    for event in events[-max(0, int(limit)):]:
        try:
            summary = json.loads(event.get("summary_json") or "{}")
        except json.JSONDecodeError:
            summary = {}
        event_type = event.get("event_type")
        projected.append({
            "ts": event.get("ts"),
            "trace_id": event.get("trajectory_id"),
            "event": "tool" if event_type in {"tool.completed", "tool.failed"} else event_type,
            "status": event.get("status"),
            **summary,
        })
    return projected


def format_trajectory_list(rows):
    if not rows:
        return "No trajectory records."
    lines = []
    for row in rows:
        lines.append(
            f"{row.get('trajectory_id')}  {row.get('process_type')}  {row.get('status')}  "
            f"{row.get('started_at')}  external={row.get('external_id') or '-'}"
        )
    return "\n".join(lines)


def format_trajectory_tree(data):
    if not data:
        return "Trajectory not found."
    trajectory = data["trajectory"]
    lines = [
        f"trajectory {trajectory['trajectory_id']}",
        f"process: {trajectory['process_type']}",
        f"status: {trajectory['status']}",
        f"duration: {_duration_text(trajectory.get('started_at'), trajectory.get('ended_at')).strip() or '-'}",
        f"external_id: {trajectory.get('external_id') or '-'}",
    ]
    spans = {row["span_id"]: row for row in data["spans"]}
    depth_cache = {}

    def depth(span_id):
        if not span_id or span_id not in spans:
            return 0
        if span_id in depth_cache:
            return depth_cache[span_id]
        parent = spans[span_id].get("parent_span_id")
        depth_cache[span_id] = 1 + depth(parent) if parent else 1
        return depth_cache[span_id]

    events_by_span = {}
    root_events = []
    for event in data["events"]:
        (events_by_span.setdefault(event.get("span_id"), []) if event.get("span_id") else root_events).append(event)
    for event in root_events:
        lines.append(_format_event_line(event, 1))
    for span in data["spans"]:
        indent = "  " * depth(span["span_id"])
        duration = _duration_text(span.get("started_at"), span.get("ended_at"))
        lines.append(f"{indent}- {span['name']} [{span['span_type']}] {span['status']}{duration}")
        for event in events_by_span.get(span["span_id"], []):
            lines.append(_format_event_line(event, depth(span["span_id"]) + 1))
    return "\n".join(lines)


def _duration_text(started_at, ended_at):
    if not started_at or not ended_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
        return f" {round((end - start).total_seconds() * 1000)}ms"
    except (TypeError, ValueError):
        return ""


def _format_event_line(event, depth):
    try:
        summary = json.loads(event.get("summary_json") or "{}")
    except json.JSONDecodeError:
        summary = {}
    details = " ".join(f"{key}={value}" for key, value in summary.items() if value not in (None, "", [], {}))
    refs = event.get("artifact_refs") or ""
    request = event.get("request_snapshot")
    if request:
        details += f" context={request['state']} path={request['path']}"
    if refs:
        details = f"{details} artifacts={refs}".strip()
    status = f" {event.get('status')}" if event.get("status") else ""
    return f"{'  ' * depth}· {event.get('event_type')}{status} {details}".rstrip()


def _migrate_legacy_observability(root=None):
    migration_key = "legacy_trace_and_context_cache_v1"
    project_root = trajectory_project_root(root)
    trace_file = project_root / ".sema" / "logs" / "trace.jsonl"
    cache_dir = project_root / ".sema" / "memory" / "context_cache"
    sources = _legacy_observability_sources(root)
    with _connect_raw(root) as connection:
        row = connection.execute("SELECT status FROM trajectory_migrations WHERE migration_key = ?", (migration_key,)).fetchone()
        if row and row["status"] == "completed":
            return
    if not sources:
        with _connect_raw(root) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO trajectory_migrations VALUES (?, 'completed', ?, ?)",
                (migration_key, _json({"source_count": 0}), _now()),
            )
        return
    source_hashes = {}
    try:
        source_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        with _LOCK, _connect_raw(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            migration_id = f"traj_legacy_migration_{uuid4().hex}"
            connection.execute(
                "INSERT INTO trajectories VALUES (?, 'legacy_migration', ?, 'completed', ?, ?, NULL, NULL, ?, NULL, NULL)",
                (migration_id, migration_key, _now(), _now(), _json({"migration": migration_key})),
            )
            trace_roots = {}

            def ensure_legacy_root(legacy_trace_id):
                legacy_trace_id = str(legacy_trace_id or "orphan")
                if legacy_trace_id in trace_roots:
                    return trace_roots[legacy_trace_id]
                trajectory_id = f"traj_legacy_{hashlib.sha256(legacy_trace_id.encode()).hexdigest()[:24]}"
                connection.execute(
                    "INSERT OR IGNORE INTO trajectories VALUES (?, 'legacy', ?, 'completed', ?, ?, NULL, NULL, ?, NULL, NULL)",
                    (trajectory_id, legacy_trace_id, _now(), _now(), _json({"legacy_trace_id": legacy_trace_id})),
                )
                trace_roots[legacy_trace_id] = trajectory_id
                return trajectory_id

            if trace_file.exists():
                for index, raw_line in enumerate(trace_file.read_bytes().splitlines(), start=1):
                    try:
                        record = json.loads(raw_line.decode("utf-8-sig"))
                    except Exception:
                        record = {"legacy_malformed_line": index}
                    trajectory_id = ensure_legacy_root(record.get("trace_id") if isinstance(record, dict) else None)
                    seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectory_events WHERE trajectory_id = ?", (trajectory_id,)).fetchone()[0]
                    event_id = f"event_{uuid4().hex}"
                    event_type = record.get("event") or "legacy.malformed" if isinstance(record, dict) else "legacy.malformed"
                    connection.execute(
                        "INSERT INTO trajectory_events VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                        (event_id, trajectory_id, seq, record.get("ts") or _now(), event_type, record.get("status"), _json(record)),
                    )
                    artifact = _store_artifact(connection, raw_line, root, "application/x-ndjson")
                    connection.execute("INSERT INTO trajectory_artifact_refs VALUES (?, 'legacy_event', ?)", (event_id, artifact["artifact_id"]))

            index_rows = []
            index_path = cache_dir / "index.json"
            if index_path.exists():
                try:
                    parsed = json.loads(index_path.read_text(encoding="utf-8-sig"))
                    index_rows = parsed.get("snapshots", []) if isinstance(parsed, dict) else parsed
                    index_rows = index_rows if isinstance(index_rows, list) else []
                except Exception:
                    index_rows = []
            indexed_paths = {str(Path(row.get("payload_path") or "")): row for row in index_rows if isinstance(row, dict)}
            for path in [item for item in sources if item != trace_file]:
                row = indexed_paths.get(str(path), {})
                trajectory_id = ensure_legacy_root(row.get("trace_id") or f"context:{row.get('snapshot_id') or path.name}")
                seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectory_events WHERE trajectory_id = ?", (trajectory_id,)).fetchone()[0]
                event_id = f"event_{uuid4().hex}"
                connection.execute(
                    "INSERT INTO trajectory_events VALUES (?, ?, NULL, ?, ?, 'legacy.context_snapshot', 'completed', ?)",
                    (event_id, trajectory_id, seq, row.get("created_at") or _now(), _json({"snapshot_id": row.get("snapshot_id"), "source_path": str(path)})),
                )
                artifact = _store_artifact(connection, path.read_bytes(), root, "application/json")
                connection.execute("INSERT INTO trajectory_artifact_refs VALUES (?, 'legacy_source', ?)", (event_id, artifact["artifact_id"]))


            for source in sources:
                seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM trajectory_events WHERE trajectory_id = ?", (migration_id,)).fetchone()[0]
                event_id = f"event_{uuid4().hex}"
                connection.execute(
                    "INSERT INTO trajectory_events VALUES (?, ?, NULL, ?, ?, 'legacy.source_imported', 'completed', ?)",
                    (event_id, migration_id, seq, _now(), _json({"source_path": str(source), "sha256": source_hashes[str(source)]})),
                )
                artifact = _store_artifact(connection, source.read_bytes(), root, "application/octet-stream")
                if artifact["artifact_id"] != source_hashes[str(source)]:
                    raise RuntimeError(f"Legacy source hash mismatch: {source}")
                connection.execute("INSERT INTO trajectory_artifact_refs VALUES (?, 'legacy_source', ?)", (event_id, artifact["artifact_id"]))
            source_artifact_ref_count = connection.execute(
                "SELECT COUNT(*) FROM trajectory_artifact_refs WHERE event_id IN "
                "(SELECT event_id FROM trajectory_events WHERE trajectory_id = ?)",
                (migration_id,),
            ).fetchone()[0]
            if source_artifact_ref_count != len(sources):
                raise RuntimeError("Legacy source artifact verification count mismatch.")
            connection.execute(
                "INSERT OR REPLACE INTO trajectory_migrations VALUES (?, 'completed', ?, ?)",
                (
                    migration_key,
                    _json({
                        "source_count": len(sources),
                        "source_hashes": source_hashes,
                        "source_artifact_ref_count": source_artifact_ref_count,
                    }),
                    _now(),
                ),
            )
            connection.commit()
        _enforce_retention(root)
        for source in sorted(sources, key=lambda item: len(item.parts), reverse=True):
            source.unlink(missing_ok=True)
        for directory in (cache_dir, trace_file.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
    except Exception as error:
        try:
            with _connect_raw(root) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO trajectory_migrations VALUES (?, 'failed', ?, ?)",
                    (migration_key, _json({"error": str(error), "source_hashes": source_hashes}), _now()),
                )
        except Exception:
            pass


def reset_trajectory_state_for_tests():
    with _LOCK:
        _INITIALIZED.clear()


def set_provider_usage_callback(callback):
    """Install a process-local observer for provider attempt/usage metadata."""
    global _PROVIDER_USAGE_CALLBACK
    _PROVIDER_USAGE_CALLBACK = callback


def _provider_usage(raw_body=None, parsed=None):
    candidates = []
    if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
        candidates.append(parsed["usage"])
    if raw_body is not None:
        try:
            decoded = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else str(raw_body)
            payload = json.loads(decoded)
        except (UnicodeDecodeError, TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            candidates.append(payload["usage"])
    for usage in candidates:
        normalized = {}
        for target, keys in {
            "prompt_tokens": ("prompt_tokens", "input_tokens"),
            "completion_tokens": ("completion_tokens", "output_tokens"),
            "total_tokens": ("total_tokens",),
        }.items():
            for key in keys:
                if usage.get(key) is not None:
                    try:
                        normalized[target] = int(usage[key])
                    except (TypeError, ValueError):
                        pass
                    break
        if normalized:
            return normalized
    return None


class ProviderExchangeObserver:
    """Capture one provider attempt without letting diagnostics affect it."""

    def __init__(self, parent, *, purpose="model", metadata=None, agent_type=None):
        self.parent = parent
        self.span = start_span(parent, "model_call", purpose, metadata=metadata)
        self.purpose = purpose
        self.agent_type = agent_type
        self.metadata = dict(metadata or {})
        self.stream_buffer = io.BytesIO()
        self.started_at = datetime.now(timezone.utc)
        self.exchange_id = uuid4().hex

    def _notify_usage(self, event_type, usage=None):
        callback = _PROVIDER_USAGE_CALLBACK
        if callback is not None:
            try:
                callback({
                    "event_type": event_type, "exchange_id": self.exchange_id,
                    "usage": usage or {}, "purpose": self.purpose,
                    "agent_type": self.agent_type,
                })
            except Exception:
                pass

    def on_request(self, raw_body, payload=None, metadata=None):
        self._notify_usage("requested")
        from persistence import trajectory_contexts
        snapshot = trajectory_contexts.save_request(
            self.span, bytes(raw_body), agent_type=self.agent_type,
            metadata={**self.metadata, **dict(metadata or {})},
        )
        event = record_event(
            self.span,
            "model.requested",
            status="running",
            summary={
                "purpose": self.purpose,
                "streaming": bool((payload or {}).get("stream")),
                "model": (payload or {}).get("model"),
                "message_count": len((payload or {}).get("messages") or []),
                "tool_count": len((payload or {}).get("tools") or []),
                **dict(metadata or {}),
                "request_id": snapshot.get("request_id") if snapshot else None,
            },
        )
        if snapshot and event:
            trajectory_contexts.attach_event(snapshot["request_id"], event["event_id"], _context_root(self.span))

    def on_stream_chunk(self, raw_chunk):
        try:
            self.stream_buffer.write(raw_chunk if isinstance(raw_chunk, bytes) else str(raw_chunk).encode("utf-8"))
        except Exception:
            pass

    def on_response(self, raw_body=None, parsed=None, finish_reason=None, metadata=None):
        artifacts = {"parsed_response": parsed if parsed is not None else {}}
        stream_raw = self.stream_buffer.getvalue()
        if stream_raw:
            artifacts["raw_sse"] = {"content": stream_raw, "media_type": "text/event-stream"}
        elif raw_body is not None:
            artifacts["provider_response"] = {"content": bytes(raw_body), "media_type": "application/json"}
        usage = _provider_usage(raw_body=raw_body, parsed=parsed)
        response_summary = {
            "purpose": self.purpose,
            "finish_reason": finish_reason or "",
            "duration_ms": round((datetime.now(timezone.utc) - self.started_at).total_seconds() * 1000),
            **dict(metadata or {}),
        }
        if usage:
            response_summary["usage"] = usage
        self._notify_usage("responded", usage)
        record_event(
            self.span,
            "model.responded",
            status="completed",
            summary=response_summary,
            artifacts=artifacts,
        )
        complete_span(self.span, metadata={"finish_reason": finish_reason or ""})

    def on_error(self, error, raw_body=None, metadata=None):
        self._notify_usage("failed", _provider_usage(raw_body=raw_body))
        artifacts = {"error": {"type": type(error).__name__, "message": str(error)}}
        if raw_body is not None:
            artifacts["provider_error_response"] = {"content": bytes(raw_body), "media_type": "application/octet-stream"}
        stream_raw = self.stream_buffer.getvalue()
        if stream_raw:
            artifacts["partial_raw_sse"] = {"content": stream_raw, "media_type": "text/event-stream"}
        record_event(
            self.span,
            "model.failed",
            status="failed",
            summary={
                "purpose": self.purpose,
                "error_type": type(error).__name__,
                "error": str(error),
                "duration_ms": round((datetime.now(timezone.utc) - self.started_at).total_seconds() * 1000),
                **dict(metadata or {}),
            },
            artifacts=artifacts,
        )
        fail_span(self.span, error)


def provider_exchange_observer(parent, *, purpose="model", metadata=None, agent_type=None):
    try:
        if not isinstance(parent, dict) or not parent.get("enabled"):
            return None
        return ProviderExchangeObserver(parent, purpose=purpose, metadata=metadata, agent_type=agent_type)
    except Exception:
        return None
