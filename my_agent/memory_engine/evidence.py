import hashlib
import copy
import json
import sqlite3
from pathlib import Path

import memory_engine.event_stream as event_stream
from .stm_projection import build_stm_projection
from .capacity_planner import preview_stm_capacity
from .dynamic_context_budget import compute_and_store_dynamic_context_budget
from .wm.ingress import effective_wm_ingress_watermark


DEFAULT_PREVIEW_CHARS = 1200
DEFAULT_STM_EVIDENCE_CHARS = 2000
VALID_PROJECTIONS = {"active", "original"}


AUDIT_KEY = "memory_engine_evidence_audit"
MAX_AUDIT_ROWS = 100


def inspect_slot(slot_seq, projection="active", max_chars=DEFAULT_PREVIEW_CHARS, runtime_state=None):
    query = {"type": "slot", "slot_seq": _safe_int(slot_seq), "projection": projection, "max_chars": max_chars}
    projection = _normalize_projection(projection)
    if projection is None:
        return _with_audit(_error(query, f"invalid projection: {projection}"), runtime_state)
    with _read_connection() as connection:
        if connection is None:
            return _with_audit(_error(query, "event stream database not found"), runtime_state)
        record = _slot_record(connection, query["slot_seq"], projection, max_chars=max_chars)
        if record is None:
            return _with_audit(_error(query, f"slot not found: {query['slot_seq']}"), runtime_state)
        return _with_audit(_ok(query, [record]), runtime_state)


def inspect_range(start, end, projection="active", max_chars=DEFAULT_PREVIEW_CHARS, runtime_state=None):
    start = _safe_int(start)
    end = _safe_int(end)
    query = {"type": "range", "start": start, "end": end, "projection": projection, "max_chars": max_chars}
    projection = _normalize_projection(projection)
    if projection is None:
        return _with_audit(_error(query, f"invalid projection: {projection}"), runtime_state)
    if end < start:
        return _with_audit(_error(query, "range end must be >= start"), runtime_state)
    with _read_connection() as connection:
        if connection is None:
            return _with_audit(_error(query, "event stream database not found"), runtime_state)
        evidence = [
            record
            for slot_seq in range(start, end + 1)
            if (record := _slot_record(connection, slot_seq, projection, max_chars=max_chars)) is not None
        ]
        if not evidence:
            return _with_audit(_error(query, f"no slots found in range: {start}..{end}"), runtime_state)
        missing = [slot_seq for slot_seq in range(start, end + 1) if slot_seq not in {record["slot_seq"] for record in evidence}]
        result = _ok(query, evidence)
        if missing:
            result["errors"].append({"message": "some slots were not found", "slot_seqs": missing})
        return _with_audit(result, runtime_state)


def inspect_interaction_block(slot_seq, projection="active", max_chars=DEFAULT_PREVIEW_CHARS, runtime_state=None):
    slot_seq = _safe_int(slot_seq)
    projection = _normalize_projection(projection)
    query = {"type": "interaction_block", "slot_seq": slot_seq, "projection": projection, "max_chars": max_chars}
    if projection is None:
        return _with_audit(_error(query, f"invalid projection: {projection}"), runtime_state)
    with _read_connection() as connection:
        if connection is None:
            return _with_audit(_error(query, "event stream database not found"), runtime_state)
        bounds = _interaction_block_bounds(connection, slot_seq)
        if bounds is None:
            return _with_audit(_error(query, f"slot not found: {slot_seq}"), runtime_state)
    start, end = bounds
    result = inspect_range(start, end, projection=projection, max_chars=max_chars)
    result["query"] = {**query, "start": start, "end": end}
    return _with_audit(result, runtime_state)


