import json
import os
from pathlib import Path

from commands.command_registry import format_help, list_slash_commands
from commands.dispatch import dispatch_command
from memory_engine.context_builder import context_status, format_context_status
from memory_engine.capacity_planner import preview_stm_capacity
from memory_engine.state import build_memory_engine_state, format_memory_engine_state
from memory_engine.dynamic_context_budget import compute_and_store_dynamic_context_budget, format_context_pressure_status
from memory_engine.context_budget_override import reset_budget_override, set_budget_override
from memory_engine.wm import effective_wm_ingress_watermark, wm_ingress_watermark_status
import memory_engine.event_stream as event_stream
from memory_engine.focus import focus_segments_status, format_focus_segments_status, run_full_segment_maintenance
from memory_engine.ltm.maintenance_runner import format_ltm_maintenance_result, run_ltm_maintenance
from memory_engine.ltm.ltm_visualization import (
    format_ltm_visualization_result,
    generate_ltm_visualization,
    open_ltm_visualization_html,
)
from integrations.image_context import build_image_context_message, parse_image_paths, validate_image_paths
from model.provider import PROVIDER_PROFILES, global_config_path, load_model_config, load_notification_config, model_label, save_global_model_config, save_global_notification_config
from persistence.trajectory import (
    artifact_metadata,
    format_trajectory_errors,
    format_trajectory_list,
    format_trajectory_store_status,
    format_trajectory_tree,
    get_trajectory,
    latest_trajectory,
    list_trajectories,
    trajectory_store_status,
    trajectory_artifact_dir,
    trajectory_db_path,
)
from persistence.trajectory_contexts import context_dir, list_requests, get_request, format_request
from persistence.session import (
    append_message_and_persist,
    get_project_info,
    project_unattended_enabled,
    save_last_session,
    save_project_unattended,
)
from memory_engine.stm_projection import STM_TOKEN_BUDGET, stm_projection_status
from agents.temporal_state import format_temporal_state
from tools import load_workspace_config, read_file_for_context
from persistence.undo import apply_undo, format_undo_records, format_undo_result, load_undo_records
from memory_engine.wm import format_working_memory_run, format_working_memory_status, run_working_memory_maintenance, working_memory_status


def reset_permissions(permissions):
    permissions.clear()
    permissions.update(
        {
            "allow_run_commands": False,
            "allow_edits": False,
            "allow_all": False,
            "unattended": False,
            "unattended_scope": "interactive",
        }
    )


def enable_unattended(permissions, scope="session"):
    permissions["unattended"] = True
    permissions["unattended_scope"] = scope
    permissions["allow_all"] = True
    permissions["allow_run_commands"] = True
    permissions["allow_edits"] = True


def disable_unattended(permissions):
    permissions["unattended"] = False
    permissions["unattended_scope"] = "interactive"
    permissions["allow_all"] = False
    permissions["allow_run_commands"] = False
    permissions["allow_edits"] = False


def initialize_permissions(permissions):
    """Reset process grants and restore the current project's preference."""
    reset_permissions(permissions)
    if project_unattended_enabled():
        enable_unattended(permissions, scope="project")


def unattended_scope(permissions):
    enabled = bool(permissions.get("unattended") or permissions.get("allow_all"))
    if not enabled:
        return "interactive"
    scope = str(permissions.get("unattended_scope") or "session")
    return scope if scope in {"session", "project"} else "session"


