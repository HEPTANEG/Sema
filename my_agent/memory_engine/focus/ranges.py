import json

def _assignment_range_exists(ranges, start, end):
    start = int(start)
    end = int(end)
    return any(int(item_start) == start and int(item_end) == end for item_start, item_end in ranges)

def _strict_ranges(raw):
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return [], 1
    if not isinstance(decoded, list):
        return [], 1
    ranges = []
    invalid_count = 0
    for item in decoded:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        start = item.get("start")
        end = item.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
        ):
            invalid_count += 1
            continue
        ranges.append({"start": start, "end": end})
    if not invalid_count and ranges != _normalize_ranges(ranges):
        invalid_count = 1
    return ranges, invalid_count

def _strict_assignment_slots(row):
    invalid_count = 0
    try:
        decoded = json.loads(row["slot_seqs"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return set(), 1
    if not isinstance(decoded, list) or not decoded:
        return set(), 1
    slots = []
    for value in decoded:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            invalid_count += 1
            continue
        slots.append(value)
    if len(slots) != len(set(slots)):
        invalid_count += 1
    slot_set = set(slots)
    slot_start = row["slot_start"]
    slot_end = row["slot_end"]
    if (
        not isinstance(slot_start, int)
        or isinstance(slot_start, bool)
        or not isinstance(slot_end, int)
        or isinstance(slot_end, bool)
    ):
        return slot_set, invalid_count + 1
    if slot_start < 0 or slot_end < slot_start:
        invalid_count += 1
    elif slot_set and (min(slot_set) != slot_start or max(slot_set) != slot_end):
        invalid_count += 1
    return slot_set, invalid_count

def _normalize_ranges(ranges):
    normalized = []
    for item in ranges or []:
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (AttributeError, TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        normalized.append({"start": start, "end": end})
    normalized.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for item in normalized:
        if not merged or item["start"] > merged[-1]["end"] + 1:
            merged.append(dict(item))
            continue
        merged[-1]["end"] = max(merged[-1]["end"], item["end"])
    return merged

def _range_contains(ranges, slot_seq):
    slot_seq = int(slot_seq)
    return any(int(item["start"]) <= slot_seq <= int(item["end"]) for item in ranges or [])

def _append_slots_to_ranges(ranges, slot_seqs):
    ranges = _normalize_ranges(ranges)
    existing = {slot for item in ranges for slot in range(int(item["start"]), int(item["end"]) + 1)}
    for slot_seq in sorted({int(slot) for slot in slot_seqs if int(slot) not in existing}):
        ranges.append({"start": slot_seq, "end": slot_seq})
        ranges = _normalize_ranges(ranges)
    return ranges

def _covered_slots(segments):
    covered = set()
    for segment in segments or []:
        for item in segment.get("slot_ranges", []):
            covered.update(range(int(item["start"]), int(item["end"]) + 1))
    return covered

def _slots_from_ranges(ranges):
    slots = set()
    for item in ranges or []:
        slots.update(range(int(item["start"]), int(item["end"]) + 1))
    return slots

def _ranges_from_slots(slots):
    return _normalize_ranges([{"start": slot, "end": slot} for slot in sorted(set(slots))])

def _remove_slots_from_ranges(ranges, slots_to_remove):
    return _ranges_from_slots(_slots_from_ranges(ranges) - {int(slot) for slot in slots_to_remove})

def _validate_slot_range(start, end, records):
    if end < start:
        start, end = end, start
    for slot in range(start, end + 1):
        record = records.get(slot)
        if not record:
            raise RuntimeError(f"Invalid focus repair range: slot {slot} does not exist.")
        if record.get("source") == "system":
            raise RuntimeError(f"Invalid focus repair range: slot {slot} is a system slot.")
    return start, end

def _assert_no_overlapping_segments(segments):
    owner_by_slot = {}
    for segment in segments.values():
        for slot in _slots_from_ranges(segment.get("slot_ranges", [])):
            if slot in owner_by_slot:
                raise RuntimeError(
                    f"Invalid focus repair plan: slot {slot} would be covered by both "
                    f"{owner_by_slot[slot]} and {segment['segment_id']}."
                )
            owner_by_slot[slot] = segment["segment_id"]

def _assert_move_preserves_assignment_units(assignments, move_slots):
    for assignment in assignments:
        assignment_slots = {int(slot) for slot in assignment.get("slot_seqs", [])}
        if not assignment_slots:
            assignment_slots = set(range(int(assignment["slot_start"]), int(assignment["slot_end"]) + 1))
        overlap = assignment_slots & move_slots
        if overlap and not assignment_slots.issubset(move_slots):
            raise RuntimeError(
                "move_range must preserve assignment audit units; move the full assigned ingest range "
                f"{assignment['slot_start']}..{assignment['slot_end']} or leave it unchanged."
            )

def _assert_protected_active_segment(segments, protected_active_segment_id):
    if not protected_active_segment_id:
        return
    segment = segments.get(protected_active_segment_id)
    if not segment or segment.get("status") != "active":
        raise RuntimeError("repair plan must preserve this turn's Detector-selected active segment.")

def _assignment_slots(assignment):
    slots = {int(slot) for slot in assignment.get("slot_seqs", [])}
    if slots:
        return slots
    return set(range(int(assignment["slot_start"]), int(assignment["slot_end"]) + 1))
