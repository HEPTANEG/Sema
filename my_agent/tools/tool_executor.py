from dataclasses import dataclass

from tools.tool_registry import registered_tool_names
from memory_engine import context_builder


@dataclass
class ToolExecutionHooks:
    functions: dict

    def __getattr__(self, name):
        try:
            return self.functions[name]
        except KeyError as error:
            raise AttributeError(name) from error


@dataclass
class ToolExecutionContext:
    messages: list | None
    permissions: dict
    runtime_state: dict | None
    risk_decision: object
    confirmation_permissions: dict
    hooks: ToolExecutionHooks
    context_snapshot: object | None = None


def execute_registered_tool(tool_request, context):
    tool = tool_request.get("tool")
    from runtime.setup_capabilities import LTM_TOOLS, require_ltm
    if tool in LTM_TOOLS:
        require_ltm()
    handler = TOOL_EXECUTORS.get(tool)
    if handler is None:
        raise RuntimeError(f"Unknown tool requested: {tool}")
    return handler(tool_request, context)


def _confirm_tool_risk(context):
    return context.hooks.confirm_tool_risk(
        context.risk_decision,
        context.confirmation_permissions,
    )


def _post_edit_result(edit_result, context, label):
    test_result = context.hooks.run_configured_test_for_context()
    if not test_result:
        return edit_result
    return f"{edit_result}\n\nPost-{label} test result:\n\n{test_result}"


def _trace_tool_metadata(metadata):
    """Remove fields reserved by the outer tool trace envelope."""
    return {
        key: value
        for key, value in (metadata or {}).items()
        if key not in {"status", "path"}
    }


def _run_list_files(tool_request, context):
    args = tool_request["args"]
    confirmation = _confirm_tool_risk(context)
    if not confirmation["allowed"]:
        return f"External directory listing cancelled.\nReason: {confirmation['reason']}"
    return context.hooks.list_directory_for_context(args, allow_external=confirmation["allow_external"])


def _run_read_file(tool_request, context):
    args = tool_request["args"]
    confirmation = _confirm_tool_risk(context)
    if not confirmation["allowed"]:
        return f"External file read cancelled: {args}\nReason: {confirmation['reason']}"

    _display_path, file_context = context.hooks.read_file_for_context(args, allow_external=confirmation["allow_external"])
    return file_context


def _run_get_overview(tool_request, context):
    args = tool_request["args"]
    confirmation = _confirm_tool_risk(context)
    if not confirmation["allowed"]:
        return f"External file overview cancelled: {args}\nReason: {confirmation['reason']}"
    return context.hooks.get_file_overview_for_context(args, allow_external=confirmation["allow_external"])


def _run_list_images(tool_request, context):
    args = tool_request["args"]
    confirmation = _confirm_tool_risk(context)
    if not confirmation["allowed"]:
        return f"External image directory access cancelled.\nReason: {confirmation['reason']}"
    return context.hooks.list_images_for_context(
        args.get("path"),
        recursive=bool(args.get("recursive")),
        allow_external=confirmation["allow_external"],
    )


def _run_vision_analyze_images(tool_request, context):
    tool = tool_request["tool"]
    args = tool_request["args"]
    confirmation = _confirm_tool_risk(context)
    if not confirmation["allowed"]:
        return f"External image access cancelled.\nReason: {confirmation['reason']}"
    with context.hooks.tool_status(tool, args):
        return context.hooks.analyze_images_for_context(
            args.get("paths"),
            args.get("instruction"),
            allow_external=confirmation["allow_external"],
        )


def _run_run_command(tool_request, context):
    args = tool_request["args"]
    if context.risk_decision.risk == "dangerous_command":
        confirmation = context.hooks.confirm_file_mutating_command(args, context.confirmation_permissions)
    else:
        confirmation = context.hooks.confirm_run_command(args, context.confirmation_permissions)
    if not confirmation["allowed"]:
        return f"Command cancelled: {args}\nReason: {confirmation['reason']}"

    returncode, command_context = context.hooks.run_command_for_context(args)
    if returncode:
        raise RuntimeError(f"Command exited with code {returncode}.\n{command_context}")
    return command_context


def _run_start_background_command(tool_request, context):
    args = tool_request["args"]
    command = args.get("command") if isinstance(args, dict) else str(args or "")
    if context.risk_decision.risk == "dangerous_command":
        confirmation = context.hooks.confirm_file_mutating_command(command, context.confirmation_permissions)
    else:
        confirmation = context.hooks.confirm_run_command(command, context.confirmation_permissions)
    if not confirmation["allowed"]:
        return f"Background command cancelled: {command}\nReason: {confirmation['reason']}"

    return context.hooks.start_background_command(command, cwd=args.get("cwd") if isinstance(args, dict) else None)


