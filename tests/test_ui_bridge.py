import io
import json
from pathlib import Path
import threading
import time

import pytest

from bridge import ui_bridge_launch
from bridge import ui_bridge_server
from bridge.headless_runtime import hard_timeout_call_chat_completion, patch_agent_for_headless
from bridge.ui_bridge_server import BridgeServer, JsonRpcError


def read_json_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_ui_bridge_initialize_returns_project_state(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)

    result = server.initialize({})

    assert result["project"]["project_id"]
    assert result["model"].startswith("deepseek:")
    assert result["bridge"] == {"model_timeout_seconds": 300}
    assert result["context_pressure"]["stm_max"] > 0
    assert result["context_pressure"]["wm_max"] > 0
    assert result["notifications"] == {"enabled": True}
    assert any(command["name"] == "/help" for command in result["slash_commands"])


def test_ui_bridge_initialize_restores_project_unattended_mode(isolated_workspace):
    from persistence import session

    session.ensure_project()
    session.save_project_unattended(True)
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())

    result = server.initialize({})

    assert result["permissions"]["allow_all"] is True
    assert result["permissions"]["unattended_scope"] == "project"


def test_ui_bridge_event_notification_shape():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)

    server.send_event("model_thinking", {"path": "structured"}, turn_id="turn-1")

    payload = read_json_lines(output)[0]
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "event.model_thinking"
    assert payload["params"]["turn_id"] == "turn-1"
    assert payload["params"]["type"] == "model_thinking"
    assert payload["params"]["payload"] == {"path": "structured"}
    assert payload["params"]["event_id"]
    assert payload["params"]["sequence"] == 1


def test_ui_bridge_event_sequences_are_monotonic():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)

    server.send_event("model_thinking", {}, turn_id="turn-1")
    server.send_event("tool_started", {"tool": "read_file", "item_id": "tool-1"}, turn_id="turn-1")
    server.send_event("tool_finished", {"tool": "read_file", "status": "ok", "item_id": "tool-1"}, turn_id="turn-1")

    events = read_json_lines(output)
    assert [event["params"]["sequence"] for event in events] == [1, 2, 3]
    assert events[1]["params"]["payload"]["item_id"] == events[2]["params"]["payload"]["item_id"]


def test_ui_bridge_serializes_concurrent_event_writes_in_sequence_order():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    barrier = threading.Barrier(2)

    def emit(prefix):
        barrier.wait()
        for index in range(10):
            server.send_event("model_thinking", {"source": f"{prefix}-{index}"}, turn_id="turn-1")

    threads = [threading.Thread(target=emit, args=(prefix,)) for prefix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = read_json_lines(output)
    sequences = [event["params"]["sequence"] for event in events]
    assert sequences == list(range(1, 21))


def test_ui_bridge_write_escapes_non_ascii_for_windows_stdout():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)

    server.write({"emoji": "😂", "text": "你好"})

    raw = output.getvalue()
    assert "\\ud83d\\ude02" in raw
    assert "\\u4f60\\u597d" in raw
    assert json.loads(raw)["emoji"] == "😂"


def test_tui_launch_command_for_windows_forces_utf8_code_page(monkeypatch):
    monkeypatch.setattr(ui_bridge_launch.os, "name", "nt")

    command = ui_bridge_launch.tui_launch_command(["node", "C:\\Program Files\\Sema TUI\\index.js"])

    assert command[:3] == ["cmd", "/d", "/c"]
    assert "chcp 65001 >nul &&" in command[3]
    assert "node" in command[3]


def test_tui_launch_project_root_points_to_repo_root():
    root = ui_bridge_launch.project_root()

    assert (root / "my_agent").is_dir()
    assert (root / "tui" / "src" / "index.tsx").exists()


def test_tui_launch_command_non_windows_uses_command(monkeypatch):
    monkeypatch.setattr(ui_bridge_launch.os, "name", "posix")

    command = ["node", "index.js"]

    assert ui_bridge_launch.tui_launch_command(command) is command


