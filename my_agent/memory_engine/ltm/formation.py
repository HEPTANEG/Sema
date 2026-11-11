"""LTM V2 Formation runtime for the Event Stream architecture.

Formation is deliberately separate from the LTM Core Store.  This module
selects durable evidence batches, asks one Formation Agent for a write
decision, and lets the runtime add IDs and provenance before committing.
"""

import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .. import event_stream
from model.provider import call_chat_completion
from persistence.trajectory import (
    complete_span,
    complete_trajectory,
    fail_span,
    fail_trajectory,
    provider_exchange_observer,
    record_event,
    start_span,
    start_trajectory,
)
from shared.prompt_loader import load_prompt

from .. import context_builder
from . import ltm_retrieval, ltm_store, meta_memory
from .formation_progress import FormationRetrievalProgress
from ..wm import ingress as wm_ingress
from ..focus import store as focus_store
from ..dynamic_context_budget import compute_and_store_dynamic_context_budget
from ..stm_projection import build_stm_projection
from ..token_counter import count_text_tokens


FORMATION_AGENT = "ltm-formation-v4"
FORMATION_TIMEOUT_SECONDS = 60
FORMATION_MAX_TOOL_CALLS = 20
FORMATION_MAX_MODEL_ROUNDS = FORMATION_MAX_TOOL_CALLS + 2
FORMATION_MAX_PLAN_REPAIRS = 2
FORMATION_BUFFER_RATIO = 0.5
FORMATION_STATUSES = {"pending", "processing", "completed", "failed"}
FORMATION_PRIORITIES = {"resolved": 0, "inactive": 1, "active": 2, "unassigned": 3}


