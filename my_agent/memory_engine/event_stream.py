import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agents.temporal_state import CONTINUOUS_SESSION_GAP_SECONDS
from .token_counter import count_text_tokens


























PROJECT_DIR_NAME = ".sema"
MEMORY_DIR_NAME = "memory"
EVENT_STREAM_DIR_NAME = "event_stream"
DB_FILE_NAME = "stream.db"
ARTIFACTS_DIR_NAME = "artifacts"
INLINE_CONTENT_MAX_CHARS = 8192



def _now():
    return datetime.now(timezone.utc).isoformat()



def memory_root(root=None):
    root = Path(root) if root is not None else Path.cwd()
    return root / PROJECT_DIR_NAME / MEMORY_DIR_NAME



def event_stream_dir(root=None):
    return memory_root(root) / EVENT_STREAM_DIR_NAME



def event_stream_db_path(root=None):
    return event_stream_dir(root) / DB_FILE_NAME



def artifacts_dir(root=None):
    return event_stream_dir(root) / ARTIFACTS_DIR_NAME



def ensure_event_stream(root=None):
    directory = event_stream_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    artifacts_dir(root).mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(event_stream_db_path(root))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS slots (
            slot_id TEXT PRIMARY KEY,
            slot_seq INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            source TEXT NOT NULL,
            active_event_id TEXT,
            original_event_id TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            slot_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content_ref TEXT NOT NULL,
            inline_content TEXT,
            token_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(slot_id) REFERENCES slots(slot_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            token_count INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_slots_seq ON slots(slot_seq)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_slot ON events(slot_id)")
    connection.commit()
    return connection



def event_stream_status(root=None):
    with ensure_event_stream(root) as connection:
        counts = _storage_counts(connection)
    return {
        "memory_root": memory_root(root),
        "event_stream_dir": event_stream_dir(root),
        "db_path": event_stream_db_path(root),
        "artifacts_dir": artifacts_dir(root),
        "message_count": counts["slot_count"],
        "slot_count": counts["slot_count"],
        "event_count": counts["event_count"],
        "inline_event_count": counts["inline_event_count"],
        "artifact_event_count": counts["artifact_event_count"],
        "raw_event_count": counts["raw_event_count"],
        "ref_event_count": counts["ref_event_count"],
        "sum_event_count": counts["sum_event_count"],
        "artifact_count": counts["artifact_count"],
        "artifact_bytes": counts["artifact_bytes"],
        "inline_content_max_chars": INLINE_CONTENT_MAX_CHARS,
    }



def event_stream_message_count(root=None):
    with ensure_event_stream(root) as connection:
        row = connection.execute("SELECT COUNT(*) AS count FROM slots").fetchone()
        return int(row["count"])



def load_slot(slot_seq, root=None):
    slot_seq = int(slot_seq)
    with ensure_event_stream(root) as connection:
        row = connection.execute(
            "SELECT * FROM slots WHERE slot_seq = ?",
            (slot_seq,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Event Stream slot not found: {slot_seq}")
        return _row_dict(row)



def load_event(event_id, root=None):
    event_id = str(event_id or "")
    with ensure_event_stream(root) as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Event Stream event not found: {event_id}")
        result = _row_dict(row)
        result["has_inline_content"] = bool(result.get("inline_content"))
        if result.get("inline_content") is not None:
            result["content"] = json.loads(result["inline_content"])
        return result



def load_artifact(artifact_id, root=None):
    artifact_id = str(artifact_id or "")
    with ensure_event_stream(root) as connection:
        row = connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Event Stream artifact not found: {artifact_id}")
        content_json = _read_artifact(connection, artifact_id)
        result = _row_dict(row)
        result["content"] = json.loads(content_json)
        return result



def check_integrity(root=None):
    errors = []
    with ensure_event_stream(root) as connection:
        counts = _storage_counts(connection)
        slot_rows = connection.execute("SELECT * FROM slots ORDER BY slot_seq ASC").fetchall()
        event_ids = {
            row["event_id"]
            for row in connection.execute("SELECT event_id FROM events").fetchall()
        }
        slot_ids = {row["slot_id"] for row in slot_rows}
        artifact_ids = {
            row["artifact_id"]
            for row in connection.execute("SELECT artifact_id FROM artifacts").fetchall()
        }

        expected_sequences = list(range(len(slot_rows)))
        actual_sequences = [int(row["slot_seq"]) for row in slot_rows]
        if actual_sequences != expected_sequences:
            errors.append(
                {
                    "check": "slot_seq_contiguous",
                    "message": "slot_seq values must start at 0 and be contiguous.",
                    "expected": expected_sequences,
                    "actual": actual_sequences,
                }
            )

        for slot in slot_rows:
            for key in ("active_event_id", "original_event_id"):
                if slot[key] not in event_ids:
                    errors.append(
                        {
                            "check": key,
                            "message": f"{key} does not point to an existing event.",
                            "slot_seq": slot["slot_seq"],
                            "slot_id": slot["slot_id"],
                            "event_id": slot[key],
                        }
                    )

        for event in connection.execute("SELECT * FROM events").fetchall():
            if event["slot_id"] not in slot_ids:
                errors.append(
                    {
                        "check": "event_slot",
                        "message": "Event points to a missing slot.",
                        "event_id": event["event_id"],
                        "slot_id": event["slot_id"],
                    }
                )
            if event["content_ref"] == "inline":
                if event["inline_content"] is None:
                    errors.append(
                        {
                            "check": "inline_content",
                            "message": "Inline event is missing inline_content.",
                            "event_id": event["event_id"],
                        }
                    )
            elif event["content_ref"] not in artifact_ids:
                errors.append(
                    {
                        "check": "artifact_ref",
                        "message": "Event content_ref does not point to an artifact.",
                        "event_id": event["event_id"],
                        "content_ref": event["content_ref"],
                    }
                )

        for artifact in connection.execute("SELECT * FROM artifacts").fetchall():
            artifact_id = artifact["artifact_id"]
            try:
                _read_artifact(connection, artifact_id)
            except RuntimeError as error:
                errors.append(
                    {
                        "check": "artifact_hash",
                        "message": str(error),
                        "artifact_id": artifact_id,
                    }
                )

    return {
        "ok": not errors,
        "errors": errors,
        "counts": counts,
        "paths": {
            "memory_root": memory_root(root),
            "event_stream_dir": event_stream_dir(root),
            "db_path": event_stream_db_path(root),
            "artifacts_dir": artifacts_dir(root),
        },
    }



def save_messages(messages, root=None, created_at_by_index=None):
    safe_messages = [dict(message) for message in messages]
    with ensure_event_stream(root) as connection:
        existing_count = _message_count(connection)
        if existing_count > len(safe_messages):
            raise RuntimeError("Event Stream does not support deleting existing slots in V1.")

        existing_messages = _load_messages(connection, root, "original_event_id")
        if existing_messages != safe_messages[:existing_count]:
            raise RuntimeError("Event Stream does not support rewriting existing slots in V1.")

        for slot_seq, message in enumerate(safe_messages[existing_count:], start=existing_count):
            created_at = _created_at_for_index(created_at_by_index, slot_seq)
            _append_message(connection, slot_seq, message, root, created_at=created_at)
        connection.commit()
    return event_stream_status(root)



def append_message(message, root=None, expected_slot_seq=None, created_at=None):
    safe_message = dict(message)
    with ensure_event_stream(root) as connection:
        existing_count = _message_count(connection)
        if expected_slot_seq is not None and existing_count != int(expected_slot_seq):
            raise RuntimeError(
                "Event Stream append position mismatch: "
                f"expected {int(expected_slot_seq)}, found {existing_count}."
            )

        new_artifacts = set()
        try:
            before_artifacts = {
                path.resolve()
                for path in artifacts_dir(root).glob("*.json")
            }
            _append_message(connection, existing_count, safe_message, root, created_at=created_at)
            connection.commit()
            new_artifacts = {
                path.resolve()
                for path in artifacts_dir(root).glob("*.json")
            } - before_artifacts
        except Exception:
            connection.rollback()
            after_artifacts = {
                path.resolve()
                for path in artifacts_dir(root).glob("*.json")
            }
            for path in after_artifacts - new_artifacts - before_artifacts:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
    return {
        "status": "appended",
        "slot_seq": existing_count,
    }



def load_messages(root=None):
    with ensure_event_stream(root) as connection:
        return _load_messages(connection, root, "original_event_id")



def load_active_projection(root=None):
    with ensure_event_stream(root) as connection:
        return _load_event_contents(connection, root, "active_event_id")



def load_active_trunk_records(root=None):
    with ensure_event_stream(root) as connection:
        rows = connection.execute(
            """
            SELECT
                slots.slot_seq,
                slots.source,
                slots.active_event_id,
                slots.original_event_id,
                events.kind AS active_kind,
                events.content_ref,
                events.inline_content,
                events.token_count AS active_token_count
            FROM slots
            JOIN events ON events.event_id = slots.active_event_id
            ORDER BY slots.slot_seq ASC
            """
        ).fetchall()
        records = []
        for row in rows:
            content_json = row["inline_content"] if row["content_ref"] == "inline" else _read_artifact(connection, row["content_ref"])
            records.append(
                {
                    "slot_seq": int(row["slot_seq"]),
                    "source": row["source"],
                    "active_event_id": row["active_event_id"],
                    "original_event_id": row["original_event_id"],
                    "active_kind": row["active_kind"],
                    "active_token_count": int(row["active_token_count"]),
                    "content": json.loads(content_json),
                }
            )
        return records


def load_active_slot_range(start_slot_seq, limit, root=None):
    """Load a bounded active-trunk range without altering Event Stream content."""
    start_slot_seq, limit = int(start_slot_seq), int(limit)
    if limit < 1:
        return []
    with ensure_event_stream(root) as connection:
        rows = connection.execute(
            """SELECT slots.slot_seq, slots.source, slots.active_event_id, slots.original_event_id,
                      events.kind AS active_kind, events.content_ref, events.inline_content,
                      events.token_count AS active_token_count
               FROM slots JOIN events ON events.event_id = slots.active_event_id
               WHERE slots.slot_seq >= ? ORDER BY slots.slot_seq ASC LIMIT ?""",
            (start_slot_seq, limit),
        ).fetchall()
        records = []
        for row in rows:
            content_json = row["inline_content"] if row["content_ref"] == "inline" else _read_artifact(connection, row["content_ref"])
            records.append({"slot_seq": int(row["slot_seq"]), "source": row["source"],
                            "active_event_id": row["active_event_id"], "original_event_id": row["original_event_id"],
                            "active_kind": row["active_kind"], "active_token_count": int(row["active_token_count"]),
                            "content": json.loads(content_json)})
        return records



def continuous_session_token_stats(root=None, gap_seconds=600):
    with ensure_event_stream(root) as connection:
        rows = connection.execute(
            """
            SELECT
                slots.slot_seq,
                slots.created_at,
                slots.source,
                events.token_count AS original_token_count
            FROM slots
            JOIN events ON events.event_id = slots.original_event_id
            WHERE slots.source != 'system'
            ORDER BY slots.slot_seq ASC
            """
        ).fetchall()
    if not rows:
        return {
            "recent_session_slot_count": 0,
            "recent_session_token_total": 0,
            "recent_session_token_average": 0,
            "recent_session_token_max": 0,
        }

    selected = [rows[-1]]
    previous_time = _parse_iso_time(rows[-1]["created_at"])
    for row in reversed(rows[:-1]):
        current_time = _parse_iso_time(row["created_at"])
        if previous_time is None or current_time is None:
            break
        gap = abs((previous_time - current_time).total_seconds())
        if gap > gap_seconds:
            break
        selected.append(row)
        previous_time = current_time

    tokens = [int(row["original_token_count"] or 0) for row in selected]
    total = sum(tokens)
    count = len(tokens)
    return {
        "recent_session_slot_count": count,
        "recent_session_token_total": total,
        "recent_session_token_average": int(total / count) if count else 0,
        "recent_session_token_max": max(tokens) if tokens else 0,
    }


def load_current_session_messages(process_started_at=None, gap_seconds=CONTINUOUS_SESSION_GAP_SECONDS, max_slot_seq=None, root=None):
    query = """
        SELECT slots.slot_seq, slots.created_at, slots.source,
               events.content_ref, events.inline_content
        FROM slots
        JOIN events ON events.event_id = slots.original_event_id
        WHERE slots.source != 'system'
    """
    parameters = []
    if max_slot_seq is not None:
        query += " AND slots.slot_seq <= ?"
        parameters.append(int(max_slot_seq))
    query += " ORDER BY slots.slot_seq ASC"
    with ensure_event_stream(root) as connection:
        rows = connection.execute(query, parameters).fetchall()

        records = []
        for row in rows:
            content_json = row["inline_content"] if row["content_ref"] == "inline" else _read_artifact(connection, row["content_ref"])
            content = json.loads(content_json)
            if not isinstance(content, dict) or _message_role(content) == "system":
                continue
            records.append(
                {
                    "slot_seq": int(row["slot_seq"]),
                    "created_at": row["created_at"],
                    "content": content,
                }
            )

    if not records:
        return []
    process_time = _parse_iso_time(process_started_at)
    if process_time is not None:
        records = [
            record
            for record in records
            if (record_time := _parse_iso_time(record["created_at"])) is not None and record_time >= process_time
        ]
    if not records:
        return []

    user_records = [record for record in records if _message_role(record["content"]) == "user"]
    if not user_records:
        return []
    session_start_seq = user_records[-1]["slot_seq"]
    previous_user_time = _parse_iso_time(user_records[-1]["created_at"])
    for record in reversed(user_records[:-1]):
        current_user_time = _parse_iso_time(record["created_at"])
        if previous_user_time is None or current_user_time is None:
            break
        if abs((previous_user_time - current_user_time).total_seconds()) > gap_seconds:
            break
        session_start_seq = record["slot_seq"]
        previous_user_time = current_user_time
    return [record["content"] for record in records if record["slot_seq"] >= session_start_seq]



def load_message(message_index, root=None):
    message_index = int(message_index)
    messages = load_messages(root)
    if message_index < 0 or message_index >= len(messages):
        raise RuntimeError(f"Message index out of range: {message_index}")
    return messages[message_index]



def create_sum_event(slot_seq, metadata, root=None):
    slot_seq = int(slot_seq)
    content = dict(metadata or {})
    with ensure_event_stream(root) as connection:
        slot = connection.execute(
            "SELECT * FROM slots WHERE slot_seq = ?",
            (slot_seq,),
        ).fetchone()
        if slot is None:
            raise RuntimeError(f"Event Stream slot not found: {slot_seq}")

        event_id = f"evt_{uuid4().hex}"
        created_at = _now()
        content.setdefault("created_at", created_at)
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True)
        content_ref, inline_content, token_count = _store_content(connection, content_json, "system", root)
        connection.execute(
            """
            INSERT INTO events (event_id, slot_id, kind, content_ref, inline_content, token_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, slot["slot_id"], "sum", content_ref, inline_content, token_count, created_at),
        )
        connection.execute(
            "UPDATE slots SET active_event_id = ? WHERE slot_seq = ?",
            (event_id, slot_seq),
        )
        connection.commit()
        return load_event(event_id, root=root)



def clear_event_stream_for_tests(root=None):
    db_path = event_stream_db_path(root)
    if db_path.exists():
        db_path.unlink()
    directory = artifacts_dir(root)
    if directory.exists():
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()


def _message_count(connection):
    row = connection.execute("SELECT COUNT(*) AS count FROM slots").fetchone()
    return int(row["count"])


def _storage_counts(connection):
    slot_count = connection.execute("SELECT COUNT(*) AS count FROM slots").fetchone()["count"]
    event_count = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
    inline_event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE content_ref = 'inline'"
    ).fetchone()["count"]
    artifact_event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE content_ref != 'inline'"
    ).fetchone()["count"]
    raw_event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE kind = 'raw'"
    ).fetchone()["count"]
    ref_event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE kind = 'ref'"
    ).fetchone()["count"]
    sum_event_count = connection.execute(
        "SELECT COUNT(*) AS count FROM events WHERE kind = 'sum'"
    ).fetchone()["count"]
    artifact_row = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes FROM artifacts"
    ).fetchone()
    return {
        "slot_count": int(slot_count),
        "event_count": int(event_count),
        "inline_event_count": int(inline_event_count),
        "artifact_event_count": int(artifact_event_count),
        "raw_event_count": int(raw_event_count),
        "ref_event_count": int(ref_event_count),
        "sum_event_count": int(sum_event_count),
        "artifact_count": int(artifact_row["count"]),
        "artifact_bytes": int(artifact_row["bytes"]),
    }


def _row_dict(row):
    return {key: row[key] for key in row.keys()}


def _parse_iso_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _created_at_for_index(created_at_by_index, index):
    if isinstance(created_at_by_index, dict):
        return created_at_by_index.get(index)
    if isinstance(created_at_by_index, (list, tuple)) and 0 <= index < len(created_at_by_index):
        return created_at_by_index[index]
    return None


def _append_message(connection, slot_seq, message, root, created_at=None):
    slot_id = f"slot_{uuid4().hex}"
    event_id = f"evt_{uuid4().hex}"
    created_at = str(created_at or _now())
    source = _message_source(message)
    provider_role = _message_role(message)
    kind = "ref" if provider_role in {"tool", "file"} else "raw"
    content_json = json.dumps(message, ensure_ascii=False, sort_keys=True)
    content_ref, inline_content, token_count = _store_content(connection, content_json, provider_role, root)

    connection.execute(
        """
        INSERT INTO slots (slot_id, slot_seq, created_at, source, active_event_id, original_event_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (slot_id, slot_seq, created_at, source, event_id, event_id),
    )
    connection.execute(
        """
        INSERT INTO events (event_id, slot_id, kind, content_ref, inline_content, token_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, slot_id, kind, content_ref, inline_content, token_count, created_at),
    )


def _store_content(connection, content_json, source, root):
    token_count = count_text_tokens(content_json)
    if len(content_json) <= INLINE_CONTENT_MAX_CHARS:
        return "inline", content_json, token_count

    artifact_id = f"art_{uuid4().hex}"
    content_bytes = content_json.encode("utf-8")
    digest = hashlib.sha256(content_bytes).hexdigest()
    path = artifacts_dir(root) / f"{artifact_id}.json"
    path.write_bytes(content_bytes)
    artifact_type = "tool_output" if source == "tool" else "text"
    connection.execute(
        """
        INSERT INTO artifacts (artifact_id, artifact_type, storage_path, size_bytes, token_count, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (artifact_id, artifact_type, str(path), len(content_bytes), token_count, digest, _now()),
    )
    return artifact_id, None, token_count


def _load_messages(connection, root, event_id_column):
    messages = []
    for content in _load_event_contents(connection, root, event_id_column):
        if isinstance(content, dict):
            messages.append(content)
    return messages


def _load_event_contents(connection, root, event_id_column):
    if event_id_column not in {"active_event_id", "original_event_id"}:
        raise RuntimeError(f"Invalid Event Stream projection column: {event_id_column}")
    rows = connection.execute(
        f"""
        SELECT slots.slot_seq, events.content_ref, events.inline_content
        FROM slots
        JOIN events ON events.event_id = slots.{event_id_column}
        ORDER BY slots.slot_seq ASC
        """
    ).fetchall()
    contents = []
    for row in rows:
        content_json = row["inline_content"] if row["content_ref"] == "inline" else _read_artifact(connection, row["content_ref"])
        contents.append(json.loads(content_json))
    return contents


def _read_artifact(connection, artifact_id):
    row = connection.execute(
        "SELECT storage_path, content_hash FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Event Stream artifact is missing: {artifact_id}")
    path = Path(row["storage_path"])
    content_bytes = path.read_bytes()
    digest = hashlib.sha256(content_bytes).hexdigest()
    if digest != row["content_hash"]:
        raise RuntimeError(f"Event Stream artifact hash mismatch: {artifact_id}")
    return content_bytes.decode("utf-8")


def _message_source(message):
    explicit = message.get("source") if isinstance(message, dict) else None
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return _legacy_role_source(_message_role(message))


def _message_role(message):
    return str((message or {}).get("role") or "")


def _legacy_role_source(role):
    if role == "assistant":
        return "sema"
    if role in {"user", "tool", "system"}:
        return role
    if role == "file":
        return "file"
    return "system"
