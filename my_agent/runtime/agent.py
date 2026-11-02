import json
import threading

from integrations.image_context import analyze_images_for_context, expire_image_context, list_images_for_context
from runtime.context import messages_for_model
from memory_engine.state import build_memory_engine_state, format_memory_engine_state
from model.provider import build_chat_payload, call_chat_completion, call_chat_completion_stream, load_model_config, model_label, parse_chat_completion_message
from shared import agent_formatting
from runtime import background_tasks
from runtime import model_runtime
from runtime import structured_loop
from tools.background_commands import list_background_commands, read_background_command_output, start_background_command, stop_background_command
from persistence.observability import summarize_args, summarize_tool_result
from persistence.trajectory import complete_span, complete_trajectory, fail_span, fail_trajectory, record_event, start_span, start_trajectory
from shared.prompt_loader import compose_main_agent_prompt
from bridge.runtime_ui import model_status, render_markdown, render_tool_progress, tool_status
from security.confirmation_policy import confirm_edit_action, confirm_external_path_access, confirm_file_mutating_command, confirm_persistent_action, confirm_run_command, confirm_session_control, confirm_tool_checkpoint, confirm_undo_edit
from tools.tool_output_formatting import diff_stats, format_insert_summary, format_patch_summary, format_replace_summary, format_write_summary, text_diff_stats
from security.security_policy import classify_tool_request, is_file_mutating_command
from persistence.session import (
    append_message_and_persist as _append_message_and_persist,
    get_project_info,
    save_last_session,
)
from agents.temporal_state import mark_assistant_turn_finished
from memory_engine.focus import (
    run_focus_consistency_audit,
    run_focus_range_retirement,
    run_focus_residency_reconciliation,
    run_focus_segment_maintenance,
    run_full_segment_maintenance,
    run_segment_ingest_maintenance,
)
from memory_engine.ltm.formation import run_formation_maintenance
from memory_engine.ltm.formation_backfill import run_formation_backfill
from memory_engine.ltm.memory_retrieval import run_retrieve_memory
from memory_engine.ltm import retrieval_observation
from memory_engine.ltm.memorize import run_memorize
from memory_engine.wm import run_preflight_working_memory_recovery, run_working_memory_maintenance
from memory_engine.wm import commit_staged_wm_ingress_watermark, stage_wm_ingress_from_messages
from runtime.directives import messages_with_runtime_directives, trace_runtime_directives
from tools.tool_runtime import run_tool_request
from tools.tool_registry import TOOL_SCHEMAS
from tools.tool_schemas import tool_call_to_request
from tools import (
    apply_patch_from_context,
    build_insert_after_edit,
    build_replace_block_edit,
    format_display_path,
    get_file_overview_for_context,
    insert_after_from_context,
    list_directory_for_context,
    path_outside_workspace,
    read_file_for_context,
    replace_block_from_context,
    resolve_workspace_write_file,
    run_configured_test_for_context,
    run_command_for_context,
    write_file_from_context,
)
from persistence.undo import apply_undo, format_undo_records, format_undo_result, load_undo_records
from bridge.ui_events import emit_ui_event
from integrations.web_search import format_web_fetch_result, format_web_search_result, tavily_extract, tavily_search
















DEFAULT_MODEL = "deepseek-chat"
MODEL_TIMEOUT_SECONDS = 120
MAX_RETRIES_PER_FAILURE_POINT = 2
MAX_MODEL_RETRIES_PER_FAILURE_POINT = 2
RECOVERABLE_MODEL_ERROR_MARKERS = (
    "API request timed out",
    "request timed out",
    "exceeded model token limit",
    "token limit",
    "Could not reach",
    "Could not complete",
    "API error 429",
    "engine_overloaded_error",
    "rate_limit",
)
CONTEXT_LIMIT_ERROR_MARKERS = (
    "exceeded model token limit",
    "token limit",
)
WRITE_TOOLS = {"write_file", "insert_after", "replace_block", "apply_patch"}
CONFIRMATION_TOOLS = WRITE_TOOLS | {
    "run_command",
    "undo_edit",
    "list_files",
    "read_file",
    "list_images",
    "vision_analyze_images",
}
class ToolRunStopped(RuntimeError):
    pass
_ORIGINAL_SAVE_LAST_SESSION = save_last_session


