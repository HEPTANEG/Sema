import json
import queue
import sys
import threading
import time
import uuid

from runtime import agent
from runtime.agent import create_initial_messages
from bridge.headless_runtime import hard_timeout_call_chat_completion, patch_agent_for_headless
import main as cli_main
from model import provider as model_provider
from model.provider import PROVIDER_PROFILES, global_config_path, load_model_config, load_notification_config, model_label, save_global_model_config
from persistence.trajectory import (
    fail_trajectory,
    get_trajectory,
    latest_trajectory,
    list_trajectories,
    start_trajectory,
    trajectory_db_path,
)
from persistence.session import (
    append_message_and_persist,
    ensure_project,
    get_project_info,
    load_last_session,
    save_last_session,
)
from commands.structured_commands import apply_settings_action, execute_slash_command, initialize_permissions, slash_command_list
from agents.temporal_state import ensure_process_temporal_state, mark_assistant_turn_finished, mark_turn_started
from bridge.ui_bridge_confirmation import BridgeConfirmationPolicy
from bridge.ui_bridge_launch import chdir_to_tui_workspace, tui_model_timeout_seconds
from bridge.event_schema import EVENT_SCHEMAS
from memory_engine.dynamic_context_budget import (
    STM_MAX_TOKENS,
    STM_MIN_TOKENS,
    WM_MAX_TOKENS,
    WM_MIN_TOKENS,
    compute_and_store_dynamic_context_budget,
)
from memory_engine.ltm import maintenance_pressure
from runtime import maintenance_gate
from runtime.agent_phase import get_agent_phase
from memory_engine.ltm.maintenance_runner import run_ltm_maintenance
from runtime.wake import WakeQueue, WakeScheduler, wake_event
from runtime.maintenance_sleep import MaintenanceControlRequested


JSONRPC_VERSION = "2.0"
INTERACTION_SOURCES = {"tui", "telegram", "eval"}


