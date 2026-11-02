PUBLIC_SLASH_COMMANDS = [
    "/help", "/settings", "/status", "/memory", "/image", "/undo", "/undo list", "/exit",
]

DEVELOPER_SLASH_COMMANDS = [
    "/pwd", "/ls", "/read", "/time", "/last-run",
    "/config", "/config show", "/config profiles", "/config profile", "/config set",
    "/permissions", "/permissions reset",
    "/notify", "/notify status", "/notify on", "/notify off",
    "/unattended", "/unattended status", "/unattended on", "/unattended session",
    "/unattended project", "/unattended off",
    "/memory status", "/memory event-stream status", "/memory event-stream check",
    "/memory context status", "/memory budget status", "/memory budget set", "/memory budget reset",
    "/memory stm status", "/memory wm status", "/memory wm run",
    "/memory focus status", "/memory focus maintain", "/memory ltm maintain", "/memory ltm visualize",
]

SLASH_COMMANDS = PUBLIC_SLASH_COMMANDS + DEVELOPER_SLASH_COMMANDS
SLASH_COMMAND_VISIBILITY = {
    **{command: "public" for command in PUBLIC_SLASH_COMMANDS},
    **{command: "developer" for command in DEVELOPER_SLASH_COMMANDS},
}

SLASH_COMMAND_HELP = {
    "/help": "Show available commands.",
    "/pwd": "Show current working directory.",
    "/ls": "List files in current working directory.",
    "/read": "Read a file into the conversation context.",
    "/image": "Add local images to the conversation context.",
    "/status": "Show current Sema runtime status.",
    "/settings": "Open grouped Sema settings.",
    "/notify": "Show Sema system notification status.",
    "/notify status": "Show Sema system notification status.",
    "/notify on": "Enable system notifications when the TUI is not focused.",
    "/notify off": "Disable system notifications.",
    "/unattended": "Show unattended mode status.",
    "/unattended status": "Show unattended mode status.",
    "/unattended on": "Bypass confirmations for this session.",
    "/unattended session": "Bypass confirmations for this Sema session.",
    "/unattended project": "Persist unattended mode for this project.",
    "/unattended off": "Disable unattended mode and restore confirmations.",
    "/time": "Show current clock and runtime rhythm.",
    "/memory": "Show high-level memory status.",
    "/memory status": "Show Event Stream memory status.",
    "/memory event-stream status": "Show Event Stream storage status.",
    "/memory event-stream check": "Run Event Stream integrity checks.",
    "/memory context status": "Show Context Builder metadata.",
    "/memory budget status": "Show dynamic context budget metadata.",
    "/memory budget set": "Set a manual STM/WM budget override.",
    "/memory budget reset": "Clear manual budget overrides.",
    "/memory stm status": "Show STM Projection metadata.",
    "/memory wm status": "Show Working Memory metadata.",
    "/memory wm run": "Run one Working Memory maintenance pass.",
    "/memory focus status": "Show Focus Segment metadata.",
    "/memory focus maintain": "Run Focus Segment maintenance.",
    "/memory ltm maintain": "Run LTM maintenance.",
    "/memory ltm visualize": "Export an LTM debug visualization.",
    "/last-run": "Show details from the previous tool run.",
    "/undo": "Undo the latest Sema file edit.",
    "/undo list": "Show recent undoable file edits.",
    "/config": "Open model provider configuration.",
    "/config show": "Show model provider configuration.",
    "/config profiles": "Show built-in model provider profiles.",
    "/config profile": "Use a built-in model provider profile.",
    "/config set": "Open model provider configuration.",
    "/permissions": "Show temporary permissions for this Sema process.",
    "/permissions reset": "Reset temporary permissions for this Sema process.",
    "/exit": "Exit Sema.",
}


def list_slash_commands(excluded=None, developer_mode=False):
    excluded = set(excluded or [])
    commands = PUBLIC_SLASH_COMMANDS + (DEVELOPER_SLASH_COMMANDS if developer_mode else [])
    return [
        {"name": command, "help": SLASH_COMMAND_HELP.get(command, ""), "visibility": SLASH_COMMAND_VISIBILITY[command]}
        for command in commands
        if command not in excluded
    ]


def format_help(excluded=None, developer_mode=False):
    excluded = set(excluded or [])
    commands = PUBLIC_SLASH_COMMANDS + (DEVELOPER_SLASH_COMMANDS if developer_mode else [])
    return _format_help_for_commands([command for command in commands if command not in excluded])


def _format_help_for_commands(commands):
    lines = ["Sema commands:", ""]
    width = max(len(command) for command in commands)
    for command in commands:
        lines.append(f"{command.ljust(width)}  {SLASH_COMMAND_HELP.get(command, '')}".rstrip())
    lines.extend([
        "", "Shell commands:", "", "sema  Start the Ink TUI.",
        "sema tui  Start the Ink TUI.", "Use /settings to view or update Sema settings.",
        "sema-eval  Run external Sema benchmark suites.",
    ])
    return "\n".join(lines)
