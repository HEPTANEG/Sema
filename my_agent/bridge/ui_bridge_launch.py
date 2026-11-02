import os
import subprocess
import shutil
from pathlib import Path


DEFAULT_TUI_MODEL_TIMEOUT_SECONDS = 300
TUI_WORKSPACE_CWD_ENV = "SEMA_TUI_WORKSPACE_CWD"


def launch_tui():
    root = project_root()
    if not shutil.which("node"):
        raise RuntimeError("Node.js is missing from PATH. Install Node.js 24 LTS, open a new terminal, then follow docs/installation.md.")
    if not (root / "tui" / "node_modules").is_dir():
        raise RuntimeError("TUI dependencies are missing. Run npm --prefix tui ci and npm --prefix tui run build from the Sema directory. See docs/installation.md.")
    workspace_cwd = Path.cwd()
    entry = root / "tui" / "dist" / "index.js"
    source_entry = root / "tui" / "src" / "index.tsx"
    if entry.exists():
        command = ["node", str(entry)]
    elif source_entry.exists():
        command = ["npx", "tsx", str(source_entry)]
    else:
        raise RuntimeError("TUI frontend is missing. Expected tui/dist/index.js or tui/src/index.tsx.")
    return subprocess.call(tui_launch_command(command), cwd=root, env=tui_launch_env(workspace_cwd))


def project_root():
    return Path(__file__).resolve().parents[2]


def tui_launch_command(command):
    if os.name != "nt":
        return command
    return ["cmd", "/d", "/c", f"chcp 65001 >nul && {subprocess.list2cmdline(command)}"]


def tui_launch_env(workspace_cwd=None):
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if workspace_cwd is not None:
        env[TUI_WORKSPACE_CWD_ENV] = str(Path(workspace_cwd).resolve())
    return env


def chdir_to_tui_workspace():
    workspace_cwd = os.getenv(TUI_WORKSPACE_CWD_ENV)
    if not workspace_cwd:
        return None
    path = Path(workspace_cwd).expanduser()
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"{TUI_WORKSPACE_CWD_ENV} is not a directory: {workspace_cwd}")
    os.chdir(path)
    return path


def tui_model_timeout_seconds():
    value = os.getenv("SEMA_TUI_MODEL_TIMEOUT_SECONDS", "").strip()
    if not value:
        return DEFAULT_TUI_MODEL_TIMEOUT_SECONDS
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_TUI_MODEL_TIMEOUT_SECONDS
    return max(5, min(parsed, 1800))
