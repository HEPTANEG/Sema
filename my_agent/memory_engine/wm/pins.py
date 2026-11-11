import json
from datetime import datetime, timezone

import memory_engine.event_stream as event_stream


TABLE = "working_memory_pins"
CURRENT_PIN_KEY = "current_user_turn"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_table(connection):
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            pin_key TEXT PRIMARY KEY,
            source_slot_seq INTEGER NOT NULL,
            original_event_id TEXT NOT NULL,
            token_count INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _has_table(connection):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TABLE,),
    ).fetchone()
    return row is not None


def is_external_user_message(message):
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    source = str(message.get("source") or "user").strip().lower()
    return source == "user"


def clear_current_pin():
    if not event_stream.event_stream_db_path().exists():
        return False
    with event_stream.ensure_event_stream() as connection:
        if not _has_table(connection):
            return False
        cursor = connection.execute(f"DELETE FROM {TABLE} WHERE pin_key = ?", (CURRENT_PIN_KEY,))
        connection.commit()
        return bool(cursor.rowcount)


def on_message_persisted(message):
    if is_external_user_message(message):
        clear_current_pin()


def load_current_pin(create=False):
    if not event_stream.event_stream_db_path().exists():
        return None
    with event_stream.ensure_event_stream() as connection:
        if not _has_table(connection):
            if not create:
                return None
            _ensure_table(connection)
        row = connection.execute(
            f"SELECT * FROM {TABLE} WHERE pin_key = ?",
            (CURRENT_PIN_KEY,),
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}


def current_external_user_slot(messages=None):
    messages = list(messages or event_stream.load_messages())
    if not messages or not event_stream.event_stream_db_path().exists():
        return None
    with event_stream.ensure_event_stream() as connection:
        for slot_seq in range(len(messages) - 1, -1, -1):
            if messages[slot_seq].get("role") != "user":
                continue
            row = connection.execute(
                "SELECT source, original_event_id FROM slots WHERE slot_seq = ?",
                (slot_seq,),
            ).fetchone()
            if row is None:
                continue
            if str(row["source"] or "").strip().lower() != "user":
                continue
            event = connection.execute(
                "SELECT token_count FROM events WHERE event_id = ?",
                (row["original_event_id"],),
            ).fetchone()
            return {
                "source_slot_seq": slot_seq,
                "original_event_id": row["original_event_id"],
                "token_count": int(event["token_count"] or 0) if event else 0,
            }
    return None


def resolve_pin_for_projection(stm_projection, messages=None, persist=False):
    source = current_external_user_slot(messages)
    if source is None:
        return None
    slot_seq = int(source["source_slot_seq"])
    selected = _projection_contains(stm_projection.get("selected_units") or [], slot_seq)
    omitted = _projection_contains(stm_projection.get("omitted_units") or [], slot_seq)
    if selected or not omitted:
        return None
    pin = {
        **source,
        "pin_key": CURRENT_PIN_KEY,
        "created_at": _now(),
    }
    if persist:
        with event_stream.ensure_event_stream() as connection:
            _ensure_table(connection)
            connection.execute(
                f"""
                INSERT INTO {TABLE}
                    (pin_key, source_slot_seq, original_event_id, token_count, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pin_key) DO UPDATE SET
                    source_slot_seq = excluded.source_slot_seq,
                    original_event_id = excluded.original_event_id,
                    token_count = excluded.token_count,
                    created_at = excluded.created_at
                """,
                (
                    CURRENT_PIN_KEY,
                    pin["source_slot_seq"],
                    pin["original_event_id"],
                    pin["token_count"],
                    pin["created_at"],
                ),
            )
            connection.commit()
    return pin


def pin_record(pin):
    if not pin:
        return None
    slot_seq = int(pin["source_slot_seq"])
    message = event_stream.load_message(slot_seq)
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    rendered = (
        "Pinned current-turn user instruction "
        "(this might be important for completing the current task):\n"
        f"[slot {slot_seq}] {content}"
    )
    return {
        **dict(pin),
        "content": content,
        "rendered": rendered,
    }


def pin_status():
    pin = load_current_pin(create=False)
    if not pin:
        return {"active": False}
    return {
        "active": True,
        "source_slot_seq": int(pin["source_slot_seq"]),
        "original_event_id": pin["original_event_id"],
        "token_count": int(pin["token_count"]),
        "created_at": pin["created_at"],
    }


def _projection_contains(units, slot_seq):
    return any(
        int(unit.get("start_index", -1)) <= slot_seq <= int(unit.get("end_index", -1))
        for unit in units
    )