class FormationLoopError(RuntimeError):
    def __init__(self, message, metrics=None):
        super().__init__(message)
        self.metrics = dict(metrics or {})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS formation_work_items (
            work_item_id TEXT PRIMARY KEY,
            work_key TEXT NOT NULL UNIQUE,
            source_kind TEXT NOT NULL,
            segment_id TEXT,
            segment_status TEXT NOT NULL,
            segment_title TEXT NOT NULL,
            segment_summary TEXT,
            segment_ranges TEXT NOT NULL DEFAULT '[]',
            priority INTEGER NOT NULL,
            pending_slot_ranges TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (source_kind IN ('segment', 'unassigned')),
            CHECK (status IN ('pending', 'processing', 'completed', 'failed'))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS formation_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_formation_work_items_status_priority "
        "ON formation_work_items(status, priority, created_at)"
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(formation_work_items)").fetchall()}
    if "segment_ranges" not in columns:
        connection.execute("ALTER TABLE formation_work_items ADD COLUMN segment_ranges TEXT NOT NULL DEFAULT '[]'")
    connection.commit()


def _state_set(connection, key, value):
    connection.execute(
        """
        INSERT INTO formation_state(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def _state_get(connection, key, default=None):
    row = connection.execute("SELECT value FROM formation_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _json(value, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _ranges(ranges):
    normalized = []
    for item in ranges or []:
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        normalized.append({"start": start, "end": end})
    normalized.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for item in normalized:
        if not merged or item["start"] > merged[-1]["end"] + 1:
            merged.append(dict(item))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
    return merged


def _slots_from_ranges(ranges):
    slots = set()
    for item in _ranges(ranges):
        slots.update(range(item["start"], item["end"] + 1))
    return slots


def _ranges_from_slots(slots):
    ordered = sorted({int(slot) for slot in slots})
    result = []
    for slot in ordered:
        if not result or slot > result[-1]["end"] + 1:
            result.append({"start": slot, "end": slot})
        else:
            result[-1]["end"] = slot
    return result


def _subtract_slots(slots, covered_slots):
    return {int(slot) for slot in slots} - {int(slot) for slot in covered_slots}


def _split_slots(slots, budget, records_by_slot):
    """Split a large source into contiguous full-slot batches."""
    ordered = sorted({int(slot) for slot in slots})
    if not ordered:
        return []
    chunks = []
    current = []
    current_tokens = 0
    for slot in ordered:
        tokens = int(records_by_slot.get(slot, {}).get("active_token_count") or 0)
        if current and current_tokens + tokens > budget:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(slot)
        current_tokens += tokens
    if current:
        chunks.append(current)
    return chunks


def _active_records_by_slot(root=None):
    return {int(record["slot_seq"]): record for record in event_stream.load_active_trunk_records(root)}


def _historical_session_slots(slot_range, root=None):
    """Return non-system slots in an explicitly finalized historical session."""
    if not isinstance(slot_range, dict):
        return set()
    try:
        start = int(slot_range["start"])
        end = int(slot_range["end"])
    except (KeyError, TypeError, ValueError):
        return set()
    if start < 0 or end < start:
        return set()
    records = _active_records_by_slot(root)
    return {
        slot_seq
        for slot_seq in range(start, end + 1)
        if slot_seq in records and records[slot_seq].get("source") != "system"
    }


def _buffer_slots(messages, runtime_state=None, root=None, historical_slot_range=None):
    if historical_slot_range is not None:


        return _historical_session_slots(historical_slot_range, root=root), int(
            (compute_and_store_dynamic_context_budget(
                messages or [], runtime_state, emit_observer=False, stabilize=False
            ).get("budgets") or {}).get("wm") or 6000
        )
    if not messages:
        return set(), 0
    pressure = compute_and_store_dynamic_context_budget(
        messages,
        runtime_state,
        emit_observer=False,
        stabilize=False,
        apply_stabilizer=False,
    )
    budgets = pressure.get("budgets") or {}
    stm_budget = wm_ingress.current_stm_budget(messages, runtime_state)
    wm_budget = int(budgets.get("wm") or 6000)
    watermark = wm_ingress.effective_wm_ingress_watermark(runtime_state)
    projection = build_stm_projection(messages, budget=stm_budget, wm_ingress_watermark=watermark)
    try:
        wm = context_builder._select_working_memory_records(projection, budget=wm_budget, root=root)
    except RuntimeError:
        return set(), wm_budget
    records = list(wm.get("selected_records") or [])
    if not records:
        return set(), wm_budget
    selected = set()
    cumulative = 0
    threshold = wm_budget * FORMATION_BUFFER_RATIO
    for record in records:
        selected.add(int(record["slot_seq"]))
        cumulative += int(record.get("rendered_tokens") or record.get("active_token_count") or count_text_tokens(record.get("rendered", "")))
        if cumulative > threshold:
            break
    return selected, wm_budget


def maintainable_slot_frontier(messages=None, runtime_state=None, root=None, historical_slot_range=None):
    """Return the newest slot currently in Formation's dynamic buffer.

    The buffer is the shared boundary between the unstable STM/new WM
    projection and material that Maintainer may use as Event Stream
    provenance.  A missing buffer is represented by -1, rather than by the
    newest Event Stream slot.
    """
    if messages is None:
        if not event_stream.event_stream_db_path(root).exists():
            return -1
        messages = event_stream.load_messages(root)
    if historical_slot_range is None:
        buffer_slots, _budget = _buffer_slots(messages, runtime_state, root=root)
    else:
        buffer_slots, _budget = _buffer_slots(
            messages, runtime_state, root=root, historical_slot_range=historical_slot_range
        )
    return max(buffer_slots) if buffer_slots else -1


def _segment_ranges_in_buffer(segment, buffer_slots, completed_slots):
    segment_slots = _slots_from_ranges(segment.get("slot_ranges", []))
    pending = _subtract_slots(segment_slots & buffer_slots, completed_slots)
    return pending


def _work_item_rows(connection, statuses=None):
    query = "SELECT * FROM formation_work_items"
    params = []
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        query += f" WHERE status IN ({placeholders})"
        params.extend(statuses)
    query += " ORDER BY priority ASC, created_at ASC, work_item_id ASC"
    rows = connection.execute(query, params).fetchall()
    result = [{key: row[key] for key in row.keys()} for row in rows]
    if statuses:
        from runtime.setup_capabilities import skipped_slots
        skipped = skipped_slots(connection)
        result = [row for row in result if _slots_from_ranges(_row_ranges(row)) - skipped]
    return result


def _row_ranges(row):
    return _ranges(_json(row.get("pending_slot_ranges"), []))


def _create_work_item(connection, source_kind, segment, ranges, priority):
    ranges = _ranges(ranges)
    if not ranges:
        return None
    segment_id = segment.get("segment_id") if segment else None
    work_key = json.dumps(
        {"source_kind": source_kind, "segment_id": segment_id, "ranges": ranges},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    existing = connection.execute(
        "SELECT work_item_id FROM formation_work_items WHERE work_key = ?", (work_key,)
    ).fetchone()
    if existing:
        return existing["work_item_id"], False
    timestamp = _now()
    work_item_id = f"formation_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO formation_work_items(
            work_item_id, work_key, source_kind, segment_id, segment_status,
            segment_title, segment_summary, segment_ranges, priority, pending_slot_ranges,
            status, attempts, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?)
        """,
        (
            work_item_id,
            work_key,
            source_kind,
            segment_id,
            (segment or {}).get("status") or "unassigned",
            (segment or {}).get("title") or "",
            (segment or {}).get("summary") if segment else None,
            json.dumps((segment or {}).get("slot_ranges") or [], ensure_ascii=False, sort_keys=True),
            int(priority),
            json.dumps(ranges, ensure_ascii=False, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    return work_item_id, True


def _refresh_work_items(messages, runtime_state=None, root=None, historical_slot_range=None):
    """Discover buffer work without writing any Event Stream or LTM facts."""
    if root is None:


        if historical_slot_range is None:
            buffer_slots, wm_budget = _buffer_slots(messages, runtime_state)
        else:
            buffer_slots, wm_budget = _buffer_slots(
                messages, runtime_state, historical_slot_range=historical_slot_range
            )
    else:
        if historical_slot_range is None:
            buffer_slots, wm_budget = _buffer_slots(messages, runtime_state, root=root)
        else:
            buffer_slots, wm_budget = _buffer_slots(
                messages, runtime_state, root=root, historical_slot_range=historical_slot_range
            )
    if not buffer_slots:
        return {"buffer_slot_count": 0, "created_count": 0, "wm_budget": wm_budget}

    from runtime.setup_capabilities import excluded_slots
    buffer_slots -= excluded_slots(root)
    records_by_slot = _active_records_by_slot(root)
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)

        connection.execute(
            "UPDATE formation_work_items SET status = 'failed', last_error = 'recovered stale processing state', updated_at = ? WHERE status = 'processing'",
            (_now(),),
        )
        existing_rows = _work_item_rows(connection)
        completed_slots = set()
        open_slots = set()
        for row in existing_rows:
            slots = _slots_from_ranges(_row_ranges(row))
            if row["status"] == "completed":
                completed_slots.update(slots)
            else:
                open_slots.update(slots)

        focus_store.ensure_schema(connection)
        segments = focus_store.load_segments(connection)
        covered_by_segment = set()
        created_count = 0
        for segment in segments:
            segment_slots = _segment_ranges_in_buffer(
                segment,
                buffer_slots,
                completed_slots | open_slots,
            )
            if not segment_slots:
                continue
            covered_by_segment.update(segment_slots)
            chunks = _split_slots(segment_slots, wm_budget, records_by_slot)
            for chunk in chunks:
                _work_item_id, created = _create_work_item(
                    connection,
                    "segment",
                    segment,
                    _ranges_from_slots(chunk),
                    FORMATION_PRIORITIES.get(segment.get("status"), 2),
                )
                if created:
                    created_count += 1

        unassigned = buffer_slots - covered_by_segment
        unassigned -= completed_slots
        unassigned -= open_slots
        for chunk in _split_slots(unassigned, wm_budget, records_by_slot):
            _work_item_id, created = _create_work_item(connection, "unassigned", None, _ranges_from_slots(chunk), FORMATION_PRIORITIES["unassigned"])
            if created:
                created_count += 1
        connection.commit()
    return {"buffer_slot_count": len(buffer_slots), "created_count": created_count, "wm_budget": wm_budget}


def _projection_records(slot_ranges, root=None):
    records_by_slot = _active_records_by_slot(root)
    result = []
    for slot_seq in sorted(_slots_from_ranges(slot_ranges)):
        record = records_by_slot.get(slot_seq)
        if not record or record.get("source") == "system":
            continue
        content = record.get("content")
        if isinstance(content, dict):
            role = content.get("role") or record.get("source") or "unknown"
            body = content.get("content", "")
            if not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
        else:
            role = record.get("source") or "unknown"
            body = str(content or "")
        if record.get("active_kind") == "sum" and isinstance(content, dict):
            body = content.get("summary") or body
            role = f"{role} summary"
        result.append({"role": str(role), "content": str(body)})
    return result


def _load_work_item(connection, work_item_id):
    row = connection.execute("SELECT * FROM formation_work_items WHERE work_item_id = ?", (work_item_id,)).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def _agent_projection(item, records, system_root_tags):
    partial = _item_is_partial(item)
    projection = {
        "segment_status": item.get("segment_status") or "unassigned",
        "segment_title": item.get("segment_title") or None,
        "content": records,
        "system_root_tags": [
            {
                "object_id": tag["object_id"],
                "tag_key": tag["tag_key"],
                "title": tag["title"],
            }
            for tag in system_root_tags
        ],
    }
    if partial and item.get("segment_summary"):
        projection["segment_summary"] = item["segment_summary"]
    return projection


def _item_is_partial(item):
    if item["source_kind"] == "unassigned":
        return True
    segment_slots = _slots_from_ranges(_json(item.get("segment_ranges"), []))
    pending_slots = _slots_from_ranges(_json(item.get("pending_slot_ranges"), []))
    return not segment_slots or pending_slots != segment_slots


def _extract_json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("Formation Agent did not return a JSON object.")
    return json.loads(text[start : end + 1])


def _call_formation_message(messages, trajectory_context=None, *, allow_retrieval=True):
    observer = provider_exchange_observer(trajectory_context, purpose="ltm formation", agent_type="formation")
    response = call_chat_completion(
        messages,
        tools=ltm_retrieval.FORMATION_RETRIEVAL_TOOL_SCHEMAS if allow_retrieval else None,
        temperature=0,
        timeout_seconds=FORMATION_TIMEOUT_SECONDS,
        purpose="ltm formation",
        exchange_observer=observer,
    )
    return response["choices"][0]["message"]


def _eval_formation_message(runtime_state):
    if not isinstance(runtime_state, dict):
        return None
    responses = runtime_state.get("eval_formation_responses")
    if isinstance(responses, list):
        index = int(runtime_state.get("eval_formation_response_index") or 0)
        if index >= len(responses):
            raise RuntimeError("eval formation response hook exhausted")
        runtime_state["eval_formation_response_index"] = index + 1
        response = responses[index]
        if not isinstance(response, dict):
            raise RuntimeError("eval formation response must be an object")
        if "role" in response or "tool_calls" in response or "content" in response:
            return {"role": "assistant", **response}
        return {"role": "assistant", "content": json.dumps(response, ensure_ascii=False)}
    decisions = runtime_state.get("eval_formation_decisions")
    if not isinstance(decisions, list):
        return None
    index = int(runtime_state.get("eval_formation_decision_index") or 0)
    if index >= len(decisions):
        raise RuntimeError("eval formation decision hook exhausted")
    runtime_state["eval_formation_decision_index"] = index + 1
    return {"role": "assistant", "content": json.dumps(decisions[index], ensure_ascii=False)}


def _assistant_history_message(message, tool_calls=None):
    history = {"role": "assistant", "content": message.get("content") or ""}
    if tool_calls is not None:
        history["tool_calls"] = tool_calls
    if "reasoning_content" in message:
        history["reasoning_content"] = message.get("reasoning_content") or ""
    return history


def _normalized_tool_calls(tool_calls, start_index):
    normalized = []
    for offset, raw in enumerate(tool_calls or [], start=1):
        raw = raw if isinstance(raw, dict) else {}
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        normalized.append(
            {
                "id": str(raw.get("id") or f"formation_tool_{start_index + offset}"),
                "type": "function",
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": function.get("arguments") if function.get("arguments") is not None else "{}",
                },
            }
        )
    return normalized


def _tool_arguments(tool_call):
    raw = (tool_call.get("function") or {}).get("arguments", "{}")
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError as error:
        return None, {
            "status": "error",
            "items": [],
            "next_cursor": None,
            "truncation_reason": None,
            "errors": [{"code": "invalid_arguments", "path": "arguments", "message": str(error)}],
        }
    if not isinstance(parsed, dict):
        return None, {
            "status": "error",
            "items": [],
            "next_cursor": None,
            "truncation_reason": None,
            "errors": [{"code": "invalid_arguments", "path": "arguments", "message": "arguments must decode to an object."}],
        }
    return parsed, None


def _empty_loop_metrics():
    return {
        "model_round_count": 0,
        "retrieval_call_count": 0,
        "retrieval_success_count": 0,
        "retrieval_error_count": 0,
        "retrieval_partial_count": 0,
        "retrieval_operations": [],
        "terminal_action": "",
        "validation_repair_count": 0,
        "validation_error_count": 0,
        "validation_error_codes": {},
        "validation_error_paths": {},
        "last_validation_errors": [],
        "idempotency_reconciled": False,
    }


def _record_validation_errors(metrics, errors):
    """Keep bounded, JSON-safe diagnostics for repeated write-plan repairs."""
    normalized = []
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        normalized.append(
            {
                "code": str(error.get("code") or "unknown"),
                "path": str(error.get("path") or ""),
                "message": str(error.get("message") or ""),
            }
        )
    metrics["validation_error_count"] = int(metrics.get("validation_error_count") or 0) + len(normalized)
    metrics["last_validation_errors"] = normalized[:20]
    for error in normalized:
        code = error["code"]
        path = error["path"]
        codes = metrics.setdefault("validation_error_codes", {})
        codes[code] = int(codes.get(code) or 0) + 1
        if path:
            paths = metrics.setdefault("validation_error_paths", {})
            paths[path] = int(paths.get(path) or 0) + 1


def _write_plan_validation(plan, root=None):
    """Validate a normalized plan before committing any LTM changes."""
    return ltm_store.validate_write_plan(plan, root=root)


def _validation_repair_message(errors, plan):
    payload_errors = [
        {
            "code": error.get("code"),
            "path": error.get("path"),
            "message": error.get("message"),
        }
        for error in errors or []
        if isinstance(error, dict)
    ]
    for error in payload_errors:
        match = re.match(r"objects\[(\d+)\]", error.get("path") or "")
        if not match:
            continue
        obj = plan["objects"][int(match[1])]
        if error["code"] == "title_length":
            error.update(actual_tokens=count_text_tokens(obj["title"]), min_tokens=1, max_tokens=50)
        elif error["code"] in {"missing_summary", "unexpected_summary", "payload_too_large"}:
            error.update(
                actual_payload_bytes=ltm_store._payload_bytes(obj["payload"]),
                summary_threshold_bytes=ltm_store.SUMMARY_THRESHOLD_BYTES,
                max_payload_bytes=ltm_store.SPLIT_THRESHOLD_BYTES,
            )
    return (
        "The write_plan failed the LTM Core Store validation. Return a corrected "
        "complete write_plan JSON. Keep unrelated objects and fields unchanged. "
        "Do not invent or omit facts. Retrieval tools are unavailable during plan repair. "
        "Apply these errors:\n"
        + json.dumps(payload_errors, ensure_ascii=False)
        + "\nFor missing_summary, add a concise summary when the payload is over 2 KiB; "
        "for unexpected_summary, set summary to null when the payload is at or below 2 KiB; "
        "for payload_too_large, split the information into multiple focused objects; "
        "for title_length, rewrite the title to 1-50 tokens without losing the fact; "
        "for Episode structured_data errors, use null or an empty object, or an object "
        "whose only field is time_scope with at least one of start, end, or anchor and "
        "an optional precision of exact, approximate, or relative."
    )


def _formation_repair_fields(errors):
    fields = {}
    episode_codes = set(ltm_store.EPISODE_STRUCTURED_DATA_ERROR_CODES)
    for error in errors or []:
        if not isinstance(error, dict):
            continue
        match = re.fullmatch(r"objects\[(\d+)\]\.payload\.structured_data(?:\..*)?", str(error.get("path") or ""))
        if match and error.get("code") in episode_codes:
            fields.setdefault(int(match[1]), set()).add("structured_data")
        match = re.fullmatch(r"objects\[(\d+)\]\.title", str(error.get("path") or ""))
        if match and error.get("code") == "title_length":
            fields.setdefault(int(match[1]), set()).add("title")
    return fields


def _formation_repair_scope_error(original, repaired, allowed_fields):
    if not isinstance(repaired, dict) or repaired.get("action") != "write_plan":
        return "Repair must return the same write_plan action."
    original_objects = original.get("objects") if isinstance(original, dict) else None
    repaired_objects = repaired.get("objects")
    if not isinstance(original_objects, list) or not isinstance(repaired_objects, list) or len(original_objects) != len(repaired_objects):
        return "Repair must preserve the number and order of objects."
    if original.get("links") != repaired.get("links"):
        return "Repair must not change links."
    for index, (before, after) in enumerate(zip(original_objects, repaired_objects)):
        if not isinstance(before, dict) or not isinstance(after, dict):
            return f"Repair must preserve object {index}."
        before_copy = copy.deepcopy(before)
        after_copy = copy.deepcopy(after)
        fields = allowed_fields.get(index, set())
        if "title" in fields:
            before_copy.pop("title", None)
            after_copy.pop("title", None)
        if "structured_data" in fields:
            before_payload = before_copy.get("payload")
            after_payload = after_copy.get("payload")
            if isinstance(before_payload, dict):
                before_payload.pop("structured_data", None)
            if isinstance(after_payload, dict):
                after_payload.pop("structured_data", None)
        if before_copy != after_copy:
            return f"Repair changed an unmarked field in objects[{index}]."
    return None


def _formation_decision(item, records, system_root_tags, runtime_state=None, max_tool_calls=None, root=None, work_span=None):
    projection = _agent_projection(item, records, system_root_tags)
    if item["source_kind"] == "segment" and _item_is_partial(item) and "segment_summary" not in projection:
        raise RuntimeError("Formation partial Segment projection requires a whole-segment summary.")
    prompt = [
        {"role": "system", "content": load_prompt("formation")},
        {"role": "user", "content": json.dumps(projection, ensure_ascii=False)},
    ]
    metrics = _empty_loop_metrics()
    tool_call_limit = int(max_tool_calls or FORMATION_MAX_TOOL_CALLS)
    if tool_call_limit < 1:
        raise ValueError("Formation retrieval tool-call limit must be positive.")
    progress = FormationRetrievalProgress(tool_call_limit)
    normal_rounds = 0
    repairing = False
    repair_base_result = None
    repair_allowed_fields = {}
    try:
        while repairing or normal_rounds < tool_call_limit + 2:
            if repairing:
                metrics["validation_repair_count"] += 1
            else:
                normal_rounds += 1
            message = _eval_formation_message(runtime_state)
            if message is None:
                context = (runtime_state or {}).get("_formation_trajectory_context")
                message = (_call_formation_message(prompt, context, allow_retrieval=False)
                           if repairing else _call_formation_message(prompt, context))
            metrics["model_round_count"] += 1
            tool_calls = _normalized_tool_calls(
                message.get("tool_calls") or [], metrics["retrieval_call_count"]
            )
            if tool_calls:
                if repairing:
                    raise RuntimeError("Formation plan repair returned tool calls; retrieval is unavailable during repair.")
                prompt.append(_assistant_history_message(message, tool_calls=tool_calls))
                round_results = []
                for tool_call in tool_calls:
                    if metrics["retrieval_call_count"] >= tool_call_limit:
                        raise RuntimeError(
                            f"Formation Agent reached the {tool_call_limit} retrieval tool-call limit."
                        )
                    metrics["retrieval_call_count"] += 1
                    name = (tool_call.get("function") or {}).get("name") or ""
                    tool_span = start_span((runtime_state or {}).get("_formation_trajectory_context"), "tool_call", name, metadata={"tool_call_id": tool_call.get("id")})
                    metrics["retrieval_operations"].append(name)
                    arguments, argument_error = _tool_arguments(tool_call)
                    result = argument_error or ltm_retrieval.run_formation_retrieval_tool(name, arguments)
                    round_results.append(result)
                    event_status = "failed" if result.get("status") == "error" else "completed"
                    record_event(tool_span, "tool.completed" if event_status == "completed" else "tool.failed", status=event_status, summary={"tool": name, "result_status": result.get("status")}, artifacts={"raw_tool_arguments": (tool_call.get("function") or {}).get("arguments") or "", "tool_arguments": arguments or {}, "tool_result": result})
                    if event_status == "completed":
                        complete_span(tool_span)
                    else:
                        fail_span(tool_span, RuntimeError(json.dumps(result, ensure_ascii=False)))
                    status = result.get("status")
                    if status in {"ok", "partial"}:
                        metrics["retrieval_success_count"] += 1
                    if status == "partial":
                        metrics["retrieval_partial_count"] += 1
                    if status == "error":
                        metrics["retrieval_error_count"] += 1
                    prompt.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call["id"],
                            "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
                        }
                    )
                progress_summary, notices = progress.observe_round(
                    round_results, metrics["retrieval_call_count"], metrics["model_round_count"]
                )
                record_event(work_span, "formation.retrieval.progress", summary=progress_summary)
                if notices:
                    prompt.append({"role": "user", "content": "\n\n".join(text for _, text in notices)})
                    for notice_summary, _text in notices:
                        record_event(work_span, "formation.retrieval.reminder", summary=notice_summary)
                continue

            raw = message.get("content") or ""
            result = _extract_json_object(raw)
            action = str(result.get("action") or "").strip()
            if action == "no_memory":
                metrics["terminal_action"] = action
                return {"action": "no_memory", "objects": [], "_metrics": metrics}
            if action == "write_plan" and isinstance(result.get("objects"), list):
                if metrics["retrieval_success_count"] < 1:
                    prompt.append(_assistant_history_message(message))
                    prompt.append(
                        {
                            "role": "user",
                            "content": (
                                "A write_plan requires at least one successful LTM Structured Query. "
                                "Inspect existing memory with an available retrieval tool before returning a write_plan."
                            ),
                        }
                    )
                    continue
                if repairing and repair_base_result is not None and repair_allowed_fields:
                    scope_error = _formation_repair_scope_error(repair_base_result, result, repair_allowed_fields)
                    if scope_error is not None:
                        if metrics["validation_repair_count"] >= FORMATION_MAX_PLAN_REPAIRS:
                            raise RuntimeError(scope_error)
                        prompt.append(_assistant_history_message(message))
                        prompt.append({"role": "user", "content": scope_error + " Only change the marked Episode structured_data or title fields."})
                        continue
                try:
                    candidate_plan = _normalized_write_plan(item, result, system_root_tags)
                    validation = _write_plan_validation(candidate_plan, root=root)
                except RuntimeError:


                    validation = None
                if validation is not None and not validation.get("valid"):
                    repairable = {
                        "missing_summary",
                        "unexpected_summary",
                        "payload_too_large",
                        "title_length",
                    } | set(ltm_store.EPISODE_STRUCTURED_DATA_ERROR_CODES)
                    validation_errors = validation.get("errors") or []
                    _record_validation_errors(metrics, validation_errors)
                    if validation_errors and all(error.get("code") in repairable for error in validation_errors):
                        record_event(work_span, "formation.plan.validation_rejected", status="failed", summary={
                            "model_round": metrics["model_round_count"],
                            "repair_attempts": metrics["validation_repair_count"],
                            "error_codes": [error.get("code") for error in validation_errors],
                        })
                        if metrics["validation_repair_count"] >= FORMATION_MAX_PLAN_REPAIRS:
                            raise RuntimeError(
                                "Formation write_plan validation failed after 2 repair attempts: "
                                + json.dumps(validation_errors, ensure_ascii=False)
                            )
                        repairing = True
                        if any(error.get("code") in ltm_store.EPISODE_STRUCTURED_DATA_ERROR_CODES for error in validation_errors):
                            repair_base_result = copy.deepcopy(result)
                            repair_allowed_fields = _formation_repair_fields(validation_errors)
                        prompt.append(_assistant_history_message(message))
                        prompt.append(
                            {
                                "role": "user",
                                "content": _validation_repair_message(validation_errors, candidate_plan)
                                + f"\nRepair attempt {metrics['validation_repair_count'] + 1} "
                                  f"of {FORMATION_MAX_PLAN_REPAIRS}.",
                            }
                        )
                        continue
                metrics["terminal_action"] = action
                return {
                    "action": "write_plan",
                    "objects": [obj for obj in result["objects"] if isinstance(obj, dict)],
                    "_metrics": metrics,
                }
            raise RuntimeError("Formation Agent returned an invalid terminal action.")
        raise RuntimeError("Formation Agent reached the model-round limit without a terminal result.")
    except FormationLoopError:
        raise
    except Exception as error:
        raise FormationLoopError(str(error), metrics) from error


