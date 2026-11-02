import json
import re






MAX_SUMMARY_CHARS = 160


def _clip(text, limit=MAX_SUMMARY_CHARS):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _redact(text):

    text = str(text or "")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-REDACTED", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret)\s*=\s*\S+", r"\1=REDACTED", text)
    return text


def summarize_args(tool, args):
    if tool == "retrieve_memory":
        return {
            "argument_keys": sorted(args.keys()) if isinstance(args, dict) else [],
            "has_query": bool(args.get("query")) if isinstance(args, dict) else False,
            "has_continuation": bool(args.get("continuation_id")) if isinstance(args, dict) else False,
            "include_context": bool(args.get("include_context")) if isinstance(args, dict) else False,
            "close": bool(args.get("close")) if isinstance(args, dict) else False,
        }
    safe_args = _redact(args)
    if tool == "read_file" and isinstance(args, dict):
        summary = {"path": _clip(_redact(args.get("path", "")))}
        for key in ("start_line", "line_count", "start_char", "char_count", "max_chars", "full_read"):
            if args.get(key) is not None:
                summary[key] = args.get(key)
        return summary
    if tool == "get_overview" and isinstance(args, dict):
        return {
            "path": _clip(_redact(args.get("path", ""))),
            "has_instruction": bool(args.get("instruction")),
            "max_chars": args.get("max_chars"),
        }
    if tool in {"read_file", "write_file", "insert_after", "replace_block"}:
        return {"path": _clip(safe_args)}
    if tool == "list_images":
        if isinstance(args, dict):
            return {
                "path": _clip(_redact(args.get("path", ""))),
                "recursive": bool(args.get("recursive")),
            }
        return {"path": _clip(safe_args)}
    if tool == "vision_analyze_images":
        if isinstance(args, dict):
            paths = args.get("paths") if isinstance(args.get("paths"), list) else []
            return {
                "image_count": len(paths),
                "instruction": _clip(_redact(args.get("instruction", ""))),
            }
        return {"args": _clip(safe_args)}
    if tool == "run_command":
        return {"command": _clip(safe_args)}
    if tool == "start_background_command":
        if isinstance(args, dict):
            return {
                "command": _clip(_redact(args.get("command", ""))),
                "cwd": _clip(_redact(args.get("cwd", ""))),
            }
        return {"command": _clip(safe_args)}
    if tool in {"list_background_commands", "read_background_command_output", "stop_background_command"}:
        return {"args": _clip(safe_args)}
    if tool == "web_search":
        if isinstance(args, dict):
            return {
                "query": _clip(_redact(args.get("query", ""))),
                "max_results": args.get("max_results"),
            }
        return {"query": _clip(safe_args)}
    if tool == "web_fetch":
        return {"url": _clip(safe_args)}
    if tool in {"show_undo_records", "undo_edit"}:
        return {"args": _clip(safe_args)}
    if tool == "apply_patch":
        return {"patch_chars": len(safe_args)}
    if tool == "show_memory_state":
        return {"args": _clip(safe_args)}
    return {"args": _clip(safe_args)}


def summarize_tool_result(tool, result):
    result_text = str(result or "")
    summary = {
        "captured_chars": len(result_text),
    }
    if result_text.startswith("Tool error:"):
        summary["error"] = _clip(_redact(result_text.removeprefix("Tool error:").strip()))
    if tool == "run_command":
        match = re.search(r"exit code\s+(-?\d+)", result_text, flags=re.IGNORECASE)
        if match:
            summary["exit_code"] = int(match.group(1))
    if tool in {"start_background_command", "stop_background_command"}:
        status_match = re.search(r"^status:\s*(.+)$", result_text, flags=re.IGNORECASE | re.MULTILINE)
        id_match = re.search(r"^id:\s*(.+)$", result_text, flags=re.IGNORECASE | re.MULTILINE)
        if status_match:
            summary["status_text"] = _clip(status_match.group(1))
        if id_match:
            summary["command_id"] = _clip(id_match.group(1))
    if tool == "web_search":
        summary["sources"] = len(re.findall(r"^\d+\. ", result_text, flags=re.MULTILINE))
    if tool == "web_fetch":
        match = re.search(r"chars:\s*(\d+)", result_text, flags=re.IGNORECASE)
        if match:
            summary["page_chars"] = int(match.group(1))
    if tool == "list_images":
        match = re.search(r"count:\s*(\d+)", result_text, flags=re.IGNORECASE)
        if match:
            summary["image_count"] = int(match.group(1))
    if tool == "vision_analyze_images":
        summary["analysis_chars"] = len(result_text)
    if tool in {"read_file", "get_overview", "write_file", "insert_after", "replace_block"}:
        line_count = result_text.count("\n") + (1 if result_text else 0)
        summary["lines"] = line_count
    return summary


def format_trace_events(events):
    if not events:
        return "No trace records."

    lines = []
    for record in events:
        timestamp = str(record.get("ts", ""))
        clock = timestamp[11:19] if len(timestamp) >= 19 else timestamp
        event = record.get("event", "?")
        status = record.get("status") or ""
        duration = record.get("duration_ms")
        duration_text = f" {duration}ms" if duration is not None else ""
        tool = f" {record.get('tool')}" if record.get("tool") else ""
        args = record.get("args_summary") or {}
        args_text = " ".join(f"{key}={value}" for key, value in args.items())
        if event == "runtime_directives":
            details = {
                "request": record.get("model_request_index"),
                "count": record.get("directive_count"),
                "clock": record.get("clock_message"),
                "clock_date": record.get("clock_local_date"),
                "clock_tz": record.get("clock_timezone"),
            }
            args_text = " ".join(
                f"{key}={value}" for key, value in details.items() if value not in (None, "")
            )
        if event == "runtime_rhythm":
            details = {
                "turns": record.get("recent_turn_count"),
                "session_s": record.get("continuous_session_duration_seconds"),
                "user_gap_s": record.get("user_gap_seconds"),
                "assistant_gap_s": record.get("assistant_gap_seconds"),
            }
            args_text = " ".join(
                f"{key}={value}" for key, value in details.items() if value not in (None, "")
            )
        if event == "context_pressure":
            details = {
                "stm": record.get("stm"),
                "wm": record.get("wm"),
                "I": record.get("I"),
                "S": record.get("S"),
                "R": record.get("R"),
                "L": record.get("L"),
                "P": record.get("P_context"),
                "failure": record.get("budget_action"),
            }
            args_text = " ".join(
                f"{key}={value}" for key, value in details.items() if value not in (None, "")
            )
        if event == "retrieval":
            skills = record.get("selected_skill_paths") or []
            details = {
                "source": record.get("source"),
                "skills": record.get("selected_skill_count"),
                "memory": record.get("selected_memory_count"),
                "topics": record.get("selected_topic_count"),
            }
            args_text = " ".join(
                f"{key}={value}" for key, value in details.items() if value not in (None, "")
            )
            if skills:
                args_text = f"{args_text} selected_skills={', '.join(skills)}".strip()
        error = record.get("error")
        error_text = f' error="{_clip(error)}"' if error else ""
        lines.append(f"{clock} {event}{tool} {status}{duration_text} {args_text}{error_text}".rstrip())
    return "\n".join(lines)
