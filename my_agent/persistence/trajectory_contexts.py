"""Readable exact provider requests, owned by the trajectory store."""

import gzip
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

AGENT_TYPES = ("main_agent", "ltm_maintainer", "memory_retriever", "memory_push", "formation", "memorize")
REQUEST_RETENTION = 50


def _store():
    from persistence import trajectory
    return trajectory


def context_dir(root=None):
    return _store().observability_root(root) / "contexts"


def create_schema(connection):

    connection.execute("""CREATE TABLE IF NOT EXISTS trajectory_requests (
        request_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id TEXT NOT NULL UNIQUE,
        agent_type TEXT NOT NULL,
        trajectory_id TEXT,
        span_id TEXT,
        event_id TEXT,
        created_at TEXT NOT NULL,
        model TEXT,
        metadata_json TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        raw_bytes INTEGER NOT NULL,
        relative_path TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'available',
        deletion_pending INTEGER NOT NULL DEFAULT 0,
        source_key TEXT UNIQUE,
        exact_wire_bytes INTEGER NOT NULL DEFAULT 1
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_requests_window ON trajectory_requests(agent_type, state, created_at DESC, request_seq DESC)")


def _owner(connection, context, explicit=None):
    if explicit is not None:
        if explicit not in AGENT_TYPES:
            raise ValueError(f"Unknown context agent type: {explicit}")
        return explicit
    span_id = context.get("span_id")
    seen = set()
    while span_id and span_id not in seen:
        seen.add(span_id)
        row = connection.execute("SELECT parent_span_id, span_type FROM trajectory_spans WHERE span_id=?", (span_id,)).fetchone()
        if not row:
            break
        if row["span_type"] == "memory_push":
            return "memory_push"
        span_id = row["parent_span_id"]
    row = connection.execute("SELECT process_type FROM trajectories WHERE trajectory_id=?", (context.get("trajectory_id"),)).fetchone()
    if row and row[0] in AGENT_TYPES:
        return row[0]
    raise ValueError("Cannot determine provider request Agent")


def _insert(connection, context, raw, root, agent_type=None, metadata=None,
            created_at=None, source_key=None, exact=True, event_id=None):
    if source_key:
        existing = connection.execute("SELECT * FROM trajectory_requests WHERE source_key=?", (source_key,)).fetchone()
        if existing:
            return dict(existing)
    agent_type = _owner(connection, context, agent_type)
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Provider request must be a JSON object")
    request_id = "request_" + uuid4().hex
    timestamp = created_at or _store()._now()
    from datetime import datetime, timezone
    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    metadata = dict(metadata or {})
    root_row = connection.execute("SELECT metadata_json FROM trajectories WHERE trajectory_id=?", (context.get("trajectory_id"),)).fetchone()
    if root_row:
        metadata = {**json.loads(root_row[0]), **metadata}
    digest = hashlib.sha256(raw).hexdigest()
    cursor = connection.execute("""INSERT INTO trajectory_requests(
        request_id,agent_type,trajectory_id,span_id,event_id,created_at,model,metadata_json,
        sha256,raw_bytes,relative_path,source_key,exact_wire_bytes)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        request_id, agent_type, context.get("trajectory_id"), context.get("span_id"), event_id,
        timestamp, (payload.get("payload", payload) if not exact else payload).get("model"),
        _store()._json(metadata), digest, len(raw), "", source_key, int(exact)))
    seq = cursor.lastrowid
    stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    relative = Path(agent_type) / f"{stamp}_{seq:09d}_{request_id}.json"
    path = context_dir(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_bytes(raw)
        os.replace(temporary, path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise OSError("Request snapshot verification failed")
    finally:
        temporary.unlink(missing_ok=True)
    connection.execute("UPDATE trajectory_requests SET relative_path=? WHERE request_id=?", (str(relative), request_id))
    return dict(connection.execute("SELECT * FROM trajectory_requests WHERE request_id=?", (request_id,)).fetchone())


def _trim(connection, root):
    for agent in AGENT_TYPES:
        rows = connection.execute("SELECT request_id FROM trajectory_requests WHERE agent_type=? AND state='available' ORDER BY created_at DESC,request_seq DESC", (agent,)).fetchall()
        for row in rows[REQUEST_RETENTION:]:
            connection.execute("UPDATE trajectory_requests SET state='expired',deletion_pending=1 WHERE request_id=?", (row[0],))
    for row in connection.execute("SELECT request_id,relative_path FROM trajectory_requests WHERE deletion_pending=1").fetchall():
        try:
            (context_dir(root) / row["relative_path"]).unlink(missing_ok=True)
            connection.execute("UPDATE trajectory_requests SET deletion_pending=0 WHERE request_id=?", (row["request_id"],))
        except OSError:
            pass



    known = {str(row[0]) for row in connection.execute("SELECT relative_path FROM trajectory_requests")}
    for agent in AGENT_TYPES:
        directory = context_dir(root) / agent
        if not directory.exists():
            continue
        for path in directory.iterdir():
            if path.suffix not in {".json", ".tmp"} or "_request_" not in path.name:
                continue
            relative = str(path.relative_to(context_dir(root)))
            if relative not in known:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def save_request(context, raw, *, agent_type=None, metadata=None):
    if not context or not context.get("enabled"):
        return None
    store = _store()
    root = context.get("root")
    result = None
    try:
        with store._LOCK, store._connect(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = _insert(connection, context, bytes(raw), root, agent_type, metadata)
            connection.commit()
            result = inserted
            connection.execute("BEGIN IMMEDIATE")
            _trim(connection, root)
        return result
    except Exception:
        return result


def attach_event(request_id, event_id, root=None):
    try:
        with _store()._connect(root) as connection:
            connection.execute("UPDATE trajectory_requests SET event_id=? WHERE request_id=?", (event_id, request_id))
    except Exception:
        pass


def _projection(row, root):
    result = dict(row)
    path = context_dir(root) / result["relative_path"]
    result["path"] = str(path.resolve())
    if result["state"] == "available" and not path.is_file():
        result["state"] = "missing"
    return result


def list_requests(agent_type="main_agent", root=None):
    if agent_type not in AGENT_TYPES:
        raise ValueError(f"Unknown Agent: {agent_type}")
    try:
        connection = _store()._connect_readonly(root)
        if connection is None:
            return []
        try:
            rows = connection.execute("SELECT * FROM trajectory_requests WHERE agent_type=? AND state='available' ORDER BY created_at DESC,request_seq DESC LIMIT ?", (agent_type, REQUEST_RETENTION)).fetchall()
        finally:
            connection.close()
        return [_projection(row, root) for row in rows]
    except Exception:
        return []


def get_request(request_id, root=None):
    try:
        connection = _store()._connect_readonly(root)
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM trajectory_requests WHERE request_id=?", (request_id,)).fetchone()
        finally:
            connection.close()
        return _projection(row, root) if row else None
    except Exception:
        return None


def format_request(row):
    if not row:
        return "Provider context not found."
    return (f"{row['request_id']}  {row['agent_type']}  {row['state']}\n"
            f"time: {row['created_at']}  model: {row['model']}  bytes: {row['raw_bytes']}\n"
            f"exact_wire_bytes: {bool(row['exact_wire_bytes'])}\npath: {row['path']}")


def migrate(root=None):
    """Convert individual verified requests; failures leave original references intact."""
    store = _store()
    key = "readable_provider_requests_v2"
    try:
        with store._LOCK, store._connect_raw(root) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("""SELECT e.*,r.role,a.artifact_id,a.relative_path
                FROM trajectory_events e JOIN trajectory_artifact_refs r USING(event_id)
                JOIN trajectory_artifacts a USING(artifact_id)
                WHERE r.role='provider_request' OR
                (e.event_type='legacy.context_snapshot' AND r.role='legacy_source')
                ORDER BY e.ts,e.seq""").fetchall()
            failures = []
            for row in rows:
                connection.execute("SAVEPOINT request_import")
                try:
                    legacy = row["role"] == "legacy_source"
                    summary = json.loads(row["summary_json"])
                    if legacy and Path(summary.get("source_path", "")).name.lower() == "index.json":
                        connection.execute("RELEASE request_import")
                        continue
                    raw = gzip.decompress((store.trajectory_artifact_dir(root) / row["relative_path"]).read_bytes())
                    if hashlib.sha256(raw).hexdigest() != row["artifact_id"]:
                        raise ValueError("Source artifact hash mismatch")
                    payload = json.loads(raw)
                    legacy_payload = payload.get("payload", payload) if isinstance(payload, dict) else None
                    if legacy and (not isinstance(legacy_payload, dict) or "messages" not in legacy_payload):
                        connection.execute("RELEASE request_import")
                        continue
                    original_time = row["ts"]
                    if legacy and isinstance(payload.get("metadata"), dict):
                        original_time = payload["metadata"].get("created_at") or original_time
                    context = {"trajectory_id": row["trajectory_id"], "span_id": row["span_id"]}
                    saved = _insert(connection, context, raw, root,
                        agent_type="main_agent" if legacy else None, metadata=summary,
                        created_at=original_time, source_key=row["event_id"]+":"+row["role"],
                        exact=not legacy, event_id=row["event_id"])
                    summary["request_id"] = saved["request_id"]
                    connection.execute("UPDATE trajectory_events SET summary_json=? WHERE event_id=?", (store._json(summary), row["event_id"]))
                    if not legacy:
                        connection.execute("DELETE FROM trajectory_artifact_refs WHERE event_id=? AND role='provider_request'", (row["event_id"],))
                    connection.execute("RELEASE request_import")
                except Exception as error:
                    connection.execute("ROLLBACK TO request_import")
                    connection.execute("RELEASE request_import")
                    failures.append({"event_id":row["event_id"],"error":str(error)})
            connection.execute("INSERT OR REPLACE INTO trajectory_migrations VALUES(?,?,?,?)", (key,"failed" if failures else "completed",store._json({"failures":failures}),store._now()))
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            _trim(connection, root)

        store._enforce_retention(root)
    except Exception:
        pass
