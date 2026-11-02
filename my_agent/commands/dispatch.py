"""Ordered slash-command dispatch table.

Command handlers remain in ``structured_commands`` for compatibility. This
module owns only routing, so command names and semantics stay in Python while
the large conditional chain no longer defines the dispatch structure.
"""


PREFIX_ROUTES = (
    ("/notify", "handle_notify_command", "direct"),
    ("/unattended", "handle_unattended_command", "permissions"),
    ("/memory", "handle_memory_command", "memory"),
    ("/undo", "handle_undo_command", "content"),
    ("/permissions", "handle_permissions_command", "permissions"),
    ("/config", "handle_config_command", "result"),
)


def dispatch_command(prompt, result, messages, permissions, runtime_state, dependencies):
    """Return a structured result for a registered command, or ``None``."""
    content = dependencies["_with_content"]

    if prompt == "/status":
        return content(result, "Status", dependencies["format_status"](runtime_state, permissions))
    if prompt == "/settings":
        return dependencies["handle_settings_command"](result, permissions, messages, runtime_state)
    if prompt == "/time":
        return content(result, "Time", dependencies["format_temporal_state"](runtime_state))
    if prompt == "/last-run":
        return content(result, "Last run", dependencies["format_last_run"](runtime_state.get("last_run", [])), view="lastRun")
    if prompt == "/exit":
        result.update({"should_exit": True, "title": "Exit", "content": "Bye."})
        return result

    for prefix, handler_name, mode in PREFIX_ROUTES:
        if not prompt.startswith(prefix):
            continue
        handler = dependencies[handler_name]
        if mode == "direct":
            return handler(prompt, result)
        if mode == "permissions":
            return handler(prompt, permissions, result)
        if mode == "memory":
            return content(result, "Memory", handler(prompt, messages, runtime_state))
        if mode == "content":
            title = {"/undo": "Undo"}[prefix]
            return content(result, title, handler(prompt))
        if mode == "result":
            return handler(prompt, result)

    return None
