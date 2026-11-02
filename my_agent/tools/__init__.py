import json
from pathlib import Path
import subprocess
import difflib
from persistence.undo import record_edit, snapshot_file



MAX_FILE_CHARS = 40000
WARN_FILE_CHARS = MAX_FILE_CHARS
TOO_LARGE_FILE_CHARS = 200000
PREVIEW_LINE_COUNT = 100
PREVIEW_MAX_CHARS = 8000
MAX_COMMAND_OUTPUT_CHARS = 20000
COMMAND_TIMEOUT_SECONDS = 60
WORKSPACE_CONFIG_PATH = Path(".sema") / "config.json"
LEGACY_WORKSPACE_CONFIG_PATH = Path(".emax") / "config.json"
PROTECTED_STATE_DIRS = {".sema", ".emax"}


def _workspace_root():
    return Path.cwd().resolve()


def resolve_user_path(path_text):

    return (Path(path_text).expanduser() if Path(path_text).expanduser().is_absolute() else _workspace_root() / path_text).resolve()


def is_inside_workspace(path):
    workspace_root = _workspace_root()
    return workspace_root == path or workspace_root in path.parents


def path_outside_workspace(path_text):
    return not is_inside_workspace(resolve_user_path(path_text))


def format_display_path(path):

    try:
        return path.relative_to(_workspace_root())
    except ValueError:
        return path


def load_workspace_config():

    config_path = Path.cwd() / WORKSPACE_CONFIG_PATH
    legacy_config_path = Path.cwd() / LEGACY_WORKSPACE_CONFIG_PATH
    if not config_path.exists():
        if not legacy_config_path.exists():
            return {}

        config_path = legacy_config_path


    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(".sema/config.json must contain a JSON object.")

    return data


def list_directory_for_display(path_text):

    entries = _list_directory_entries(path_text)
    return "\n".join(f"{entry.name}{'/' if entry.is_dir() else ''}" for entry in entries)


def list_directory_for_context(path_text, allow_external=False):

    path_text = path_text.strip() or "."
    directory_path = resolve_workspace_directory(path_text, allow_external=allow_external)
    entries = _list_directory_entries(path_text, allow_external=allow_external)
    lines = [f"{entry.name}{'/' if entry.is_dir() else ''}" for entry in entries]
    if not lines:
        lines = ["(empty)"]

    display = format_display_path(directory_path)
    return f"--- directory: {display} ---\n" + "\n".join(lines) + "\n--- end ---"


def resolve_workspace_directory(path_text, allow_external=False):

    directory_path = resolve_user_path(path_text)


    if not allow_external and not is_inside_workspace(directory_path):
        raise RuntimeError("Cannot list directories outside the current working directory.")
    if any(part in PROTECTED_STATE_DIRS for part in directory_path.parts):
        raise RuntimeError("Refusing to list sensitive Sema files.")
    if not directory_path.exists():
        raise RuntimeError(f"Directory not found: {path_text}")
    if not directory_path.is_dir():
        raise RuntimeError(f"Not a directory: {path_text}")

    return directory_path


def _list_directory_entries(path_text, allow_external=False):

    directory_path = resolve_workspace_directory(path_text, allow_external=allow_external)
    return sorted(
        directory_path.iterdir(),
        key=lambda entry: (not entry.is_dir(), entry.name.lower()),
    )



def resolve_workspace_file(path_text, allow_external=False):
    if not path_text:
        raise RuntimeError("Usage: /read <file>")

    file_path = resolve_user_path(path_text)


    if not allow_external and not is_inside_workspace(file_path):
        raise RuntimeError("Cannot read files outside the current working directory.")

    if file_path.name == ".env" or any(part in PROTECTED_STATE_DIRS for part in file_path.parts):
        raise RuntimeError("Refusing to read sensitive Sema files.")

    if not file_path.exists():
        raise RuntimeError(f"File not found: {path_text}")

    if not file_path.is_file():
        raise RuntimeError(f"Not a file: {path_text}")

    return file_path


