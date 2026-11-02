from commands.command_registry import DEVELOPER_SLASH_COMMANDS, PUBLIC_SLASH_COMMANDS, SLASH_COMMAND_HELP, SLASH_COMMANDS, format_help, list_slash_commands


def test_registry_contains_current_tui_slash_commands():
    for command in (
        "/help",
        "/memory",
        "/memory context status",
        "/memory wm status",
        "/memory focus status",
        "/undo",
        "/config",
        "/settings",
        "/permissions",
        "/unattended",
        "/unattended session",
        "/unattended project",
    ):
        assert command in SLASH_COMMANDS
        assert SLASH_COMMAND_HELP[command]

    assert "/memory architecture" not in SLASH_COMMANDS
    assert "/intent" not in SLASH_COMMANDS
    assert "/task" not in SLASH_COMMANDS
    assert "/permissions allow-all" not in SLASH_COMMANDS
    assert "/safety" not in SLASH_COMMANDS
    assert "/run" not in SLASH_COMMANDS
    assert "/verbose" not in SLASH_COMMANDS
    assert "/trajectory" not in SLASH_COMMANDS


def test_list_slash_commands_uses_registry_metadata():
    records = list_slash_commands()

    assert {"name": "/help", "help": SLASH_COMMAND_HELP["/help"], "visibility": "public"} in records
    assert all(record["visibility"] == "public" for record in records)
    assert len(records) == len(PUBLIC_SLASH_COMMANDS)
    assert len(list_slash_commands(developer_mode=True)) == len(SLASH_COMMANDS)
    assert all(record["visibility"] == "developer" for record in list_slash_commands(developer_mode=True)[len(PUBLIC_SLASH_COMMANDS):])


def test_help_uses_the_single_command_registry():
    help_text = format_help()

    assert "/memory context status" not in help_text
    assert "/memory wm status" not in help_text
    assert "/memory focus status" not in help_text
    assert "/memory architecture" not in help_text
    developer_help = format_help(developer_mode=True)
    assert "/memory context status" in developer_help
    assert "/memory wm status" in developer_help
    assert "/memory focus status" in developer_help