def execute_slash_command(prompt, messages, permissions, runtime_state, create_initial_messages):
    prompt = str(prompt or "").strip()
    result = _base_result(prompt, permissions)

    if prompt == "/help":
        return _with_content(result, "Help", format_help(developer_mode=bool(runtime_state.get("developer_mode"))), view="commandOutput")

    if prompt == "/pwd":
        return _with_content(result, "Working directory", str(Path.cwd()))

    if prompt == "/ls":
        return _with_content(result, "Files", format_directory_listing())

    if prompt.startswith("/read"):
        path_text = prompt.removeprefix("/read").strip()
        display_path, context = read_file_for_context(path_text)
        append_message_and_persist(
            messages,
            {"role": "user", "content": f"Local file context from {display_path}:\n\n{context}"},
        )
        save_last_session(messages)
        result.update(
            {
                "title": f"Read {display_path}",
                "content": context,
                "messages": messages,
                "view": "commandOutput",
                "transcript_items": [{"role": "system", "content": f"Added file context: {display_path}"}],
            }
        )
        return result

    if prompt.startswith("/image"):
        records = validate_image_paths(parse_image_paths(prompt.removeprefix("/image").strip()))
        append_message_and_persist(messages, build_image_context_message(records))
        save_last_session(messages)
        content = "Added image context:\n" + "\n".join(f"- {record['filename']}" for record in records)
        result.update(
            {
                "title": "Images added",
                "content": content,
                "messages": messages,
                "view": "commandOutput",
                "transcript_items": [{"role": "system", "content": f"Added {len(records)} image(s) to context."}],
            }
        )
        return result

    dispatched = dispatch_command(
        prompt,
        result,
        messages,
        permissions,
        runtime_state,
        dependencies={
            "_with_content": _with_content,
            "format_last_run": format_last_run,
            "format_status": format_status,
            "handle_settings_command": handle_settings_command,
            "format_temporal_state": format_temporal_state,
            "handle_config_command": handle_config_command,
            "handle_memory_command": handle_memory_command,
            "handle_notify_command": handle_notify_command,
            "handle_permissions_command": handle_permissions_command,
            "handle_trajectory_command": handle_trajectory_command,
            "handle_undo_command": handle_undo_command,
            "handle_unattended_command": handle_unattended_command,
            "handle_verbose_command": handle_verbose_command,
        },
    )
    if dispatched is not None:
        return dispatched

    result["handled"] = False
    return result


def _base_result(prompt, permissions):
    return {
        "handled": True,
        "should_exit": False,
        "view": "commandOutput",
        "title": prompt,
        "content": "",
        "project": get_project_info(),
        "permissions": dict(permissions),
        "config": None,
        "session": {},
        "transcript_items": [],
    }


