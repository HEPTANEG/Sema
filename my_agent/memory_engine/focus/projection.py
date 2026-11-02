from functools import lru_cache

import memory_engine.event_stream as event_stream
from ..token_counter import count_text_tokens

from .common import (
    FOCUS_METADATA_BUDGET_MAX,
    FOCUS_METADATA_BUDGET_RATIO,
    FOCUS_UNOWNED_DETAIL_RATIO,
    _single_line,
)

from .ranges import (
    _assignment_slots,
    _range_contains,
    _slots_from_ranges,
)

from .store import (
    _assignment_from_row,
    _load_segments,
    _read_only_event_stream_connection,
    _table_exists,
)

def build_working_memory_projection(records, render_record, budget):
    """Build a token-bounded Focus projection without mutating Focus state."""
    budget = max(1, int(budget or 0))
    count_tokens = _projection_token_counter()
    state = _focus_projection_state()
    if state is None or not state["segments"]:
        return None

    prepared = []
    for source in records or []:
        record = dict(source)
        rendered = record.get("rendered") or render_record(record)
        record["rendered"] = rendered
        record["rendered_tokens"] = int(record.get("rendered_tokens") or count_tokens(rendered))
        prepared.append(record)
    prepared.sort(key=lambda item: int(item["slot_seq"]))

    segments = state["segments"]
    assignments = state["assignments"]
    retired_slots = {
        slot
        for assignment in assignments
        if assignment.get("lifecycle_status") == "retired"
        for slot in _assignment_slots(assignment)
    }
    segment_slots = {
        segment["segment_id"]: _slots_from_ranges(segment.get("slot_ranges", []))
        for segment in segments
    }
    covered_slots = set().union(*segment_slots.values()) if segment_slots else set()
    resident_records = [
        record for record in prepared if int(record["slot_seq"]) not in retired_slots
    ]
    active_by_slot = {}
    for segment in segments:
        if segment.get("status") != "active":
            continue
        for slot in segment_slots.get(segment["segment_id"], set()):
            active_by_slot[slot] = segment["segment_id"]
    active_records = [
        record for record in resident_records if int(record["slot_seq"]) in active_by_slot
    ]
    unowned_records = [
        record for record in resident_records if int(record["slot_seq"]) not in covered_slots
    ]

    metadata_limit = min(FOCUS_METADATA_BUDGET_MAX, int(budget * FOCUS_METADATA_BUDGET_RATIO))
    active = _segments_by_recency(
        [segment for segment in segments if segment.get("status") == "active"],
        segment_slots,
        prefer_latest_slot=True,
    )
    inactive = _segments_by_recency(
        [segment for segment in segments if segment.get("status") == "inactive"],
        segment_slots,
    )
    resolved = _segments_by_recency(
        [segment for segment in segments if segment.get("status") == "resolved"],
        segment_slots,
    )
    selected_active = []
    selected_inactive = []
    selected_resolved = []

    for segment in active:
        _append_metadata_segment(
            selected_active,
            segment,
            metadata_limit,
            lambda: _render_focus_projection(
                selected_resolved,
                selected_inactive,
                selected_active,
                [],
                {},
                render_record,
            ),
            include_summary=True,
            count_tokens=count_tokens,
        )
    for segment in inactive:
        _append_metadata_segment(
            selected_inactive,
            segment,
            metadata_limit,
            lambda: _render_focus_projection(
                selected_resolved,
                selected_inactive,
                selected_active,
                [],
                {},
                render_record,
            ),
            include_summary=True,
            count_tokens=count_tokens,
        )
    for segment in resolved:
        _append_metadata_segment(
            selected_resolved,
            segment,
            metadata_limit,
            lambda: _render_focus_projection(
                selected_resolved,
                selected_inactive,
                selected_active,
                [],
                {},
                render_record,
            ),
            include_summary=False,
            count_tokens=count_tokens,
        )

    selected_active.reverse()
    selected_inactive.reverse()
    selected_resolved.reverse()
    selected_active_ids = {segment["segment_id"] for segment in selected_active}
    active_candidates = [
        record
        for record in reversed(active_records)
        if active_by_slot.get(int(record["slot_seq"])) in selected_active_ids
    ]
    unowned_candidates = list(reversed(unowned_records))
    selected_active_records = []
    selected_unowned_records = []

    metadata_content = _render_focus_projection(
        selected_resolved,
        selected_inactive,
        selected_active,
        [],
        {},
        render_record,
    )
    metadata_tokens = count_tokens(metadata_content)
    if active_candidates:
        newest = active_candidates[0]
        required_content = _render_focus_projection(
            selected_resolved,
            selected_inactive,
            selected_active,
            [],
            {active_by_slot[int(newest["slot_seq"])]: [newest]},
            render_record,
        )
        required_tokens = count_tokens(required_content)
        if required_tokens > budget:
            raise RuntimeError(
                "Context Builder WM slot exceeds budget: "
                f"slot_seq={newest['slot_seq']}, tokens={required_tokens}, budget={budget}."
            )

    detail_pool = max(0, budget - metadata_tokens)
    unowned_cap = detail_pool if not active_candidates else int(detail_pool * FOCUS_UNOWNED_DETAIL_RATIO)
    active_target = budget - unowned_cap
    remaining_active = _pack_active_records(
        active_candidates,
        selected_active_records,
        selected_resolved,
        selected_inactive,
        selected_active,
        selected_unowned_records,
        active_by_slot,
        render_record,
        active_target,
        count_tokens=count_tokens,
    )
    active_base_tokens = count_tokens(
        _render_focus_projection(
            selected_resolved,
            selected_inactive,
            selected_active,
            [],
            _active_record_map(selected_active_records, active_by_slot),
            render_record,
        )
    )
    _pack_unowned_records(
        unowned_candidates,
        selected_unowned_records,
        selected_resolved,
        selected_inactive,
        selected_active,
        selected_active_records,
        active_by_slot,
        render_record,
        budget,
        unowned_cap,
        active_base_tokens,
        count_tokens=count_tokens,
    )
    _pack_active_records(
        remaining_active,
        selected_active_records,
        selected_resolved,
        selected_inactive,
        selected_active,
        selected_unowned_records,
        active_by_slot,
        render_record,
        budget,
        count_tokens=count_tokens,
    )

    active_record_map = _active_record_map(selected_active_records, active_by_slot)
    content = _render_focus_projection(
        selected_resolved,
        selected_inactive,
        selected_active,
        selected_unowned_records,
        active_record_map,
        render_record,
    )
    active_only_content = _render_focus_projection(
        selected_resolved,
        selected_inactive,
        selected_active,
        [],
        active_record_map,
        render_record,
    )
    active_only_tokens = count_tokens(active_only_content)
    active_detail_tokens = max(0, active_only_tokens - metadata_tokens)
    selected_tokens = count_tokens(content)
    selected_records = sorted(
        [*selected_active_records, *selected_unowned_records],
        key=lambda item: int(item["slot_seq"]),
    )
    resident_slot_seqs = {int(record["slot_seq"]) for record in resident_records}
    resident_raw_tokens = sum(
        int(record.get("active_token_count") or record.get("rendered_tokens") or 0)
        for record in resident_records
    )
    return {
        "budget": budget,
        "candidate_count": len(prepared),
        "selected_records": selected_records,
        "selected_tokens": selected_tokens,
        "skipped_count": max(0, len(prepared) - len(selected_records)),
        "content": content,
        "projection_mode": "focus",
        "selected_slot_seqs": [int(record["slot_seq"]) for record in selected_records],
        "metadata_tokens": metadata_tokens,
        "active_detail_tokens": active_detail_tokens,
        "unowned_detail_tokens": max(0, selected_tokens - active_only_tokens),
        "active_detail_count": len(selected_active_records),
        "unowned_detail_count": len(selected_unowned_records),
        "inactive_summary_count": len(selected_inactive),
        "resolved_title_count": len(selected_resolved),
        "resident_slot_count": len(resident_slot_seqs),
        "resident_raw_token_count": resident_raw_tokens,
    }


