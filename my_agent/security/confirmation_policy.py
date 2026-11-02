class ConfirmationRequired(RuntimeError):
    pass


def _confirmation_required(action, details=""):
    suffix = f": {details}" if details else ""
    raise ConfirmationRequired(f"No confirmation policy is installed for {action}{suffix}")


def confirm_run_command(command, permissions=None):
    if (permissions or {}).get("allow_all") or (permissions or {}).get("allow_run_commands"):
        return {"allowed": True, "reason": ""}
    return _confirmation_required("run_command", command)


def confirm_file_mutating_command(command, permissions=None):
    if (permissions or {}).get("allow_all"):
        return {"allowed": True, "reason": ""}
    return _confirmation_required("file-mutating run_command", command)


def confirm_external_path_access(action_name, details, permissions=None):
    if (permissions or {}).get("allow_all"):
        return {"allowed": True, "reason": "", "allow_external": True}
    return _confirmation_required(f"{action_name} outside workspace", details)


def confirm_session_control(action_name, details, permissions=None):
    if (permissions or {}).get("allow_all"):
        return {"allowed": True, "reason": ""}
    return _confirmation_required(action_name, details)


def confirm_undo_edit(details, permissions=None):
    if (permissions or {}).get("allow_all"):
        return {"allowed": True, "reason": ""}
    return _confirmation_required("undo this edit", details)


def confirm_edit_action(action_name, details, allow_key, permissions=None, preview_callback=None):
    permissions = permissions or {}
    if permissions.get("allow_all") or permissions.get(allow_key):
        return {"allowed": True, "reason": ""}
    return _confirmation_required(action_name, details)


def confirm_persistent_action(action_name, details, preview=""):
    return _confirmation_required(action_name, details)


def confirm_tool_checkpoint(_tool_count):
    return "Yes"