def append_message_and_persist(messages, message):


    if save_last_session is not _ORIGINAL_SAVE_LAST_SESSION:
        candidate = [*messages, dict(message)]
        save_last_session(candidate)
        messages.append(dict(message))
        return messages[-1]
    return _append_message_and_persist(messages, message)



def create_initial_messages():
    return [
        {
            "role": "system",
            "content": compose_main_agent_prompt(),
        }
    ]


def model_timeout_message():
    return model_runtime.model_timeout_message(globals())


def build_model_tools_payload(messages, runtime_state=None):
    return model_runtime.build_model_tools_payload(messages, runtime_state, dependency_source=globals())


def tool_schemas_for_model(runtime_state=None):
    from runtime.setup_capabilities import ltm_enabled, LTM_TOOLS
    return TOOL_SCHEMAS if ltm_enabled() else [schema for schema in TOOL_SCHEMAS if schema.get("function", {}).get("name") not in LTM_TOOLS]


def call_model_with_tools(messages, runtime_state=None):
    return model_runtime.call_model_with_tools(messages, runtime_state, dependency_source=globals())


def trace_retrieval_plan(runtime_state, retrieval_plan):
    if runtime_state is None or not runtime_state.get("trace_id") or retrieval_plan is None:
        return
    selected_skills = retrieval_plan.get("selected_skill_paths") or []
    if not selected_skills and not retrieval_plan.get("error"):
        return
    trace_event(
        runtime_state,
        "retrieval",
        status="error" if retrieval_plan.get("error") else "ok",
        selected_skill_count=len(selected_skills),
        selected_skill_paths=selected_skills[:10],
        selected_memory_count=len(retrieval_plan.get("selected_memory_paths") or []),
        selected_topic_count=len(retrieval_plan.get("selected_topic_ids") or []),
        source=retrieval_plan.get("source") or "",
        error=retrieval_plan.get("error") or "",
    )


def parse_model_tool_message(data):
    return model_runtime.parse_model_tool_message(data, dependency_source=globals())


def is_recoverable_model_error(error):
    return model_runtime.is_recoverable_model_error(error, dependency_source=globals())


def is_context_limit_model_error(error):
    return model_runtime.is_context_limit_model_error(error, dependency_source=globals())


def recoverable_model_error_message(error):
    return model_runtime.recoverable_model_error_message(error)


def attempt_preflight_working_memory_recovery(error, messages, runtime_state=None):
    if not model_runtime.is_context_build_error(error):
        return {"triggered": False, "updated": False, "reason": "not_context_build_error", "error": None}
    ui_event(runtime_state, "working_memory_preflight_started", path="preflight", error=str(error))
    trace_event(runtime_state, "working_memory_preflight", status="started", path="preflight", error=str(error))
    result = run_preflight_working_memory_recovery(messages, error, runtime_state=runtime_state)
    status = "error" if result.get("error") else "ok"
    ui_event(
        runtime_state,
        "working_memory_preflight_finished",
        path="preflight",
        status=status,
        triggered=result.get("triggered", False),
        updated=result.get("updated", False),
        reason=result.get("reason", ""),
        processed_count=result.get("processed_count", 0),
        summarized_count=result.get("summarized_count", 0),
        kept_raw_count=result.get("kept_raw_count", 0),
        error=result.get("error") or "",
    )
    trace_event(
        runtime_state,
        "working_memory_preflight",
        status=status,
        path="preflight",
        triggered=result.get("triggered", False),
        updated=result.get("updated", False),
        reason=result.get("reason", ""),
        processed_count=result.get("processed_count", 0),
        summarized_count=result.get("summarized_count", 0),
        kept_raw_count=result.get("kept_raw_count", 0),
        error=result.get("error") or "",
    )
    return result


