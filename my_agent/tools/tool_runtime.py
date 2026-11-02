from security.security_policy import classify_tool_request, is_blocked_model_git_undo_command
from tools.tool_executor import ToolExecutionContext, ToolExecutionHooks, execute_registered_tool
from runtime.dependency_validation import validate_dependencies


TOOL_EXECUTION_REQUIRED_DEPENDENCIES = (
    "ToolRunStopped",
    "analyze_images_for_context",
    "apply_undo",
    "cancellation_message",
    "check_cancel_requested",
    "confirm_file_mutating_command",
    "confirm_insert",
    "confirm_patch",
    "confirm_replace",
    "confirm_run_command",
    "confirm_undo_edit",
    "confirm_write",
    "confirmation_allowed",
    "create_initial_messages",
    "format_memory_state",
    "format_undo_records",
    "format_undo_result",
    "format_web_fetch_result",
    "format_web_search_result",
    "get_file_overview_for_context",
    "run_get_object",
    "latest_user_content",
    "list_background_commands",
    "list_directory_for_context",
    "list_images_for_context",
    "load_undo_records",
    "read_background_command_output",
    "read_file_for_context",
    "run_command_for_context",
    "run_configured_test_for_context",
    "run_memorize",
    "run_retrieve_memory",
    "start_background_command",
    "stop_background_command",
    "tavily_extract",
    "tavily_search",
    "trace_event",
    "tool_status",
    "ui_event",
)
TOOL_REQUEST_REQUIRED_DEPENDENCIES = (
    "blocked_git_undo_message",
)


def confirm_tool_risk(decision, permissions=None, dependencies=None):

    dependencies = dependencies or {}
    if not decision.needs_confirmation:
        return {
            "allowed": True,
            "reason": "",
            "allow_external": decision.allow_external,
        }
    if decision.risk in {"read_external", "edit_external"}:
        confirmation = dependencies["confirm_external_path_access"](decision.confirmation_action, decision.details, permissions)
        confirmation["allow_external"] = bool(confirmation.get("allowed"))
        return confirmation
    return {
        "allowed": True,
        "reason": "",
        "allow_external": decision.allow_external,
    }


def _call_write_file_hook(dependency_source, runtime_state, path_text, content, allow_external=False):
    hook = dependency_source.get("write_file_from_context_with_stats")
    if hook:
        return hook(path_text, content, allow_external=allow_external, runtime_state=runtime_state)
    return dependency_source["write_file_from_context"](path_text, content, allow_external=allow_external)


def _call_apply_patch_hook(dependency_source, runtime_state, patch):
    hook = dependency_source.get("apply_patch_from_context_with_stats")
    if hook:
        return hook(patch, runtime_state=runtime_state)
    return dependency_source["apply_patch_from_context"](patch)


def _call_insert_after_hook(dependency_source, runtime_state, path_text, anchor, content, anchor_context=None, allow_external=False):
    hook = dependency_source.get("insert_after_from_context_with_stats")
    if hook:
        return hook(path_text, anchor, content, anchor_context, allow_external=allow_external, runtime_state=runtime_state)
    return dependency_source["insert_after_from_context"](path_text, anchor, content, anchor_context, allow_external=allow_external)


def _call_replace_block_hook(dependency_source, runtime_state, path_text, old_block, new_block, allow_external=False):
    hook = dependency_source.get("replace_block_from_context_with_stats")
    if hook:
        return hook(path_text, old_block, new_block, allow_external=allow_external, runtime_state=runtime_state)
    return dependency_source["replace_block_from_context"](path_text, old_block, new_block, allow_external=allow_external)