def _formation_tag_error(code, path, message):
    return {"code": code, "path": path, "message": message}


def _normalized_write_plan(item, decision, system_root_tags):
    if decision["action"] == "no_memory":
        return None
    ranges = _ranges(_json(item["pending_slot_ranges"], []))
    links = []
    objects = []
    errors = []
    roots_by_id = {tag["object_id"]: tag for tag in system_root_tags}
    others_id = next(
        (tag["object_id"] for tag in system_root_tags if tag["tag_key"] == "others"),
        None,
    )
    for object_index, raw_object in enumerate(decision.get("objects", [])):
        object_id = f"mem_{uuid4().hex}"
        obj = {
            "object_id": object_id,
            "object_type": raw_object.get("object_type"),
            "title": raw_object.get("title"),
            "summary": raw_object.get("summary"),
            "payload": raw_object.get("payload"),
        }
        objects.append(obj)
        raw_links = raw_object.get("outgoing_links")
        target_ids = []
        seen_targets = set()
        if not isinstance(raw_links, list) or not raw_links:
            errors.append(
                _formation_tag_error(
                    "missing_root_tag_link",
                    f"objects[{object_index}].outgoing_links",
                    "every Formation object requires at least one system root tagged_with Link.",
                )
            )
            raw_links = []
        for link_index, raw_link in enumerate(raw_links):
            path = f"objects[{object_index}].outgoing_links[{link_index}]"
            if not isinstance(raw_link, dict):
                errors.append(
                    _formation_tag_error(
                        "invalid_outgoing_link",
                        path,
                        "outgoing link intent must be an object.",
                    )
                )
                continue
            if raw_link.get("relation_type") != "tagged_with":
                errors.append(
                    _formation_tag_error(
                        "invalid_formation_relation",
                        f"{path}.relation_type",
                        "Formation V4 only allows tagged_with root Links.",
                    )
                )
                continue
            target_id = raw_link.get("target_object_id")
            if not isinstance(target_id, str) or target_id not in roots_by_id:
                errors.append(
                    _formation_tag_error(
                        "invalid_system_root_target",
                        f"{path}.target_object_id",
                        "target_object_id must reference an injected system root Tag.",
                    )
                )
                continue
            if target_id in seen_targets:
                errors.append(
                    _formation_tag_error(
                        "duplicate_root_tag_link",
                        path,
                        "the same root Tag cannot be linked more than once.",
                    )
                )
                continue
            seen_targets.add(target_id)
            target_ids.append(target_id)
        if others_id in seen_targets and len(seen_targets) > 1:
            errors.append(
                _formation_tag_error(
                    "others_conflict",
                    f"objects[{object_index}].outgoing_links",
                    "Others cannot be combined with another system root Tag.",
                )
            )
        for source_range in ranges:
            links.append(
                {
                    "source": {"object_id": object_id},
                    "relation_type": "sourced_from",
                    "target": {"event_slots": dict(source_range)},
                }
            )
        for target_id in target_ids:
            links.append(
                {
                    "source": {"object_id": object_id},
                    "relation_type": "tagged_with",
                    "target": {"object_id": target_id},
                }
            )
    if errors:
        raise RuntimeError(json.dumps(errors, ensure_ascii=False))
    return {"objects": objects, "links": links}