def test_tui_launch_env_forces_python_utf8(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    env = ui_bridge_launch.tui_launch_env("C:\\workspace")

    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env[ui_bridge_launch.TUI_WORKSPACE_CWD_ENV].endswith("workspace")


def test_chdir_to_tui_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "selected"
    workspace.mkdir()
    monkeypatch.setenv(ui_bridge_launch.TUI_WORKSPACE_CWD_ENV, str(workspace))

    result = ui_bridge_launch.chdir_to_tui_workspace()

    assert result == workspace
    assert Path.cwd() == workspace


def test_ui_bridge_slash_execute_returns_structured_result(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})

    result = server.execute_slash({"command": "/status"})

    assert result["handled"] is True
    assert result["view"] == "commandOutput"
    assert result["title"] == "Status"
    assert "Sema status:" in result["content"]
    assert "stdout" not in result


def test_ui_bridge_slash_execute_accepts_frontend_command_without_leading_slash(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})

    result = server.execute_slash({"command": "status"})

    assert result["handled"] is True
    assert result["view"] == "commandOutput"
    assert result["title"] == "Status"


def test_ui_bridge_delivers_user_input_at_full_ltm_pressure(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation
    from persistence import session

    for index in range(100):
        event_stream.append_message({"role": "user", "content": f"slot {index}"}, root=isolated_workspace)



    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 99)
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})
    monkeypatch.setattr(server, "_preflight_provider", lambda: server.runtime_state.update({"provider_preflight_ok": True}))
    monkeypatch.setattr(ui_bridge_server, "load_model_config", lambda: {"supports_streaming": False})
    called = []
    monkeypatch.setattr(ui_bridge_server.agent, "answer_with_tools", lambda messages, *_args: called.append(messages))

    result = server.send_message({"prompt": "hello"})
    assert result["ok"] is True
    assert called
    assert any(message.get("role") == "user" and message.get("content") == "hello" for message in server.messages)
    assert session.load_last_session() == server.messages
    assert any(item["method"] == "event.user_message" for item in read_json_lines(server.output_stream))


def test_sleep_control_is_dispatched_after_main_turn_releases_locks(isolated_workspace, monkeypatch):
    from runtime.maintenance_sleep import MaintenanceControlRequested

    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    monkeypatch.setattr(ui_bridge_server, "load_model_config", lambda: {"supports_streaming": False})
    monkeypatch.setattr(
        ui_bridge_server.agent,
        "answer_with_tools",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MaintenanceControlRequested({"action": "sleep", "wake_prompt": "Continue the file study at section 8."})),
    )
    observed = []

    def dispatch(action, parent_turn_id=None, parent_request_id=None):
        observed.append((action, server.turn_lock.locked(), parent_turn_id, parent_request_id))

    monkeypatch.setattr(server, "_dispatch_maintenance_control", dispatch)
    result = server.send_message({"prompt": "continue"})

    assert result["suspended"] is True
    assert observed and observed[0][0]["action"] == "sleep"
    assert observed[0][1] is False
    assert any(message.get("role") == "user" and message.get("content") == "continue" for message in server.messages)


def test_maintenance_failure_wake_reports_attempt_count_and_run_id(monkeypatch):
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.runtime_state["maintenance_continuation"] = {
        "status": "running",
        "continuation_id": "cont-1",
        "wake_prompt": "Continue at section 8.",
        "failure_count": 0,
        "run_id": "maintenance_run_1",
    }
    wakes = []
    monkeypatch.setattr(server, "enqueue_wake", lambda *args, **kwargs: wakes.append((args, kwargs)) or True)

    server._maintenance_continuation_failed("provider unavailable", "maintenance_run_1")
    continuation = server.runtime_state["maintenance_continuation"]

    assert continuation["status"] == "failed"
    assert continuation["failure_count"] == 1
    assert continuation["run_id"] == "maintenance_run_1"
    assert wakes[0][0][1] == "maintenance_sleep_failed"
    assert wakes[0][0][2]["wake_prompt"] == "Continue at section 8."
    assert wakes[0][0][2]["failure_count"] == 1


def test_user_cannot_race_a_pending_maintenance_resume_wake(isolated_workspace):
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})
    server.runtime_state["maintenance_continuation"] = {
        "status": "running",
        "continuation_id": "continuation-1",
        "wake_prompt": "Continue at section 8.",
    }

    with pytest.raises(ui_bridge_server.JsonRpcError, match="task is paused"):
        server.send_message({"prompt": "new user input"})