def _projection_token_counter():
    """Cache repeated token counts during one projection build only."""
    @lru_cache(maxsize=256)
    def count_tokens(text):
        return count_text_tokens(text)

    return count_tokens

def render_segmented_working_memory(records, render_record, budget=50000):
    projection = build_working_memory_projection(records, render_record, budget)
    return projection.get("content") if projection else None

def _focus_projection_state():
    path = event_stream.event_stream_db_path()
    if not path.exists():
        return None
    with _read_only_event_stream_connection() as connection:
        if not _table_exists(connection, "focus_segments"):
            return None
        segments = _load_segments(connection)
        assignments = []
        if _table_exists(connection, "focus_assignments"):
            assignments = [
                _assignment_from_row(row)
                for row in connection.execute(
                    "SELECT * FROM focus_assignments ORDER BY slot_start, slot_end, assignment_id"
                ).fetchall()
            ]
    return {"segments": segments, "assignments": assignments}

def _segments_by_recency(segments, segment_slots, prefer_latest_slot=False):
    def sort_key(segment):
        latest_slot = max(segment_slots.get(segment["segment_id"], {-1}) or {-1})
        touched_at = str(segment.get("last_touched_at") or segment.get("updated_at") or "")
        if prefer_latest_slot:
            return latest_slot, touched_at, str(segment.get("segment_id") or "")
        return touched_at, latest_slot, str(segment.get("segment_id") or "")

    return sorted(
        segments,
        key=sort_key,
        reverse=True,
    )

