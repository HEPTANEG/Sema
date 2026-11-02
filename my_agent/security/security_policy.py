import re
from dataclasses import dataclass

from tools.tool_registry import get_tool_definition
from tools import path_outside_workspace


LEGACY_EDIT_TOOLS = {"apply_patch"}
DANGEROUS_COMMAND_PATTERNS = [
    r"\b(remove-item|rm|del|erase|rmdir|rd|unlink)\b",
    r"\b(move-item|mv|rename-item|ren|copy-item|set-content|add-content|out-file|new-item)\b",
    r"\bgit\s+(checkout|restore|reset|clean)\b",
    r"(^|[^>])>{1,2}[^>]",
]
BLOCKED_MODEL_GIT_UNDO_RE = re.compile(r"\bgit\s+(checkout|restore|reset|clean)\b", re.IGNORECASE)


@dataclass
class ToolRiskDecision:
    risk: str
    needs_confirmation: bool
    allow_external: bool = False
    blocked: bool = False
    reason: str = ""
    confirmation_action: str = ""
    details: str = ""


def is_file_mutating_command(command):

    command_text = str(command or "").lower()
    return any(re.search(pattern, command_text) for pattern in DANGEROUS_COMMAND_PATTERNS)


def is_blocked_model_git_undo_command(command):

    return bool(BLOCKED_MODEL_GIT_UNDO_RE.search(str(command or "")))


def _tool_paths(tool_request):
    tool = tool_request.get("tool")
    definition = get_tool_definition(tool)
    if definition is None:
        return []
    paths = []
    for key in definition.path_arg_keys:
        value = _nested_tool_value(tool_request, key)
        if isinstance(value, list):
            paths.extend(value)
        elif isinstance(value, dict):
            continue
        else:
            paths.append(value)
    return paths


def _nested_tool_value(tool_request, key):
    value = tool_request
    for part in str(key or "").split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _has_external_path(tool_request):
    return any(path and path_outside_workspace(str(path)) for path in _tool_paths(tool_request))


def classify_tool_request(tool_request, permissions=None):

    permissions = permissions or {}
    tool = tool_request.get("tool")
    args = tool_request.get("args")
    definition = get_tool_definition(tool)

    if tool in {"run_command", "start_background_command"}:
        command = args.get("command") if isinstance(args, dict) else args
        action = "start_background_command" if tool == "start_background_command" else "run_command"
        if is_blocked_model_git_undo_command(command):
            return ToolRiskDecision(
                risk="dangerous_command",
                needs_confirmation=False,
                blocked=True,
                reason="model-requested git checkout/restore/reset/clean must use undo_edit first",
                details=str(command or ""),
            )
        if is_file_mutating_command(command):
            return ToolRiskDecision(
                risk="dangerous_command",
                needs_confirmation=not permissions.get("allow_all"),
                confirmation_action="file-mutating run_command",
                details=str(command or ""),
            )
        return ToolRiskDecision(
            risk="run_command",
            needs_confirmation=not (permissions.get("allow_all") or permissions.get("allow_run_commands")),
            confirmation_action=action,
            details=str(command or ""),
        )

    if definition and definition.risk == "read_file":
        if _has_external_path(tool_request):
            return ToolRiskDecision(
                risk="read_external",
                needs_confirmation=not permissions.get("allow_all"),
                allow_external=bool(permissions.get("allow_all")),
                confirmation_action=definition.external_path_action or "read external path",
                details="\n".join(str(path) for path in _tool_paths(tool_request) if path),
            )
        return ToolRiskDecision(risk="read_local", needs_confirmation=False)

    if tool in LEGACY_EDIT_TOOLS or (definition and definition.risk == "edit_file"):
        if _has_external_path(tool_request):
            return ToolRiskDecision(
                risk="edit_external",
                needs_confirmation=not permissions.get("allow_all"),
                allow_external=bool(permissions.get("allow_all")),
                confirmation_action=(definition.external_path_action if definition else "") or ("write external file" if tool == "write_file" else "edit external file"),
                details="\n".join(str(path) for path in _tool_paths(tool_request) if path),
            )
        return ToolRiskDecision(
            risk="edit_local",
            needs_confirmation=not (permissions.get("allow_all") or permissions.get("allow_edits")),
            confirmation_action=tool,
            details=str(args or "Apply proposed patch"),
        )

    if definition and definition.risk == "network_read":
        return ToolRiskDecision(risk="network_read", needs_confirmation=False)
    if definition and definition.risk == "safe_read":
        return ToolRiskDecision(risk="safe_read", needs_confirmation=False)
    if definition and definition.risk == "memory_write":
        return ToolRiskDecision(risk="memory_write", needs_confirmation=False)
    if definition and definition.risk == "runtime_state":
        return ToolRiskDecision(risk="runtime_state", needs_confirmation=False)
    if definition and definition.risk == "session_control":
        return ToolRiskDecision(
            risk="session_control",
            needs_confirmation=not permissions.get("allow_all"),
            confirmation_action=tool,
            details=str(args or ""),
        )
    if definition and definition.risk == "undo":
        return ToolRiskDecision(
            risk="undo",
            needs_confirmation=not permissions.get("allow_all"),
            confirmation_action=tool,
            details=str(args or "(latest edit)"),
        )
    if definition and definition.risk == "persistent_state":
        return ToolRiskDecision(
            risk="persistent_state",
            needs_confirmation=not permissions.get("allow_all"),
            confirmation_action=tool,
            details=str(args or ""),
        )

    return ToolRiskDecision(
        risk="unknown",
        needs_confirmation=True,
        blocked=True,
        reason=f"Unknown tool requested: {tool}",
        details=str(args or ""),
    )