def test_wake_delivery_deferral_has_a_reason_and_retry_count():
    from runtime.wake import wake_event

    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server._pending_message_responses = 1
    event = wake_event("maintainer", "maintenance_task_resumed", {})

    assert server._deliver_wake(event) is False
    assert event.last_delivery_status == "deferred"
    assert event.last_delivery_reason == "pending message response"


def test_wake_delivery_retries_before_agent_loop_without_reappending_wake(monkeypatch):
    from runtime.wake import wake_event

    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.messages = []
    server._ensure_initialized = lambda: None
    server._start_turn = lambda *_args, **_kwargs: None
    server._finish_turn = lambda *_args, **_kwargs: None
    sent_events = []
    server.send_event = lambda event_type, *_args, **_kwargs: sent_events.append(event_type)
    monkeypatch.setattr(ui_bridge_server, "get_project_info", lambda: {"root": "test-root"})

    class Phase:
        def acquire_main(self):
            return True

        def release_main(self):
            return None

    monkeypatch.setattr(ui_bridge_server, "get_agent_phase", lambda *_args: Phase())
    attempts = []

    def run_turn(*_args, on_message_persisted=None, message_already_persisted=False, on_agent_loop_started=None, **_kwargs):
        attempts.append(message_already_persisted)
        if len(attempts) == 1:
            on_message_persisted()
            raise RuntimeError("preflight failed before the Main-Agent loop started")
        on_agent_loop_started()
        return {"ok": True}

    monkeypatch.setattr(server, "_run_message_turn", run_turn)
    event = wake_event(
        "maintainer",
        "maintenance_task_resumed",
        {"continuation_id": "cont-1", "maintenance_run_id": "run-1"},
    )

    assert server._deliver_wake(event) is False
    assert event.message_persisted is True
    assert event.main_agent_started is False
    assert server._deliver_wake(event) is True
    assert attempts == [False, True]
    assert sent_events.count("wake_delivered") == 1
    assert event.main_agent_started is True
    assert server.runtime_state["maintenance_continuation"]["status"] == "resuming"


def test_wake_announcement_failure_retries_same_persisted_message(monkeypatch):
    from runtime.wake import wake_event

    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.messages = []
    server._ensure_initialized = lambda: None
    server._start_turn = lambda *_args, **_kwargs: None
    server._finish_turn = lambda *_args, **_kwargs: None
    monkeypatch.setattr(ui_bridge_server, "get_project_info", lambda: {"root": "test-root"})

    class Phase:
        def acquire_main(self):
            return True

        def release_main(self):
            return None

    monkeypatch.setattr(ui_bridge_server, "get_agent_phase", lambda *_args: Phase())
    announcements = []
    agent_starts = []

    def send_event(event_type, *_args, **_kwargs):
        if event_type == "wake_delivered":
            announcements.append(1)
            if len(announcements) == 1:
                raise RuntimeError("temporary transport failure")

    server.send_event = send_event

    def run_turn(*_args, on_message_persisted=None, message_already_persisted=False, on_agent_loop_started=None, **_kwargs):
        if not message_already_persisted:
            on_message_persisted()
            return {"ok": True}
        on_agent_loop_started()
        agent_starts.append(1)
        return {"ok": True}

    monkeypatch.setattr(server, "_run_message_turn", run_turn)
    event = wake_event("maintainer", "maintenance_task_resumed", {"continuation_id": "cont-2"})

    assert server._deliver_wake(event) is False
    assert event.message_persisted is True
    assert event.wake_announced is False
    assert server._deliver_wake(event) is True
    assert event.wake_announced is True
    assert announcements == [1, 1]
    assert agent_starts == [1]


def test_sleep_continuation_retains_remote_request_provenance(monkeypatch):
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    monkeypatch.setattr(server, "_start_maintenance_continuation", lambda: True)

    server._dispatch_maintenance_control(
        {"action": "sleep", "wake_prompt": "Continue at section 8."},
        "turn-1",
        "request-1",
    )

    continuation = server.runtime_state["maintenance_continuation"]
    assert continuation["parent_turn_id"] == "turn-1"
    assert continuation["parent_request_id"] == "request-1"


