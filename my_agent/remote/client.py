import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from bridge.backend_transport import connect_pipe, pipe_name


class BackendRpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class BackendClient:
    """Python client using one event stream and one short-lived RPC connection per call."""

    def __init__(self, workspace=None, event_handler=None):
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.event_handler = event_handler
        self.event_connection = None
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.closed = False
        self._connect_with_backend()
        self.reader = threading.Thread(target=self._read_event_loop, daemon=True)
        self.reader.start()

    def _connect_with_backend(self):
        try:
            self.event_connection = connect_pipe(pipe_name(self.workspace), blocking=False)
            return
        except Exception:
            pass

        env = dict(os.environ)
        env["SEMA_TUI_WORKSPACE_CWD"] = str(self.workspace)
        entry = Path(__file__).resolve().parents[1] / "main.py"
        subprocess.Popen(
            [sys.executable, str(entry), "backend"],
            cwd=str(self.workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        last_error = None
        for _ in range(40):
            try:
                self.event_connection = connect_pipe(pipe_name(self.workspace), timeout_ms=500, blocking=False)
                return
            except Exception as error:
                last_error = error
                time.sleep(0.1)
        raise RuntimeError(f"Sema Backend did not start: {last_error}")

    def _next_id(self):
        with self.id_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def request(self, method, params=None, timeout=600):
        if self.closed:
            raise RuntimeError("Sema Backend client is closed")
        request_id = self._next_id()
        connection = connect_pipe(pipe_name(self.workspace), timeout_ms=5000, blocking=False)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        deadline = time.time() + timeout
        try:
            connection.write(payload)
            buffer = b""
            for chunk in connection.read_chunks(poll=True):
                if chunk:
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if not raw.strip():
                            continue
                        response = json.loads(raw.decode("utf-8"))
                        if response.get("method", "").startswith("event."):




                            continue
                        if response.get("id") != request_id:
                            continue
                        error = response.get("error")
                        if error:
                            raise BackendRpcError(error.get("code", -32000), error.get("message", "Backend error"))
                        return response.get("result")
                if time.time() >= deadline:
                    raise TimeoutError(f"Backend request timed out: {method}")
            raise RuntimeError("Sema Backend connection closed before the response")
        finally:
            connection.close()

    def _read_event_loop(self):
        while not self.closed:
            buffer = b""
            try:
                for chunk in self.event_connection.read_chunks():
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if raw.strip():
                            payload = json.loads(raw.decode("utf-8"))
                            if payload.get("method", "").startswith("event."):
                                self._handle_event(payload.get("params") or {})
            except Exception:
                pass
            if self.closed:
                return
            self.event_connection.close()
            for _ in range(30):
                if self.closed:
                    return
                time.sleep(1)
                try:
                    self._connect_with_backend()
                    self.request("initialize")
                    break
                except Exception:
                    if self.event_connection is not None:
                        self.event_connection.close()
                    continue
            else:
                return

    def _handle_event(self, event):
        if self.event_handler is not None:
            self.event_handler(event)

    def close(self):
        self.closed = True
        if self.event_connection is not None:
            self.event_connection.close()