def outbound_context_error_message(messages, runtime_state=None):
    state = runtime_state if isinstance(runtime_state, dict) else None
    previous_phase = state.get("_context_build_phase") if state is not None else None
    if state is not None:
        state["_context_build_phase"] = "preflight"
    try:
        try:
            messages_for_model(messages, runtime_state=runtime_state)
        except RuntimeError as error:
            if not model_runtime.is_context_build_error(error):
                raise
            failure = model_runtime.apply_context_build_failure_pressure(error, runtime_state)
            result = {"reason": "budget_saturation", "triggered": False}
            if failure.get("needs_preflight_recovery"):
                result = attempt_preflight_working_memory_recovery(error, messages, runtime_state)
            try:
                messages_for_model(messages, runtime_state=runtime_state)
            except RuntimeError as retry_error:
                message = model_runtime.context_build_error_message(retry_error)
                trace_event(
                    runtime_state,
                    "context_build",
                    status="error",
                    path="preflight",
                    recovery_reason=result.get("reason", ""),
                    error=str(retry_error),
                )
                ui_event(runtime_state, "model_error", path="preflight", error=str(retry_error))
                return message
            finally:
                model_runtime.clear_context_build_failure_pressure(runtime_state)
        return ""
    finally:
        if state is not None:
            if previous_phase is None:
                state.pop("_context_build_phase", None)
            else:
                state["_context_build_phase"] = previous_phase


def call_model_with_structured_retries(messages, runtime_state=None):
    return model_runtime.call_model_with_structured_retries(messages, runtime_state, dependency_source=globals())


def assistant_history_message(assistant_message, tool_calls=None):
    return model_runtime.assistant_history_message(assistant_message, tool_calls)



def confirm_write(path_text, content, permissions=None):
    return confirm_edit_action(
        "write_file",
        format_write_summary(path_text, content),
        "allow_edits",
        permissions or {},
        content,
    )


def confirm_patch(patch, permissions=None):
    return confirm_edit_action(
        "apply_patch",
        format_patch_summary(patch),
        "allow_edits",
        permissions or {},
        patch,
    )


def confirm_insert(path_text, anchor, content, anchor_context=None, permissions=None, allow_external=False):
    edit = build_insert_after_edit(path_text, anchor, content, anchor_context, allow_external=allow_external)
    return confirm_edit_action(
        "insert_after",
        format_insert_summary(path_text, edit["diff"]),
        "allow_edits",
        permissions or {},
        edit["diff"],
    )


def confirm_replace(path_text, old_block, new_block, permissions=None, allow_external=False):
    edit = build_replace_block_edit(path_text, old_block, new_block, allow_external=allow_external)
    return confirm_edit_action(
        "replace_block",
        format_replace_summary(path_text, edit["diff"]),
        "allow_edits",
        permissions or {},
        edit["diff"],
    )


def record_edit_stats(runtime_state, tool, added, removed, files=None):
    if runtime_state is None:
        return
    runtime_state["_last_edit_stats"] = {
        "tool": tool,
        "added": int(added or 0),
        "removed": int(removed or 0),
        "files": [str(file) for file in (files or []) if str(file)],
    }


def pop_last_edit_stats(runtime_state):
    if runtime_state is None:
        return None
    return runtime_state.pop("_last_edit_stats", None)


def write_file_from_context_with_stats(path_text, content, allow_external=False, runtime_state=None):
    file_path = resolve_workspace_write_file(path_text, allow_external=allow_external)
    before = file_path.read_text(encoding="utf-8", errors="replace") if file_path.exists() else ""
    result = write_file_from_context(path_text, content, allow_external=allow_external)
    added, removed = text_diff_stats(before, content)
    record_edit_stats(runtime_state, "write_file", added, removed, [format_display_path(file_path)])
    return result


def apply_patch_from_context_with_stats(patch, runtime_state=None):
    result = apply_patch_from_context(patch)
    added, removed = diff_stats(patch)
    record_edit_stats(runtime_state, "apply_patch", added, removed, [])
    return result


def insert_after_from_context_with_stats(path_text, anchor, content, anchor_context=None, allow_external=False, runtime_state=None):
    edit = build_insert_after_edit(path_text, anchor, content, anchor_context, allow_external=allow_external)
    result = insert_after_from_context(path_text, anchor, content, anchor_context, allow_external=allow_external)
    added, removed = diff_stats(edit["diff"])
    record_edit_stats(runtime_state, "insert_after", added, removed, [edit["display_path"]])
    return result


def replace_block_from_context_with_stats(path_text, old_block, new_block, allow_external=False, runtime_state=None):
    edit = build_replace_block_edit(path_text, old_block, new_block, allow_external=allow_external)
    result = replace_block_from_context(path_text, old_block, new_block, allow_external=allow_external)
    added, removed = diff_stats(edit["diff"])
    record_edit_stats(runtime_state, "replace_block", added, removed, [edit["display_path"]])
    return result