def _set_item_state(connection, item_id, status, error="", attempts=None):
    now = _now()
    if attempts is None:
        connection.execute(
            "UPDATE formation_work_items SET status = ?, last_error = ?, updated_at = ? WHERE work_item_id = ?",
            (status, str(error or ""), now, item_id),
        )
    else:
        connection.execute(
            "UPDATE formation_work_items SET status = ?, attempts = ?, last_error = ?, updated_at = ? WHERE work_item_id = ?",
            (status, int(attempts), str(error or ""), now, item_id),
        )


def _process_item(
    item_id,
    system_root_tags,
    runtime_state=None,
    max_tool_calls=None,
    root=None,
    reconcile_idempotency_conflicts=False,
):
    work_span = start_span((runtime_state or {}).get("_formation_trajectory_context"), "formation_work_item", item_id)
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        item = _load_work_item(connection, item_id)
        if not item or item["status"] == "completed":
            record_event(work_span, "formation.work_item.skipped", status="completed", summary={"work_item_id": item_id})
            complete_span(work_span)
            return {"status": "skipped", "work_item_id": item_id}
        from runtime.setup_capabilities import skipped_slots
        allowed = _slots_from_ranges(_json(item["pending_slot_ranges"], [])) - skipped_slots(connection)
        if not allowed:
            complete_span(work_span)
            return {"status": "skipped", "work_item_id": item_id}
        attempts = int(item.get("attempts") or 0) + 1
        _set_item_state(connection, item_id, "processing", attempts=attempts)
        connection.commit()
    metrics = _empty_loop_metrics()
    metrics.update({"validation_status": "not_run", "commit_status": "not_run"})
    try:
        item["pending_slot_ranges"] = json.dumps(_ranges_from_slots(allowed))
        with event_stream.ensure_event_stream() as connection:
            connection.execute(
                "UPDATE formation_work_items SET pending_slot_ranges = ? WHERE work_item_id = ?",
                (item["pending_slot_ranges"], item_id),
            )
            connection.commit()
        records = _projection_records(_ranges_from_slots(allowed))
        if not records:
            raise RuntimeError("Formation WorkItem has no readable active Event Stream records.")
        decision = _formation_decision(
            item,
            records,
            system_root_tags,
            runtime_state=runtime_state,
            max_tool_calls=max_tool_calls,
            root=root,
            work_span=work_span,
        )
        metrics.update(decision.pop("_metrics", {}))
        plan = _normalized_write_plan(item, decision, system_root_tags)
        if plan is None:
            metrics["validation_status"] = "not_applicable"
            metrics["commit_status"] = "not_applicable"
            result = {"status": "completed", "action": "no_memory", "object_count": 0, "link_count": 0}
        else:
            idempotency_key = f"formation:{item_id}"
            commit = ltm_store.commit_write_plan(plan, idempotency_key)
            if (
                reconcile_idempotency_conflicts
                and commit.get("status") == "rejected"
                and any(
                    error.get("code") == "idempotency_conflict"
                    for error in commit.get("errors") or []
                    if isinstance(error, dict)
                )
            ):
                existing = ltm_store.get_idempotent_operation(
                    idempotency_key,
                    operation_type="write",
                    root=root,
                )
                existing_result = (existing or {}).get("result") or {}
                if existing_result.get("status") == "committed":
                    commit = {
                        **existing_result,
                        "status": "already_committed",
                        "idempotency_reconciled": True,
                    }
                    metrics["idempotency_reconciled"] = True
            if commit.get("status") not in {"committed", "already_committed"}:
                metrics["validation_status"] = "failed"
                metrics["commit_status"] = str(commit.get("status") or "rejected")
                raise RuntimeError(json.dumps(commit.get("errors") or commit, ensure_ascii=False))
            metrics["validation_status"] = "passed"
            metrics["commit_status"] = commit.get("status")
            result = {
                "status": "completed",
                "action": "write_plan",
                "object_count": len(commit.get("object_ids") or []),
                "link_count": len(commit.get("link_ids") or []),
            }
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            _set_item_state(connection, item_id, "completed", error="", attempts=attempts)
            connection.commit()
        record_event(work_span, "formation.work_item.completed", status="completed", summary={"work_item_id": item_id, **result}, artifacts={"result": result})
        complete_span(work_span, metadata={"work_item_id": item_id})
        return {"work_item_id": item_id, **result, **metrics}
    except Exception as error:
        if isinstance(error, FormationLoopError):
            metrics.update(error.metrics)
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            _set_item_state(connection, item_id, "failed", error=str(error), attempts=attempts)
            connection.commit()
        record_event(work_span, "formation.work_item.failed", status="failed", summary={"work_item_id": item_id, "error": str(error)}, artifacts={"metrics": metrics})
        fail_span(work_span, error)
        return {"work_item_id": item_id, "status": "failed", "error": str(error), **metrics}


