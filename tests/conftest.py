from pathlib import Path
import sys

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
MY_AGENT_DIR = ROOT_DIR / "my_agent"


if str(MY_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(MY_AGENT_DIR))


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()


    monkeypatch.chdir(workspace)
    monkeypatch.setenv("SEMA_HOME", str(home))

    from persistence import session


    session._active_project = None
    session._active_conversation_id = None
    yield workspace

    session._active_project = None
    session._active_conversation_id = None


@pytest.fixture(autouse=True)
def isolate_setup_runtime(monkeypatch):
    from runtime import setup_capabilities
    monkeypatch.setattr(setup_capabilities, "_enabled", True)
    monkeypatch.setattr(setup_capabilities, "_managed", False)
    monkeypatch.setattr(setup_capabilities, "_accepted", False)
    monkeypatch.setattr(setup_capabilities, "_threads", [])


@pytest.fixture(autouse=True)
def disable_real_ltm_vector_sync_by_default(request, monkeypatch):
    if request.node.get_closest_marker("real_ltm_vector_sync"):
        yield
        return

    from memory_engine.ltm import ltm_store

    monkeypatch.setattr(ltm_store, "_stage_vector_objects", lambda *_args, **_kwargs: None)
    yield


@pytest.fixture(autouse=True)
def isolate_trajectory_storage(tmp_path, monkeypatch):
    """Unit tests must never migrate or write the real project's diagnostics."""
    from persistence import trajectory

    base = tmp_path / "trajectory-project"
    monkeypatch.setattr(
        trajectory,
        "trajectory_project_root",
        lambda root=None: Path(root) if root is not None else base,
    )
    trajectory.reset_trajectory_state_for_tests()
    yield
    trajectory.reset_trajectory_state_for_tests()