def expand_around(slot_seq, before_blocks=1, after_blocks=1, projection="active", max_chars=DEFAULT_PREVIEW_CHARS, runtime_state=None):
    slot_seq = _safe_int(slot_seq)
    before_blocks = max(0, _safe_int(before_blocks))
    after_blocks = max(0, _safe_int(after_blocks))
    projection = _normalize_projection(projection)
    query = {
        "type": "expand_around",
        "slot_seq": slot_seq,
        "before_blocks": before_blocks,
        "after_blocks": after_blocks,
        "projection": projection,
        "max_chars": max_chars,
    }
    if projection is None:
        return _with_audit(_error(query, f"invalid projection: {projection}"), runtime_state)
    with _read_connection() as connection:
        if connection is None:
            return _with_audit(_error(query, "event stream database not found"), runtime_state)
        blocks = _interaction_blocks(connection)
        target_index = next((index for index, block in enumerate(blocks) if block["start"] <= slot_seq <= block["end"]), None)
        if target_index is None:
            return _with_audit(_error(query, f"slot not found: {slot_seq}"), runtime_state)
        selected = blocks[max(0, target_index - before_blocks) : target_index + after_blocks + 1]
        start = min(block["start"] for block in selected)
        end = max(block["end"] for block in selected)
    result = inspect_range(start, end, projection=projection, max_chars=max_chars)
    result["query"] = {**query, "start": start, "end": end}
    return _with_audit(result, runtime_state)


def inspect_artifact(artifact_id, max_chars=2000, runtime_state=None):
    artifact_id = str(artifact_id or "")
    query = {"type": "artifact", "artifact_id": artifact_id, "max_chars": max_chars}
    with _read_connection() as connection:
        if connection is None:
            return _with_audit(_error(query, "event stream database not found"), runtime_state)
        row = connection.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            return _with_audit(_error(query, f"artifact not found: {artifact_id}"), runtime_state)
        try:
            content_json = _read_artifact_json(row)
        except RuntimeError as error:
            return _with_audit(_error(query, str(error)), runtime_state)
        content = json.loads(content_json)
        preview, truncated = _preview_content(content, max_chars=max_chars)
        return _with_audit({
            "ok": True,
            "query": query,
            "artifact": {
                "artifact_id": row["artifact_id"],
                "artifact_type": row["artifact_type"],
                "size_bytes": int(row["size_bytes"]),
                "token_count": int(row["token_count"]),
                "content_preview": preview,
                "truncated": truncated,
            },
            "evidence": [],
            "errors": [],
        }, runtime_state)


def get_stm_evidence(messages=None, runtime_state=None, max_chars=DEFAULT_STM_EVIDENCE_CHARS):
    current_messages = messages or event_stream.load_messages()
    if isinstance(runtime_state, dict):
        try:
            runtime_copy = copy.deepcopy(runtime_state)
        except Exception:
            runtime_copy = dict(runtime_state)
    else:
        runtime_copy = {}
    pressure = compute_and_store_dynamic_context_budget(
        current_messages,
        runtime_copy,
        emit_observer=False,
        stabilize=False,
        apply_stabilizer=False,
    )
    watermark = effective_wm_ingress_watermark(runtime_state)
    capacity = preview_stm_capacity(current_messages, pressure, watermark, runtime_copy, advance_seq=False)
    budget = capacity["effective_budget"]
    projection = build_stm_projection(
        current_messages,
        budget=budget,
        wm_ingress_watermark=watermark,
    )
    text = json.dumps(
        {
            "selected_unit_count": len(projection.get("selected_units") or []),
            "selected_tokens": projection.get("selected_tokens", 0),
            "messages": projection.get("messages", []),
        },
        ensure_ascii=False,
    )
    preview, truncated = _preview_text(text, max_chars)
    return _with_audit({
        "ok": True,
        "query": {
            "type": "stm_evidence",
            "budget": budget,
            "max_chars": max_chars,
            "wm_ingress_watermark": watermark,
        },
        "selected_unit_count": len(projection.get("selected_units") or []),
        "selected_tokens": projection.get("selected_tokens", 0),
        "content_preview": preview,
        "truncated": truncated,
        "errors": [],
    }, runtime_state)


def evidence_audit(runtime_state=None):
    if not isinstance(runtime_state, dict):
        return []
    rows = runtime_state.get(AUDIT_KEY)
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows]


