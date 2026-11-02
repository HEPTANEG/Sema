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
                blocks.append(_finalize_block(current))
            current = {"kind": "interaction", "messages": [message]}
            continue

        if current is None:
            current = {"kind": "orphan", "messages": [message]}
        else:
            current["messages"].append(message)

    if current:
        blocks.append(_finalize_block(current))
    return blocks



def build_stm_projection(messages, budget=STM_TOKEN_BUDGET, wm_ingress_watermark=-1):
    system_messages, _remaining = split_system_messages(messages)
    blocks = build_interaction_blocks(messages)
    selected_blocks, omitted_blocks, selected_tokens, watermark_locked_count = _select_blocks(
        blocks,
        budget,
        wm_ingress_watermark=wm_ingress_watermark,
    )
    projected = [
        _strip_internal_fields(message)
        for message in [*system_messages, *[message for block in selected_blocks for message in block["messages"]]]
    ]
    return {
        "messages": projected,
        "budget": budget,
        "selected_tokens": selected_tokens,
        "selected_blocks": selected_blocks,
        "omitted_blocks": omitted_blocks,
        "wm_ingress_watermark": int(wm_ingress_watermark),
        "watermark_locked_omitted_block_count": watermark_locked_count,
        "latest_block_tokens": blocks[-1]["token_count"] if blocks else 0,
        "total_blocks": len(blocks),
    }



def stm_projection_status(messages, budget=STM_TOKEN_BUDGET, wm_ingress_watermark=-1):
    projection = build_stm_projection(messages, budget=budget, wm_ingress_watermark=wm_ingress_watermark)
    selected_blocks = projection["selected_blocks"]
    omitted_blocks = projection["omitted_blocks"]
    all_blocks = [*omitted_blocks, *selected_blocks]
    omitted_tokens = sum(int(block["token_count"]) for block in omitted_blocks)
    total_block_tokens = omitted_tokens + int(projection["selected_tokens"])
    largest_block_tokens = max([int(block["token_count"]) for block in all_blocks], default=0)
    return {
        "budget": projection["budget"],
        "wm_ingress_watermark": projection["wm_ingress_watermark"],
        "watermark_locked_omitted_block_count": projection["watermark_locked_omitted_block_count"],
        "selected_tokens": projection["selected_tokens"],
        "omitted_tokens": omitted_tokens,
        "total_block_tokens": total_block_tokens,
        "budget_used_ratio": round(projection["selected_tokens"] / projection["budget"], 4) if projection["budget"] else 0,
        "selected_block_count": len(selected_blocks),
        "omitted_block_count": len(omitted_blocks),
        "latest_block_tokens": projection["latest_block_tokens"],
        "largest_block_tokens": largest_block_tokens,
        "total_block_count": projection["total_blocks"],
        "total_message_count": len(messages or []),
        "non_system_message_count": sum(1 for message in messages or [] if message.get("role") != "system"),
        "selected_blocks": [_block_metadata(block) for block in selected_blocks],
        "omitted_blocks": [_block_metadata(block) for block in omitted_blocks],
    }


def _finalize_block(block):
    messages = block["messages"]
    token_count = sum(count_message_tokens(_strip_internal_fields(message)) for message in messages)
    indexes = [message["_message_index"] for message in messages]
    return {
        "kind": block["kind"],
        "messages": messages,
        "start_index": indexes[0],
        "end_index": indexes[-1],
        "message_count": len(messages),
        "token_count": token_count,
    }


def _select_blocks(blocks, budget, wm_ingress_watermark=-1):
    if budget < 1:
        raise RuntimeError("STM token budget must be positive.")
    watermark = int(wm_ingress_watermark if wm_ingress_watermark is not None else -1)
    locked = [block for block in blocks if int(block["end_index"]) <= watermark]
    eligible = [block for block in blocks if int(block["end_index"]) > watermark]
    if eligible and eligible[-1]["token_count"] > budget:
        raise RuntimeError(
            f"STM latest interaction block exceeds budget: "
            f"latest_block_tokens={eligible[-1]['token_count']}, budget={budget}. "
            "STM V1 does not truncate messages or interaction blocks."
        )

    selected = []
    selected_tokens = 0
    for block in reversed(eligible):
        next_tokens = selected_tokens + block["token_count"]
        if next_tokens > budget:
            break
        selected.append(block)
        selected_tokens = next_tokens
    selected.reverse()
    omitted_count = len(eligible) - len(selected)
    omitted = [_omit_block_messages(block) for block in [*locked, *eligible[:omitted_count]]]
    return selected, omitted, selected_tokens, len(locked)


def _block_metadata(block):
    return {
        "kind": block["kind"],
        "start_index": block["start_index"],
        "end_index": block["end_index"],
        "message_count": block["message_count"],
        "token_count": block["token_count"],
    }


def _strip_internal_fields(message):
    copied = dict(message)
    copied.pop("_message_index", None)
    return copied

def _omit_block_messages(block):
    copied = dict(block)
    copied.pop("messages", None)
    return copied
