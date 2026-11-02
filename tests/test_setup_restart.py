import os
import subprocess
import time

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe Backend")
def test_real_backend_restarts_and_reconnects_without_local_models(isolated_workspace, monkeypatch):
    from remote.client import BackendClient

    processes = []
    original_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", tracked_popen)
    client = None
    try:
        client = BackendClient(workspace=isolated_workspace)
        before = client.request("initialize", timeout=30)
        assert before["setup"]["ltm_enabled"] is False
        old_connection = client.event_connection
        assert client.request("backend.restart", timeout=10) == {"restarting": True}
        deadline = time.monotonic() + 20
        while client.event_connection is old_connection and time.monotonic() < deadline:
            time.sleep(0.1)
        assert client.event_connection is not old_connection
        after = client.request("initialize", timeout=30)
        assert after["setup"]["ltm_enabled"] is False
        assert not after["setup"]["limited_mode_accepted"]
        assert processes[0].poll() == 0
    finally:
        if client is not None:
            client.close()
            client.reader.join(timeout=10)
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