def blocked_git_undo_message(command):
    return agent_formatting.blocked_git_undo_message(command)


def confirmation_allowed(confirmation, permissions=None):

    if (permissions or {}).get("allow_all"):
        return True, ""
    if isinstance(confirmation, bool):
        return confirmation, ""
    return bool(confirmation.get("allowed")), confirmation.get("reason", "")


def cancellation_message(message, reason):
    return agent_formatting.cancellation_message(message, reason)


def format_memory_state(messages=None, runtime_state=None):
    return format_memory_engine_state(build_memory_engine_state(messages=messages, runtime_state=runtime_state))


def latest_user_content(messages):
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def confirm_external_paths_if_needed(action_name, paths, permissions=None):
    tool_request = {"tool": "vision_analyze_images", "args": {"paths": paths}} if "image files" in action_name else {"tool": "read_file", "args": paths[0] if paths else ""}
    decision = classify_tool_request(tool_request, permissions)
    if decision.risk != "read_external":
        return {"allowed": True, "reason": "", "allow_external": False}
    confirmation = confirm_external_path_access(action_name, decision.details, permissions)
    confirmation["allow_external"] = bool(confirmation.get("allowed"))
    return confirmation



def run_requested_tool(tool_request, messages=None, permissions=None, runtime_state=None, context_snapshot=None):
    return run_tool_request(
        tool_request,
        messages=messages,
        permissions=permissions or {},
        runtime_state=runtime_state,
        context_snapshot=context_snapshot,
        dependency_source=globals(),
    )


def run_get_object(args):
    """Expose the shared bounded read-only object observation to Main Agent."""
    return retrieval_observation.run("get_object", args or {})


def tool_failure_key(tool_request, error):

    error_text = " ".join(str(error).split())
    args_text = json.dumps(tool_request.get("args"), ensure_ascii=False, sort_keys=True, default=str)
    return (tool_request.get("tool"), args_text, error_text)


def is_write_tool_success(tool, tool_result):

    if tool not in WRITE_TOOLS:
        return False

    success_prefixes = (
        "Wrote file:",
        "Inserted content after anchor in:",
        "Replaced block in:",
        "Patch applied.",
        "Patch applied with insert fallback:",
    )
    return tool_result.startswith(success_prefixes)


def structured_retry_limit_message(tool, error):

    return (
        f"Tool error: {error}\n\n"
        "Instruction: this same failure point has already been retried two times. "
        "Do not request another tool for this same action. Explain the blocker to the user "
        "and ask for the missing information if needed."
    )


def should_show_tool_status(tool):

    return tool not in CONFIRMATION_TOOLS



def answer_with_tools(messages, permissions=None, runtime_state=None):
    reset_last_run(runtime_state)
    if runtime_state is not None:
        from runtime.setup_capabilities import ltm_enabled
        runtime_state["_memory_push_enabled"] = ltm_enabled()
        runtime_state["_main_agent_context_timing_enabled"] = True
        check_cancel_requested(runtime_state)
        runtime_state["allow_all_permissions"] = bool((permissions or {}).get("allow_all"))
        if not (runtime_state.get("trajectory_context") or {}).get("enabled"):
            runtime_state["trajectory_context"] = start_trajectory("main_agent")
        runtime_state["trace_id"] = runtime_state["trajectory_context"].get("trajectory_id")
        ui_event(runtime_state, "turn_start", path="structured")
        trace_event(runtime_state, "turn_start", path="structured")
    try:
        answer_with_structured_tools(messages, permissions, runtime_state)
        complete_trajectory((runtime_state or {}).get("trajectory_context"), metadata={"path": "structured"})
    except ToolRunStopped as error:

        fail_trajectory((runtime_state or {}).get("trajectory_context"), error, metadata={"path": "structured", "cancelled": True})
        raise
    except RuntimeError as error:
        ui_event(runtime_state, "agent_error", path="structured", error=str(error))
        trace_event(runtime_state, "agent_error", status="error", path="structured", error=str(error))
        fail_trajectory((runtime_state or {}).get("trajectory_context"), error, metadata={"path": "structured"})
        raise
    finally:
        if isinstance(runtime_state, dict):
            runtime_state.pop("_memory_push_enabled", None)
            runtime_state.pop("_main_agent_context_timing_enabled", None)