def resolve_workspace_write_file(path_text, allow_external=False):

    if not path_text:
        raise RuntimeError("Missing file path for write_file.")

    file_path = resolve_user_path(path_text)


    if not allow_external and not is_inside_workspace(file_path):
        raise RuntimeError("Cannot write files outside the current working directory.")
    if file_path.name == ".env" or any(part in PROTECTED_STATE_DIRS for part in file_path.parts):
        raise RuntimeError("Refusing to write sensitive Sema files.")
    if file_path.exists() and not file_path.is_file():
        raise RuntimeError(f"Not a file: {path_text}")

    return file_path



def read_file_for_context(path_text, allow_external=False):
    request = _normalize_read_file_request(path_text)
    file_path = resolve_workspace_file(request["path"], allow_external=allow_external)
    content = file_path.read_text(encoding="utf-8", errors="replace")
    display = format_display_path(file_path)
    metadata = _file_metadata(file_path, content, display)
    range_kind = _read_range_kind(request)
    max_chars = _positive_int(request.get("max_chars"), MAX_FILE_CHARS)

    if range_kind == "line":
        return display, _format_ranged_file_context(display, content, metadata, request, max_chars)
    if range_kind == "char":
        return display, _format_char_ranged_file_context(display, content, metadata, request, max_chars)

    if metadata["file_chars"] > TOO_LARGE_FILE_CHARS:
        preview, preview_metadata = _preview_content(content)
        preview_metadata["next_read"] = _next_line_read(display, min(PREVIEW_LINE_COUNT, metadata["total_lines"]) + 1, metadata["total_lines"])
        return display, _format_file_context(
            display,
            "too_large",
            preview,
            metadata | preview_metadata,
            message="File is too large for full raw reading. Use read_file with a line or character range, or use get_overview first.",
            content_label="preview",
        )

    if metadata["file_chars"] > WARN_FILE_CHARS and not request.get("full_read"):
        preview, preview_metadata = _preview_content(content)
        preview_metadata["next_read"] = _next_line_read(display, min(PREVIEW_LINE_COUNT, metadata["total_lines"]) + 1, metadata["total_lines"])
        return display, _format_file_context(
            display,
            "warn_large",
            preview,
            metadata | preview_metadata,
            message="Large file preview only. Use read_file with a line or character range, get_overview, or set full_read=true if full raw reading is necessary.",
            content_label="preview",
        )

    selected = content
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True

    status = "warn_large" if truncated else "ok"
    body_metadata = metadata | {
        "range": "full",
        "returned_lines": _line_range_label(1, _line_count(content)),
        "returned_chars": len(selected),
        "estimated_returned_tokens": _estimate_text_tokens(selected),
        "truncated": truncated,
        "truncated_by_chars": truncated,
        "next_read": _next_char_read(display, len(selected), len(content)) if truncated else "",
    }
    return display, _format_file_context(display, status, selected, body_metadata)


def get_file_overview_for_context(args, allow_external=False):
    request = _normalize_overview_request(args)
    file_path = resolve_workspace_file(request["path"], allow_external=allow_external)
    content = file_path.read_text(encoding="utf-8", errors="replace")
    display = format_display_path(file_path)
    metadata = _file_metadata(file_path, content, display)
    lines = content.splitlines()
    instruction = request.get("instruction") or "Provide a navigation overview for this file."
    max_chars = _positive_int(request.get("max_chars"), 12000)
    preview_sections = _overview_sections(lines, max_chars=max_chars)
    body = "\n\n".join(preview_sections)
    overview_metadata = metadata | {
        "status": "ok",
        "instruction": instruction,
        "returned_chars": len(body),
        "estimated_returned_tokens": _estimate_text_tokens(body),
        "covered_ranges": _overview_covered_ranges(lines),
        "suggested_next_action": "Use read_file with start_line/line_count or start_char/char_count for exact source ranges.",
    }
    metadata_text = _metadata_lines(overview_metadata)
    return (
        f"--- overview: {display} ---\n"
        "This overview is for navigation and triage. It is not source text and must not replace reading relevant raw ranges.\n"
        f"{metadata_text}\n"
        "--- overview content ---\n"
        f"{body}\n"
        "--- end overview ---"
    )


