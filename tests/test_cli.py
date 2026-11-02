import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT_DIR / "my_agent" / "main.py"


def test_run_agent_turn_persists_user_before_model_call_without_auto_intent(isolated_workspace, monkeypatch):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    import main
    from persistence import session
    import memory_engine.event_stream as event_stream

    session.ensure_project()
    messages = [{"role": "system", "content": "system"}]
    session.save_last_session(messages)
    permissions = {}
    runtime_state = {}
    calls = []

    def fake_answer(current, current_permissions, state):
        assert event_stream.load_messages() == current
        calls.append(("answer", list(current), current_permissions, dict(state)))

    monkeypatch.setattr(main, "answer_with_tools", fake_answer)

    main.run_agent_turn("fix the bug", messages, permissions, runtime_state)

    assert calls[0][0] == "answer"
    assert calls[0][1][-1] == {"role": "user", "content": "fix the bug"}
    assert "intent" not in calls[0][3]
    assert "temporal" not in calls[0][3]
    assert runtime_state["turn_start_index"] == 1


def test_main_without_args_dispatches_to_tui(monkeypatch):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    import main
    from bridge import ui_bridge

    monkeypatch.setattr(sys, "argv", ["sema"])
    monkeypatch.setattr(ui_bridge, "launch_tui", lambda: 17)

    try:
        main.main()
    except SystemExit as exit_error:
        assert exit_error.code == 17
    else:
        raise AssertionError("main() should exit with TUI launcher code")


def test_main_tui_arg_dispatches_to_tui(monkeypatch):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    import main
    from bridge import ui_bridge

    monkeypatch.setattr(sys, "argv", ["sema", "tui"])
    monkeypatch.setattr(ui_bridge, "launch_tui", lambda: 23)

    try:
        main.main()
    except SystemExit as exit_error:
        assert exit_error.code == 23
    else:
        raise AssertionError("main() should exit with TUI launcher code")


def run_cli(tmp_path, args):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["SEMA_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(MAIN_PATH), *args],
        cwd=workspace,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )


def test_config_cli_is_removed(tmp_path):
    completed = run_cli(tmp_path, ["config", "show"])

    assert completed.returncode == 1
    assert "use slash commands such as `/memory status` and `/config` inside the TUI" in completed.stderr


def test_structured_help_and_status_commands_do_not_call_api(isolated_workspace):
    from runtime.agent import create_initial_messages
    from persistence import session
    from commands.structured_commands import execute_slash_command

    session.ensure_project()
    messages = create_initial_messages()
    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    help_result = execute_slash_command("/help", messages, permissions, runtime_state, create_initial_messages)
    status_result = execute_slash_command("/status", messages, permissions, runtime_state, create_initial_messages)

    assert help_result["handled"] is True
    assert "/memory show" not in help_result["content"]
    assert "/memory context status" not in help_result["content"]
    assert "/skills" not in help_result["content"]
    assert "/intent" not in help_result["content"]
    assert status_result["title"] == "Status"
    assert "Sema status:" in status_result["content"]
    assert "notifications: true" in status_result["content"]

    assert "/task" not in help_result["content"]


