import sys

from bridge.headless_runtime import hard_timeout_call_chat_completion as _hard_timeout_call_chat_completion
from bridge.ui_bridge_launch import (
    TUI_WORKSPACE_CWD_ENV,
    chdir_to_tui_workspace as _chdir_to_tui_workspace,
    launch_tui,
    tui_launch_command as _tui_launch_command,
    tui_launch_env as _tui_launch_env,
    tui_model_timeout_seconds as _tui_model_timeout_seconds,
)
from bridge.ui_bridge_server import BridgeServer, JsonRpcError


def main(argv=None):
    _ = argv or []
    BridgeServer().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
