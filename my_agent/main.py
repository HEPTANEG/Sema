import os
from pathlib import Path
import sys

try:
    from runtime.agent import answer_with_tools, create_initial_messages
except ModuleNotFoundError as error:
    raise SystemExit(f"Missing Sema dependency: {error.name}. Install requirements.txt with the Python used to launch Sema. See docs/installation.md.") from error
from persistence.trajectory import start_trajectory
from persistence.session import append_message_and_persist
from agents.temporal_state import mark_turn_started


APP_DIR = Path(__file__).resolve().parent


def load_env_file():
    env_path = APP_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_agent_turn(prompt, messages, permissions, runtime_state):
    runtime_state["turn_start_index"] = len(messages)
    runtime_state.pop("trajectory_context", None)
    runtime_state.pop("trace_id", None)
    mark_turn_started(runtime_state)
    append_message_and_persist(messages, {"role": "user", "content": prompt})
    runtime_state["trajectory_context"] = start_trajectory("main_agent")
    runtime_state["trace_id"] = runtime_state["trajectory_context"].get("trajectory_id")
    answer_with_tools(messages, permissions, runtime_state)


def main():
    load_env_file()

    if len(sys.argv) == 1 or (len(sys.argv) > 1 and sys.argv[1] == "tui"):
        from bridge.ui_bridge import launch_tui

        raise SystemExit(launch_tui())

    if len(sys.argv) > 1 and sys.argv[1] == "ui-bridge":
        from bridge.ui_bridge import main as ui_bridge_main

        raise SystemExit(ui_bridge_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "backend":
        from bridge.backend_server import BackendServer
        from bridge.ui_bridge_launch import chdir_to_tui_workspace

        chdir_to_tui_workspace()
        raise SystemExit(BackendServer().run() or 0)

    if len(sys.argv) > 1 and sys.argv[1] == "remote":
        from remote.gateway import main as remote_main

        raise SystemExit(remote_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "eval-bridge":
        from eval.eval_bridge import main as eval_bridge_main

        raise SystemExit(eval_bridge_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        print("Error: `sema eval` has moved to the external `sema-eval` command.", file=sys.stderr)
        raise SystemExit(1)

    print(
        "Error: Sema's Python interactive CLI commands have been removed. Use `sema` or `sema tui`; use slash commands such as `/memory status` and `/config` inside the TUI.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except ModuleNotFoundError as error:
        raise SystemExit(f"Missing Python dependency: {error.name}. Run python -m pip install -r requirements.txt from the Sema installation directory. See docs/installation.md.") from None
