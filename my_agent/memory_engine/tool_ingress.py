import json

import memory_engine.event_stream as event_stream
from .context_budget_override import effective_budget_ranges
from .dynamic_context_budget import STM_MAX_TOKENS, STM_MIN_TOKENS, WM_MAX_TOKENS, WM_MIN_TOKENS
from .stm_projection import build_projection_units
from .token_counter import count_text_tokens


FILE_TOOLS = {"read_file", "get_overview"}
PREVIEW_CHARS = 2000


def guard_tool_result(tool, content, tool_call_id, messages, runtime_state=None, root=None):
    content = str(content or "")
    maximum = _stm_maximum(runtime_state)
    message = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
    bundle_tokens = _latest_bundle_tokens([*(messages or []), message])
    if bundle_tokens <= maximum:
        return {
            "status": "ok",
            "content": content,
            "bundle_tokens": bundle_tokens,
            "maximum": maximum,
            "artifact_id": None,
        }
    try:
        artifact = event_stream.store_artifact(content, artifact_type="tool_output", root=root)
    except Exception as error:
        envelope = {
            "status": "too_large_storage_failed",
            "tool": str(tool or "unknown_tool"),
            "message": "The tool result exceeded the active context limit and durable artifact storage failed. The full result was not added to Event Stream.",
            "error_type": type(error).__name__,
            "output_chars": len(content),
            "output_tokens": count_text_tokens(content),
            "available_actions": _actions(tool),
        }
        return {
            "status": "too_large_storage_failed",
            "content": _bounded_envelope(envelope, "", messages, tool_call_id, maximum),
            "bundle_tokens": bundle_tokens,
            "maximum": maximum,
            "artifact_id": None,
        }
    envelope = {
        "status": "too_large",
        "tool": str(tool or "unknown_tool"),
        "message": "The full tool result was stored as an Event Stream artifact and replaced by this bounded result.",
        "output_chars": len(content),
        "output_tokens": count_text_tokens(content),
        "artifact_id": artifact["artifact_id"],
        "available_actions": _actions(tool),
    }
    bounded = _bounded_envelope(envelope, content[:PREVIEW_CHARS], messages, tool_call_id, maximum)
    return {
        "status": "too_large",
        "content": bounded,
        "bundle_tokens": bundle_tokens,
        "maximum": maximum,
        "artifact_id": artifact["artifact_id"],
    }


def _bounded_envelope(envelope, preview, messages, tool_call_id, maximum):
    preview = str(preview or "")
    while True:
        payload = dict(envelope)
        payload["preview"] = preview
        payload["preview_truncated"] = True
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        candidate = {"role": "tool", "tool_call_id": tool_call_id, "content": rendered}
        if _latest_bundle_tokens([*(messages or []), candidate]) <= maximum or not preview:
            return rendered
        preview = preview[: max(0, len(preview) // 2)]


def _latest_bundle_tokens(messages):
    units = build_projection_units(messages)
    return int(units[-1]["token_count"]) if units else 0


def _stm_maximum(runtime_state):
    pressure = (runtime_state or {}).get("context_pressure") if isinstance(runtime_state, dict) else None
    maximum = int((((pressure or {}).get("budgets") or {}).get("stm_max")) or 0)
    if maximum > 0:
        return maximum
    ranges = effective_budget_ranges(
        {
            "stm_min": STM_MIN_TOKENS,
            "stm_max": STM_MAX_TOKENS,
            "wm_min": WM_MIN_TOKENS,
            "wm_max": WM_MAX_TOKENS,
        },
        create=False,
    )
    return int(ranges["stm_max"])


def _actions(tool):
    if str(tool or "") in FILE_TOOLS:
        return [
            "Retry read_file with start_line, line_count, and max_chars.",
            "Use get_overview for a bounded overview that does not replace the original source.",
        ]
    return ["Retry the tool with narrower arguments or a smaller requested range."]
