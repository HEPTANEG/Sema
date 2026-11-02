from pathlib import Path

from bridge.remote_receipts import RemoteReceiptStore
from remote.service import TASK_NAME, task_xml
from remote.state import GatewayState
from remote.gateway import _polling_heartbeat_stale
from remote.transport import PollingLiveness


def test_gateway_state_deduplicates_inbound_updates(tmp_path):
    state = GatewayState(tmp_path)
    first = state.record_inbound(42, 7, 9, "hello")
    again = state.record_inbound(42, 7, 9, "hello")

    assert first["request_id"] == "telegram-update-42"
    assert again == first
    assert len(state.pending()) == 1


def test_gateway_state_keeps_completed_delivery_out_of_pending(tmp_path):
    state = GatewayState(tmp_path)
    entry = state.record_inbound(42, 7, 9, "hello")
    state.complete_delivery(entry["request_id"])

    assert state.pending() == []
    assert state.snapshot()["pending_count"] == 0


def test_gateway_state_completes_all_updates_in_one_batch(tmp_path):
    state = GatewayState(tmp_path)
    first = state.record_inbound(42, 7, 9, "hello")
    second = state.record_inbound(43, 7, 10, "world")
    state.assign_batch([first["request_id"], second["request_id"]], "telegram-batch-42-43")
    state.complete_batch("telegram-batch-42-43")

    assert state.pending() == []
    assert state.snapshot()["pending_count"] == 0


def test_backend_receipt_survives_duplicate_and_marks_restart_interrupted(tmp_path):
    receipts = RemoteReceiptStore(tmp_path)
    receipts.begin("telegram-update-42", "turn-a")
    assert receipts.begin("telegram-update-42", "turn-b")["turn_id"] == "turn-a"

    restarted = RemoteReceiptStore(tmp_path)
    result = restarted.get("telegram-update-42")
    assert result["status"] == "interrupted"
    assert "Backend restarted" in result["error"]


def test_backend_receipt_persists_final_content(tmp_path):
    receipts = RemoteReceiptStore(tmp_path)
    receipts.begin("telegram-update-42", "turn-a")
    receipts.complete("telegram-update-42", "done")

    assert RemoteReceiptStore(tmp_path).get("telegram-update-42")["final_content"] == "done"


def test_polling_liveness_is_fresh_after_success(monkeypatch):
    ticks = iter((100.0, 103.0, 103.0))
    monkeypatch.setattr("remote.transport.time.monotonic", lambda: next(ticks))
    liveness = PollingLiveness()
    liveness.mark_success()
    assert liveness.seconds_since_success() == 0.0


def test_external_heartbeat_watchdog_detects_stale_worker(tmp_path):
    state = GatewayState(tmp_path)
    state.health(last_polling_success="2026-01-01T00:00:00+00:00")

    assert _polling_heartbeat_stale(tmp_path, started_at=1767225600, now=1767225721)


def test_scheduled_task_xml_uses_fixed_workspace_and_no_credentials():
    xml = task_xml(Path.home(), user_identity="LAPTOP\\ExampleUser")
    assert TASK_NAME not in xml
    assert "remote service --workspace" in xml
    assert str(Path.home()) in xml
    assert "<Command>" in xml and "main.py" in xml
    assert "cmd.exe" not in xml
    assert "SEMA_TELEGRAM_BOT_TOKEN" not in xml
    assert "StartWhenAvailable" in xml
    assert "<UserId>LAPTOP\\ExampleUser</UserId>" in xml


def test_restart_ends_existing_task_before_starting(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("remote.service._run", lambda *args: calls.append(args) or Result())
    monkeypatch.setattr("remote.service._task_is_running", lambda: False)
    monkeypatch.setattr("remote.service._stop_orphaned_service_processes", lambda: calls.append("orphans"))
    from remote.service import restart
    restart()

    assert calls[0][1] == "/End"
    assert calls[1] == "orphans"
    assert calls[2][1] == "/Run"


def test_restart_waits_for_task_to_finish_before_starting(monkeypatch):
    from remote import service
    calls = []
    states = iter((True, False, False))

    monkeypatch.setattr(service, "_task_is_running", lambda: next(states))
    monkeypatch.setattr(service, "_stop_orphaned_service_processes", lambda: calls.append("orphans"))
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: calls.append("wait"))
    monkeypatch.setattr(service, "_run", lambda *args: calls.append(args) or _result(0, ""))

    service.restart()

    assert calls[0][1] == "/End"
    assert calls[1] == "orphans"
    assert calls[2] == "wait"
    assert calls[3][1] == "/Run"


def test_windows_command_runner_uses_active_console_encoding(monkeypatch):
    from remote import service

    observed = {}
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: observed.update(kwargs) or None)
    monkeypatch.setattr(service.locale, "getpreferredencoding", lambda _do_setlocale: "cp936")
    service._run("schtasks.exe", "/Query")

    assert observed["encoding"] == "cp936"


def test_install_explains_task_scheduler_access_denied(monkeypatch, tmp_path):
    from remote import service

    monkeypatch.setattr(service, "persistent_credentials_present", lambda: True)
    monkeypatch.setattr(service, "task_xml", lambda: "<Task />")
    monkeypatch.setattr(service.tempfile, "NamedTemporaryFile", lambda *args, **kwargs: _temporary_task_file(tmp_path))
    monkeypatch.setattr(service, "_run", lambda *args: _result(1, "ERROR: Access is denied."))

    import pytest
    with pytest.raises(RuntimeError, match="Administrator"):
        service.install()


class _temporary_task_file:
    def __init__(self, root):
        self.name = str(root / "task.xml")
    def __enter__(self):
        self.handle = open(self.name, "w", encoding="utf-16")
        return self.handle
    def __exit__(self, *args):
        self.handle.close()


def _result(returncode, stderr):
    return type("Result", (), {"returncode": returncode, "stdout": "", "stderr": stderr})()