def _normalize_read_file_request(value):
    if isinstance(value, dict):
        request = dict(value)
    else:
        request = {"path": value}
    path = str(request.get("path") or "").strip()
    if not path:
        raise RuntimeError("Usage: /read <file>")
    request["path"] = path
    if _has_line_range(request) and _has_char_range(request):
        raise RuntimeError("read_file accepts either line range or character range, not both.")
    return request


def _normalize_overview_request(value):
    if isinstance(value, dict):
        request = dict(value)
    else:
        request = {"path": value}
    path = str(request.get("path") or "").strip()
    if not path:
        raise RuntimeError("get_overview requires a file path.")
    request["path"] = path
    return request


def _has_line_range(request):
    return request.get("start_line") is not None or request.get("line_count") is not None


def _has_char_range(request):
    return request.get("start_char") is not None or request.get("char_count") is not None


def _read_range_kind(request):
    if _has_line_range(request):
        return "line"
    if _has_char_range(request):
        return "char"
    return "full"


def _format_ranged_file_context(display, content, metadata, request, max_chars):
    lines = content.splitlines()
    start_line = _positive_int(request.get("start_line"), 1)
    line_count = _positive_int(request.get("line_count"), PREVIEW_LINE_COUNT)
    start_index = min(start_line - 1, len(lines))
    end_index = min(start_index + line_count, len(lines))
    selected = "\n".join(lines[start_index:end_index])
    line_too_long = any(len(line) > max_chars for line in lines[start_index:end_index])
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True
    status = "warn_large" if truncated or line_too_long else "ok"
    body_metadata = metadata | {
        "range": "line",
        "requested_start_line": start_line,
        "requested_line_count": line_count,
        "returned_lines": _line_range_label(start_index + 1, end_index),
        "returned_chars": len(selected),
        "estimated_returned_tokens": _estimate_text_tokens(selected),
        "truncated": truncated,
        "truncated_by_chars": truncated,
        "line_too_long": line_too_long,
        "next_read": _next_line_read(display, end_index + 1, len(lines)) if end_index < len(lines) else "",
    }
    return _format_file_context(display, status, selected, body_metadata)


def _format_char_ranged_file_context(display, content, metadata, request, max_chars):
    start_char = _non_negative_int(request.get("start_char"), 0)
    char_count = _positive_int(request.get("char_count"), max_chars)
    start = min(start_char, len(content))
    end = min(start + char_count, len(content))
    selected = content[start:end]
    truncated = False
    if len(selected) > max_chars:
        selected = selected[:max_chars]
        truncated = True
    status = "warn_large" if truncated else "ok"
    body_metadata = metadata | {
        "range": "char",
        "requested_start_char": start_char,
        "requested_char_count": char_count,
        "returned_char_range": f"{start}..{start + len(selected)}",
        "returned_chars": len(selected),
        "estimated_returned_tokens": _estimate_text_tokens(selected),
        "truncated": truncated,
        "truncated_by_chars": truncated,
        "line_too_long": _line_too_long_near_content(selected, max_chars),
        "next_read": _next_char_read(display, start + len(selected), len(content)) if start + len(selected) < len(content) else "",
    }
    return _format_file_context(display, status, selected, body_metadata)


def _format_file_context(display, status, body, metadata, message="", content_label="content"):
    info = dict(metadata or {})
    info["status"] = status
    if message:
        info["message"] = message
    info.setdefault("available_actions", "read_file range; get_overview")
    metadata_text = _metadata_lines(info)
    return (
        f"--- file: {display} ---\n"
        f"{metadata_text}\n"
        f"--- {content_label} ---\n"
        f"{body}\n"
        f"--- end ({status}) ---"
    )


def _metadata_lines(metadata):
    return "\n".join(f"{key}: {value}" for key, value in metadata.items() if value not in {None, ""})


def _file_metadata(file_path, content, display):
    lines = content.splitlines()
    return {
        "path": display,
        "file_size_bytes": file_path.stat().st_size,
        "file_chars": len(content),
        "total_lines": len(lines),
        "estimated_file_tokens": _estimate_text_tokens(content),
        "warn_file_chars": WARN_FILE_CHARS,
        "too_large_file_chars": TOO_LARGE_FILE_CHARS,
    }


