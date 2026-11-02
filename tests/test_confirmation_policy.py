import pytest

from security import confirmation_policy


def test_default_policy_allows_existing_process_permissions():
    assert confirmation_policy.confirm_run_command("pytest", {"allow_run_commands": True}) == {"allowed": True, "reason": ""}
    assert confirmation_policy.confirm_file_mutating_command("Remove-Item x", {"allow_all": True}) == {"allowed": True, "reason": ""}


def test_default_policy_requires_runtime_policy_for_risky_actions():
    with pytest.raises(confirmation_policy.ConfirmationRequired):
        confirmation_policy.confirm_run_command("pytest", {})

    with pytest.raises(confirmation_policy.ConfirmationRequired):
        confirmation_policy.confirm_edit_action("write_file", "path: notes.txt", "allow_edits", {})

    with pytest.raises(confirmation_policy.ConfirmationRequired):
        confirmation_policy.confirm_persistent_action("save memory", "type: project")


def test_default_tool_checkpoint_is_non_interactive():
    assert confirmation_policy.confirm_tool_checkpoint(20) == "Yes"
