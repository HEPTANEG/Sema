"""Strict HaluMem Judge response decoding, independent of provider transport."""

import json


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_judge_json(text):
    value = str(text or "").strip()
    if not value:
        return None, "HaluMem Judge returned empty content."
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or lines[0].lower() not in {"```", "```json"} or lines[-1] != "```":
            return None, "HaluMem Judge returned an incomplete JSON fence."
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_fields)
    except (ValueError, TypeError) as error:
        return None, f"HaluMem Judge returned invalid JSON: {error}"
    if not isinstance(parsed, dict):
        return None, "HaluMem Judge JSON must be an object."
    return parsed, None


def decode_judgment(response, schema):
    raw = response.get("raw_response") or {}
    if not isinstance(raw, dict):
        return None, None, "Invalid Judge response envelope."
    choices = raw.get("choices") or []
    if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
        return None, None, "Invalid Judge choices."
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        return None, None, "Invalid Judge message."
    content = message.get("content", response.get("text", ""))
    source = "content"
    if not str(content or "").strip():
        content = message.get("reasoning_content", "")
        source = "reasoning_content"
    if choice.get("finish_reason") in {"length", "tool_calls", "content_filter"}:
        return None, source, "Judge response did not finish normally."
    parsed, error = parse_judge_json(content)
    if error:
        return None, source, error
    fields = {
        "qa": {"evaluation_result", "reasoning"},
        "update": {"evaluation_result", "reason"},
        "integrity": {"score", "reasoning"},
        "accuracy": {"accuracy_score", "is_included_in_golden_memories", "reason"},
    }[schema]
    valid = fields <= parsed.keys()
    reason = "reasoning" if schema in {"qa", "integrity"} else "reason"
    valid = valid and isinstance(parsed.get(reason), str)
    if schema in {"qa", "update"}:
        labels = {"Correct", "Hallucination", "Omission"}
        if schema == "update":
            labels.add("Other")
        valid = valid and isinstance(parsed.get("evaluation_result"), str) and parsed["evaluation_result"] in labels
    else:
        key = "score" if schema == "integrity" else "accuracy_score"
        valid = valid and not isinstance(parsed.get(key), bool) and str(parsed.get(key)) in {"0", "1", "2"}
        if schema == "accuracy":
            valid = valid and str(parsed.get("is_included_in_golden_memories")).lower() in {"true", "false"}
    if not valid:
        return None, source, f"HaluMem Judge returned invalid {schema} scoring fields."
    return {key: parsed[key] for key in fields}, source, None
