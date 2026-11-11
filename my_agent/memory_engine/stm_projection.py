from .token_counter import count_message_tokens


STM_TOKEN_BUDGET = 10000


def split_system_messages(messages):
    safe_messages = [dict(message) for message in messages or []]
    system_messages = []
    index = 0
    while index < len(safe_messages) and safe_messages[index].get("role") == "system":
        copied = dict(safe_messages[index])
        copied["_message_index"] = index
        system_messages.append(copied)
        index += 1
    remaining = []
    for message_index, message in enumerate(safe_messages[index:], start=index):
        copied = dict(message)
        copied["_message_index"] = message_index
        remaining.append(copied)
    return system_messages, remaining


def build_interaction_blocks(messages):
    _system_messages, remaining = split_system_messages(messages)
    blocks = []
    current = None
    for message in remaining:
        if message.get("role") == "user":
            if current:
                blocks.append(_finalize_group(current))
            current = {"kind": "interaction", "messages": [message]}
            continue
        if current is None:
            current = {"kind": "orphan", "messages": [message]}
        else:
            current["messages"].append(message)
    if current:
        blocks.append(_finalize_group(current))
    return blocks


def build_projection_units(messages):
    _system_messages, remaining = split_system_messages(messages)
    units = []
    index = 0
    while index < len(remaining):
        message = remaining[index]
        role = message.get("role")
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if isinstance(tool_calls, list) and tool_calls:
            call_ids = {
                str(call.get("id") or "")
                for call in tool_calls
                if isinstance(call, dict) and str(call.get("id") or "")
            }
            bundled = [message]
            cursor = index + 1
            while cursor < len(remaining):
                candidate = remaining[cursor]
                if candidate.get("role") != "tool":
                    break
                tool_call_id = str(candidate.get("tool_call_id") or "")
                if call_ids and tool_call_id not in call_ids:
                    break
                bundled.append(candidate)
                cursor += 1
            units.append(_finalize_group({"kind": "tool_bundle", "messages": bundled}))
            index = cursor
            continue
        if role == "user":
            kind = "user"
        elif role == "assistant":
            kind = "assistant"
        elif role == "tool":
            kind = "orphan_tool"
        else:
            kind = "orphan"
        units.append(_finalize_group({"kind": kind, "messages": [message]}))
        index += 1
    return units


def build_stm_projection(messages, budget=STM_TOKEN_BUDGET, wm_ingress_watermark=-1):
    system_messages, _remaining = split_system_messages(messages)
    units = build_projection_units(messages)
    selected_units, omitted_units, selected_tokens, watermark_locked_count = _select_units(
        units,
        budget,
        wm_ingress_watermark=wm_ingress_watermark,
    )
    projected = [
        _strip_internal_fields(message)
        for message in [*system_messages, *[message for unit in selected_units for message in unit["messages"]]]
    ]
    return {
        "messages": projected,
        "budget": int(budget),
        "selected_tokens": selected_tokens,
        "selected_units": selected_units,
        "omitted_units": omitted_units,
        "wm_ingress_watermark": int(wm_ingress_watermark),
        "watermark_locked_omitted_unit_count": watermark_locked_count,
        "latest_unit_tokens": units[-1]["token_count"] if units else 0,
        "total_units": len(units),
    }


def stm_projection_status(messages, budget=STM_TOKEN_BUDGET, wm_ingress_watermark=-1):
    projection = build_stm_projection(messages, budget=budget, wm_ingress_watermark=wm_ingress_watermark)
    selected_units = projection["selected_units"]
    omitted_units = projection["omitted_units"]
    all_units = [*omitted_units, *selected_units]
    omitted_tokens = sum(int(unit["token_count"]) for unit in omitted_units)
    total_unit_tokens = omitted_tokens + int(projection["selected_tokens"])
    largest_unit_tokens = max([int(unit["token_count"]) for unit in all_units], default=0)
    return {
        "budget": projection["budget"],
        "wm_ingress_watermark": projection["wm_ingress_watermark"],
        "watermark_locked_omitted_unit_count": projection["watermark_locked_omitted_unit_count"],
        "selected_tokens": projection["selected_tokens"],
        "omitted_tokens": omitted_tokens,
        "total_unit_tokens": total_unit_tokens,
        "budget_used_ratio": round(projection["selected_tokens"] / projection["budget"], 4) if projection["budget"] else 0,
        "selected_unit_count": len(selected_units),
        "omitted_unit_count": len(omitted_units),
        "latest_unit_tokens": projection["latest_unit_tokens"],
        "largest_unit_tokens": largest_unit_tokens,
        "total_unit_count": projection["total_units"],
        "total_message_count": len(messages or []),
        "non_system_message_count": sum(1 for message in messages or [] if message.get("role") != "system"),
        "selected_units": [_unit_metadata(unit) for unit in selected_units],
        "omitted_units": [_unit_metadata(unit) for unit in omitted_units],
    }


def latest_eligible_unit_tokens(messages, wm_ingress_watermark=-1):
    watermark = int(wm_ingress_watermark if wm_ingress_watermark is not None else -1)
    eligible = [unit for unit in build_projection_units(messages) if int(unit["start_index"]) > watermark]
    return int(eligible[-1]["token_count"]) if eligible else 0


def _finalize_group(group):
    messages = group["messages"]
    token_count = sum(count_message_tokens(_strip_internal_fields(message)) for message in messages)
    indexes = [message["_message_index"] for message in messages]
    return {
        "kind": group["kind"],
        "messages": messages,
        "start_index": indexes[0],
        "end_index": indexes[-1],
        "message_count": len(messages),
        "token_count": token_count,
    }


def _select_units(units, budget, wm_ingress_watermark=-1):
    if budget < 1:
        raise RuntimeError("STM token budget must be positive.")
    watermark = int(wm_ingress_watermark if wm_ingress_watermark is not None else -1)
    locked = [unit for unit in units if int(unit["start_index"]) <= watermark]
    eligible = [unit for unit in units if int(unit["start_index"]) > watermark]
    if eligible and eligible[-1]["token_count"] > budget:
        raise RuntimeError(
            "STM newest atomic unit exceeds budget: "
            f"latest_unit_tokens={eligible[-1]['token_count']}, budget={budget}."
        )
    selected = []
    selected_tokens = 0
    for unit in reversed(eligible):
        next_tokens = selected_tokens + unit["token_count"]
        if next_tokens > budget:
            break
        selected.append(unit)
        selected_tokens = next_tokens
    selected.reverse()
    omitted_count = len(eligible) - len(selected)
    omitted = [_omit_group_messages(unit) for unit in [*locked, *eligible[:omitted_count]]]
    return selected, omitted, selected_tokens, len(locked)


def _unit_metadata(unit):
    return {
        "kind": unit["kind"],
        "start_index": unit["start_index"],
        "end_index": unit["end_index"],
        "message_count": unit["message_count"],
        "token_count": unit["token_count"],
    }


def _strip_internal_fields(message):
    copied = dict(message)
    copied.pop("_message_index", None)
    return copied


def _omit_group_messages(group):
    copied = dict(group)
    copied.pop("messages", None)
    return copied