def _append_metadata_segment(selected, segment, limit, render, include_summary, count_tokens=count_text_tokens):
    candidate = dict(segment)
    if not include_summary:
        candidate["summary"] = ""
    selected.append(candidate)
    if count_tokens(render()) <= limit:
        return True
    if include_summary and candidate.get("summary"):
        original = _single_line(candidate.get("summary"))
        low = 0
        high = len(original)
        best = ""
        while low <= high:
            middle = (low + high) // 2
            candidate["summary"] = _truncated_text(original, middle)
            if count_tokens(render()) <= limit:
                best = candidate["summary"]
                low = middle + 1
            else:
                high = middle - 1
        candidate["summary"] = best
        if count_tokens(render()) <= limit:
            return True
    selected.pop()
    return False

def _truncated_text(text, char_count):
    text = _single_line(text)
    if char_count >= len(text):
        return text
    if char_count <= 0:
        return ""
    return text[:char_count].rstrip() + "..."

def _active_record_map(records, active_by_slot):
    grouped = {}
    for record in sorted(records, key=lambda item: int(item["slot_seq"])):
        segment_id = active_by_slot.get(int(record["slot_seq"]))
        if segment_id:
            grouped.setdefault(segment_id, []).append(record)
    return grouped

def _pack_active_records(
    candidates,
    selected,
    resolved,
    inactive,
    active,
    unowned,
    active_by_slot,
    render_record,
    limit,
    count_tokens=count_text_tokens,
):
    for index, record in enumerate(candidates):
        selected.append(record)
        content = _render_focus_projection(
            resolved,
            inactive,
            active,
            unowned,
            _active_record_map(selected, active_by_slot),
            render_record,
        )
        if count_tokens(content) > limit:
            selected.pop()
            return candidates[index:]
    return []

def _pack_unowned_records(
    candidates,
    selected,
    resolved,
    inactive,
    active,
    active_records,
    active_by_slot,
    render_record,
    budget,
    cap,
    baseline_tokens,
    count_tokens=count_text_tokens,
):
    for record in candidates:
        selected.append(record)
        content = _render_focus_projection(
            resolved,
            inactive,
            active,
            selected,
            _active_record_map(active_records, active_by_slot),
            render_record,
        )
        tokens = count_tokens(content)
        if tokens > budget or tokens - baseline_tokens > cap:
            selected.pop()
            break

def _render_focus_projection(resolved, inactive, active, unowned, active_records, render_record):
    lines = [
        "<working-memory>",
        (
            "This is segmented working memory. Resolved items are closed history, inactive items are parked context, "
            "and active focuses are current working material. Later active focuses are more recent and usually more important."
        ),
        "",
    ]
    if resolved:
        lines.append("Resolved:")
        lines.extend(f"- {_single_line(segment.get('title'))}" for segment in resolved)
        lines.append("")
    if inactive:
        lines.append("Inactive:")
        lines.extend(
            f"- {_single_line(segment.get('title'))}: {_single_line(segment.get('summary'))}"
            for segment in inactive
        )
        lines.append("")
    if unowned:
        lines.append("Unassigned recent context:")
        lines.extend(render_record(record) for record in sorted(unowned, key=lambda item: int(item["slot_seq"])))
        lines.append("")
    if active:
        lines.append("Active:")
        for segment in active:
            lines.append(f"{_single_line(segment.get('title'))}: {_single_line(segment.get('summary'))}")
            lines.extend(render_record(record) for record in active_records.get(segment["segment_id"], []))
            lines.append("")
    if lines[-1] == "":
        lines.pop()
    lines.append("</working-memory>")
    return "\n".join(lines)

def _segments_with_records(segments, status, selected_by_slot):
    grouped = []
    for segment in segments:
        if segment.get("status") != status:
            continue
        segment_records = [
            selected_by_slot[slot_seq]
            for slot_seq in sorted(selected_by_slot)
            if _range_contains(segment.get("slot_ranges", []), slot_seq)
        ]
        if segment_records:
            latest_slot = max(int(record["slot_seq"]) for record in segment_records)
            grouped.append((latest_slot, segment, segment_records))
    grouped.sort(key=lambda item: item[0])
    return [(segment, records) for _latest, segment, records in grouped]