class JsonRpcError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class BridgeServer:
    def __init__(self, input_stream=None, output_stream=None, event_sink=None):
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.event_sink = event_sink
        self.write_lock = threading.Lock()
        self.pending_confirmations = {}
        self.messages = None
        self.permissions = {}
        self.runtime_state = {
            "continued_session": False,
            "last_run": [],
            "verbose": False,
            "ui_event_sink": self._agent_ui_event,
        }
        ensure_process_temporal_state(self.runtime_state)
        self.current_turn_id = None
        self.turn_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.turn_started_at = None
        self.last_event_at = None
        self.last_event_type = ""
        self.last_event_payload_summary = ""
        self.event_sequence = 0
        self.confirmations = BridgeConfirmationPolicy(self)
        self._user_arrival_counter = 0
        self._pending_message_responses = 0
        self._auto_ltm_maintenance_running = False
        self._auto_ltm_maintenance_error = ""
        self.runtime_state["maintenance_continuation"] = None
        self._auto_ltm_condition = threading.Condition(self.state_lock)
        self._eval_queue = queue.Queue()
        self._eval_requests = {}
        self._eval_worker = None
        self._eval_worker_stop = threading.Event()
        self._turn_request_ids = {}
        self._turn_parent_ids = {}
        self.wake_queue = WakeQueue()
        self.wake_scheduler = WakeScheduler(self.wake_queue, self._deliver_wake, on_retry=self._wake_retry_diagnostic)
        self.runtime_state["wake_callback"] = self.enqueue_wake
        self.runtime_state["wake_enqueue_status_callback"] = self.enqueue_wake_status
        self.runtime_state["wake_discard_session"] = self.wake_queue.discard_session

    def run(self):
        workers = []
        try:
            for line in self.input_stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    self.send_error(None, -32700, "Parse error", str(error))
                    continue
                worker = threading.Thread(target=self.handle_request, args=(payload,))
                worker.start()
                workers.append(worker)
            for worker in workers:
                worker.join()
        finally:
            from model.provider import close_provider_connections

            close_provider_connections()

    def handle_request(self, payload):
        request_id = payload.get("id")
        method = payload.get("method")
        is_message_request = method == "message.send"
        if is_message_request:
            with self.state_lock:
                self._pending_message_responses += 1
        try:
            if payload.get("jsonrpc") != JSONRPC_VERSION:
                raise JsonRpcError(-32600, "Invalid Request")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "Params must be an object")
            result = self.dispatch(method, params)
            if request_id is not None:
                self.send_response(request_id, result)
        except JsonRpcError as error:
            self.send_error(request_id, error.code, error.message, error.data)
        except Exception as error:
            self.send_error(request_id, -32000, str(error))
            self.send_event("error", {"message": str(error)})
        finally:
            if is_message_request:
                with self.state_lock:
                    self._pending_message_responses = max(0, self._pending_message_responses - 1)

    def dispatch(self, method, params):
        methods = {
            "initialize": self.initialize,
            "message.send": self.send_message,
            "slash.execute": self.execute_slash,
            "slash.list": self.list_slash_commands,
            "settings.update": self.settings_update,
            "ltm.storage.status": self.ltm_storage_status,
            "ltm.storage.warning": self.ltm_storage_warning,
            "ltm.storage.preview": self.ltm_storage_preview,
            "ltm.storage.purge": self.ltm_storage_purge,
            "settings.get": self.setup_status,
            "setup.status": self.setup_status,
            "setup.test": self.setup_test,
            "setup.continueWithoutLtm": self.setup_continue,
            "confirmation.respond": self.confirmation_respond,
            "config.get": self.config_get,
            "config.update": self.config_update,
            "config.useProfile": self.config_use_profile,
            "session.state": self.session_state,
            "lastRun.get": self.last_run_get,
            "trajectory.list": self.trajectory_list,
            "remote.request.status": self.remote_request_status,
            "permissions.get": self.permissions_get,
            "permissions.update": self.permissions_update,
            "turn.cancel": self.turn_cancel,
        }
        handler = methods.get(method)
        if handler is None:
            raise JsonRpcError(-32601, f"Method not found: {method}")
        return handler(params)

    def initialize(self, _params):
        cli_main.load_env_file()
        chdir_to_tui_workspace()
        ensure_project()
        initialize_permissions(self.permissions)
        try:
            self.messages = load_last_session()
            self.runtime_state["continued_session"] = True
        except RuntimeError as error:
            if str(error) != "No saved session found.":
                raise
            self.messages = create_initial_messages()
            save_last_session(self.messages)
            self.runtime_state["continued_session"] = False
        if not self.runtime_state.get("setup_initializing"):
            agent.schedule_formation_backfill(self.messages, self.runtime_state)
        try:
            context_pressure = compute_and_store_dynamic_context_budget(
                self.messages,
                self.runtime_state,
                emit_observer=False,
                stabilize=False,
            )
            budgets = context_pressure.get("budgets") if isinstance(context_pressure, dict) else None
            if isinstance(budgets, dict):
                context_pressure = {
                    **context_pressure,
                    "stm": context_pressure.get("stm", budgets.get("stm")),
                    "stm_min": context_pressure.get("stm_min", budgets.get("stm_min")),
                    "stm_max": context_pressure.get("stm_max", budgets.get("stm_max")),
                    "wm": context_pressure.get("wm", budgets.get("wm")),
                    "wm_min": context_pressure.get("wm_min", budgets.get("wm_min")),
                    "wm_max": context_pressure.get("wm_max", budgets.get("wm_max")),
                }
        except Exception:
            context_pressure = self.runtime_state.get("context_pressure")
            if not isinstance(context_pressure, dict) or not context_pressure.get("stm_max") or not context_pressure.get("wm_max"):
                context_pressure = {
                    "budget_source": "default",
                    "stm": STM_MIN_TOKENS,
                    "stm_min": STM_MIN_TOKENS,
                    "stm_max": STM_MAX_TOKENS,
                    "wm": WM_MIN_TOKENS,
                    "wm_min": WM_MIN_TOKENS,
                    "wm_max": WM_MAX_TOKENS,
                }
        return {
            "project": get_project_info(),
            "model": model_label(),
            "bridge": {"model_timeout_seconds": tui_model_timeout_seconds()},
            "continued_session": self.runtime_state["continued_session"],
            "permissions": dict(self.permissions),
            "notifications": load_notification_config(),
            "slash_commands": self.list_slash_commands({})["commands"],
            "context_pressure": context_pressure,
        }

    def session_state(self, _params):
        state = {
            "project": get_project_info(),
            "model": model_label(),
            "continued_session": self.runtime_state.get("continued_session", False),
            "permissions": dict(self.permissions),
            "last_run": list(self.runtime_state.get("last_run", [])),
        }
        state.update(self._turn_state())
        return state

    def last_run_get(self, _params):
        return {"steps": list(self.runtime_state.get("last_run", []))}

    def trajectory_list(self, params):
        limit = int(params.get("limit") or 20)
        limit = max(1, min(limit, 200))
        mode = str(params.get("mode") or "")
        trajectory_id = None
        if mode == "current":
            trajectory_id = ((self.runtime_state.get("trajectory_context") or {}).get("trajectory_id"))
            if not trajectory_id:
                latest = latest_trajectory("main_agent")
                trajectory_id = latest.get("trajectory_id") if latest else None
        if mode == "current" and trajectory_id:
            data = get_trajectory(trajectory_id)
            rows = [data["trajectory"]] if data else []
        else:
            rows = list_trajectories(limit=limit, errors_only=mode == "errors")
        return {
            "trajectories": rows,
            "path": str(trajectory_db_path()),
            "trajectory_id": trajectory_id or "",
        }

    def permissions_get(self, _params):
        return {"permissions": dict(self.permissions)}

    def permissions_update(self, params):
        action = str(params.get("action") or "")
        if action == "allow-all":
            raise JsonRpcError(-32602, "allow-all was replaced by /unattended session or /unattended project")
        elif action == "reset":
            initialize_permissions(self.permissions)
        else:
            raise JsonRpcError(-32602, "action must be reset")
        return self.permissions_get({})

    def send_message(self, params):
        from runtime.setup_capabilities import require_chat
        self._ensure_initialized()
        require_chat()
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise JsonRpcError(-32602, "prompt is required")
        source = str(params.get("source") or "tui").strip().lower()
        if source not in INTERACTION_SOURCES:
            raise JsonRpcError(-32602, f"unsupported interaction source: {source}")
        request_id = str(params.get("request_id") or "").strip()
        if request_id and source not in {"telegram", "eval"}:
            raise JsonRpcError(-32602, "request_id is supported only for telegram and eval messages")
        if source == "eval":
            return self._enqueue_eval_message(params, prompt, request_id)
        receipts = getattr(self, "remote_receipts", None)
        if request_id and receipts is not None:
            existing = receipts.get(request_id)
            if existing:
                return _remote_receipt_response(existing)
        with self.state_lock:
            self._user_arrival_counter += 1
        if not self.turn_lock.acquire(blocking=False):
            raise JsonRpcError(-32010, "A turn is already running. Use turn.cancel or wait for completion.")
        turn_id = uuid.uuid4().hex
        phase = None
        main_phase_held = False
        turn_result = None
        try:
            if request_id and receipts is not None:
                receipts.begin(request_id, turn_id)
                self.remote_request_ids[turn_id] = request_id
            self._ensure_initialized()
            phase = get_agent_phase(get_project_info().get("root"))
            if phase.snapshot()["maintainer_active"]:
                raise JsonRpcError(-32011, "LTM Maintainer is running. User messages are disabled until it finishes.")
            continuation = self.runtime_state.get("maintenance_continuation")
            if isinstance(continuation, dict) and continuation.get("status") in {"sleep_requested", "running", "resuming"}:
                raise JsonRpcError(-32011, "The Main-Agent task is paused for LTM maintenance or its resume Wake. Wait for the Runtime to resume it.")
            if not phase.acquire_main():
                raise JsonRpcError(-32011, "LTM Maintainer is running. User messages are disabled until it finishes.")
            main_phase_held = True
            self._start_turn(turn_id, source)
            self.send_event("user_message", {"content": prompt}, turn_id=turn_id)
            turn_result = self._run_message_turn(prompt, turn_id)
        except Exception as error:
            if request_id and receipts is not None:
                receipts.fail(request_id, error)
            raise
        finally:
            if request_id and receipts is not None:
                receipt = receipts.get(request_id)
                if receipt and receipt.get("status") == "running" and not self.runtime_state.get("_maintenance_control_result"):
                    receipts.interrupt(request_id)
                self.remote_request_ids.pop(turn_id, None)
            self._finish_turn(turn_id)
            if phase is not None and main_phase_held:
                phase.release_main()
            self.turn_lock.release()
        control = self.runtime_state.pop("_maintenance_control_result", None)
        if control:
            self._dispatch_maintenance_control(control, turn_id, request_id)
        return turn_result or {"ok": True, "last_run": list(self.runtime_state.get("last_run", []))}

    def _enqueue_eval_message(self, params, prompt, request_id):
        request_id = request_id or "eval_" + uuid.uuid4().hex
        with self.state_lock:
            existing = self._eval_requests.get(request_id)
            if existing is not None:
                return {
                    "accepted": True,
                    "status": existing["status"],
                    "request_id": request_id,
                    "queue_position": existing.get("queue_position", 0),
                }
            record = {
                "request_id": request_id,
                "prompt": prompt,
                "params": dict(params),
                "status": "queued",
                "queue_position": self._eval_queue.qsize() + 1,
                "event": threading.Event(),
                "result": None,
                "error": None,
            }
            self._eval_requests[request_id] = record
        self._eval_queue.put(record)
        self._ensure_eval_worker()
        self.send_event(
            "input_queued",
            {
                "request_id": request_id,
                "queue_position": record["queue_position"],
                "content_chars": len(prompt),
            },
            request_id=request_id,
        )
        return {
            "accepted": True,
            "status": "queued",
            "request_id": request_id,
            "queue_position": record["queue_position"],
        }

    def _ensure_eval_worker(self):
        with self.state_lock:
            if self._eval_worker and self._eval_worker.is_alive():
                return
            self._eval_worker_stop.clear()
            self._eval_worker = threading.Thread(
                target=self._run_eval_queue,
                name="eval-input-queue",
                daemon=True,
            )
            self._eval_worker.start()

    def _run_eval_queue(self):
        while not self._eval_worker_stop.is_set():
            try:
                record = self._eval_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            request_id = record["request_id"]
            with self.state_lock:
                record["status"] = "running"
            self.send_event("input_started", {"request_id": request_id}, request_id=request_id)
            try:
                record["result"] = self._run_eval_message(record)
                record["status"] = "completed"
            except Exception as error:
                record["error"] = str(error)
                record["status"] = "failed"
                record["result"] = {
                    "type": "turn_result",
                    "request_id": request_id,
                    "error": str(error),
                }
                self.send_event(
                    "eval_request_failed",
                    {"request_id": request_id, "error": str(error)},
                    request_id=request_id,
                )
            finally:
                record["event"].set()
                self._eval_queue.task_done()

    def _run_eval_message(self, record):
        params = record["params"]
        request_id = record["request_id"]
        prompt = record["prompt"]
        self._ensure_initialized()
        while True:
            self.turn_lock.acquire()
            lock_held = True
            turn_id = None
            try:
                gate = maintenance_gate.user_turn_gate_status()
                if gate["blocked"]:
                    self.turn_lock.release()
                    lock_held = False
                    self._start_auto_ltm_maintenance()
                    with self._auto_ltm_condition:
                        while self._auto_ltm_maintenance_running:
                            self._auto_ltm_condition.wait(timeout=0.5)
                        if self._auto_ltm_maintenance_error:
                            raise RuntimeError(
                                f"automatic LTM maintenance failed: {self._auto_ltm_maintenance_error}"
                            )
                    continue
                turn_id = uuid.uuid4().hex
                self._start_turn(
                    turn_id,
                    "eval",
                    request_id=request_id,
                    parent_id=params.get("parent_id"),
                )
                self.send_event(
                    "user_message",
                    {
                        "content": prompt,
                        "request_id": request_id,
                        "phase": params.get("phase") or "user",
                    },
                    turn_id=turn_id,
                    request_id=request_id,
                    parent_id=params.get("parent_id"),
                )
                start_index = len(self.messages)
                turn_result = self._run_message_turn(
                    prompt,
                    turn_id,
                    created_at=params.get("created_at"),
                    request_id=request_id,
                    parent_id=params.get("parent_id"),
                )
                delta = [dict(message) for message in self.messages[start_index:]]
                answers = [
                    message.get("content") or ""
                    for message in delta
                    if message.get("role") == "assistant" and message.get("content")
                ]
                return {
                    "type": "turn_result",
                    "request_id": request_id,
                    "turn_id": turn_id,
                    "final_answer": answers[-1] if answers else "",
                    "assistant_messages": answers,
                    "messages": delta,
                    "tool_trace": _tool_trace(self.runtime_state.get("last_run") or []),
                    "maintenance_status": {
                        "gate": gate,
                        "runtime": self.runtime_status({}),
                    },
                    "error": None,
                    **(turn_result or {}),
                }
            finally:
                if turn_id is not None:
                    self._finish_turn(turn_id)
                if lock_held:
                    self.turn_lock.release()

    def wait_eval_request(self, request_id, timeout=None):
        with self.state_lock:
            record = self._eval_requests.get(str(request_id))
        if record is None:
            raise JsonRpcError(-32602, f"Unknown eval request: {request_id}")
        if not record["event"].wait(timeout):
            raise TimeoutError(f"eval request timed out after {timeout} seconds")
        return {
            "request_id": record["request_id"],
            "status": record["status"],
            "result": record["result"] or {},
            "error": record["error"],
        }

    def forget_eval_request(self, request_id):
        with self.state_lock:
            return self._eval_requests.pop(str(request_id), None) is not None

    def wait_runtime_stable(self, quiet_seconds=1.0, timeout_seconds=30.0):
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            status = self.runtime_status({})
            if (
                status["turn_status"] == "idle"
                and status["eval_queue_size"] == 0
                and status["wake_queue_size"] == 0
                and not status["auto_ltm_maintenance_running"]
            ):
                quiet_since = status.get("last_event_at") or time.time()
                if time.time() - quiet_since >= quiet_seconds:
                    return {"stable": True, "status": status}
            time.sleep(0.05)
        return {"stable": False, "status": self.runtime_status({}), "timeout": True}

    def runtime_status(self, _params):
        state = self._turn_state()
        state["agent_phase"] = get_agent_phase(get_project_info().get("root")).snapshot()
        with self.state_lock:
            state.update(
                {
                    "eval_queue_size": self._eval_queue.qsize(),
                    "eval_requests": {
                        key: value.get("status") for key, value in self._eval_requests.items()
                    },
                    "auto_ltm_maintenance_running": bool(self._auto_ltm_maintenance_running),
                    "auto_ltm_maintenance_error": self._auto_ltm_maintenance_error,
                    "wake_queue_size": len(self.wake_queue.snapshot()),
                }
            )
        return state

    def _start_auto_ltm_maintenance(self):
        from runtime.setup_capabilities import ltm_enabled
        if not ltm_enabled():
            return False
        with self.state_lock:
            if self._auto_ltm_maintenance_running:
                return False
            self._auto_ltm_maintenance_running = True
            self._auto_ltm_maintenance_error = ""

        def run():
            continuation = self.runtime_state.get("maintenance_continuation") or {}
            try:
                run_ltm_maintenance(runtime_state=self.runtime_state, force=True)
            except Exception as error:
                with self.state_lock:
                    self._auto_ltm_maintenance_error = str(error)
                self.send_event(
                    "ltm_maintenance_failed",
                    {"status": "failed", "failure_reason": str(error), "automatic": True},
                )
            finally:
                with self._auto_ltm_condition:
                    self._auto_ltm_maintenance_running = False
                    self._auto_ltm_condition.notify_all()

        threading.Thread(target=run, name="ltm-maintenance-auto", daemon=True).start()
        return True

    def enqueue_wake(self, source, kind, payload, processing_class="user_like", wake_id=None):
        """Queue a background-agent result for a future Main Agent turn."""
        return self.enqueue_wake_status(source, kind, payload, processing_class, wake_id) == "queued"

    def enqueue_wake_status(self, source, kind, payload, processing_class="user_like", wake_id=None):
        session_key = (payload or {}).get("continuation_id") if source == "retriever" else None
        return self.wake_queue.put_status(
            wake_event(
                source,
                kind,
                payload,
                session_key=session_key,
                wake_id=wake_id,
                processing_class=processing_class,
                parent_turn_id=(payload or {}).get("parent_turn_id") if isinstance(payload, dict) else None,
                parent_request_id=(payload or {}).get("parent_request_id") if isinstance(payload, dict) else None,
            )
        )

    def _wake_retry_diagnostic(self, event, status, reason):
        reason = reason or "delivery not accepted"
        report_attempt = event.delivery_attempts & (event.delivery_attempts - 1) == 0
        should_report = (
            event.last_diagnostic_status != status
            or event.last_diagnostic_reason != reason
            or report_attempt
        )
        if not should_report:
            return
        event.last_diagnostic_status = status
        event.last_diagnostic_reason = reason
        event.last_diagnostic_attempt = event.delivery_attempts
        event_type = "wake_deferred" if status == "deferred" else "wake_failed"
        payload = {
            "wake_id": event.wake_id,
            "attempt": event.delivery_attempts,
            "reason": reason,
        }
        if event_type == "wake_failed":
            payload["error"] = payload["reason"]
        self.send_event(event_type, payload)

    def _deliver_wake(self, event):

        if event.main_agent_started:
            return True

        def defer(reason):
            event.last_delivery_status = "deferred"
            event.last_delivery_reason = reason
            return False

        with self.state_lock:
            arrival_counter = self._user_arrival_counter
            if self._pending_message_responses:
                return defer("pending message response")
            if event.source == "maintainer" and event.kind in {"maintenance_task_resumed", "maintenance_sleep_failed"} and self._auto_ltm_maintenance_running:
                return defer("Maintainer has not released its running state")
            if self._eval_queue.qsize() > 0:
                return defer("evaluation request is pending")
        time.sleep(0.01)
        with self.state_lock:
            if self._user_arrival_counter != arrival_counter:
                return defer("a user message arrived before wake delivery")
        if event.source == "retriever":
            continuation_id = (event.payload or {}).get("continuation_id") if isinstance(event.payload, dict) else None
            session = self.runtime_state.get("memory_retrieval_session")
            if continuation_id and (not isinstance(session, dict) or session.get("continuation_id") != continuation_id):
                return True
        if not self.turn_lock.acquire(blocking=False):
            return defer("Main Agent turn lock is occupied")
        turn_id = uuid.uuid4().hex
        phase = None
        main_phase_held = False
        wake_request_id = event.parent_request_id
        try:
            self._ensure_initialized()
            phase = get_agent_phase(get_project_info().get("root"))
            if not phase.acquire_main():
                return defer("Main Agent phase is unavailable")
            main_phase_held = True
            self._start_turn(
                turn_id,
                "wake",
                request_id=wake_request_id,
                parent_id=event.parent_turn_id,
            )
            if wake_request_id and hasattr(self, "remote_request_ids"):
                self.remote_request_ids[turn_id] = wake_request_id
            payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
            content = json.dumps({"source": event.source, "kind": event.kind, "payload": payload}, ensure_ascii=False)
            message = {"role": "user", "source": event.source, "content": f"<runtime-wake source=\"{event.source}\">{content}</runtime-wake>"}

            def mark_wake_started():
                event.message_persisted = True
                if event.source == "maintainer" and event.kind == "maintenance_sleep_failed":
                    continuation = self.runtime_state.get("maintenance_continuation") or {}
                    continuation.update({
                        "status": "failed",
                        "failure_count": int(payload.get("failure_count") or continuation.get("failure_count") or 1),
                        "failure_reason": payload.get("failure_reason") or "Maintenance failed.",
                        "run_id": payload.get("maintenance_run_id") or continuation.get("run_id"),
                    })
                    self.runtime_state["maintenance_continuation"] = continuation
                elif event.source == "maintainer" and event.kind == "maintenance_task_resumed":
                    continuation = self.runtime_state.get("maintenance_continuation") or {}
                    continuation["status"] = "resuming"
                    continuation["run_id"] = payload.get("maintenance_run_id") or continuation.get("run_id")
                    self.runtime_state["maintenance_continuation"] = continuation
                    self.runtime_state["maintenance_resume_wake"] = True
                if not event.wake_announced:
                    self.send_event(
                        "wake_delivered",
                        {
                            "wake_id": event.wake_id,
                            "source": event.source,
                            "kind": event.kind,
                            "processing_class": event.processing_class,
                        },
                        turn_id=turn_id,
                        request_id=event.parent_request_id,
                        parent_id=event.parent_turn_id,
                    )
                    event.wake_announced = True

            def mark_main_agent_started():
                event.main_agent_started = True

            if event.message_persisted:
                mark_wake_started()

            self._run_message_turn(
                message["content"],
                turn_id,
                message_record=message,
                processing_class=event.processing_class,
                message_already_persisted=event.message_persisted,
                on_message_persisted=mark_wake_started,
                on_agent_loop_started=mark_main_agent_started,
            )
            control = self.runtime_state.pop("_maintenance_control_result", None)
            if control:
                self._dispatch_maintenance_control(control, turn_id, wake_request_id)
            if event.source == "maintainer" and event.kind == "maintenance_task_resumed" and not event.main_agent_started:
                self.runtime_state["maintenance_continuation"] = None
                self.runtime_state.pop("maintenance_resume_wake", None)
            return True
        except Exception as error:
            if event.main_agent_started:
                return True
            event.last_delivery_status = "failed"
            event.last_delivery_reason = str(error)
            return False
        finally:
            self._finish_turn(turn_id)
            if hasattr(self, "remote_request_ids"):
                self.remote_request_ids.pop(turn_id, None)
            if phase is not None and main_phase_held:
                phase.release_main()
            self.turn_lock.release()

    def remote_request_status(self, params):
        request_id = str(params.get("request_id") or "").strip()
        if not request_id:
            raise JsonRpcError(-32602, "request_id is required")
        receipts = getattr(self, "remote_receipts", None)
        if receipts is None:
            return {"found": False}
        receipt = receipts.get(request_id)
        if not receipt:
            return {"found": False}
        return {"found": True, **_remote_receipt_response(receipt)}

    def turn_cancel(self, _params):
        with self.state_lock:
            active_turn_id = self.current_turn_id
            if active_turn_id is None:
                return {"ok": False, "active_turn_id": ""}
            self.runtime_state["cancel_requested"] = True
        self.send_event("cancel_requested", {"message": "Turn cancellation requested."}, turn_id=active_turn_id)
        return {"ok": True, "active_turn_id": active_turn_id}

    def _preflight_provider(self):
        if self.runtime_state.get("provider_preflight_ok"):
            return
        timeout = tui_model_timeout_seconds()
        try:
            hard_timeout_call_chat_completion(model_provider.call_chat_completion, timeout)(
                [{"role": "user", "content": "hello"}],
                timeout_seconds=timeout,
                purpose="TUI provider preflight",
            )
        except Exception as error:
            message = str(error)
            self.send_event("agent_error", {"error": message})
            raise RuntimeError(message) from error
        self.runtime_state["provider_preflight_ok"] = True

    def _run_message_turn(
        self,
        prompt,
        turn_id,
        message_record=None,
        processing_class="user_like",
        created_at=None,
        request_id=None,
        parent_id=None,
        on_message_persisted=None,
        message_already_persisted=False,
        on_agent_loop_started=None,
    ):
        with patch_agent_for_headless(
            self.confirmations,
            timeout_seconds=tui_model_timeout_seconds(),
            max_model_retries=0,
        ):
            try:
                self.runtime_state["turn_processing_class"] = processing_class or "user_like"
                self.runtime_state["turn_start_index"] = max(0, len(self.messages) - 1) if message_already_persisted else len(self.messages)
                turn_start_index = self.runtime_state["turn_start_index"]
                self.runtime_state.pop("trajectory_context", None)
                self.runtime_state.pop("trace_id", None)
                mark_turn_started(self.runtime_state)
                self.runtime_state["streaming_enabled"] = bool(load_model_config().get("supports_streaming"))
                self.runtime_state["assistant_message_id"] = f"assistant-{uuid.uuid4().hex}"
                if not message_already_persisted:
                    append_message_and_persist(
                        self.messages,
                        message_record or {"role": "user", "content": prompt},
                        created_at=created_at,
                    )
                    if callable(on_message_persisted):
                        on_message_persisted()
                self.send_event(
                    "assistant_stage",
                    {
                        "message_id": self.runtime_state["assistant_message_id"],
                        "label": "Preparing your request...",
                    },
                    turn_id=turn_id,
                )
                self._preflight_provider()
                context_error = agent.outbound_context_error_message(self.messages, self.runtime_state)
                if context_error:
                    append_message_and_persist(self.messages, {"role": "assistant", "content": context_error})
                    save_last_session(self.messages)
                    mark_assistant_turn_finished(self.runtime_state)
                    self.send_event(
                        "final_answer",
                        {
                            "path": "stm",
                            "message_id": self.runtime_state["assistant_message_id"],
                            "content": context_error,
                            "content_chars": len(context_error),
                            "source": "context_build_error",
                        },
                        turn_id=turn_id,
                    )
                    return {"ok": True, "last_run": list(self.runtime_state.get("last_run", []))}
                self.runtime_state["trajectory_context"] = start_trajectory("main_agent")
                self.runtime_state["trace_id"] = self.runtime_state["trajectory_context"].get("trajectory_id")
                if callable(on_agent_loop_started):
                    on_agent_loop_started()
                agent.answer_with_tools(self.messages, self.permissions, self.runtime_state)
                self._send_final_answer_fallback(turn_id, turn_start_index)
            except agent.ToolRunStopped as error:
                append_message_and_persist(self.messages, {"role": "assistant", "content": str(error)})
                save_last_session(self.messages)
                mark_assistant_turn_finished(self.runtime_state)
                event_type = "turn_cancelled" if self.runtime_state.get("cancel_requested") else "agent_stopped"
                self.send_event(event_type, {"message": str(error)}, turn_id=turn_id)
            except MaintenanceControlRequested as error:
                action = error.action
                self.runtime_state["_maintenance_control_result"] = action
                mark_assistant_turn_finished(self.runtime_state)
                if action.get("action") == "handoff":
                    message = str(action.get("message") or "Maintenance failed; the task remains paused.")
                    append_message_and_persist(self.messages, {"role": "assistant", "content": message})
                    continuation = self.runtime_state.get("maintenance_continuation") or {}
                    continuation["status"] = "handed_off"
                    self.runtime_state["maintenance_continuation"] = continuation
                    self.send_event("final_answer", {
                        "path": "maintenance_handoff",
                        "message_id": self.runtime_state.get("assistant_message_id"),
                        "content": message,
                        "content_chars": len(message),
                    }, turn_id=turn_id)
                    save_last_session(self.messages)
                else:
                    self.send_event("assistant_stage", {
                        "label": "Pausing for LTM maintenance...",
                        "message_id": self.runtime_state.get("assistant_message_id"),
                    }, turn_id=turn_id)
            except KeyboardInterrupt:
                append_message_and_persist(
                    self.messages,
                    {"role": "assistant", "content": "Tool run interrupted by user."},
                )
                save_last_session(self.messages)
                mark_assistant_turn_finished(self.runtime_state)
                self.send_event("agent_stopped", {"message": "Tool run interrupted by user."}, turn_id=turn_id)
            except RuntimeError:


                raise
            except Exception as error:



                fail_trajectory(
                    self.runtime_state.get("trajectory_context"),
                    error,
                    metadata={"path": "structured", "source": "bridge_unexpected_error"},
                )
                self.send_event("agent_error", {"error": str(error)}, turn_id=turn_id)
                raise
            finally:
                self.runtime_state.pop("turn_start_index", None)
                self.runtime_state.pop("streaming_enabled", None)
                self.runtime_state.pop("assistant_message_id", None)
                self.runtime_state.pop("turn_processing_class", None)
        result = {"ok": True, "last_run": list(self.runtime_state.get("last_run", []))}
        control = self.runtime_state.get("_maintenance_control_result")
        if control:
            result.update({"suspended": True, "maintenance_action": control.get("action")})
        return result

    def _dispatch_maintenance_control(self, action, parent_turn_id=None, parent_request_id=None):
        """Start maintenance only after the Main-Agent turn lock has been released."""
        if action.get("action") == "handoff":
            self.runtime_state.pop("maintenance_resume_wake", None)
            return
        continuation = self.runtime_state.get("maintenance_continuation")
        if action.get("action") == "sleep":
            previous = continuation if isinstance(continuation, dict) and continuation.get("status") == "resuming" else {}
            continuation = {
                "status": "running",
                "continuation_id": previous.get("continuation_id") or uuid.uuid4().hex,
                "wake_prompt": action.get("wake_prompt", ""),
                "failure_count": int(previous.get("failure_count") or 0),
                "run_id": None,
                "parent_turn_id": parent_turn_id or "",
                "parent_request_id": parent_request_id or "",
            }
            self.runtime_state["maintenance_continuation"] = continuation
            from runtime.maintenance_sleep import (
                clear_request_decision,
                pressure_trace_fields,
                release_mandatory_latch,
            )
            released_latch = release_mandatory_latch(self.runtime_state)
            clear_request_decision(self.runtime_state)
            if released_latch:
                agent.trace_event(
                    self.runtime_state,
                    "maintenance.gate_released",
                    status="ok",
                    reason="continuation_running",
                    request_index=released_latch.get("request_index") or 0,
                    **pressure_trace_fields(released_latch.get("snapshot")),
                )
        elif action.get("action") == "retry":
            if not isinstance(continuation, dict):
                return
            continuation["status"] = "running"
        else:
            return
        if not self._start_maintenance_continuation():
            self._maintenance_continuation_failed("Runtime could not start the LTM Maintainer.", continuation.get("run_id"))

    def _start_maintenance_continuation(self):
        from runtime.setup_capabilities import ltm_enabled
        if not ltm_enabled():
            return False
        with self.state_lock:
            if self._auto_ltm_maintenance_running:
                return False
            self._auto_ltm_maintenance_running = True
            self._auto_ltm_maintenance_error = ""

        def run():
            continuation = self.runtime_state.get("maintenance_continuation") or {}
            try:
                # A Wake can request another sleep while its delivery still owns the turn lock.
                self.turn_lock.acquire()
                self.turn_lock.release()
                continuation = self.runtime_state.get("maintenance_continuation") or {}
                result = run_ltm_maintenance(runtime_state=self.runtime_state, force=True)
                if result.get("status") != "completed":
                    self._maintenance_continuation_failed(result.get("failure_reason") or result.get("status") or "Maintenance failed.", result.get("run_id"))
            except Exception as error:
                self._maintenance_continuation_failed(str(error), continuation.get("run_id"))
            finally:
                with self._auto_ltm_condition:
                    self._auto_ltm_maintenance_running = False
                    self._auto_ltm_condition.notify_all()

        threading.Thread(target=run, name="ltm-maintenance-continuation", daemon=True).start()
        return True

    def _maintenance_continuation_failed(self, reason, run_id=None):
        continuation = self.runtime_state.get("maintenance_continuation") or {}
        continuation["status"] = "failed"
        continuation["failure_count"] = int(continuation.get("failure_count") or 0) + 1
        continuation["failure_reason"] = str(reason)
        continuation["run_id"] = run_id or continuation.get("run_id")
        self.runtime_state["maintenance_continuation"] = continuation
        payload = {
            "maintenance_run_id": continuation.get("run_id"),
            "continuation_id": continuation.get("continuation_id"),
            "wake_prompt": continuation.get("wake_prompt"),
            "parent_turn_id": continuation.get("parent_turn_id"),
            "parent_request_id": continuation.get("parent_request_id"),
            "failure_count": continuation["failure_count"],
            "failure_reason": continuation["failure_reason"],
        }
        self.enqueue_wake(
            "maintainer", "maintenance_sleep_failed", payload,
            processing_class="tool_result_like",
            wake_id=f"wake_maintenance_failed_{continuation.get('run_id')}_{continuation['failure_count']}",
        )

    def _send_final_answer_fallback(self, turn_id, start_index=0):
        if self.runtime_state.get("ui_final_answer_sent"):
            return
        assistant_messages = [
            message
            for message in self.messages[start_index:]
            if message.get("role") == "assistant" and message.get("content")
        ]
        if not assistant_messages:
            return
        content = assistant_messages[-1].get("content") or ""
        self.send_event(
            "final_answer",
            {
                "path": "structured",
                "message_id": self.runtime_state.get("assistant_message_id") or "",
                "content": content,
                "content_chars": len(content),
                "source": "bridge_fallback",
            },
            turn_id=turn_id,
        )

    def execute_slash(self, params):
        prompt = str(params.get("command") or "").strip()
        if not prompt:
            raise JsonRpcError(-32602, "command must not be empty")
        if not prompt.startswith("/"):
            prompt = "/" + prompt
        self._ensure_initialized()
        with patch_agent_for_headless(self.confirmations, timeout_seconds=tui_model_timeout_seconds(), max_model_retries=0):
            try:
                result = execute_slash_command(prompt, self.messages, self.permissions, self.runtime_state, create_initial_messages)
            except RuntimeError as error:
                raise JsonRpcError(-32000, str(error)) from error
        if "messages" in result:
            self.messages = result.pop("messages")
        result["permissions"] = dict(self.permissions)
        result["project"] = get_project_info()
        return result

    def list_slash_commands(self, _params):
        return {"commands": slash_command_list(bool(self.runtime_state.get("developer_mode", False)))}

    def settings_update(self, params):
        if "component" in params:
            from runtime import setup_config
            try:
                setup_config.save(params["component"], params.get("updates"))
            except ValueError as error:
                raise JsonRpcError(-32602, str(error)) from error
            return self.setup_status({})
        action = str(params.get("action") or "")
        self._ensure_initialized()
        try:
            apply_settings_action(action, self.permissions, self.runtime_state)
        except RuntimeError as error:
            raise JsonRpcError(-32602, str(error)) from error
        result = execute_slash_command("/settings", self.messages, self.permissions, self.runtime_state, create_initial_messages)
        result["permissions"] = dict(self.permissions)
        result["notifications"] = load_notification_config()
        result["project"] = get_project_info()
        result["slash_commands"] = self.list_slash_commands({})["commands"]
        return result

    def ltm_storage_status(self, params):
        from memory_engine.ltm import storage_management
        return storage_management.status()

    def ltm_storage_warning(self, params):
        from memory_engine.ltm import storage_management
        return storage_management.update_warning(params.get("warning_mib"))

    def ltm_storage_preview(self, params):
        from memory_engine.ltm import storage_management
        return storage_management.preview()

    def ltm_storage_purge(self, params):
        from memory_engine.ltm import storage_management
        return storage_management.purge(params.get("preview_token"), confirmed=params.get("confirmed") is True)

    def confirmation_respond(self, params):
        confirmation_id = str(params.get("confirmation_id") or "")
        pending = self.pending_confirmations.get(confirmation_id)
        if pending is None:
            raise JsonRpcError(-32602, f"Unknown confirmation_id: {confirmation_id}")
        pending["response"] = {
            "choice": params.get("choice"),
            "reason": params.get("reason") or "",
        }
        pending["event"].set()
        return {"ok": True}

    def config_get(self, _params):
        from runtime.setup_config import public_model
        return {
            "config": public_model(),
            "notifications": load_notification_config(),
            "profiles": PROVIDER_PROFILES,
            "path": str(global_config_path()),
        }

    def config_update(self, params):
        updates = params.get("updates")
        if not isinstance(updates, dict):
            raise JsonRpcError(-32602, "updates must be an object")
        allowed = {"provider", "protocol", "model", "base_url", "api_key_env", "api_key", "clear_api_key", "thinking", "supports_tools", "supports_streaming", "supports_vision"}
        if set(updates) - allowed:
            raise JsonRpcError(-32602, "Unknown model configuration field")
        for key, value in updates.items():
            if key in {"thinking", "supports_tools", "supports_streaming", "supports_vision", "clear_api_key"}:
                if not isinstance(value, bool) and not (key == "thinking" and value is None):
                    raise JsonRpcError(-32602, f"{key} must be a boolean")
            elif not isinstance(value, str):
                raise JsonRpcError(-32602, f"{key} must be text")
        if "protocol" in updates and updates["protocol"] not in {"openai_compatible", "ollama_native"}:
            raise JsonRpcError(-32602, "Unsupported protocol")
        updates = dict(updates)
        if updates.pop("clear_api_key", False):
            updates["api_key"] = ""
        elif updates.get("api_key") == "":
            updates.pop("api_key")
        save_global_model_config(updates)
        return self.config_get({})

    def config_use_profile(self, params):
        name = str(params.get("name") or "")
        if name not in PROVIDER_PROFILES:
            raise JsonRpcError(-32602, f"Unknown profile: {name}")
        save_global_model_config(dict(PROVIDER_PROFILES[name]))
        return self.config_get({})

    def setup_status(self, _params):
        from runtime.setup_capabilities import snapshot
        return snapshot()

    def setup_test(self, params):
        from runtime import setup_config
        if self.turn_lock.locked():
            raise JsonRpcError(-32010, "Wait for the current turn before testing setup.")
        setup_config.test(str(params.get("component", "")))
        return self.setup_status({})

    def setup_continue(self, _params):
        from runtime.setup_capabilities import accept_limited
        return accept_limited()

    def _ensure_initialized(self):
        if self.messages is None:
            self.initialize({})

    def _start_turn(self, turn_id, source="tui", request_id=None, parent_id=None):
        now = time.time()
        with self.state_lock:
            self.current_turn_id = turn_id
            self.turn_started_at = now
            self.last_event_at = now
            self.last_event_type = "turn_started"
            self.last_event_payload_summary = ""
            self.runtime_state["ui_turn_id"] = turn_id
            self.runtime_state["ui_final_answer_sent"] = False
            self.runtime_state["cancel_requested"] = False
            self.runtime_state["interaction_source"] = source
            self._turn_request_ids[turn_id] = request_id or ""
            self._turn_parent_ids[turn_id] = parent_id or ""

    def _finish_turn(self, turn_id):
        with self.state_lock:
            if self.current_turn_id == turn_id:
                self.current_turn_id = None
            if self.runtime_state.get("ui_turn_id") == turn_id:
                self.runtime_state.pop("ui_turn_id", None)
            self._turn_request_ids.pop(turn_id, None)
            self._turn_parent_ids.pop(turn_id, None)
            self.runtime_state["cancel_requested"] = False
            self.runtime_state.pop("interaction_source", None)

    def _turn_state(self):
        now = time.time()
        with self.state_lock:
            active_turn_id = self.current_turn_id or ""
            turn_started_at = self.turn_started_at
            last_event_at = self.last_event_at
            return {
                "turn_status": "running" if active_turn_id else "idle",
                "active_turn_id": active_turn_id,
                "interaction_source": self.runtime_state.get("interaction_source") if active_turn_id else "",
                "turn_started_at": turn_started_at,
                "last_event_at": last_event_at,
                "last_event_type": self.last_event_type,
                "last_event_payload_summary": self.last_event_payload_summary,
                "cancel_requested": bool(self.runtime_state.get("cancel_requested")),
                "elapsed_seconds": int(now - turn_started_at) if active_turn_id and turn_started_at else 0,
                "idle_seconds": int(now - last_event_at) if active_turn_id and last_event_at else 0,
            }

    def _agent_ui_event(self, event):
        event_type = {"turn_start": "turn_started"}.get(event.get("type", "unknown"), event.get("type", "unknown"))
        payload = {key: value for key, value in event.items() if key != "type"}
        self.send_event(event_type, payload)

    def send_event(self, event_type, payload, turn_id=None, request_id=None, parent_id=None):


        _ = EVENT_SCHEMAS.get(event_type)
        resolved_turn_id = turn_id or self.current_turn_id or self.runtime_state.get("ui_turn_id") or self.runtime_state.get("trace_id") or ""
        with self.write_lock:
            sequence = self._allocate_event_sequence()
            self._record_event_progress(event_type, payload)
            event = {
                "event_id": uuid.uuid4().hex,
                "turn_id": resolved_turn_id,
                "request_id": request_id or self._turn_request_ids.get(resolved_turn_id, ""),
                "parent_id": parent_id or self._turn_parent_ids.get(resolved_turn_id, ""),
                "type": event_type,
                "sequence": sequence,
                "payload": payload,
            }
            self.output_stream.write(json.dumps({
                "jsonrpc": JSONRPC_VERSION,
                "method": f"event.{event_type}",
                "params": event,
            }, ensure_ascii=True, default=str) + "\n")
            self.output_stream.flush()
        if self.event_sink is not None:
            self.event_sink(event)
        if event_type == "final_answer":
            self.runtime_state["ui_final_answer_sent"] = True

    def _allocate_event_sequence(self):
        with self.state_lock:
            self.event_sequence += 1
            return self.event_sequence

    def _record_event_progress(self, event_type, payload):
        with self.state_lock:
            self.last_event_at = time.time()
            self.last_event_type = event_type
            self.last_event_payload_summary = _summarize_event_payload(payload)

    def send_response(self, request_id, result):
        self.write({"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result})

    def send_error(self, request_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.write({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error})

    def write(self, payload):
        with self.write_lock:
            self.output_stream.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")
            self.output_stream.flush()


def _remote_receipt_response(receipt):
    return {
        "ok": True,
        "request_id": receipt.get("request_id", ""),
        "turn_id": receipt.get("turn_id", ""),
        "status": receipt.get("status", ""),
        "final_content": receipt.get("final_content", ""),
        "error": receipt.get("error", ""),
    }


def _summarize_event_payload(payload):
    if not isinstance(payload, dict):
        return str(payload)[:160]
    parts = []
    for key in ("tool", "status", "message", "error", "content"):
        if key in payload and payload[key]:
            value = str(payload[key])
            parts.append(f"{key}={value[:80]}")
    return " ".join(parts)[:160]


def _tool_trace(steps):
    trace = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        trace.append(
            {
                "tool": step.get("tool"),
                "args": step.get("args"),
                "result": step.get("result"),
                "error": step.get("error"),
                "duration_ms": step.get("duration_ms"),
            }
        )
    return trace