def _save_last_result(connection, result):
    _state_set(connection, "last_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
    connection.commit()


def _run_formation_maintenance_impl(
    messages=None,
    runtime_state=None,
    force=False,
    max_tool_calls=None,
    historical_slot_range=None,
):
    messages = messages or event_stream.load_messages()
    try:
        bootstrap = meta_memory.bootstrap_system_tags()
        if bootstrap.get("status") == "failed":
            result = {
                "triggered": False,
                "reason": "meta_memory_bootstrap_failed",
                "bootstrap_status": bootstrap.get("status"),
                "processed_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "object_count": 0,
                "link_count": 0,
                "model_round_count": 0,
                "retrieval_call_count": 0,
                "retrieval_success_count": 0,
                "retrieval_error_count": 0,
                "retrieval_partial_count": 0,
                "validation_repair_count": 0,
                "validation_error_count": 0,
                "validation_error_codes": {},
                "validation_error_paths": {},
                "last_validation_errors": [],
                "idempotency_reconciled": False,
                "retrieval_operations": [],
                "terminal_actions": [],
                "error": bootstrap.get("error") or "Meta-Memory Bootstrap failed.",
            }
            with event_stream.ensure_event_stream() as connection:
                _ensure_tables(connection)
                _save_last_result(connection, result)
            return result
        system_root_tags = meta_memory.system_owned_tags()
        expected_tag_keys = {key for key, _title in meta_memory.SYSTEM_TAG_DEFINITIONS}
        if {tag.get("tag_key") for tag in system_root_tags} != expected_tag_keys:
            raise RuntimeError("Meta-Memory system root Tag integrity check failed.")
        if historical_slot_range is None:
            discovery = _refresh_work_items(messages, runtime_state)
        else:
            discovery = _refresh_work_items(
                messages, runtime_state, historical_slot_range=historical_slot_range
            )
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            rows = _work_item_rows(connection, statuses=("pending", "failed"))
        if historical_slot_range is None:


            buffer_slots, _budget = _buffer_slots(messages, runtime_state)
            rows = [row for row in rows if buffer_slots and all(
                slot < min(buffer_slots) or slot in buffer_slots
                for slot in _slots_from_ranges(_row_ranges(row))
            )]
        if force and not rows:
            reason = "manual_no_work_items"
        elif not rows:
            result = {
                "triggered": False,
                "reason": "no_formation_work_items",
                "bootstrap_status": bootstrap.get("status"),
                "buffer_slot_count": discovery["buffer_slot_count"],
                "created_count": discovery["created_count"],
                "processed_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "object_count": 0,
                "link_count": 0,
                "model_round_count": 0,
                "retrieval_call_count": 0,
                "retrieval_success_count": 0,
                "retrieval_error_count": 0,
                "retrieval_partial_count": 0,
                "validation_error_count": 0,
                "validation_error_codes": {},
                "validation_error_paths": {},
                "last_validation_errors": [],
                "idempotency_reconciled": False,
                "retrieval_operations": [],
                "terminal_actions": [],
                "error": "",
            }
            with event_stream.ensure_event_stream() as connection:
                _ensure_tables(connection)
                _save_last_result(connection, result)
            return result
        else:
            reason = "processed_formation_work_items"

        processed = [
            _process_item(
                row["work_item_id"],
                system_root_tags,
                runtime_state=runtime_state,
                max_tool_calls=max_tool_calls,
                root=None,
                reconcile_idempotency_conflicts=historical_slot_range is not None,
            )
            for row in rows
        ]
        result = {
            "triggered": True,
            "reason": reason,
            "bootstrap_status": bootstrap.get("status"),
            "buffer_slot_count": discovery["buffer_slot_count"],
            "created_count": discovery["created_count"],
            "processed_count": len(processed),
            "completed_count": sum(1 for item in processed if item.get("status") == "completed"),
            "failed_count": sum(1 for item in processed if item.get("status") == "failed"),
            "object_count": sum(int(item.get("object_count") or 0) for item in processed),
            "link_count": sum(int(item.get("link_count") or 0) for item in processed),
            "model_round_count": sum(int(item.get("model_round_count") or 0) for item in processed),
            "retrieval_call_count": sum(int(item.get("retrieval_call_count") or 0) for item in processed),
            "retrieval_success_count": sum(int(item.get("retrieval_success_count") or 0) for item in processed),
            "retrieval_error_count": sum(int(item.get("retrieval_error_count") or 0) for item in processed),
            "retrieval_partial_count": sum(int(item.get("retrieval_partial_count") or 0) for item in processed),
            "validation_repair_count": sum(int(item.get("validation_repair_count") or 0) for item in processed),
            "validation_error_count": sum(int(item.get("validation_error_count") or 0) for item in processed),
            "validation_error_codes": {},
            "validation_error_paths": {},
            "last_validation_errors": [],
            "idempotency_reconciled": any(item.get("idempotency_reconciled") for item in processed),
            "retrieval_operations": [
                operation
                for item in processed
                for operation in item.get("retrieval_operations") or []
            ],
            "terminal_actions": [item.get("terminal_action") for item in processed if item.get("terminal_action")],
            "validation_statuses": [item.get("validation_status") for item in processed if item.get("validation_status")],
            "commit_statuses": [item.get("commit_status") for item in processed if item.get("commit_status")],
            "error": next((item.get("error") for item in processed if item.get("error")), ""),
        }
        for item in processed:
            for key in ("validation_error_codes", "validation_error_paths"):
                for name, count in (item.get(key) or {}).items():
                    result[key][name] = int(result[key].get(name) or 0) + int(count or 0)
            if item.get("last_validation_errors"):
                result["last_validation_errors"] = item["last_validation_errors"]
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            _save_last_result(connection, result)
        return result
    except Exception as error:
        result = {
            "triggered": False,
            "reason": "formation_maintenance_failed",
            "processed_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "object_count": 0,
            "link_count": 0,
            "model_round_count": 0,
            "retrieval_call_count": 0,
            "retrieval_success_count": 0,
            "retrieval_error_count": 0,
            "retrieval_partial_count": 0,
            "validation_repair_count": 0,
            "validation_error_count": 0,
            "validation_error_codes": {},
            "validation_error_paths": {},
            "last_validation_errors": [],
            "idempotency_reconciled": False,
            "retrieval_operations": [],
            "terminal_actions": [],
            "error": str(error),
        }
        try:
            with event_stream.ensure_event_stream() as connection:
                _ensure_tables(connection)
                _save_last_result(connection, result)
        except Exception:
            pass
        return result


from .storage_gate import memory_task


@memory_task
def run_formation_maintenance(
    messages=None,
    runtime_state=None,
    force=False,
    max_tool_calls=None,
    historical_slot_range=None,
):
    from runtime.setup_capabilities import ltm_enabled, restoration_pending
    if not ltm_enabled():
        return {"triggered": False, "status": "unavailable", "reason": "LTM is unconfigured"}
    if restoration_pending():
        return {"triggered": False, "status": "deferred", "reason": "Waiting for a valid Formation window after setup"}
    formation_mode = "historical_session" if historical_slot_range is not None else "online"
    caller = (runtime_state or {}).get("trajectory_context")
    context = start_trajectory(
        "formation",
        caller=caller,
        metadata={
            "formation_mode": formation_mode,
            "force": bool(force),
            "max_retrieval_tool_calls": int(max_tool_calls or FORMATION_MAX_TOOL_CALLS),
        },
    )
    if isinstance(runtime_state, dict):
        runtime_state["_formation_trajectory_context"] = context
    try:
        if historical_slot_range is None:
            result = _run_formation_maintenance_impl(messages, runtime_state, force, max_tool_calls)
        else:
            result = _run_formation_maintenance_impl(
                messages,
                runtime_state,
                force,
                max_tool_calls,
                historical_slot_range,
            )
        record_event(context, "formation.completed", status="failed" if result.get("error") else "completed", summary={key: value for key, value in result.items() if key not in {"retrieval_operations"}}, artifacts={"result": result})
        if result.get("error"):
            fail_trajectory(context, RuntimeError(result["error"]), metadata={"formation_mode": formation_mode})
        else:
            complete_trajectory(context, metadata={"formation_mode": formation_mode})
        return result
    except Exception as error:
        fail_trajectory(context, error, metadata={"formation_mode": formation_mode})
        raise
    finally:
        if isinstance(runtime_state, dict):
            runtime_state.pop("_formation_trajectory_context", None)


def formation_status():
    snapshot = {
        "status": "not_initialized",
        "work_item_count": 0,
        "pending_count": 0,
        "processing_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "explicitly_skipped_count": 0,
        "last_result": {},
        "historical_backfill": {
            "status": "not_initialized",
            "cursor_slot_seq": -1,
            "historical_backfill_end_slot": -1,
            "last_error": "",
            "updated_at": "",
        },
    }
    path = event_stream.event_stream_db_path()
    if not path.exists():
        return snapshot
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='formation_work_items'"
        ).fetchone()
        if not table:
            return snapshot
        snapshot["status"] = "active"
        from runtime.setup_capabilities import skipped_slots
        skipped = skipped_slots(connection)
        rows = _work_item_rows(connection)
        for row in rows:
            state = row['status']
            if state != 'completed' and _slots_from_ranges(_row_ranges(row)) <= skipped:
                state = 'explicitly_skipped'
            snapshot[f"{state}_count"] += 1
        snapshot["work_item_count"] = len(rows)
        if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='formation_state'").fetchone():
            row = connection.execute("SELECT value FROM formation_state WHERE key='last_result'").fetchone()
            snapshot["last_result"] = _json(row["value"], {}) if row else {}
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM formation_state WHERE key LIKE 'historical_backfill_%'"
                )
            }
            snapshot["historical_backfill"] = {
                "status": values.get("historical_backfill_status", "not_started"),
                "cursor_slot_seq": int(values.get("historical_backfill_cursor_slot", -1) or -1),
                "historical_backfill_end_slot": int(values.get("historical_backfill_end_slot", -1) or -1),
                "last_error": values.get("historical_backfill_last_error", ""),
                "updated_at": values.get("historical_backfill_updated_at", ""),
            }
        return snapshot
    finally:
        connection.close()


