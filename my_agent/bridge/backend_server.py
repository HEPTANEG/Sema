"""Shared multi-client Backend/Core service."""

import json
import hashlib
import threading
import time

import pywintypes
import win32api
import win32event
import win32pipe

from bridge.backend_transport import NamedPipeConnection, create_server_pipe, pipe_name, set_blocking_mode
from bridge.remote_receipts import RemoteReceiptStore
from bridge.ui_bridge_server import BridgeServer, JSONRPC_VERSION, JsonRpcError


class BackendServer(BridgeServer):
    """Own one Runtime state and expose it to multiple local clients."""

    def __init__(self, input_stream=None, output_stream=None, pipe=None):
        super().__init__(input_stream=input_stream, output_stream=output_stream)
        self.pipe = pipe or pipe_name()
        self.clients = set()
        self.clients_lock = threading.Lock()
        self.initialize_lock = threading.Lock()
        self.backend_initialized = False
        self.stop_event = threading.Event()
        self.idle_timeout_seconds = 30
        self.idle_since = time.time()
        self.instance_mutex = None
        self.remote_receipts = RemoteReceiptStore()
        self.remote_request_ids = {}
        self.restart_pending = False
        self.request_count = 0
        self.request_count_lock = threading.RLock()

    def run(self):
        mutex_name = r"Local\SemaBackend-" + hashlib.sha256(self.pipe.encode("utf-8")).hexdigest()[:24]
        self.instance_mutex = win32event.CreateMutex(None, False, mutex_name)
        if win32api.GetLastError() == 183:
            win32file_close_handle(self.instance_mutex)
            self.instance_mutex = None
            return
        try:
            while not self.stop_event.is_set():
                handle = create_server_pipe(self.pipe)
                try:
                    connected = False
                    while not connected and not self.stop_event.is_set():
                        try:
                            win32pipe.ConnectNamedPipe(handle, None)
                            connected = True
                        except pywintypes.error as error:
                            if error.winerror == 535:
                                connected = True
                            elif error.winerror == 536:
                                if self._idle_expired():
                                    self.stop_event.set()
                                    break
                                time.sleep(0.1)
                            else:
                                raise
                    if not connected:
                        self._close_handle(handle)
                        break
                    set_blocking_mode(handle)
                    connection = NamedPipeConnection(handle)
                    with self.clients_lock:
                        self.clients.add(connection)
                        self.idle_since = None
                    threading.Thread(target=self._serve_client, args=(connection,), daemon=True).start()
                except Exception:
                    self._close_handle(handle)
                    raise
        except KeyboardInterrupt:
            self.stop_event.set()
        finally:
            from model.provider import close_provider_connections

            close_provider_connections()
            if self.instance_mutex is not None:
                win32file_close_handle(self.instance_mutex)
                self.instance_mutex = None

    @staticmethod
    def _close_handle(handle):
        win32file_close_handle(handle)

    def _serve_client(self, connection):
        buffer = b""
        workers = []
        try:
            for chunk in connection.read_chunks():
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if raw.strip():



                        worker = threading.Thread(
                            target=self._handle_client_payload,
                            args=(connection, raw),
                            daemon=True,
                        )
                        workers.append(worker)
                        worker.start()
        finally:
            with self.clients_lock:
                self.clients.discard(connection)
                if not self.clients and self.backend_initialized:
                    self.idle_since = time.time()
            connection.close()

    def _idle_expired(self):
        with self.clients_lock:
            return self.idle_since is not None and time.time() - self.idle_since >= self.idle_timeout_seconds

    def _handle_client_payload(self, connection, raw):
        request_id = None
        with self.request_count_lock:
            self.request_count += 1
        try:
            payload = json.loads(raw.decode("utf-8"))
            request_id = payload.get("id")
            if payload.get("jsonrpc") != JSONRPC_VERSION:
                raise JsonRpcError(-32600, "Invalid Request")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "Params must be an object")
            result = self.dispatch(payload.get("method"), params)
            if request_id is not None:
                connection.write({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})
            if payload.get("method") == "backend.restart" and self.restart_pending:
                self.stop_event.set()
                with self.clients_lock:
                    clients = list(self.clients)
                for client in clients:
                    client.close()
        except JsonRpcError as error:
            if request_id is not None:
                connection.write({
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request_id,
                    "error": {"code": error.code, "message": error.message, "data": error.data},
                })
        except Exception as error:
            if request_id is not None:
                connection.write({
                    "jsonrpc": JSONRPC_VERSION,
                    "id": request_id,
                    "error": {"code": -32000, "message": str(error)},
                })

        finally:
            with self.request_count_lock:
                self.request_count -= 1

    def dispatch(self, method, params):
        from runtime.setup_capabilities import background_busy
        from runtime.setup_config import TEST_LOCK
        from tools.background_commands import has_running_commands
        with self.request_count_lock:
            if self.restart_pending:
                raise JsonRpcError(-32010, "Backend is restarting; retry after reconnecting.")
            if method == "backend.restart":
                from memory_engine.ltm.memory_retrieval import SESSION_KEY
                retrieval = self.runtime_state.get(SESSION_KEY) or {}
                worker = retrieval.get("worker")
                if self.request_count > 1 or self.turn_lock.locked() or self.pending_confirmations or self._auto_ltm_maintenance_running or not self._eval_queue.empty() or background_busy() or has_running_commands() or self.wake_queue.has_work() or TEST_LOCK.locked() or (worker is not None and worker.is_active):
                    raise JsonRpcError(-32010, "Backend is busy. Wait for all foreground and background work.")
                self.restart_pending = True
                self.wake_scheduler.stop()
                return {"restarting": True}
        return super().dispatch(method, params)

    def initialize(self, _params):
        with self.initialize_lock:
            if self.backend_initialized:
                return self._initialize_result()
            from runtime import setup_capabilities
            setup_capabilities.initialize()
            self.runtime_state["setup_initializing"] = True
            result = super().initialize(_params)
            setup_capabilities.observe_window(self.messages, self.runtime_state)
            self.runtime_state.pop("setup_initializing", None)
            from runtime import agent
            result["setup"] = self.setup_status({})
            if result["setup"]["components"]["llm"]["status"] == "Ready":
                agent.schedule_formation_backfill(self.messages, self.runtime_state)
            self.backend_initialized = True
            return result

    def _initialize_result(self):
        return {
            "setup": self.setup_status({}),
            "project": self._project_info(),
            "model": self._model_label(),
            "bridge": {"model_timeout_seconds": self._model_timeout()},
            "continued_session": self.runtime_state["continued_session"],
            "permissions": dict(self.permissions),
            "notifications": self._notification_config(),
            "slash_commands": self.list_slash_commands({})["commands"],
        }

    def _project_info(self):
        from persistence.session import get_project_info

        return get_project_info()

    def _model_label(self):
        from model.provider import model_label

        return model_label()

    def _model_timeout(self):
        from bridge.ui_bridge_launch import tui_model_timeout_seconds

        return tui_model_timeout_seconds()

    def _notification_config(self):
        from model.provider import load_notification_config

        return load_notification_config()

    def send_event(self, event_type, payload, turn_id=None, request_id=None, parent_id=None):
        resolved_turn_id = turn_id or self.current_turn_id or self.runtime_state.get("ui_turn_id") or ""
        resolved_request_id = request_id or self.remote_request_ids.get(resolved_turn_id)
        if resolved_request_id:
            if event_type == "final_answer":
                self.remote_receipts.complete(resolved_request_id, payload.get("content") or "")
            elif event_type in {"turn_cancelled", "agent_stopped"}:
                self.remote_receipts.interrupt(resolved_request_id, payload.get("message") or "Remote request interrupted.")
            elif event_type in {"agent_error", "error"}:
                self.remote_receipts.fail(resolved_request_id, payload.get("error") or payload.get("message") or "Remote request failed.")
        with self.write_lock:
            event = self._event_payload(event_type, payload, turn_id, resolved_request_id, parent_id)
            with self.clients_lock:
                clients = list(self.clients)
            for client in clients:
                try:
                    client.write({"jsonrpc": JSONRPC_VERSION, "method": f"event.{event_type}", "params": event})
                except Exception:
                    with self.clients_lock:
                        self.clients.discard(client)
                    client.close()
        if event_type == "final_answer":
            self.runtime_state["ui_final_answer_sent"] = True

    def _event_payload(self, event_type, payload, turn_id=None, request_id=None, parent_id=None):
        sequence = self._allocate_event_sequence()
        self._record_event_progress(event_type, payload)
        resolved_turn_id = turn_id or self.current_turn_id or self.runtime_state.get("ui_turn_id") or self.runtime_state.get("trace_id") or ""
        return {
            "event_id": __import__("uuid").uuid4().hex,
            "turn_id": resolved_turn_id,
            "request_id": request_id or self._turn_request_ids.get(resolved_turn_id, ""),
            "parent_id": parent_id or self._turn_parent_ids.get(resolved_turn_id, ""),
            "type": event_type,
            "sequence": sequence,
            "payload": payload,
        }


def win32file_close_handle(handle):
    try:
        import win32file

        win32file.CloseHandle(handle)
    except Exception:
        pass