def _slot_record(connection, slot_seq, projection, max_chars=DEFAULT_PREVIEW_CHARS):
    event_column = "active_event_id" if projection == "active" else "original_event_id"
    row = connection.execute(
        f"""
        SELECT
            slots.slot_seq,
            slots.source,
            slots.active_event_id,
            slots.original_event_id,
            events.event_id,
            events.kind,
            events.content_ref,
            events.inline_content,
            events.token_count
        FROM slots
        JOIN events ON events.event_id = slots.{event_column}
        WHERE slots.slot_seq = ?
        """,
        (slot_seq,),
    ).fetchone()
    if row is None:
        return None
    content_json = row["inline_content"]
    artifact_id = ""
    if row["content_ref"] != "inline":
        artifact_id = row["content_ref"]
        artifact_row = connection.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if artifact_row is None:
            content = {"error": f"artifact not found: {artifact_id}"}
        else:
            content_json = _read_artifact_json(artifact_row)
            content = json.loads(content_json)
    else:
        content = json.loads(content_json)
    preview, truncated = _preview_content(content, max_chars=max_chars)
    return {
        "slot_seq": int(row["slot_seq"]),
        "source": row["source"],
        "event_id": row["event_id"],
        "event_kind": row["kind"],
        "projection": projection,
        "token_count": int(row["token_count"]),
        "content_preview": preview,
        "truncated": truncated,
        "artifact_id": artifact_id,
        "active_event_id": row["active_event_id"],
        "original_event_id": row["original_event_id"],
    }


def _interaction_block_bounds(connection, slot_seq):
    blocks = _interaction_blocks(connection)
    for block in blocks:
        if block["start"] <= slot_seq <= block["end"]:
            return block["start"], block["end"]
    return None


def _interaction_blocks(connection):
    rows = connection.execute("SELECT slot_seq, source FROM slots ORDER BY slot_seq ASC").fetchall()
    if not rows:
        return []
    blocks = []
    current = None
    for row in rows:
        slot_seq = int(row["slot_seq"])
        if row["source"] == "user":
            if current is not None:
                current["end"] = slot_seq - 1
                blocks.append(current)
            current = {"start": slot_seq, "end": slot_seq}
            continue
        if current is None:
            current = {"start": slot_seq, "end": slot_seq, "orphan": True}
        else:
            current["end"] = slot_seq
    if current is not None:
        blocks.append(current)
    return blocks


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


def _read_artifact_json(row):
    path = Path(row["storage_path"])
    content_bytes = path.read_bytes()
    digest = hashlib.sha256(content_bytes).hexdigest()
    if digest != row["content_hash"]:
        raise RuntimeError(f"Event Stream artifact hash mismatch: {row['artifact_id']}")
    return content_bytes.decode("utf-8")


def _preview_content(content, max_chars=DEFAULT_PREVIEW_CHARS):
    if isinstance(content, dict) and isinstance(content.get("content"), str):
        return _preview_text(content["content"], max_chars)
    if isinstance(content, dict) and isinstance(content.get("summary"), str):
        return _preview_text(content["summary"], max_chars)
    return _preview_text(json.dumps(content, ensure_ascii=False), max_chars)


def _preview_text(text, max_chars):
    text = str(text or "")
    max_chars = max(0, _safe_int(max_chars))
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _normalize_projection(projection):
    projection = str(projection or "active").strip().lower()
    return projection if projection in VALID_PROJECTIONS else None


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ok(query, evidence):
    return {"ok": True, "query": query, "evidence": evidence, "errors": []}


def _error(query, message):
    return {"ok": False, "query": query, "evidence": [], "errors": [{"message": message}]}


def _with_audit(result, runtime_state):
    _record_audit(runtime_state, result)
    return result


def _record_audit(runtime_state, result):
    if not isinstance(runtime_state, dict):
        return
    query = dict((result or {}).get("query") or {})
    evidence_rows = (result or {}).get("evidence") or []
    if (result or {}).get("artifact"):
        result_count = 1
    else:
        result_count = len(evidence_rows)
    row = {
        "query_type": query.get("type", ""),
        "projection": query.get("projection", ""),
        "slot_seq": query.get("slot_seq", ""),
        "start": query.get("start", ""),
        "end": query.get("end", ""),
        "before_blocks": query.get("before_blocks", ""),
        "after_blocks": query.get("after_blocks", ""),
        "artifact_id": query.get("artifact_id", ""),
        "max_chars": query.get("max_chars", ""),
        "result_count": result_count,
        "error_count": len((result or {}).get("errors") or []),
        "ok": bool((result or {}).get("ok")),
        "read_only": True,
    }
    rows = runtime_state.setdefault(AUDIT_KEY, [])
    if not isinstance(rows, list):
        rows = []
        runtime_state[AUDIT_KEY] = rows
    rows.append(row)
    if len(rows) > MAX_AUDIT_ROWS:
        del rows[:-MAX_AUDIT_ROWS]
