from commands.dispatch import dispatch_command


def _dependencies(calls):
    def content(result, title, value, **kwargs):
        calls.append(("content", title, value, kwargs))
        result.update({"title": title, "content": value, **kwargs})
        return result

    def handler(name, value=None):
        def wrapped(*args):
            calls.append((name, args))
            return value if value is not None else f"{name} result"

        return wrapped

    return {
        "_with_content": content,
        "format_last_run": handler("format_last_run", "last run"),
        "format_status": handler("format_status", "status"),
        "format_temporal_state": handler("format_temporal_state", "time"),
        "handle_config_command": handler("config", {"handled": True}),
        "handle_memory_command": handler("memory"),
        "handle_notify_command": handler("notify", {"handled": True}),
        "handle_permissions_command": handler("permissions", {"handled": True}),
        "handle_trajectory_command": handler("trajectory"),
        "handle_undo_command": handler("undo"),
        "handle_unattended_command": handler("unattended", {"handled": True}),
        "handle_verbose_command": handler("verbose"),
    }


def test_dispatch_routes_direct_and_content_commands():
    calls = []
    dependencies = _dependencies(calls)
    result = dispatch_command("/trajectory current", {"prompt": "/trajectory current"}, [], {}, {}, dependencies)

    assert result is None
    assert calls == []


def test_dispatch_routes_runtime_status_and_exit_commands():
    calls = []
    dependencies = _dependencies(calls)

    status = dispatch_command("/status", {"prompt": "/status"}, [], {}, {}, dependencies)
    assert status["title"] == "Status"
    assert status["content"] == "status"

    exit_result = dispatch_command("/exit", {"prompt": "/exit"}, [], {}, {}, dependencies)
    assert exit_result["should_exit"] is True


def test_dispatch_leaves_unknown_commands_for_legacy_handling():
    calls = []
    result = dispatch_command("/unknown", {"prompt": "/unknown"}, [], {}, {}, _dependencies(calls))

    assert result is None