def _run_list_background_commands(_tool_request, context):
    return context.hooks.list_background_commands()


def _run_read_background_command_output(tool_request, context):
    args = tool_request["args"]
    return context.hooks.read_background_command_output(args.get("command_id"), max_chars=args.get("max_chars"))


def _run_stop_background_command(tool_request, context):
    return context.hooks.stop_background_command(tool_request["args"])


def _run_web_search(tool_request, context):
    args = tool_request["args"]
    result = context.hooks.tavily_search(args.get("query"), args.get("max_results"))
    return context.hooks.format_web_search_result(result)


def _run_web_fetch(tool_request, context):
    args = tool_request["args"]
    result = context.hooks.tavily_extract(args)
    return context.hooks.format_web_fetch_result(result)


def _run_show_undo_records(tool_request, context):
    args = tool_request["args"]
    limit = args if isinstance(args, int) and args > 0 else 20
    return context.hooks.format_undo_records(context.hooks.load_undo_records(limit=limit))


def _run_undo_edit(tool_request, context):
    args = tool_request["args"]
    record_id = args.strip() if isinstance(args, str) else ""
    details = f"record_id: {record_id or '(latest edit)'}"
    confirmation = context.hooks.confirm_undo_edit(details, context.confirmation_permissions)
    if not confirmation["allowed"]:
        return f"Undo cancelled: {record_id or 'latest edit'}\nReason: {confirmation['reason']}"
    return context.hooks.format_undo_result(context.hooks.apply_undo(record_id or None))


def _run_show_memory_state(_tool_request, context):
    return context.hooks.format_memory_state(messages=context.messages, runtime_state=context.runtime_state)


def _run_get_object(tool_request, context):
    result = context.hooks.run_get_object(tool_request.get("args") or {})
    if not isinstance(result, dict):
        raise RuntimeError("get_object returned an invalid result.")
    return context.hooks.json_dumps(result)


def _run_write_file(tool_request, context):
    args = tool_request["args"]
    content = tool_request.get("content")
    if content is None or not tool_request.get("content_closed"):
        raise RuntimeError("write_file requires CONTENT followed by END_CONTENT.")
    external_confirmation = _confirm_tool_risk(context)
    if not external_confirmation["allowed"]:
        return f"External file write cancelled: {args}\nReason: {external_confirmation['reason']}"
    confirmation = context.hooks.confirm_write(args, content, context.confirmation_permissions)
    if not confirmation["allowed"]:
        return f"Write cancelled: {args}\nReason: {confirmation['reason']}"

    write_result = context.hooks.write_file_from_context(args, content, allow_external=external_confirmation["allow_external"])
    return _post_edit_result(write_result, context, "write")


def _run_insert_after(tool_request, context):
    args = tool_request["args"]
    anchor = tool_request.get("anchor")
    content = tool_request.get("content")
    anchor_context = tool_request.get("anchor_context")
    if anchor is None or not tool_request.get("anchor_closed"):
        raise RuntimeError("insert_after requires ANCHOR followed by END_ANCHOR.")
    if content is None or not tool_request.get("content_closed"):
        raise RuntimeError("insert_after requires CONTENT followed by END_CONTENT.")
    external_confirmation = _confirm_tool_risk(context)
    if not external_confirmation["allowed"]:
        return f"External file edit cancelled: {args}\nReason: {external_confirmation['reason']}"
    confirmation = context.hooks.confirm_insert(
        args,
        anchor,
        content,
        anchor_context,
        context.confirmation_permissions,
        allow_external=external_confirmation["allow_external"],
    )
    if not confirmation["allowed"]:
        return f"Insert cancelled: {args}\nReason: {confirmation['reason']}"

    insert_result = context.hooks.insert_after_from_context(
        args,
        anchor,
        content,
        anchor_context,
        allow_external=external_confirmation["allow_external"],
    )
    return _post_edit_result(insert_result, context, "insert")