def _preview_content(content):
    lines = content.splitlines()
    preview = "\n".join(lines[:PREVIEW_LINE_COUNT])
    truncated_by_chars = False
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[:PREVIEW_MAX_CHARS]
        truncated_by_chars = True
    metadata = {
        "range": "preview",
        "preview_lines": _line_range_label(1, min(PREVIEW_LINE_COUNT, len(lines))),
        "preview_max_chars": PREVIEW_MAX_CHARS,
        "returned_chars": len(preview),
        "estimated_returned_tokens": _estimate_text_tokens(preview),
        "truncated": True,
        "truncated_by_chars": truncated_by_chars,
        "line_too_long": bool(lines and len(lines[0]) > PREVIEW_MAX_CHARS),
        "next_read": "",
    }
    return preview, metadata


def _overview_sections(lines, max_chars):
    sections = []
    total = len(lines)
    windows = []
    if total:
        windows.append(("start", 1, min(40, total)))
    if total > 80:
        middle_start = max(1, total // 2 - 19)
        windows.append(("middle", middle_start, min(middle_start + 39, total)))
    if total > 40:
        end_start = max(1, total - 39)
        windows.append(("end", end_start, total))
    seen = set()
    used = 0
    for label, start, end in windows:
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        chunk = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        sections.append(f"[{label} lines {start}-{end}]\n{chunk}")
        used += len(chunk)
    return sections or ["(empty file)"]


def _overview_covered_ranges(lines):
    total = len(lines)
    if not total:
        return "(empty)"
    ranges = [f"1-{min(40, total)}"]
    if total > 80:
        middle_start = max(1, total // 2 - 19)
        ranges.append(f"{middle_start}-{min(middle_start + 39, total)}")
    if total > 40:
        ranges.append(f"{max(1, total - 39)}-{total}")
    return ", ".join(dict.fromkeys(ranges))


def _line_count(content):
    return len(content.splitlines())


def _line_range_label(start, end):
    if end < start:
        return "(none)"
    return f"{start}..{end}"


def _next_line_read(display, start_line, total_lines):
    if start_line > total_lines:
        return ""
    path = str(display).replace("\\", "/")
    return f'read_file(path="{path}", start_line={start_line}, line_count={PREVIEW_LINE_COUNT})'


def _next_char_read(display, start_char, total_chars):
    if start_char >= total_chars:
        return ""
    path = str(display).replace("\\", "/")
    return f'read_file(path="{path}", start_char={start_char}, char_count={MAX_FILE_CHARS})'


def _positive_int(value, default):
    if value is None:
        return int(default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Expected positive integer, got {value!r}.") from error
    if parsed < 1:
        raise RuntimeError(f"Expected positive integer, got {value!r}.")
    return parsed


def _non_negative_int(value, default):
    if value is None:
        return int(default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Expected non-negative integer, got {value!r}.") from error
    if parsed < 0:
        raise RuntimeError(f"Expected non-negative integer, got {value!r}.")
    return parsed


def _line_too_long_near_content(content, max_chars):
    return any(len(line) > max_chars for line in str(content or "").splitlines())


def _estimate_text_tokens(text):
    try:
        from memory_engine.token_counter import count_text_tokens

        return count_text_tokens(text)
    except Exception:
        return max(1, len(str(text or "")) // 4) if text else 0


def write_file_from_context(path_text, content, allow_external=False):

    file_path = resolve_workspace_write_file(path_text, allow_external=allow_external)
    before = snapshot_file(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    display = format_display_path(file_path)
    record_edit("write_file", [(file_path, before)], summary=str(display))
    return f"Wrote file: {display}"


def build_insert_after_edit(path_text, anchor, content, anchor_context=None, allow_external=False):

    file_path = resolve_workspace_write_file(path_text, allow_external=allow_external)
    original = file_path.read_text(encoding="utf-8", errors="replace")
    updated = insert_after_content(original, anchor, content, anchor_context)
    display = format_display_path(file_path)
    display_text = display.as_posix() if hasattr(display, "as_posix") else str(display)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{display_text}",
            tofile=f"b/{display_text}",
        )
    )
    return {
        "file_path": file_path,
        "display_path": display_text,
        "updated": updated,
        "diff": diff,
    }


def insert_after_from_context(path_text, anchor, content, anchor_context=None, allow_external=False):
    edit = build_insert_after_edit(path_text, anchor, content, anchor_context, allow_external=allow_external)
    before = snapshot_file(edit["file_path"])
    edit["file_path"].write_text(edit["updated"], encoding="utf-8")
    record_edit("insert_after", [(edit["file_path"], before)], summary=edit["display_path"])
    return f"Inserted content after anchor in: {edit['display_path']}"


def insert_after_content(original, anchor, content, anchor_context=None):

    anchor = anchor.strip()
    if not anchor:
        raise RuntimeError("insert_after requires ANCHOR content.")
    if not content.strip():
        raise RuntimeError("insert_after requires CONTENT.")

    lines = original.splitlines(keepends=True)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    matches = [index for index, line in enumerate(plain_lines) if line.strip() == anchor]
    if not matches:
        raise RuntimeError(f"Anchor not found: {anchor}")
    if len(matches) > 1:
        matches = narrow_anchor_matches_with_context(plain_lines, matches, anchor_context)
    if len(matches) > 1:
        raise RuntimeError(f"Anchor is not unique: {anchor}")

    anchor_index = matches[0]
    newline = detect_newline(original)
    prepared_content = prepare_insert_content(content, plain_lines[anchor_index], newline)
    return "".join(lines[: anchor_index + 1] + prepared_content + lines[anchor_index + 1 :])


def narrow_anchor_matches_with_context(lines, matches, anchor_context):
    if not anchor_context:
        return matches

    anchor_context = anchor_context.strip()
    if not anchor_context:
        return matches

    narrowed = []
    for position, match_index in enumerate(matches):
        next_match_index = matches[position + 1] if position + 1 < len(matches) else len(lines)
        following_lines = lines[match_index + 1 : next_match_index]
        if any(line.strip() == anchor_context for line in following_lines):
            narrowed.append(match_index)

    return narrowed or matches


def prepare_insert_content(content, anchor_line, newline):

    content_lines = content.splitlines()
    non_empty_lines = [line for line in content_lines if line.strip()]
    should_inherit_indent = non_empty_lines and all(line == line.lstrip() for line in non_empty_lines)
    indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip())] if should_inherit_indent else ""
    return [indent + line + newline for line in content_lines]


def build_replace_block_edit(path_text, old_block, new_block, allow_external=False):

    file_path = resolve_workspace_write_file(path_text, allow_external=allow_external)
    original = file_path.read_text(encoding="utf-8", errors="replace")
    updated = replace_block_content(original, old_block, new_block)
    display = format_display_path(file_path)
    display_text = display.as_posix() if hasattr(display, "as_posix") else str(display)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{display_text}",
            tofile=f"b/{display_text}",
        )
    )
    return {
        "file_path": file_path,
        "display_path": display_text,
        "updated": updated,
        "diff": diff,
    }


