import json

import memory_engine.event_stream as event_stream
import memory_engine.evidence as evidence
import memory_engine.wm.ingress as wm_ingress
from ..dynamic_context_budget import compute_and_store_dynamic_context_budget
from ..stm_projection import build_interaction_blocks, build_stm_projection

from .common import _single_line

from .ranges import (
    _append_slots_to_ranges,
    _assignment_range_exists,
    _covered_slots,
)

from .store import (
    _active_segment,
    _assignment_ranges,
    _load_segments,
    _update_segment_ranges,
)

def _eligible_wm_slots(messages, runtime_state=None):
    if not messages:
        return []
    pressure = compute_and_store_dynamic_context_budget(messages, runtime_state, emit_observer=False, stabilize=False)
    budgets = pressure.get("budgets") or {}
    stm_budget = int(budgets.get("stm") or 10000)
    projection = build_stm_projection(
        messages,
        budget=stm_budget,
        wm_ingress_watermark=wm_ingress.effective_wm_ingress_watermark(runtime_state),
    )
    slots = []
    for block in projection.get("omitted_blocks") or []:
        start = int(block.get("start_index", 0))
        end = int(block.get("end_index", start))
        for slot_seq in range(start, end + 1):
            if slot_seq >= 0 and slot_seq < len(messages) and messages[slot_seq].get("role") != "system":
                slots.append(slot_seq)
    return sorted(set(slots))

def _uncovered_wm_slots(connection, messages=None, runtime_state=None):
    slot_seqs = _eligible_wm_slots(messages or [], runtime_state)
    covered_by_any_segment = _covered_slots(_load_segments(connection))
    return [slot for slot in slot_seqs if slot not in covered_by_any_segment]

def _uncovered_wm_interaction_blocks(connection, messages=None, runtime_state=None):
    uncovered_slots = set(_uncovered_wm_slots(connection, messages or [], runtime_state))
    if not uncovered_slots:
        return []
    audited_ranges = _assignment_ranges(connection)
    records_by_slot = {int(record["slot_seq"]): record for record in event_stream.load_active_trunk_records()}
    blocks = []
    for block in build_interaction_blocks(messages or []):
        start = int(block.get("start_index", 0))
        end = int(block.get("end_index", start))
        if _assignment_range_exists(audited_ranges, start, end):
            continue
        block_slots = [slot for slot in range(start, end + 1) if slot in uncovered_slots]
        if not block_slots:
            continue
        blocks.append(
            {
                "slot_range": {"start": min(block_slots), "end": max(block_slots)},
                "slot_seqs": block_slots,
                "token_count": int(block.get("token_count") or 0),
                "records": [_active_record_preview(records_by_slot[slot]) for slot in block_slots if slot in records_by_slot],
            }
        )
    return blocks

def _active_record_preview(record, limit=1200):
    content = record.get("content")
    if record.get("active_kind") == "sum" and isinstance(content, dict):
        text = content.get("summary", "")
        role = content.get("source_role") or record.get("source", "")
    elif isinstance(content, dict):
        text = _message_text(content)
        role = content.get("role") or record.get("source", "")
    else:
        text = str(content or "")
        role = record.get("source", "")
    text = _single_line(text)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return {
        "slot_seq": int(record.get("slot_seq") or 0),
        "source": record.get("source", ""),
        "role": role,
        "active_kind": record.get("active_kind", ""),
        "active_token_count": int(record.get("active_token_count") or 0),
        "content": text,
    }

def _extend_active_segment(connection, messages=None, runtime_state=None, slot_seqs=None):
    segment = _active_segment(connection)
    if not segment:
        return {"updated": False, "added_slots": [], "reason": "no_active_segment"}
    if slot_seqs is None:
        slot_seqs = _uncovered_wm_slots(connection, messages or [], runtime_state)
    if not slot_seqs:
        return {"updated": False, "added_slots": [], "reason": "no_uncovered_wm_slots"}
    before = segment["slot_ranges"]
    after = _append_slots_to_ranges(before, slot_seqs)
    if after == before:
        return {"updated": False, "added_slots": [], "reason": "already_current"}
    _update_segment_ranges(connection, segment["segment_id"], after)
    return {"updated": True, "added_slots": slot_seqs, "reason": "active_range_extended"}

def _latest_focus_context(messages):
    messages = list(messages or [])
    current_user_index = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            current_user_index = index
            break
    if current_user_index is None:
        return []

    previous_assistant_index = None
    for index in range(current_user_index - 1, -1, -1):
        if messages[index].get("role") == "assistant":
            previous_assistant_index = index
            break

    previous_user_index = None
    if previous_assistant_index is not None:
        for index in range(previous_assistant_index - 1, -1, -1):
            if messages[index].get("role") == "user":
                previous_user_index = index
                break

    indexes = [index for index in [previous_user_index, previous_assistant_index, current_user_index] if index is not None]
    return [_message_preview(messages[index]) for index in indexes]

def _interactions_for_slots(messages, slot_seqs):
    wanted = {int(slot) for slot in slot_seqs or []}
    interactions = []
    for block in build_interaction_blocks(messages or []):
        start = int(block.get("start_index", 0))
        end = int(block.get("end_index", start))
        if not any(start <= slot <= end for slot in wanted):
            continue
        interactions.append(
            {
                "slot_range": {"start": start, "end": end},
                "messages": [_message_preview(message) for message in block.get("messages", [])],
            }
        )
    return interactions

def _message_preview(message, limit=1200):
    text = _message_text(message)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return {"role": message.get("role", ""), "content": text}

def _message_text(message):
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)

def _stm_evidence(messages=None, runtime_state=None, limit=2000):
    result = evidence.get_stm_evidence(messages=messages, runtime_state=runtime_state, max_chars=limit)
    return {
        "available": bool(result.get("ok")),
        "stm_budget": (result.get("query") or {}).get("budget", 0),
        "selected_token_count": int(result.get("selected_tokens") or 0),
        "selected_block_count": int(result.get("selected_block_count") or 0),
        "content_preview": result.get("content_preview", ""),
        "truncated": bool(result.get("truncated", False)),
        "errors": result.get("errors") or [],
    }

def _protected_active_segment_id(runtime_state=None):
    if not isinstance(runtime_state, dict):
        return ""
    return str(runtime_state.get("_focus_detector_active_segment_id") or "").strip()

def _latest_user_slot_seq(connection, messages):
    for index in range(len(messages or []) - 1, -1, -1):
        if (messages[index] or {}).get("role") != "user":
            continue
        row = connection.execute("SELECT source FROM slots WHERE slot_seq = ?", (index,)).fetchone()
        if row is not None and row["source"] == "user":
            return index
        return None
    return None

def _user_turn_count(messages):
    return sum(1 for message in messages or [] if message.get("role") == "user")

def _records_by_slot():
    return {int(record["slot_seq"]): record for record in event_stream.load_active_trunk_records()}

def _record_previews_for_slots(slot_seqs, limit=800):
    records = _records_by_slot()
    return [_active_record_preview(records[slot], limit=limit) for slot in slot_seqs if slot in records]
