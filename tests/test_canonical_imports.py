import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_runtime_packages_import():
    from bridge import ui_bridge
    from commands import command_registry, structured_commands
    from persistence import session
    from runtime import agent, model_runtime
    from tools import tool_registry, tool_schemas

    assert ui_bridge.BridgeServer
    assert command_registry.SLASH_COMMANDS
    assert structured_commands.execute_slash_command
    assert session.save_last_session
    assert agent.answer_with_tools
    assert model_runtime.call_model_with_tools
    assert tool_registry.TOOL_DEFINITIONS
    assert tool_schemas.TOOL_SCHEMAS


def test_root_python_tree_contains_only_entrypoint():
    files = {path.name for path in (ROOT / "my_agent").glob("*.py")}

    assert files == {"main.py"}


def test_existing_project_architecture_field_is_inert(isolated_workspace):
    from persistence import session

    project_dir = isolated_workspace / ".sema"
    project_dir.mkdir()
    project_path = project_dir / "project.json"
    project_path.write_text(
        json.dumps(
            {
                "app": "Sema",
                "project_id": "existing",
                "name": "workspace",
                "root": str(isolated_workspace),
                "memory_architecture": "legacy",
                "created_at": "before",
                "updated_at": "before",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    project = session.ensure_project()

    assert project["memory_architecture"] == "legacy"
    assert session.get_project_info().get("memory_architecture") is None