def handle_settings_command(result, permissions, messages=None, runtime_state=None):
    """Return the structured settings snapshot consumed by the TUI."""
    from runtime.setup_config import public_model
    model = public_model()
    notifications = load_notification_config()
    runtime_state = runtime_state or {}
    current_unattended_scope = unattended_scope(permissions)
    unattended_enabled = current_unattended_scope != "interactive"

    def grant_status(enabled):
        if not enabled:
            return "Confirmation required"
        source = "project unattended" if current_unattended_scope == "project" else "session grant"
        return f"Allowed without confirmation; source: {source}"

    sections = [
        {
            "id": "ltm-storage", "label": "LTM Storage",
            "description": "Capacity warning and permanent cleanup of logically deleted memories.",
            "actions": [{"id": "ltm-storage-open", "label": "Manage storage", "kind": "action", "action": "ltm.storage"}],
        },
        {
            "id": "model",
            "label": "Model",
            "description": "View or edit the active provider configuration.",
            "actions": [
                {"id": "model-status", "label": "Status", "kind": "status", "value": model},
                {"id": "open-model", "label": "Edit model configuration", "kind": "config"},
            ],
        },
        {
            "id": "permissions",
            "label": "Permissions",
            "description": "Review permission state and execution mode.",
            "actions": [
                {"id": "grants-heading", "label": "Permission state", "kind": "section"},
                {"id": "allow-run", "label": "Commands", "kind": "info", "value": grant_status(bool(permissions.get("allow_run_commands")))},
                {"id": "allow-edits", "label": "File edits", "kind": "info", "value": grant_status(bool(permissions.get("allow_edits")))},
                {"id": "allow-all", "label": "All operations", "kind": "info", "value": grant_status(bool(permissions.get("allow_all"))) if permissions.get("allow_all") else "No broad grant; confirmation required"},
                {"id": "execution-mode", "label": "Execution mode", "kind": "choice", "value": current_unattended_scope, "description": "Choose whether protected operations require confirmation.", "options": [
                    {"value": "interactive", "label": "Interactive", "action": "unattended.interactive", "description": "Ask before protected operations."},
                    {"value": "session", "label": "Unattended for this session", "action": "unattended.session", "description": "Skip confirmations until this Sema session ends.", "warning": "Sema will skip protected-operation confirmations until this session ends.", "confirm_label": "Enable for this session"},
                    {"value": "project", "label": "Unattended for this project", "action": "unattended.project", "description": "Restore unattended mode whenever this project opens.", "warning": "Sema will skip protected-operation confirmations whenever this project is opened. This persists across sessions.", "confirm_label": "Enable for this project"},
                ]},
                {"id": "actions-heading", "label": "Actions", "kind": "section"},
                {"id": "permissions-reset", "label": "Reset temporary grants", "kind": "action", "action": "permissions.reset", "warning": "This removes temporary permissions granted during this session. Project-level unattended mode, if enabled, remains active.", "confirm_label": "Reset temporary grants"},
            ],
        },
        {
            "id": "interface",
            "label": "Interface",
            "description": "User-facing notification preferences.",
            "actions": [
                {"id": "notifications", "label": "Notifications", "kind": "toggle", "value": bool(notifications.get("enabled", True)), "action_on": "notifications.enable", "action_off": "notifications.disable", "description": "Notify when the TUI is not focused."},
            ],
        },
        {
            "id": "developer",
            "label": "Developer",
            "description": "Developer-only command visibility for this process.",
            "actions": [
                {"id": "developer-mode", "label": "Developer mode", "kind": "toggle", "value": bool(runtime_state.get("developer_mode", False)), "action_on": "developer.enable", "action_off": "developer.disable", "description": "Show Developer commands in help and autocomplete."},
            ],
        },
    ]
    return _with_content(
        result,
        "Settings",
        "Use the grouped settings panel to manage Sema configuration.",
        view="settings",
    ) | {
        "settings": {
            "categories": sections + [
                {"id": "search", "label": "Search", "description": "Tavily configuration", "actions": []},
                {"id": "remote", "label": "Remote", "description": "Telegram configuration", "actions": []},
                {"id": "overview", "label": "Preparation", "description": "Required dependency checks", "actions": []},
            ],
            "model": model,
            "permissions": dict(permissions),
            "unattended": {"enabled": unattended_enabled, "scope": current_unattended_scope},
            "notifications": notifications,
            "developer": {"enabled": bool(runtime_state.get("developer_mode", False)), "persistent": False, "display_only": True},
        }
    }


SETTINGS_ACTIONS = {
    "notifications.enable",
    "notifications.disable",
    "developer.enable",
    "developer.disable",
    "unattended.interactive",
    "unattended.session",
    "unattended.project",
    "permissions.reset",
}


def apply_settings_action(action, permissions, runtime_state):
    """Apply one allow-listed Settings mutation using existing command semantics."""
    action = str(action or "")
    if action not in SETTINGS_ACTIONS:
        raise RuntimeError(f"Unknown Settings action: {action or '(missing)'}")
    if action == "notifications.enable":
        save_global_notification_config({"enabled": True})
    elif action == "notifications.disable":
        save_global_notification_config({"enabled": False})
    elif action == "developer.enable":
        runtime_state["developer_mode"] = True
    elif action == "developer.disable":
        runtime_state["developer_mode"] = False
    elif action == "unattended.interactive":
        save_project_unattended(False)
        disable_unattended(permissions)
    elif action == "unattended.session":
        save_project_unattended(False)
        enable_unattended(permissions, scope="session")
    elif action == "unattended.project":
        save_project_unattended(True)
        enable_unattended(permissions, scope="project")
    elif action == "permissions.reset":
        initialize_permissions(permissions)


def _with_content(result, title, content, view="commandOutput"):
    result.update({"title": title, "content": str(content or ""), "view": view})
    return result


def _clear_runtime_for_new_turn(runtime_state):
    runtime_state["continued_session"] = False