def test_ui_bridge_allows_ltm_maintain_slash_when_ltm_pressure_is_full(isolated_workspace, monkeypatch):
    from memory_engine import event_stream

    for index in range(100):
        event_stream.append_message({"role": "user", "content": f"slot {index}"}, root=isolated_workspace)
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})
    monkeypatch.setattr(
        ui_bridge_server,
        "execute_slash_command",
        lambda *_args, **_kwargs: {"handled": True, "view": "ltmMaintenance", "content": "LTM Maintenance:\nstatus: completed"},
    )

    result = server.execute_slash({"command": "/memory ltm maintain"})

    assert result["handled"] is True
    assert result["view"] == "ltmMaintenance"
    assert "status: completed" in result["content"]


def test_ui_bridge_confirmation_round_trip():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    result = {}

    def ask():
        result.update(server.confirmations.confirm_run_command("pytest", {}))

    thread = threading.Thread(target=ask)
    thread.start()
    lines = []
    while not lines:
        lines = read_json_lines(output)
    confirmation = lines[0]["params"]["payload"]

    response = server.confirmation_respond(
        {
            "confirmation_id": confirmation["confirmation_id"],
            "choice": "yes",
        }
    )

    thread.join(timeout=1)
    assert response == {"ok": True}
    assert result == {"allowed": True, "reason": ""}


def test_ui_bridge_allow_edits_updates_empty_permissions_dict():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    permissions = {}
    result = {}

    def ask():
        result.update(server.confirmations.confirm_write("poem1.txt", "content", permissions))

    thread = threading.Thread(target=ask)
    thread.start()
    lines = []
    while not lines:
        lines = read_json_lines(output)
    confirmation = lines[0]["params"]["payload"]
    assert [choice["label"] for choice in confirmation["choices"]] == [
        "Yes",
        "Preview modification",
        "No and explain why",
        "Hold on and discuss",
        "Allow all edits for this session",
    ]
    assert confirmation["choices"][1]["local_preview"] is True

    server.confirmation_respond(
        {
            "confirmation_id": confirmation["confirmation_id"],
            "choice": "allow_edits",
        }
    )

    thread.join(timeout=1)
    assert result == {"allowed": True, "reason": ""}
    assert permissions["allow_edits"] is True
    assert server.confirmations.confirm_write("poem2.txt", "content", permissions) == {"allowed": True, "reason": ""}


def test_ui_bridge_allow_run_commands_updates_empty_permissions_dict():
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    permissions = {}
    result = {}

    def ask():
        result.update(server.confirmations.confirm_run_command("pytest", permissions))

    thread = threading.Thread(target=ask)
    thread.start()
    lines = []
    while not lines:
        lines = read_json_lines(output)
    confirmation = lines[0]["params"]["payload"]

    server.confirmation_respond(
        {
            "confirmation_id": confirmation["confirmation_id"],
            "choice": "allow_run_commands",
        }
    )

    thread.join(timeout=1)
    assert result == {"allowed": True, "reason": ""}
    assert permissions["allow_run_commands"] is True
    assert server.confirmations.confirm_run_command("pytest", permissions) == {"allowed": True, "reason": ""}


def test_ui_bridge_config_profile_update(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)

    result = server.config_use_profile({"name": "kimi"})

    assert result["config"]["provider"] == "kimi"
    assert result["notifications"] == {"enabled": True}
    assert "kimi" in result["profiles"]


def test_ui_bridge_settings_update_uses_allowlisted_actions(isolated_workspace):
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})

    enabled = server.settings_update({"action": "developer.enable"})
    assert enabled["settings"]["developer"]["enabled"] is True
    assert any(command["name"] == "/pwd" for command in enabled["slash_commands"])

    disabled = server.settings_update({"action": "developer.disable"})
    assert disabled["settings"]["developer"]["enabled"] is False
    assert not any(command["name"] == "/pwd" for command in disabled["slash_commands"])

    notifications = server.settings_update({"action": "notifications.disable"})
    assert notifications["notifications"]["enabled"] is False

    with pytest.raises(JsonRpcError, match="Unknown Settings action"):
        server.settings_update({"action": "permissions.allow_all"})


