TOOL_STYLE = "orange1"


def summarize_tool_output(tool, args, text):
    if text.startswith("Tool error:"):
        return text
    if _is_cancelled_tool_result(text):
        return text

    char_count = len(text)
    line_count = len(text.splitlines())

    if tool == "read_file":
        path = _extract_read_path(text) or (args.get("path") if isinstance(args, dict) else args)
        return f"Read {path}; {line_count} lines, {char_count} chars captured for context."

    if tool == "get_overview":
        path = args.get("path") if isinstance(args, dict) else args
        return f"Generated overview for {path}; {line_count} lines, {char_count} chars captured for context."

    if tool == "list_files":
        entry_count = _count_directory_entries(text)
        return f"Listed {args or '.'}; {entry_count} entries captured for context."

    if tool == "list_images":
        image_count = _extract_count_field(text)
        target = args.get("path") if isinstance(args, dict) else args
        if image_count is None:
            return f"Listed images in {target or '.'}; {line_count} lines, {char_count} chars captured for context."
        return f"Listed images in {target or '.'}; {image_count} images captured for context."

    if tool == "vision_analyze_images":
        paths = args.get("paths", []) if isinstance(args, dict) else []
        image_count = len(paths) if isinstance(paths, list) else 0
        return f"Analyzed {image_count} image(s); {line_count} lines, {char_count} chars captured for context."

    if tool == "run_command":
        exit_code = _extract_exit_code(text)
        if exit_code is None:
            return f"Ran command: {args}; {line_count} lines, {char_count} chars captured for context."
        return f"Ran command: {args}; exit code {exit_code}; {line_count} lines, {char_count} chars captured for context."

    if tool == "start_background_command":
        command_id = _extract_field(text, "id")
        status = _extract_field(text, "status")
        command = args.get("command") if isinstance(args, dict) else args
        return f"Started background command: {command}; id {command_id or '(unknown)'}; status {status or '(unknown)'}."

    if tool == "list_background_commands":
        return f"Listed background commands; {line_count} lines, {char_count} chars captured for context."

    if tool == "read_background_command_output":
        command_id = args.get("command_id") if isinstance(args, dict) else args
        return f"Read background command output: {command_id}; {line_count} lines, {char_count} chars captured for context."

    if tool == "stop_background_command":
        command_id = args if not isinstance(args, dict) else args.get("command_id")
        status = _extract_field(text, "status")
        return f"Stopped background command: {command_id}; status {status or '(unknown)'}."

    if tool == "web_search":
        source_count = len([line for line in text.splitlines() if line[:2].strip(".").isdigit() and ". " in line[:4]])
        query = args.get("query") if isinstance(args, dict) else args
        return f"Searched web: {query}; {source_count} sources, {char_count} chars captured for context."

    if tool == "web_fetch":
        return f"Fetched web page: {args}; {line_count} lines, {char_count} chars captured for context."

    if tool == "show_undo_records":
        return f"Listed undo records; {line_count} lines, {char_count} chars captured for context."

    if tool == "undo_edit":
        return f"Undo result: {text}"

    return text


def format_tool_label(tool, args):
    args_text = format_tool_args(tool, args)
    return f"{tool} {args_text}".strip()