def format_directory_listing():
    rows = []
    for path in sorted(Path.cwd().iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        suffix = "/" if path.is_dir() else ""
        rows.append(f"{path.name}{suffix}")
    return "\n".join(rows) if rows else "(empty)"


def format_status(runtime_state, permissions):
    try:
        config = load_workspace_config()
        test_command = str(config.get("test_command") or "")
    except RuntimeError as error:
        test_command = f"config error: {error}"
    project = get_project_info()
    model_config = load_model_config()
    return "\n".join(
        [
            "Sema status:",
            f"cwd: {Path.cwd()}",
            f"project_name: {project.get('name')}",
            f"project_id: {project.get('project_id')}",
            f"project_file: {project.get('project_path')}",
            f"memory_root: {project.get('memory_root')}",
            f"event_stream_dir: {project.get('event_stream_dir')}",
            f"active_stream: {project.get('active_conversation_id') or '(none)'}",
            f"stream_session_count: {project.get('conversation_count')}",
            f"model: {model_label()}",
            f"supports_vision: {str(bool(model_config.get('supports_vision'))).lower()}",
            f"notifications: {str(bool(load_notification_config().get('enabled', True))).lower()}",
            f"continued_session: {str(runtime_state.get('continued_session', False)).lower()}",
            f"verbose: {str(runtime_state.get('verbose', False)).lower()}",
            f"test_command: {test_command or '(none)'}",
            f"unattended: {str(bool(permissions.get('unattended') or permissions.get('allow_all', False))).lower()}",
            f"unattended_scope: {unattended_scope(permissions)}",
            f"allow_all: {str(bool(permissions.get('allow_all', False))).lower()}",
        ]
    )


def format_memory_status():
    project = get_project_info()
    status = event_stream.event_stream_status()
    return "\n".join(
        [
            "Sema memory status:",
            f"memory_root: {project.get('memory_root')}",
            f"event_stream_dir: {project.get('event_stream_dir')}",
            f"active_stream: {project.get('active_conversation_id') or '(none)'}",
            f"event_stream_message_count: {status.get('message_count', 0)}",
            f"event_count: {status.get('event_count', 0)}",
            f"inline_event_count: {status.get('inline_event_count', 0)}",
            f"artifact_event_count: {status.get('artifact_event_count', 0)}",
            f"artifact_count: {status.get('artifact_count', 0)}",
            f"sum_event_count: {status.get('sum_event_count', 0)}",
        ]
    )


def format_event_stream_status(status=None):
    status = status or event_stream.event_stream_status()
    return "\n".join(
        [
            "Event Stream status:",
            f"memory_root: {status.get('memory_root')}",
            f"event_stream_dir: {status.get('event_stream_dir')}",
            f"db_path: {status.get('db_path')}",
            f"artifacts_dir: {status.get('artifacts_dir')}",
            f"slot_count: {status.get('slot_count', 0)}",
            f"event_count: {status.get('event_count', 0)}",
            f"inline_event_count: {status.get('inline_event_count', 0)}",
            f"artifact_event_count: {status.get('artifact_event_count', 0)}",
            f"raw_event_count: {status.get('raw_event_count', 0)}",
            f"ref_event_count: {status.get('ref_event_count', 0)}",
            f"sum_event_count: {status.get('sum_event_count', 0)}",
            f"artifact_count: {status.get('artifact_count', 0)}",
            f"artifact_bytes: {status.get('artifact_bytes', 0)}",
            f"inline_content_max_chars: {status.get('inline_content_max_chars', 0)}",
        ]
    )


def format_event_stream_integrity(result=None):
    result = result or event_stream.check_integrity()
    lines = [
        "Event Stream integrity:",
        f"ok: {str(bool(result.get('ok', False))).lower()}",
    ]
    counts = result.get("counts") or {}
    if counts:
        lines.append(f"slot_count: {counts.get('slot_count', 0)}")
        lines.append(f"event_count: {counts.get('event_count', 0)}")
        lines.append(f"artifact_count: {counts.get('artifact_count', 0)}")
    errors = result.get("errors") or []
    if errors:
        lines.append("errors:")
        for error in errors:
            lines.append(f"- {error.get('check')}: {error.get('message')}")
    return "\n".join(lines)


def format_stm_status(messages, runtime_state=None):
    try:
        budget = None
        raw_budget = None
        watermark = -1
        watermark_status = {}
        pressure = compute_and_store_dynamic_context_budget(
            messages or [],
            runtime_state,
            emit_observer=False,
            stabilize=False,
            apply_stabilizer=False,
        )
        budgets = pressure.get("budgets") or {}
        watermark = effective_wm_ingress_watermark(runtime_state)
        capacity = preview_stm_capacity(messages or [], pressure, watermark, runtime_state, advance_seq=False)
        budget = capacity["effective_budget"]
        raw_budget = budgets.get("stm_raw", budgets.get("stm"))
        watermark_status = wm_ingress_watermark_status(runtime_state)
        status = stm_projection_status(messages, budget=budget or STM_TOKEN_BUDGET, wm_ingress_watermark=watermark)
    except RuntimeError as error:
        return "\n".join(
            [
                "STM Projection status:",
                "status: error",
                f"error: {error}",
            ]
        )
    lines = [
        "STM Projection status:",
        "status: ok",
        f"budget: {status.get('budget', 0)}",
        f"raw_budget: {raw_budget or status.get('budget', 0)}",
        f"persisted_wm_ingress_watermark: {watermark_status.get('persisted_watermark', -1)}",
        f"staged_wm_ingress_watermark: {watermark_status.get('staged_watermark', -1)}",
        f"effective_wm_ingress_watermark: {status.get('wm_ingress_watermark', -1)}",
        f"watermark_locked_omitted_unit_count: {status.get('watermark_locked_omitted_unit_count', 0)}",
        f"selected_tokens: {status.get('selected_tokens', 0)}",
        f"omitted_tokens: {status.get('omitted_tokens', 0)}",
        f"total_unit_tokens: {status.get('total_unit_tokens', 0)}",
        f"budget_used_ratio: {status.get('budget_used_ratio', 0)}",
        f"total_message_count: {status.get('total_message_count', 0)}",
        f"non_system_message_count: {status.get('non_system_message_count', 0)}",
        f"selected_unit_count: {status.get('selected_unit_count', 0)}",
        f"omitted_unit_count: {status.get('omitted_unit_count', 0)}",
        f"latest_unit_tokens: {status.get('latest_unit_tokens', 0)}",
        f"largest_unit_tokens: {status.get('largest_unit_tokens', 0)}",
        f"total_unit_count: {status.get('total_unit_count', 0)}",
        "selected_units:",
    ]
    for unit in status.get("selected_units", []):
        lines.append(_format_stm_block(unit))
    if not status.get("selected_units"):
        lines.append("- none")
    lines.append("omitted_units:")
    for unit in status.get("omitted_units", []):
        lines.append(_format_stm_block(unit))
    if not status.get("omitted_units"):
        lines.append("- none")
    return "\n".join(lines)


def _format_stm_block(block):
    return (
        "- "
        f"{block.get('kind')} "
        f"messages={block.get('message_count', 0)} "
        f"tokens={block.get('token_count', 0)} "
        f"range={block.get('start_index')}..{block.get('end_index')}"
    )


def handle_memory_command(prompt, messages=None, runtime_state=None):
    if prompt == "/memory event-stream status":
        return format_event_stream_status()
    if prompt == "/memory event-stream check":
        return format_event_stream_integrity()
    if prompt == "/memory context status":
        try:
            return format_context_status(context_status(messages or [], runtime_state=runtime_state))
        except RuntimeError as error:
            return "\n".join(
                [
                    "Context Builder status:",
                    "status: error",
                    f"error: {error}",
                ]
            )
    if prompt == "/memory budget status":
        return format_context_pressure_status(
            compute_and_store_dynamic_context_budget(messages or [], runtime_state, emit_observer=False, stabilize=False)
        )
    if prompt.startswith("/memory budget set "):
        parts = prompt.split()
        if len(parts) != 5:
            raise RuntimeError("Usage: /memory budget set <stm|wm|both> <tokens>")
        set_budget_override(parts[3], parts[4])
        return format_context_pressure_status(
            compute_and_store_dynamic_context_budget(messages or [], runtime_state, emit_observer=False, stabilize=False)
        )
    if prompt == "/memory budget reset":
        reset_budget_override()
        return format_context_pressure_status(
            compute_and_store_dynamic_context_budget(messages or [], runtime_state, emit_observer=False, stabilize=False)
        )
    if prompt == "/memory stm status":
        return format_stm_status(messages or [], runtime_state)
    if prompt == "/memory wm status":
        return format_working_memory_status(working_memory_status(messages or []))
    if prompt == "/memory wm run":
        return format_working_memory_run(run_working_memory_maintenance(messages or [], force=True))
    if prompt == "/memory focus status":
        return format_focus_segments_status(focus_segments_status(messages or [], runtime_state))
    if prompt == "/memory focus maintain":
        return format_focus_segments_status(
            {
                **focus_segments_status(messages or [], runtime_state),
                "last_full_maintainer_result": run_full_segment_maintenance(messages or [], runtime_state, force=True),
            }
        )
    if prompt == "/memory ltm maintain":
        return format_ltm_maintenance_result(run_ltm_maintenance(runtime_state=runtime_state, force=True))
    if prompt == "/memory ltm visualize":
        return format_ltm_visualization_result(open_ltm_visualization_html(generate_ltm_visualization()))
    if prompt in {"/memory", "/memory status"}:
        return format_memory_status()
    raise RuntimeError("Unknown /memory command. Use /memory, /memory status, or Event Stream status commands.")


def handle_trajectory_command(prompt, runtime_state=None):
    parts = prompt.split()
    store_status = trajectory_store_status()
    status_text = format_trajectory_store_status(store_status)
    status_prefix = f"status: {store_status.get('state')}\n{status_text}\n\n" if status_text else ""
    if len(parts) >= 2 and parts[1] in {"context", "contexts"}:
        if parts[1] == "context" and len(parts) == 4 and parts[2] == "request":
            return status_prefix + format_request(get_request(parts[3]))
        if len(parts) > 3:
            raise RuntimeError("Use /trajectory context [agent_type], /trajectory contexts [agent_type], or /trajectory context request <request_id>.")
        rows = list_requests(parts[2] if len(parts) == 3 else "main_agent")
        if parts[1] == "context":
            return status_prefix + format_request(rows[0] if rows else None)
        return status_prefix + ("\n\n".join(format_request(row) for row in rows) or "No provider contexts.")
    if prompt == "/trajectory":
        return status_prefix + format_trajectory_list(list_trajectories(limit=20))
    if prompt == "/trajectory current":
        current_id = ((runtime_state or {}).get("trajectory_context") or {}).get("trajectory_id")
        current = get_trajectory(current_id) if current_id else None
        if current is None:
            latest = latest_trajectory("main_agent")
            current = get_trajectory(latest["trajectory_id"]) if latest else None
        return status_prefix + format_trajectory_tree(current)
    if prompt == "/trajectory errors":
        return status_prefix + format_trajectory_errors(limit=20)
    if prompt == "/trajectory path":
        return f"status: {store_status.get('state')}\ndatabase: {trajectory_db_path()}\nartifacts: {trajectory_artifact_dir()}\ncontexts: {context_dir()}"
    if prompt.startswith("/trajectory artifact "):
        artifact_id = prompt.removeprefix("/trajectory artifact ").strip()
        metadata = artifact_metadata(artifact_id)
        return status_prefix + (json.dumps(metadata, ensure_ascii=False, indent=2) if metadata else "Trajectory artifact not found.")
    if prompt.startswith("/trajectory "):
        trajectory_id = prompt.removeprefix("/trajectory ").strip()
        return status_prefix + format_trajectory_tree(get_trajectory(trajectory_id))
    raise RuntimeError("Unknown /trajectory command. Use /trajectory, /trajectory current, /trajectory errors, /trajectory path, /trajectory <id>, or /trajectory artifact <id>.")


def handle_undo_command(prompt):
    if prompt == "/undo list":
        return format_undo_records(load_undo_records(limit=20))
    if prompt == "/undo":
        return format_undo_result(apply_undo())
    record_id = prompt.removeprefix("/undo").strip()
    if record_id:
        return format_undo_result(apply_undo(record_id))
    raise RuntimeError("Unknown /undo command. Use /undo, /undo list, or /undo <id>.")


def handle_verbose_command(prompt, runtime_state):
    if prompt == "/verbose":
        return f"Verbose tool output: {str(runtime_state.get('verbose', False)).lower()}"
    value = prompt.removeprefix("/verbose").strip().lower()
    if value == "on":
        runtime_state["verbose"] = True
        return "Verbose tool output: true"
    if value == "off":
        runtime_state["verbose"] = False
        return "Verbose tool output: false"
    raise RuntimeError("Unknown /verbose command. Use /verbose, /verbose on, or /verbose off.")


def handle_permissions_command(prompt, permissions, result):
    if prompt == "/permissions":
        return _with_content(result, "Permissions", format_permissions(permissions), view="permissions")
    if prompt == "/permissions reset":
        initialize_permissions(permissions)
        result["permissions"] = dict(permissions)
        return _with_content(result, "Permissions", format_permissions(permissions), view="permissions")
    raise RuntimeError("Unknown /permissions command. Use /permissions or /permissions reset. Use /unattended session or /unattended project to bypass confirmations.")


def handle_unattended_command(prompt, permissions, result):
    if prompt in {"/unattended", "/unattended status"}:
        return _with_content(result, "Unattended", format_unattended(permissions), view="permissions")
    if prompt in {"/unattended on", "/unattended session"}:
        save_project_unattended(False)
        enable_unattended(permissions, scope="session")
        result["permissions"] = dict(permissions)
        return _with_content(result, "Unattended", format_unattended(permissions), view="permissions")
    if prompt == "/unattended project":
        save_project_unattended(True)
        enable_unattended(permissions, scope="project")
        result["permissions"] = dict(permissions)
        return _with_content(result, "Unattended", format_unattended(permissions), view="permissions")
    if prompt == "/unattended off":
        save_project_unattended(False)
        disable_unattended(permissions)
        result["permissions"] = dict(permissions)
        return _with_content(result, "Unattended", format_unattended(permissions), view="permissions")
    raise RuntimeError("Unknown /unattended command. Use /unattended status, /unattended session, /unattended project, or /unattended off.")


def handle_notify_command(prompt, result):
    if prompt in {"/notify", "/notify status"}:
        return _with_notify_result(result, "Notify", format_notify())
    if prompt == "/notify on":
        save_global_notification_config({"enabled": True})
        return _with_notify_result(result, "Notify", format_notify())
    if prompt == "/notify off":
        save_global_notification_config({"enabled": False})
        return _with_notify_result(result, "Notify", format_notify())
    raise RuntimeError("Unknown /notify command. Use /notify, /notify status, /notify on, or /notify off.")


def _with_notify_result(result, title, content):
    result.update({"notifications": load_notification_config()})
    return _with_content(result, title, content, view="commandOutput")


def format_notify():
    config = load_notification_config()
    return "\n".join(
        [
            "Sema notifications:",
            f"enabled: {str(bool(config.get('enabled', True))).lower()}",
            "trigger: terminal is not focused",
            "focus_tracking: TUI terminal focus events",
            "",
            "Use /notify on or /notify off.",
        ]
    )


def format_unattended(permissions):
    enabled = bool(permissions.get("unattended") or permissions.get("allow_all", False))
    scope = unattended_scope(permissions)
    effect = "bypass all confirmation prompts" if enabled else "ask before protected operations"
    return "\n".join(
        [
            "Sema unattended mode:",
            f"enabled: {str(enabled).lower()}",
            f"effect: {effect}",
            f"scope: {scope}",
            "",
            "Use /unattended session, /unattended project, or /unattended off.",
        ]
    )


def format_permissions(permissions):
    return "\n".join(
        [
            "Sema permissions:",
            f"unattended: {str(bool(permissions.get('unattended') or permissions.get('allow_all', False))).lower()}",
            f"unattended scope: {unattended_scope(permissions)}",
            f"run commands: {str(bool(permissions.get('allow_run_commands', False))).lower()}",
            f"edit files: {str(bool(permissions.get('allow_edits', False))).lower()}",
            "",
            "Use /unattended session or /unattended project to bypass confirmations.",
            "Temporary permissions reset when Sema exits; project unattended mode persists.",
        ]
    )


def _redacted_secret(value):
    return "configured" if value else "(not saved)"


def format_model_config():
    config = load_model_config()
    return "\n".join(
        [
            "Sema model config:",
            f"provider: {config.get('provider')}",
            f"protocol: {config.get('protocol') or 'openai_compatible'}",
            f"base_url: {config.get('base_url') or '(missing)'}",
            f"model: {config.get('model') or '(missing)'}",
            f"api_key_env: {config.get('api_key_env') or '(none)'}",
            f"saved_api_key: {_redacted_secret(config.get('api_key'))}",
            f"supports_tools: {str(bool(config.get('supports_tools'))).lower()}",
            f"supports_vision: {str(bool(config.get('supports_vision'))).lower()}",
            f"thinking: {'default' if config.get('thinking') is None else str(bool(config.get('thinking'))).lower()}",
            f"config_file: {global_config_path()}",
        ]
    )


def format_model_profiles():
    lines = ["Available model profiles:"]
    for name, profile in PROVIDER_PROFILES.items():
        lines.append(f"- {name}: {profile.get('model')}  vision={str(bool(profile.get('supports_vision'))).lower()}")
    return "\n".join(lines)


def parse_config_value(key, value):
    if key in {"supports_tools", "supports_vision", "supports_streaming", "thinking"}:
        return str(value).lower() in {"1", "true", "yes", "on"}
    return value


def handle_config_command(prompt, result):
    from runtime.setup_config import public_model
    args = prompt.removeprefix("/config").strip().split()
    if not args or args == ["set"]:
        result.update({"view": "config", "title": "Config", "content": "", "config": {"open": True}})
        return result
    if args == ["show"]:
        result["config"] = {"config": public_model(), "profiles": PROVIDER_PROFILES, "path": str(global_config_path())}
        return _with_content(result, "Config", format_model_config(), view="config")
    if args == ["profiles"]:
        return _with_content(result, "Profiles", format_model_profiles(), view="commandOutput")
    if args[0] == "profile":
        if len(args) != 2:
            raise RuntimeError("Usage: /config profile <deepseek|kimi|kimi_intl|custom_openai|ollama>")
        name = args[1]
        if name not in PROVIDER_PROFILES:
            raise RuntimeError(f"Unknown model profile: {name}")
        save_global_model_config(dict(PROVIDER_PROFILES[name]))
        result["config"] = {"config": public_model(), "profiles": PROVIDER_PROFILES, "path": str(global_config_path())}
        return _with_content(result, "Config", f"Model profile set: {name}\nConfig file: {global_config_path()}", view="config")
    if args[0] == "set":
        if len(args) < 3:
            raise RuntimeError("Usage: /config set <provider|base_url|model|api_key_env|api_key|supports_vision|thinking> <value>")
        key = args[1]
        value = " ".join(args[2:])
        allowed = {"provider", "base_url", "model", "api_key_env", "api_key", "supports_tools", "supports_vision", "supports_streaming", "thinking"}
        if key not in allowed:
            raise RuntimeError(f"Unknown config key: {key}")
        save_global_model_config({key: parse_config_value(key, value)})
        result["config"] = {"config": public_model(), "profiles": PROVIDER_PROFILES, "path": str(global_config_path())}
        return _with_content(result, "Config", f"Model config updated: {key}\nConfig file: {global_config_path()}", view="config")
    raise RuntimeError("Unknown /config command. Use /config, /config show, /config profiles, /config profile <name>, or /config set <key> <value>.")


def format_last_run(transcript):
    if not transcript:
        return "No tool activity recorded for the last run."
    lines = []
    for index, record in enumerate(transcript, start=1):
        lines.append(f"{index}. {record.get('tool')} {record.get('args')}".rstrip())
        lines.append(str(record.get("result") or record.get("error") or ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def slash_command_list(developer_mode=False):
    return list_slash_commands(developer_mode=developer_mode)