def test_ui_bridge_structured_last_run_and_permissions(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.runtime_state["last_run"] = [{"tool": "read_file", "args": "app.py", "result": "content"}]

    assert server.last_run_get({})["steps"][0]["tool"] == "read_file"

    with pytest.raises(JsonRpcError, match="allow-all was replaced"):
        server.permissions_update({"action": "allow-all"})

    reset = server.permissions_update({"action": "reset"})
    assert reset["permissions"]["allow_all"] is False


def test_ui_bridge_structured_trajectory(isolated_workspace):
    from persistence import session

    session.ensure_project()
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["trajectory_context"] = {"trajectory_id": "trajectory-1"}

    traces = server.trajectory_list({"mode": "current"})

    assert traces["trajectory_id"] == "trajectory-1"
    assert traces["trajectories"] == []


def test_ui_bridge_removed_session_rpc_methods_are_not_registered(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})

    with pytest.raises(JsonRpcError, match="Method not found: sessions.list"):
        server.dispatch("sessions.list", {})
    with pytest.raises(JsonRpcError, match="Method not found: session.switch"):
        server.dispatch("session.switch", {"conversation_id": "old"})


def test_headless_runtime_patch_uses_short_tui_model_timeout(monkeypatch):
    from runtime import agent

    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    original_timeout = agent.MODEL_TIMEOUT_SECONDS
    original_retries = agent.MAX_MODEL_RETRIES_PER_FAILURE_POINT

    with patch_agent_for_headless(server.confirmations, timeout_seconds=17, max_model_retries=0):
        assert agent.MODEL_TIMEOUT_SECONDS == 17
        assert agent.MODEL_TIMEOUT_SECONDS == 17
        assert agent.MAX_MODEL_RETRIES_PER_FAILURE_POINT == 0

    assert agent.MODEL_TIMEOUT_SECONDS == original_timeout
    assert agent.MAX_MODEL_RETRIES_PER_FAILURE_POINT == original_retries


def test_hard_timeout_call_chat_completion_returns_before_hung_provider():
    def hung_call(_messages, **_kwargs):
        time.sleep(10)
        return {"choices": [{"message": {"content": "late"}}]}

    wrapped = hard_timeout_call_chat_completion(hung_call, 1)
    started_at = time.perf_counter()

    try:
        wrapped([{"role": "user", "content": "hello"}], timeout_seconds=1, purpose="test")
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected hard timeout.")

    assert "TUI test request timed out after 1 seconds." == message
    assert time.perf_counter() - started_at < 2