def replace_block_from_context(path_text, old_block, new_block, allow_external=False):
    edit = build_replace_block_edit(path_text, old_block, new_block, allow_external=allow_external)
    before = snapshot_file(edit["file_path"])
    edit["file_path"].write_text(edit["updated"], encoding="utf-8")
    record_edit("replace_block", [(edit["file_path"], before)], summary=edit["display_path"])
    return f"Replaced block in: {edit['display_path']}"


def replace_block_content(original, old_block, new_block):
    if not old_block.strip():
        raise RuntimeError("replace_block requires OLD content.")

    newline = detect_newline(original)
    old_block = normalize_edit_block_newlines(old_block, newline)
    new_block = normalize_edit_block_newlines(new_block, newline)

    occurrences = original.count(old_block)
    if occurrences == 0:
        raise RuntimeError("OLD block not found.")
    if occurrences > 1:
        raise RuntimeError("OLD block is not unique.")

    return original.replace(old_block, new_block, 1)


def normalize_edit_block_newlines(block, newline):
    if block == "":
        return ""

    block = block.replace("\r\n", "\n").replace("\r", "\n")
    normalized = block.replace("\n", newline)
    if not normalized.endswith(newline):
        normalized += newline
    return normalized


def apply_patch_from_context(patch):

    if not patch.strip():
        raise RuntimeError("apply_patch requires PATCH content.")
    if not patch.endswith("\n"):
        patch += "\n"


    patch = normalize_patch_paths(patch)
    snapshots = snapshot_patch_targets(patch)

    check = subprocess.run(

        ["git", "apply", "--recount", "--check", "-"],
        input=patch,
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if check.returncode != 0:
        fallback_result = apply_insert_only_patch(patch)
        if fallback_result:
            return fallback_result

        output = check.stderr or check.stdout or "git apply --check failed"
        raise RuntimeError(output.strip())

    applied = subprocess.run(

        ["git", "apply", "--recount", "-"],
        input=patch,
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if applied.returncode != 0:
        output = applied.stderr or applied.stdout or "git apply failed"
        raise RuntimeError(output.strip())

    record_edit("apply_patch", snapshots, summary=", ".join(path.relative_to(Path.cwd().resolve()).as_posix() for path, _before in snapshots))
    return "Patch applied."


def apply_insert_only_patch(patch):

    hunks = parse_insert_only_patch(patch)
    if not hunks:
        return None

    changed_paths = []
    snapshots = snapshot_patch_targets(patch)
    for hunk in hunks:
        file_path = resolve_workspace_write_file(hunk["path"])
        content = file_path.read_text(encoding="utf-8", errors="replace")
        new_content = apply_insert_hunk_to_content(content, hunk)
        if new_content is None:
            return None
        file_path.write_text(new_content, encoding="utf-8")
        changed_paths.append(file_path.relative_to(Path.cwd().resolve()).as_posix())

    changed_display = ", ".join(dict.fromkeys(changed_paths))
    record_edit("apply_patch", snapshots, summary=changed_display)
    return f"Patch applied with insert fallback: {changed_display}"


def snapshot_patch_targets(patch):

    snapshots = []
    seen = set()
    for path_text in patch_target_paths(patch):
        file_path = resolve_workspace_write_file(path_text)
        display_path = file_path.relative_to(Path.cwd().resolve()).as_posix()
        if display_path in seen:
            continue
        seen.add(display_path)
        snapshots.append((file_path, snapshot_file(file_path)))
    return snapshots


def patch_target_paths(patch):

    paths = []
    for line in patch.splitlines():
        if not (line.startswith("--- ") or line.startswith("+++ ")):
            continue
        path_text, _suffix = split_patch_path_suffix(line[4:].rstrip())
        if path_text == "/dev/null":
            continue
        normalized = normalize_patch_file_path(path_text)
        if normalized:
            paths.append(normalized)
    return paths


def parse_insert_only_patch(patch):

    hunks = []
    current_path = None
    current_hunk = None

    for line in patch.splitlines():
        if line.startswith("+++ "):
            path_text, _suffix = split_patch_path_suffix(line[4:].rstrip())
            if path_text != "/dev/null":
                current_path = normalize_patch_file_path(path_text)
            continue

        if line.startswith("@@ "):
            if current_hunk:
                hunks.append(current_hunk)
            if not current_path:
                return []
            current_hunk = {
                "path": current_path,
                "context": [],
                "insertions": [],
            }
            continue

        if current_hunk is None:
            continue

        if line.startswith("-"):
            return []
        if line.startswith("+"):
            current_hunk["insertions"].append(line[1:])
            continue
        if line.startswith(" "):
            current_hunk["context"].append(line[1:])
            continue
        if line == "":
            current_hunk["context"].append("")
            continue
        if line.startswith("\\"):
            continue
        return []

    if current_hunk:
        hunks.append(current_hunk)

    return [hunk for hunk in hunks if hunk["insertions"] and hunk["context"]]


def apply_insert_hunk_to_content(content, hunk):
    lines = content.splitlines(keepends=True)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    anchor_index = find_insert_anchor(plain_lines, hunk["context"])
    if anchor_index is None:
        return None

    newline = detect_newline(content)
    insert_lines = [line + newline for line in hunk["insertions"]]
    updated_lines = lines[: anchor_index + 1] + insert_lines + lines[anchor_index + 1 :]
    return "".join(updated_lines)


def find_insert_anchor(lines, context_lines):

    for context_line in reversed(context_lines):
        if not context_line.strip():
            continue

        matches = [index for index, line in enumerate(lines) if line.strip() == context_line.strip()]
        if len(matches) == 1:
            return matches[0]

    return None


def detect_newline(content):
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def normalize_patch_paths(patch):

    normalized_lines = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            normalized_lines.append(normalize_diff_git_line(line))
        elif line.startswith("--- "):
            normalized_lines.append(normalize_file_header_line(line, "a/"))
        elif line.startswith("+++ "):
            normalized_lines.append(normalize_file_header_line(line, "b/"))
        else:
            normalized_lines.append(line)
    return "\n".join(normalized_lines) + "\n"


def normalize_diff_git_line(line):

    parts = line.split()
    if len(parts) < 4:
        return line

    old_path = resolve_patch_target(parts[2])
    new_path = resolve_patch_target(parts[3])
    if not old_path or not new_path:
        return line

    return f"diff --git a/{old_path} b/{new_path}"


def normalize_file_header_line(line, prefix):

    path_and_suffix = line[4:].rstrip()
    path_text, suffix = split_patch_path_suffix(path_and_suffix)
    if path_text == "/dev/null":
        return line

    target_path = resolve_patch_target(path_text)
    if not target_path:
        return line

    return f"{line[:4]}{prefix}{target_path}{suffix}"


def split_patch_path_suffix(path_and_suffix):

    if "\t" not in path_and_suffix:
        return path_and_suffix.strip(), ""

    path_text, suffix = path_and_suffix.split("\t", 1)
    return path_text.strip(), "\t" + suffix


def resolve_patch_target(path_text):

    normalized_path = normalize_patch_file_path(path_text)
    if not normalized_path:
        return None
    if workspace_path_exists(normalized_path):
        return normalized_path
    return find_unique_workspace_file(normalized_path)


def normalize_patch_file_path(path_text):

    path_text = path_text.strip().strip('"')
    if path_text.startswith("a/") or path_text.startswith("b/"):
        path_text = path_text[2:]
    return path_text.replace("\\", "/")


def workspace_path_exists(path_text):
    try:
        resolve_workspace_file(path_text)
    except RuntimeError:
        return False
    return True


def find_unique_workspace_file(path_text):

    basename = Path(path_text).name
    if not basename:
        return None

    workspace_root = Path.cwd().resolve()
    matches = []
    for candidate in workspace_root.rglob(basename):
        if any(part in PROTECTED_STATE_DIRS for part in candidate.parts) or candidate.name == ".env":
            continue
        if candidate.is_file():
            matches.append(candidate)

    if len(matches) != 1:
        return None

    return matches[0].relative_to(workspace_root).as_posix()


def run_configured_test_for_context():

    config = load_workspace_config()
    test_command = str(config.get("test_command") or "").strip()
    if not test_command:
        return None

    _returncode, context = run_command_for_context(test_command)
    return context


def run_command_for_context(command):

    if not command.strip():
        raise RuntimeError("Usage: /run <command>")


    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        shell=True,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )

    output = completed.stdout
    if completed.stderr:
        output += ("\n" if output else "") + completed.stderr

    if not output:
        output = "(no output)"

    truncated = False
    if len(output) > MAX_COMMAND_OUTPUT_CHARS:
        output = output[:MAX_COMMAND_OUTPUT_CHARS]
        truncated = True

    footer = "--- end"
    if truncated:
        footer += " (truncated)"
    footer += " ---"

    context = (
        f"--- command: {command} ---\n"
        f"exit code: {completed.returncode}\n"
        f"{output}\n"
        f"{footer}"
    )
    return completed.returncode, context