def _run_retrieve_memory(tool_request, context):
    try:
        result = context.hooks.run_retrieve_memory(
            tool_request.get("args") or {},
            context_snapshot=context.context_snapshot,
            runtime_state=context.runtime_state,
            check_cancel=lambda: context.hooks.check_cancel_requested(context.runtime_state),
        )
    except Exception:
        metadata = _trace_tool_metadata(
            ((context.runtime_state or {}).get("memory_engine_retrieval_observer") or {}).get("last_result") or {}
        )
        context.hooks.trace_event(context.runtime_state, "ltm_retrieval", status="error", path="tool", **metadata)
        raise
    metadata = _trace_tool_metadata(
        ((context.runtime_state or {}).get("memory_engine_retrieval_observer") or {}).get("last_result") or {}
    )
    context.hooks.trace_event(
        context.runtime_state,
        "ltm_retrieval",
        status="ok",
        path="tool",
        **metadata,
    )


    if isinstance(result, dict) and result.get("kind") == "checkpoint" and result.get("evidence"):
        session = (context.runtime_state or {}).get("memory_retrieval_session")
        if isinstance(session, dict) and session.get("continuation_id") == result.get("continuation_id"):
            refreshed = session.setdefault("push_refresh_object_ids", set())
            fresh = {item["object_id"] for item in result["evidence"]} - refreshed
            if fresh:
                context_builder.schedule_memory_push_refresh(context.messages, runtime_state=context.runtime_state)
                refreshed.update(fresh)
        else:
            context_builder.schedule_memory_push_refresh(context.messages, runtime_state=context.runtime_state)
    return context.hooks.json_dumps(result)


def _run_memorize(tool_request, context):
    try:
        result = context.hooks.run_memorize(
            (tool_request.get("args") or {}).get("instruction")
            if isinstance(tool_request.get("args"), dict)
            else "",
            messages=context.messages,
            context_snapshot=context.context_snapshot,
            runtime_state=context.runtime_state,
            check_cancel=lambda: context.hooks.check_cancel_requested(context.runtime_state),
        )
    except Exception:
        metadata = _trace_tool_metadata(
            ((context.runtime_state or {}).get("memory_engine_memorize_observer") or {}).get("last_result") or {}
        )
        context.hooks.trace_event(context.runtime_state, "ltm_memorize", status="error", path="tool", **metadata)
        raise
    metadata = _trace_tool_metadata(
        ((context.runtime_state or {}).get("memory_engine_memorize_observer") or {}).get("last_result") or {}
    )
    context.hooks.trace_event(
        context.runtime_state,
        "ltm_memorize",
        status="ok",
        path="tool",
        **metadata,
    )
    return context.hooks.json_dumps(result)


def _run_replace_block(tool_request, context):
    args = tool_request["args"]
    old_block = tool_request.get("old_block")
    new_block = tool_request.get("new_block")
    if old_block is None or not tool_request.get("old_block_closed"):
        raise RuntimeError("replace_block requires OLD followed by END_OLD.")
    if new_block is None or not tool_request.get("new_block_closed"):
        raise RuntimeError("replace_block requires NEW followed by END_NEW.")
    external_confirmation = _confirm_tool_risk(context)
    if not external_confirmation["allowed"]:
        return f"External file edit cancelled: {args}\nReason: {external_confirmation['reason']}"
    confirmation = context.hooks.confirm_replace(
        args,
        old_block,
        new_block,
        context.confirmation_permissions,
        allow_external=external_confirmation["allow_external"],
    )
    if not confirmation["allowed"]:
        return f"Replace cancelled: {args}\nReason: {confirmation['reason']}"

    replace_result = context.hooks.replace_block_from_context(
        args,
        old_block,
        new_block,
        allow_external=external_confirmation["allow_external"],
    )
    return _post_edit_result(replace_result, context, "replace")


def _run_apply_patch(tool_request, context):
    patch = tool_request.get("patch")
    if patch is None or not tool_request.get("patch_closed"):
        raise RuntimeError("apply_patch requires PATCH followed by END_PATCH.")
    confirmation = context.hooks.confirm_patch(patch, context.confirmation_permissions)
    if not confirmation["allowed"]:
        return f"Patch cancelled.\nReason: {confirmation['reason']}"

    patch_result = context.hooks.apply_patch_from_context(patch)
    return _post_edit_result(patch_result, context, "patch")


TOOL_EXECUTORS = {
    "read_file": _run_read_file,
    "get_overview": _run_get_overview,
    "list_files": _run_list_files,
    "list_images": _run_list_images,
    "vision_analyze_images": _run_vision_analyze_images,
    "run_command": _run_run_command,
    "start_background_command": _run_start_background_command,
    "list_background_commands": _run_list_background_commands,
    "read_background_command_output": _run_read_background_command_output,
    "stop_background_command": _run_stop_background_command,
    "write_file": _run_write_file,
    "insert_after": _run_insert_after,
    "replace_block": _run_replace_block,
    "web_search": _run_web_search,
    "web_fetch": _run_web_fetch,
    "show_undo_records": _run_show_undo_records,
    "undo_edit": _run_undo_edit,
    "show_memory_state": _run_show_memory_state,
    "get_object": _run_get_object,
    "retrieve_memory": _run_retrieve_memory,
    "memorize": _run_memorize,

    "apply_patch": _run_apply_patch,
}


def missing_registered_executors():
    return sorted(registered_tool_names() - set(TOOL_EXECUTORS))