def format_formation_status(status):
    status = status or {}
    last = status.get("last_result") or {}
    return "\n".join(
        [
            "LTM Formationer status:",
            f"status: {status.get('status', 'not_initialized')}",
            f"work_items: {status.get('work_item_count', 0)}",
            f"pending: {status.get('pending_count', 0)}",
            f"completed: {status.get('completed_count', 0)}",
            f"failed: {status.get('failed_count', 0)}",
            f"explicitly_skipped: {status.get('explicitly_skipped_count', 0)}",
            f"last_reason: {last.get('reason', '')}",
            f"last_processed_count: {last.get('processed_count', 0)}",
            f"last_object_count: {last.get('object_count', 0)}",
            f"last_model_rounds: {last.get('model_round_count', 0)}",
            f"last_retrieval_calls: {last.get('retrieval_call_count', 0)}",
            f"last_retrieval_errors: {last.get('retrieval_error_count', 0)}",
            f"last_validation_repairs: {last.get('validation_repair_count', 0)}",
            f"last_validation_errors: {last.get('validation_error_count', 0)}",
            f"last_validation_error_codes: {json.dumps(last.get('validation_error_codes', {}), ensure_ascii=False, sort_keys=True)}",
            f"last_validation_error_paths: {json.dumps(last.get('validation_error_paths', {}), ensure_ascii=False, sort_keys=True)}",
            f"last_error: {last.get('error', '')}",
        ]
    )