def format_tool_args(tool, args):
    if tool == "start_background_command" and isinstance(args, dict):
        command = _clip_inline(args.get("command", ""), 100)
        cwd = _clip_inline(args.get("cwd", ""), 60)
        return f"{command}" + (f" cwd={cwd}" if cwd else "")
    if tool == "read_background_command_output" and isinstance(args, dict):
        return _clip_inline(args.get("command_id", ""), 80)
    if tool == "vision_analyze_images" and isinstance(args, dict):
        paths = args.get("paths") if isinstance(args.get("paths"), list) else []
        instruction = _clip_inline(args.get("instruction", ""), 80)
        return f"{len(paths)} image(s)" + (f"; {instruction}" if instruction else "")
    if tool == "list_images" and isinstance(args, dict):
        path = _clip_inline(args.get("path", ""), 100)
        recursive = " recursive" if args.get("recursive") else ""
        return f"{path}{recursive}".strip()
    if tool in {"read_file", "get_overview"} and isinstance(args, dict):
        path = _clip_inline(args.get("path", ""), 100)
        if tool == "get_overview":
            instruction = _clip_inline(args.get("instruction", ""), 80)
            return f"{path}" + (f"; {instruction}" if instruction else "")
        range_bits = []
        if args.get("start_line") is not None:
            range_bits.append(f"line {args.get('start_line')}+{args.get('line_count') or 100}")
        if args.get("start_char") is not None:
            range_bits.append(f"char {args.get('start_char')}+{args.get('char_count') or ''}".rstrip("+"))
        if args.get("full_read"):
            range_bits.append("full")
        return f"{path}" + (f" ({', '.join(range_bits)})" if range_bits else "")
    if isinstance(args, dict):
        return _clip_inline(args, 120)
    return _clip_inline(args, 120)


def tool_progress_line(tool, args, text):
    return f"{_tool_progress_status(summarize_tool_output(tool, args, text))} {format_tool_label(tool, args)}".rstrip()


def format_last_run(transcript):
    if not transcript:
        return "No tool activity recorded for the last run."
    lines = []
    for index, record in enumerate(transcript, start=1):
        lines.append(f"{index}. {record['tool']} {record['args']}".rstrip())
        lines.append(record["result"])
        lines.append("")
    return "\n".join(lines).rstrip()


def format_write_summary(path_text, content):
    return f"[proposed write] {path_text}\nfull file replacement; new size: {_line_count(content)} lines, {len(content)} chars"


def format_patch_summary(patch):
    added, removed = diff_stats(patch)
    return f"[proposed patch]\n{removed} lines removed, {added} lines added"


def format_insert_summary(path_text, diff):
    added, removed = diff_stats(diff)
    return f"[proposed insert_after] {path_text}\n{removed} lines removed, {added} lines added"


def format_replace_summary(path_text, diff):
    added, removed = diff_stats(diff)
    return f"[proposed replace_block] {path_text}\n{removed} lines removed, {added} lines added"


def _clip_inline(value, limit):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _tool_progress_status(summary):
    if summary.startswith("Tool error:"):
        return "!"
    if _is_cancelled_tool_result(summary):
        return "x"
    return "ok"


def _is_cancelled_tool_result(text):
    first_line = str(text or "").splitlines()[0] if str(text or "").splitlines() else ""
    return "cancelled" in first_line.lower()


def _extract_read_path(text):
    lines = text.splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    if first.startswith("--- ") and first.endswith(" ---"):
        return first.removeprefix("--- ").removesuffix(" ---")
    return None


def _count_directory_entries(text):
    lines = text.splitlines()
    if len(lines) <= 2:
        return 0
    return len([line for line in lines[1:-1] if line and line != "(empty)"])


def _extract_count_field(text):
    for line in text.splitlines():
        if line.startswith("count:"):
            try:
                return int(line.removeprefix("count:").strip())
            except ValueError:
                return None
    return None


def _extract_exit_code(text):
    for line in text.splitlines():
        if line.startswith("exit code:"):
            return line.removeprefix("exit code:").strip()
    return None


def _extract_field(text, field):
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _line_count(text):
    if not text:
        return 0
    return len(text.splitlines())


def diff_stats(diff):
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def text_diff_stats(before, after):
    import difflib

    diff = "".join(
        difflib.unified_diff(
            str(before or "").splitlines(keepends=True),
            str(after or "").splitlines(keepends=True),
        )
    )
    return diff_stats(diff)


_diff_stats = diff_stats


def _guess_lexer(path_text):
    suffix = str(path_text).lower().rsplit(".", 1)[-1] if "." in str(path_text) else ""
    return {
        "py": "python",
        "md": "markdown",
        "json": "json",
        "ps1": "powershell",
        "cmd": "batch",
        "bat": "batch",
    }.get(suffix, "text")