def reset_last_run(runtime_state):
    if runtime_state is not None:
        runtime_state["last_run"] = []
        runtime_state.pop("_memory_push_attempted", None)
        runtime_state.pop("_memory_push_result", None)


def record_tool_step(runtime_state, tool, args, result):
    if runtime_state is None:
        return
    runtime_state.setdefault("last_run", []).append(
        {
            "tool": tool,
            "args": args,
            "result": result,
        }
    )


def check_cancel_requested(runtime_state):
    if runtime_state is not None and runtime_state.get("cancel_requested"):
        raise ToolRunStopped("Turn cancelled by user.")


def trace_event(runtime_state, event_type, **fields):

    if runtime_state is None:
        return None
    context = runtime_state.get("trajectory_context")
    if not (context or {}).get("enabled"):
        return None
    try:
        status = fields.pop("status", None)
        artifacts = fields.pop("artifacts", None)
        return record_event(context, event_type, status=status, summary=fields, artifacts=artifacts)
    except Exception:

        return None


def start_trace_span(runtime_state, span_type, name, metadata=None):
    return start_span((runtime_state or {}).get("trajectory_context"), span_type, name, metadata=metadata)


def trace_span_event(context, event_type, **fields):
    status = fields.pop("status", None)
    artifacts = fields.pop("artifacts", None)
    return record_event(context, event_type, status=status, summary=fields, artifacts=artifacts)


def finish_trace_span(context, error=None, metadata=None):
    if error is None:
        return complete_span(context, metadata=metadata)
    return fail_span(context, error, metadata=metadata)


def ui_event(runtime_state, event_type, **fields):
    return emit_ui_event(runtime_state, event_type, **fields)


def schedule_working_memory_maintenance(messages, runtime_state=None):
    return background_tasks.schedule_working_memory_maintenance(messages, runtime_state, dependency_source=globals())


def schedule_focus_segment_maintenance(messages, runtime_state=None):
    return background_tasks.schedule_focus_segment_maintenance(messages, runtime_state, dependency_source=globals())


def schedule_event_stream_memory_maintenance(messages, runtime_state=None):
    return background_tasks.schedule_event_stream_memory_maintenance(messages, runtime_state, dependency_source=globals())


def schedule_final_answer_background_tasks(messages, runtime_state=None):
    return background_tasks.schedule_final_answer_background_tasks(messages, runtime_state, dependency_source=globals())


def schedule_formation_backfill(messages, runtime_state=None):
    from runtime.setup_capabilities import ltm_enabled, register_thread
    if not ltm_enabled():
        return None
    if (runtime_state or {}).get("historical_backfill_thread") is not None:
        thread = runtime_state.get("historical_backfill_thread")
        if thread.is_alive():
            return thread

    def worker():
        emit_status = (runtime_state or {}).get("ui_event_consumer") != "eval"
        if emit_status:
            ui_event(runtime_state, "formation_backfill_started", path="background", mode="backfill")
        try:
            result = run_formation_backfill(messages, runtime_state=runtime_state)
            if emit_status:
                ui_event(
                    runtime_state,
                    "formation_backfill_finished",
                    path="background",
                    mode="backfill",
                    status="error" if result.get("error") else "ok",
                    error=result.get("error") or "",
                )
        except Exception as error:
            if emit_status:
                ui_event(runtime_state, "formation_backfill_finished", path="background", mode="backfill", status="error", error=str(error))
            raise
        finally:
            if isinstance(runtime_state, dict):
                runtime_state.pop("historical_backfill_thread", None)

    thread = threading.Thread(target=worker, daemon=True)
    if isinstance(runtime_state, dict):
        runtime_state["historical_backfill_thread"] = thread
    register_thread(thread)
    thread.start()
    return thread


def answer_with_structured_tools(messages, permissions=None, runtime_state=None):
    return structured_loop.run_structured_loop(messages, permissions, runtime_state, dependency_source=globals())


def build_structured_tool_result_message(tool, tool_result):
    return agent_formatting.build_structured_tool_result_message(
        tool_result,
        is_write_tool_success(tool, tool_result),
    )


def format_tool_error(tool_request, error):
    return agent_formatting.format_tool_error(tool_request, error)