def test_send_message_has_no_turn_level_timeout(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    monkeypatch.setattr(ui_bridge_server, "tui_model_timeout_seconds", lambda: 1)

    def hung_turn(_prompt, _turn_id):
        time.sleep(1.2)
        return {"ok": True}

    monkeypatch.setattr(server, "_run_message_turn", hung_turn)
    started_at = time.perf_counter()

    result = server.send_message({"prompt": "hello"})

    assert result["ok"] is True
    assert time.perf_counter() - started_at >= 1
    events = read_json_lines(output)
    assert all("timed out" not in json.dumps(event) for event in events)


def test_eval_message_is_queued_and_returns_request_metadata(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.permissions.update({"allow_edits": True, "allow_run_commands": True})
    server.runtime_state["provider_preflight_ok"] = True

    monkeypatch.setattr(server, "_run_message_turn", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(ui_bridge_server.maintenance_gate, "user_turn_gate_status", lambda: {"blocked": False, "pressure": {}})

    accepted = server.send_message({"prompt": "hello", "source": "eval", "request_id": "req-1"})
    assert accepted["accepted"] is True
    assert accepted["request_id"] == "req-1"

    completed = server.wait_eval_request("req-1", timeout=2)
    assert completed["status"] == "completed"
    assert completed["result"]["request_id"] == "req-1"
    assert completed["result"]["turn_id"]
    assert server.runtime_status({})["eval_queue_size"] == 0


def test_send_message_echoes_user_message_before_preflight(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    observed = {}

    def fake_preflight():
        observed["events_during_preflight"] = read_json_lines(output)
        server.runtime_state["provider_preflight_ok"] = True

    monkeypatch.setattr(server, "_preflight_provider", fake_preflight)
    monkeypatch.setattr(
        ui_bridge_server.agent,
        "answer_with_tools",
        lambda messages, _permissions, _runtime_state: messages.append({"role": "assistant", "content": "done"}),
    )

    result = server.send_message({"prompt": "hello"})

    assert result["ok"] is True
    events = observed["events_during_preflight"]
    user_event = next(event for event in events if event["method"] == "event.user_message")
    stage_event = next(event for event in events if event["method"] == "event.assistant_stage")
    assert user_event["params"]["payload"]["content"] == "hello"
    assert events.index(user_event) < events.index(stage_event)


def test_message_turn_has_no_intent_routing_stage(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    monkeypatch.setattr(ui_bridge_server, "load_model_config", lambda: {"supports_streaming": True})

    def fake_answer(messages, _permissions, _runtime_state):
        assert _runtime_state["streaming_enabled"] is True
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(ui_bridge_server.agent, "answer_with_tools", fake_answer)

    result = server.send_message({"prompt": "hello"})

    assert result["ok"] is True
    events = read_json_lines(output)
    methods = [event["method"] for event in events]
    assert "event.assistant_stage" in methods
    stage = next(event for event in events if event["method"] == "event.assistant_stage")
    fallback = next(event for event in events if event["method"] == "event.final_answer")
    assert stage["params"]["payload"]["message_id"].startswith("assistant-")
    assert fallback["params"]["payload"]["message_id"] == stage["params"]["payload"]["message_id"]
    assert stage["params"]["payload"]["label"] == "Preparing your request..."
    assert "streaming_enabled" not in server.runtime_state


def test_message_turn_respects_streaming_disabled_provider(monkeypatch, isolated_workspace):
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    monkeypatch.setattr(ui_bridge_server, "load_model_config", lambda: {"supports_streaming": False})

    def fake_answer(_messages, _permissions, runtime_state):
        assert runtime_state["streaming_enabled"] is False
        _messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(ui_bridge_server.agent, "answer_with_tools", fake_answer)

    result = server.send_message({"prompt": "hello"})

    assert result["ok"] is True
    assert "streaming_enabled" not in server.runtime_state


def test_message_turn_does_not_use_semantic_temporal_routing(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True

    def fake_answer(messages, _permissions, _runtime_state):
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(ui_bridge_server.agent, "answer_with_tools", fake_answer)

    result = server.send_message({"prompt": "research this year"})

    assert result["ok"] is True
    events = read_json_lines(output)
    methods = [event["method"] for event in events]
    assert "event.temporal_routing_finished" not in methods
    assert all(
        event.get("params", {}).get("payload", {}).get("label") != "Sema is checking time context..."
        for event in events
    )


def test_send_message_rejects_concurrent_turn(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    release = threading.Event()

    def blocked_turn(_prompt, _turn_id):
        release.wait(timeout=2)
        return {"ok": True}

    monkeypatch.setattr(server, "_run_message_turn", blocked_turn)
    thread = threading.Thread(target=lambda: server.send_message({"prompt": "first"}))
    thread.start()
    while server.current_turn_id is None:
        time.sleep(0.01)

    try:
        server.send_message({"prompt": "second"})
    except JsonRpcError as error:
        message = error.message
    else:
        raise AssertionError("Expected concurrent turn rejection.")
    finally:
        release.set()
        thread.join(timeout=2)

    assert message == "A turn is already running. Use turn.cancel or wait for completion."


def test_session_state_reports_running_turn(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server._start_turn("turn-1")
    server.send_event("model_thinking", {"path": "structured"}, turn_id="turn-1")

    state = server.session_state({})

    assert state["turn_status"] == "running"
    assert state["active_turn_id"] == "turn-1"
    assert state["last_event_type"] == "model_thinking"
    assert state["elapsed_seconds"] >= 0
    assert state["idle_seconds"] >= 0


def test_turn_cancel_sets_cancel_flag_and_emits_event(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server._start_turn("turn-1")

    result = server.turn_cancel({})

    assert result == {"ok": True, "active_turn_id": "turn-1"}
    assert server.runtime_state["cancel_requested"] is True
    payload = read_json_lines(output)[-1]
    assert payload["method"] == "event.cancel_requested"


def test_agent_loop_honors_cancel_before_model_call():
    from runtime import agent

    try:
        agent.answer_with_structured_tools([], runtime_state={"cancel_requested": True})
    except agent.ToolRunStopped as error:
        message = str(error)
    else:
        raise AssertionError("Expected cancellation to stop the agent loop.")

    assert message == "Turn cancelled by user."


def test_preflight_provider_surfaces_auth_errors(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})

    def fake_call(_messages, **_kwargs):
        raise RuntimeError("deepseek auth failed")

    monkeypatch.setattr(ui_bridge_server.model_provider, "call_chat_completion", fake_call)

    try:
        server.send_message({"prompt": "hello"})
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected preflight error.")

    assert message == "deepseek auth failed"
    payloads = read_json_lines(output)
    assert payloads[-1]["method"] == "event.agent_error"
    assert payloads[-1]["params"]["payload"]["error"] == "deepseek auth failed"


def test_stm_context_error_runs_after_intent_routing(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    marks = []

    monkeypatch.setattr(
        ui_bridge_server.agent,
        "outbound_context_error_message",
        lambda _messages, _runtime_state=None: "Context build failed: STM latest interaction block exceeds budget.",
    )
    monkeypatch.setattr(ui_bridge_server, "mark_assistant_turn_finished", lambda _runtime_state: marks.append(True))

    result = server._run_message_turn("oversized input", "turn-1")

    assert result["ok"] is True
    assert "intent" not in server.runtime_state
    assert marks == [True]
    assert server.messages[-2] == {"role": "user", "content": "oversized input"}
    assert server.messages[-1]["role"] == "assistant"
    assert "Context build failed:" in server.messages[-1]["content"]
    payloads = read_json_lines(output)
    final = [payload for payload in payloads if payload["method"] == "event.final_answer"][-1]
    assert final["params"]["payload"]["source"] == "context_build_error"
    assert "Context build failed:" in final["params"]["payload"]["content"]


def test_tool_run_stopped_visible_reply_marks_assistant_finished(monkeypatch, isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    marks = []

    monkeypatch.setattr(ui_bridge_server.agent, "outbound_context_error_message", lambda _messages, _runtime_state=None: "")
    monkeypatch.setattr(
        ui_bridge_server.agent,
        "answer_with_tools",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ui_bridge_server.agent.ToolRunStopped("Turn cancelled by user.")),
    )
    monkeypatch.setattr(ui_bridge_server, "mark_assistant_turn_finished", lambda _runtime_state: marks.append(True))

    result = server._run_message_turn("stop", "turn-1")

    assert result["ok"] is True
    assert marks == [True]
    assert server.messages[-1] == {"role": "assistant", "content": "Turn cancelled by user."}


def test_unexpected_turn_error_emits_agent_error_and_fails_trajectory(monkeypatch, isolated_workspace):
    from persistence.trajectory import get_trajectory

    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["provider_preflight_ok"] = True
    monkeypatch.setattr(ui_bridge_server.agent, "outbound_context_error_message", lambda _messages, _runtime_state=None: "")
    monkeypatch.setattr(
        ui_bridge_server.agent,
        "answer_with_tools",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unexpected provider transport failure")),
    )

    with pytest.raises(ValueError, match="unexpected provider transport failure"):
        server._run_message_turn("hello", "turn-1")

    payloads = read_json_lines(output)
    error_event = [payload for payload in payloads if payload["method"] == "event.agent_error"][-1]
    assert error_event["params"]["payload"]["error"] == "unexpected provider transport failure"
    trajectory = get_trajectory(server.runtime_state["trajectory_context"]["trajectory_id"])
    assert trajectory["trajectory"]["status"] == "failed"


def test_final_answer_fallback_emits_last_assistant_message(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.messages.extend(
        [
            {"role": "user", "content": "hello"},
            {"role": "tool", "content": "tool result"},
            {"role": "assistant", "content": "fallback answer"},
            {"role": "tool", "content": "late tool"},
        ]
    )

    server._send_final_answer_fallback("turn-1", start_index=1)

    payload = read_json_lines(output)[-1]
    assert payload["method"] == "event.final_answer"
    assert payload["params"]["turn_id"] == "turn-1"
    assert payload["params"]["payload"]["content"] == "fallback answer"
    assert payload["params"]["payload"]["source"] == "bridge_fallback"


def test_final_answer_fallback_skips_when_agent_event_was_sent(isolated_workspace):
    output = io.StringIO()
    server = BridgeServer(input_stream=io.StringIO(), output_stream=output)
    server.initialize({})
    server.runtime_state["ui_final_answer_sent"] = True
    server.messages.append({"role": "assistant", "content": "fallback answer"})
    before = len(read_json_lines(output))

    server._send_final_answer_fallback("turn-1")

    assert len(read_json_lines(output)) == before
