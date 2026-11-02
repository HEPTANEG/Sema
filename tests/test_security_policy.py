from security import security_policy
from tools import tool_schemas


def test_read_file_workspace_path_is_read_local(isolated_workspace):
    (isolated_workspace / "notes.txt").write_text("hello", encoding="utf-8")

    decision = security_policy.classify_tool_request({"tool": "read_file", "args": "notes.txt"})

    assert decision.risk == "read_local"
    assert decision.needs_confirmation is False


def test_read_file_external_path_is_read_external(isolated_workspace, tmp_path):
    external = tmp_path / "outside" / "notes.txt"
    external.parent.mkdir()
    external.write_text("hello", encoding="utf-8")

    decision = security_policy.classify_tool_request({"tool": "read_file", "args": str(external)})

    assert decision.risk == "read_external"
    assert decision.needs_confirmation is True
    assert str(external) in decision.details


def test_read_file_dict_args_external_path_is_read_external(isolated_workspace, tmp_path):
    external = tmp_path / "outside" / "notes.txt"
    external.parent.mkdir()
    external.write_text("hello", encoding="utf-8")

    decision = security_policy.classify_tool_request(
        {"tool": "read_file", "args": {"path": str(external), "start_line": 1, "line_count": 10}}
    )

    assert decision.risk == "read_external"
    assert decision.needs_confirmation is True
    assert str(external) in decision.details


def test_get_overview_external_path_is_read_external(isolated_workspace, tmp_path):
    external = tmp_path / "outside" / "notes.txt"
    external.parent.mkdir()
    external.write_text("hello", encoding="utf-8")

    decision = security_policy.classify_tool_request(
        {"tool": "get_overview", "args": {"path": str(external), "instruction": "Map it."}}
    )

    assert decision.risk == "read_external"
    assert decision.needs_confirmation is True
    assert str(external) in decision.details


def test_write_file_workspace_path_is_edit_local(isolated_workspace):
    decision = security_policy.classify_tool_request({"tool": "write_file", "args": "app.py"})

    assert decision.risk == "edit_local"
    assert decision.needs_confirmation is True


def test_write_file_external_path_is_edit_external(isolated_workspace, tmp_path):
    external = tmp_path / "outside" / "app.py"

    decision = security_policy.classify_tool_request({"tool": "write_file", "args": str(external)})

    assert decision.risk == "edit_external"
    assert decision.needs_confirmation is True
    assert str(external) in decision.details


def test_allow_all_bypasses_confirmation_but_keeps_external_flag(isolated_workspace, tmp_path):
    external = tmp_path / "outside" / "app.py"

    decision = security_policy.classify_tool_request(
        {"tool": "write_file", "args": str(external)},
        permissions={"allow_all": True},
    )

    assert decision.risk == "edit_external"
    assert decision.needs_confirmation is False
    assert decision.allow_external is True


def test_run_command_risk_classes():
    assert security_policy.classify_tool_request({"tool": "run_command", "args": "python --version"}).risk == "run_command"
    assert security_policy.classify_tool_request({"tool": "run_command", "args": "Remove-Item notes.txt"}).risk == "dangerous_command"
    assert security_policy.classify_tool_request({"tool": "run_command", "args": "echo hi > notes.txt"}).risk == "dangerous_command"
    decision = security_policy.classify_tool_request({"tool": "start_background_command", "args": {"command": "npm run dev"}})
    assert decision.risk == "run_command"
    assert decision.confirmation_action == "start_background_command"


def test_model_requested_git_undo_command_is_blocked():
    decision = security_policy.classify_tool_request({"tool": "run_command", "args": "git checkout -- app.py"})

    assert decision.risk == "dangerous_command"
    assert decision.blocked is True
    assert "undo_edit" in decision.reason


def test_known_tool_categories():
    assert security_policy.classify_tool_request({"tool": "web_search", "args": {"query": "Sema"}}).risk == "network_read"
    assert security_policy.classify_tool_request({"tool": "web_fetch", "args": "https://example.com"}).risk == "network_read"
    assert security_policy.classify_tool_request({"tool": "list_background_commands", "args": ""}).risk == "safe_read"
    assert security_policy.classify_tool_request({"tool": "read_background_command_output", "args": {"command_id": "abc"}}).risk == "safe_read"
    assert security_policy.classify_tool_request({"tool": "stop_background_command", "args": "abc"}).risk == "runtime_state"
    assert security_policy.classify_tool_request({"tool": "show_memory_state", "args": ""}).risk == "safe_read"
    assert security_policy.classify_tool_request({"tool": "show_undo_records", "args": 5}).risk == "safe_read"
    assert security_policy.classify_tool_request({"tool": "undo_edit", "args": ""}).risk == "undo"


def test_unknown_tool_is_blocked():
    decision = security_policy.classify_tool_request({"tool": "unknown_tool", "args": ""})

    assert decision.risk == "unknown"
    assert decision.blocked is True


def test_all_structured_tools_have_explicit_risk_classification():
    for name in tool_schemas.tool_schema_names():
        decision = security_policy.classify_tool_request({"tool": name, "args": ""})

        assert decision.risk != "unknown", name
        assert decision.blocked is False, name