def test_removed_slash_wrappers_are_not_handled(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": []}
    for command in ("/run echo hi", "/verbose", "/trajectory current"):
        result = execute_slash_command(command, create_initial_messages(), permissions, runtime_state, create_initial_messages)
        assert result["handled"] is False


def test_settings_command_returns_aggregate_without_calling_model(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    result = execute_slash_command("/settings", create_initial_messages(), permissions, runtime_state, create_initial_messages)

    assert result["view"] == "settings"
    assert result["title"] == "Settings"
    assert set(result["settings"]) >= {"categories", "model", "notifications", "permissions", "unattended", "developer"}
    assert "verbose" not in result["settings"]
    assert "memory" not in result["settings"]
    assert [category["id"] for category in result["settings"]["categories"]] == ["ltm-storage", "model", "permissions", "interface", "developer", "search", "remote", "overview"]
    assert result["settings"]["permissions"]["allow_all"] is False
    permissions_category = next(category for category in result["settings"]["categories"] if category["id"] == "permissions")
    assert permissions_category["label"] == "Permissions"
    assert permissions_category["actions"][0] == {"id": "grants-heading", "label": "Permission state", "kind": "section"}
    assert permissions_category["actions"][1]["value"] == "Confirmation required"
    execution_mode = next(action for action in permissions_category["actions"] if action["id"] == "execution-mode")
    assert execution_mode["kind"] == "choice"
    assert execution_mode["value"] == "interactive"
    assert execution_mode["options"][1]["action"] == "unattended.session"
    assert execution_mode["options"][1]["confirm_label"] == "Enable for this session"
    assert execution_mode["options"][2]["action"] == "unattended.project"
    assert execution_mode["options"][2]["confirm_label"] == "Enable for this project"


def test_structured_read_command_updates_messages_and_session(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    Path("notes.txt").write_text("hello", encoding="utf-8")
    messages = create_initial_messages()
    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    result = execute_slash_command("/read notes.txt", messages, permissions, runtime_state, create_initial_messages)

    assert result["title"] == "Read notes.txt"
    assert result["transcript_items"][0]["content"] == "Added file context: notes.txt"
    assert result["messages"][-1]["role"] == "user"
    assert "Local file context from notes.txt" in result["messages"][-1]["content"]


def test_structured_image_command_updates_messages(isolated_workspace, tmp_path, monkeypatch):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SEMA_HOME", str(home))
    (home / "config.json").write_text(json.dumps({"model": {"provider": "kimi"}}, ensure_ascii=False), encoding="utf-8")
    Path("one.png").write_bytes(b"png")
    messages = create_initial_messages()
    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    result = execute_slash_command("/image one.png", messages, permissions, runtime_state, create_initial_messages)

    assert result["title"] == "Images added"
    assert "Added 1 image(s)" in result["transcript_items"][0]["content"]
    assert result["messages"][-1]["role"] == "user"


def test_structured_unattended_supports_session_scope(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    on_result = execute_slash_command("/unattended on", [], permissions, runtime_state, create_initial_messages)
    status_result = execute_slash_command("/unattended status", [], permissions, runtime_state, create_initial_messages)
    reset_result = execute_slash_command("/permissions reset", [], permissions, runtime_state, create_initial_messages)

    assert on_result["permissions"]["unattended"] is True
    assert on_result["permissions"]["allow_all"] is True
    assert on_result["permissions"]["allow_run_commands"] is True
    assert on_result["permissions"]["allow_edits"] is True
    assert on_result["permissions"]["unattended_scope"] == "session"
    assert "enabled: true" in status_result["content"]
    assert "scope: session" in status_result["content"]
    assert reset_result["permissions"]["unattended"] is False
    assert reset_result["permissions"]["allow_all"] is False


def test_structured_unattended_persists_for_project(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command, initialize_permissions

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    project_result = execute_slash_command("/unattended project", [], permissions, runtime_state, create_initial_messages)
    restored_permissions = {}
    initialize_permissions(restored_permissions)

    project = json.loads((isolated_workspace / ".sema" / "project.json").read_text(encoding="utf-8"))
    assert project["settings"]["unattended"] is True
    assert project_result["permissions"]["unattended_scope"] == "project"
    assert restored_permissions["allow_all"] is True
    assert restored_permissions["unattended_scope"] == "project"

    reset_result = execute_slash_command("/permissions reset", [], restored_permissions, runtime_state, create_initial_messages)
    assert reset_result["permissions"]["allow_all"] is True
    assert reset_result["permissions"]["unattended_scope"] == "project"

    session_result = execute_slash_command("/unattended session", [], restored_permissions, runtime_state, create_initial_messages)
    project = json.loads((isolated_workspace / ".sema" / "project.json").read_text(encoding="utf-8"))
    assert project["settings"]["unattended"] is False
    assert session_result["permissions"]["unattended_scope"] == "session"

    execute_slash_command("/unattended project", [], restored_permissions, runtime_state, create_initial_messages)
    off_result = execute_slash_command("/unattended off", [], restored_permissions, runtime_state, create_initial_messages)
    project = json.loads((isolated_workspace / ".sema" / "project.json").read_text(encoding="utf-8"))
    assert project["settings"]["unattended"] is False
    assert off_result["permissions"]["unattended_scope"] == "interactive"


def test_structured_permissions_allow_all_command_is_removed(isolated_workspace):
    from runtime.agent import create_initial_messages
    from commands.structured_commands import execute_slash_command

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    with pytest.raises(RuntimeError, match="/unattended session"):
        execute_slash_command("/permissions allow-all", [], permissions, runtime_state, create_initial_messages)


def test_structured_notify_commands_persist_config(isolated_workspace):
    from runtime.agent import create_initial_messages
    from model.provider import load_notification_config
    from commands.structured_commands import execute_slash_command

    permissions = {"allow_run_commands": False, "allow_edits": False, "allow_all": False}
    runtime_state = {"continued_session": False, "last_run": [], "verbose": False}

    off_result = execute_slash_command("/notify off", [], permissions, runtime_state, create_initial_messages)
    status_result = execute_slash_command("/notify status", [], permissions, runtime_state, create_initial_messages)
    on_result = execute_slash_command("/notify on", [], permissions, runtime_state, create_initial_messages)

    assert off_result["notifications"]["enabled"] is False
    assert "enabled: false" in status_result["content"]
    assert on_result["notifications"]["enabled"] is True
    assert load_notification_config()["enabled"] is True