def build_tool_execution_hooks(dependency_source, runtime_state=None):

    dependency_source = validate_dependencies(
        dependency_source,
        TOOL_EXECUTION_REQUIRED_DEPENDENCIES,
        "tool_runtime.build_tool_execution_hooks",
    )
    return ToolExecutionHooks(
        {
            "analyze_images_for_context": dependency_source["analyze_images_for_context"],
            "apply_patch_from_context": lambda patch: _call_apply_patch_hook(dependency_source, runtime_state, patch),
            "apply_undo": dependency_source["apply_undo"],
            "cancellation_message": dependency_source["cancellation_message"],
            "confirm_file_mutating_command": dependency_source["confirm_file_mutating_command"],
            "confirm_insert": dependency_source["confirm_insert"],
            "confirm_patch": dependency_source["confirm_patch"],
            "confirm_replace": dependency_source["confirm_replace"],
            "confirm_run_command": dependency_source["confirm_run_command"],
            "confirm_tool_risk": lambda decision, permissions=None: confirm_tool_risk(
                decision,
                permissions,
                dependencies=dependency_source,
            ),
            "confirm_undo_edit": dependency_source["confirm_undo_edit"],
            "confirm_write": dependency_source["confirm_write"],
            "confirmation_allowed": dependency_source["confirmation_allowed"],
            "create_initial_messages": dependency_source["create_initial_messages"],
            "format_memory_state": lambda messages=None, runtime_state=None: dependency_source["format_memory_state"](
                messages=messages,
                runtime_state=runtime_state,
            ),
            "json_dumps": lambda value: __import__("json").dumps(value, ensure_ascii=False, sort_keys=True),
            "format_undo_records": dependency_source["format_undo_records"],
            "format_undo_result": dependency_source["format_undo_result"],
            "format_web_fetch_result": dependency_source["format_web_fetch_result"],
            "format_web_search_result": dependency_source["format_web_search_result"],
            "get_file_overview_for_context": dependency_source["get_file_overview_for_context"],
            "insert_after_from_context": lambda path_text, anchor, content, anchor_context=None, allow_external=False: _call_insert_after_hook(
                dependency_source,
                runtime_state,
                path_text,
                anchor,
                content,
                anchor_context,
                allow_external=allow_external,
            ),
            "latest_user_content": dependency_source["latest_user_content"],
            "list_directory_for_context": dependency_source["list_directory_for_context"],
            "list_images_for_context": dependency_source["list_images_for_context"],
            "load_undo_records": dependency_source["load_undo_records"],
            "read_file_for_context": dependency_source["read_file_for_context"],
            "replace_block_from_context": lambda path_text, old_block, new_block, allow_external=False: _call_replace_block_hook(
                dependency_source,
                runtime_state,
                path_text,
                old_block,
                new_block,
                allow_external=allow_external,
            ),
            "run_command_for_context": dependency_source["run_command_for_context"],
            "run_configured_test_for_context": dependency_source["run_configured_test_for_context"],
            "run_get_object": dependency_source["run_get_object"],
            "start_background_command": dependency_source["start_background_command"],
            "list_background_commands": dependency_source["list_background_commands"],
            "read_background_command_output": dependency_source["read_background_command_output"],
            "stop_background_command": dependency_source["stop_background_command"],
            "tavily_extract": dependency_source["tavily_extract"],
            "tavily_search": dependency_source["tavily_search"],
            "tool_run_stopped": dependency_source["ToolRunStopped"],
            "check_cancel_requested": dependency_source["check_cancel_requested"],
            "run_retrieve_memory": dependency_source["run_retrieve_memory"],
            "run_memorize": dependency_source["run_memorize"],
            "trace_event": dependency_source["trace_event"],
            "tool_status": dependency_source["tool_status"],
            "ui_event": dependency_source["ui_event"],
            "write_file_from_context": lambda path_text, content, allow_external=False: _call_write_file_hook(
                dependency_source,
                runtime_state,
                path_text,
                content,
                allow_external=allow_external,
            ),
        }
    )


def run_tool_request(
    tool_request,
    messages=None,
    permissions=None,
    runtime_state=None,
    context_snapshot=None,
    dependency_source=None,
):
    dependency_source = validate_dependencies(
        dependency_source,
        TOOL_REQUEST_REQUIRED_DEPENDENCIES,
        "tool_runtime.run_tool_request",
    )
    tool = tool_request["tool"]
    args = tool_request["args"]
    risk_decision = dependency_source.get("classify_tool_request", classify_tool_request)(tool_request, permissions or {})
    if risk_decision.blocked:
        if tool == "run_command" and is_blocked_model_git_undo_command(args):
            return dependency_source["blocked_git_undo_message"](args)
        raise RuntimeError(risk_decision.reason)
    confirmation_permissions = permissions or {}
    context = ToolExecutionContext(
        messages=messages,
        permissions=permissions or {},
        runtime_state=runtime_state,
        risk_decision=risk_decision,
        confirmation_permissions=confirmation_permissions,
        hooks=build_tool_execution_hooks(dependency_source, runtime_state=runtime_state),
        context_snapshot=context_snapshot,
    )
    return execute_registered_tool(tool_request, context)
