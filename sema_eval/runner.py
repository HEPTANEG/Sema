import argparse
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from queue import Queue, Empty
from datetime import datetime, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.markdown import Markdown
except ImportError:
    Console = None
    Markdown = None
ROOT_DIR = Path(__file__).resolve().parents[1]


def default_runs_dir():
    """Return the run-artifact directory for the current CLI working directory."""
    return Path.cwd() / "sema_eval" / "runs"


try:
    from .graders import grade_eval
    from .visibility import build_visibility_audit
except ImportError:
    from graders import grade_eval
    from visibility import build_visibility_audit
TASKS_DIR = Path(__file__).resolve().parent / "tasks"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
RESULTS_DIR = Path(os.getenv("SEMA_EVAL_RESULTS_DIR", Path(__file__).resolve().parent / "results"))
RUNS_DIR = Path(os.getenv("SEMA_EVAL_RUNS_DIR", default_runs_dir()))
LATEST_RUN_PATH = Path(os.getenv("SEMA_EVAL_LATEST_RUN_PATH", RUNS_DIR / "latest.json"))
SUITES = {
    "smoke",
    "regression",
    "tool",
    "file",
    "recovery",
    "conversation",
    "hard",
    "path",
    "scenario",
    "research",
    "memory_engine",
}
SEMANTIC_SCORE_THRESHOLD = 6
DEFAULT_JUDGE_MODEL = "glm-5.1"
DEFAULT_JUDGE_MAX_TOKENS = 1200
BRIDGE_PROTOCOL = "sema-eval-bridge-v2"
DEFAULT_SEMA_COMMAND = "sema"
DEFAULT_TURN_TIMEOUT_SECONDS = 180
DEFAULT_TOOL_RESULT_CHARS = 600
MARKDOWN_CONSOLE = Console(file=sys.__stdout__) if Console else None


class BridgeTimeoutError(RuntimeError):
    pass


def normalize_base_url(value):
    """Accept a plain URL and repair a Markdown link copied from a report."""
    text = str(value or "").strip()
    if text.startswith("[") and "](" in text and text.endswith(")"):
        label, target = text[1:].rsplit("](", 1)
        text = label if label == target[:-1] else target[:-1]
    return text.rstrip("/")


def resolve_sema_command(cli_value=None):
    return cli_value or os.getenv("SEMA_COMMAND") or DEFAULT_SEMA_COMMAND


def split_command(command):
    if isinstance(command, (list, tuple)):
        return [str(part) for part in command]
    text = str(command or "").strip()
    if not text:
        raise RuntimeError("Sema command is empty.")
    try:
        return shlex.split(text, posix=os.name != "nt")
    except ValueError as error:
        raise RuntimeError(f"Invalid Sema command: {error}") from error


def build_bridge_command(workspace, sema_home, sema_command=None):
    return [
        *split_command(resolve_sema_command(sema_command)),
        "eval-bridge",
        "--workspace",
        str(workspace),
        "--sema-home",
        str(sema_home),
        "--policy",
        "eval-workspace",
    ]


def build_bridge_environment():
    """Build the child environment without exposing credentials in arguments."""
    env = os.environ.copy()
    for name in ("SEMA_BASE_URL", "HALUMEM_MODEL_BASE_URL"):
        if env.get(name):
            env[name] = normalize_base_url(env[name])
    api_key = env.get("HALUMEM_MODEL_API_KEY")
    api_key_env = env.get("HALUMEM_MODEL_API_KEY_ENV") or env.get("SEMA_API_KEY_ENV")
    if api_key and api_key_env and not env.get(api_key_env):
        env[api_key_env] = api_key
    return env


def load_task(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def load_tasks(paths):
    tasks = []
    for path in paths:
        task_path = Path(path)
        if task_path.is_dir():
            tasks.extend(load_task(candidate) for candidate in sorted(task_path.glob("*.json")))
        else:
            tasks.append(load_task(task_path))
    return tasks


def load_suite_tasks(suite):
    tasks = load_tasks([TASKS_DIR])
    if suite == "all":
        return tasks
    if suite not in SUITES:
        raise RuntimeError(f"Unknown eval suite: {suite}")
    return [task for task in tasks if task.get("suite") == suite]


def list_suites():
    tasks = load_tasks([TASKS_DIR])
    counts = {suite: 0 for suite in sorted(SUITES)}
    for task in tasks:
        suite = task.get("suite")
        if suite in counts:
            counts[suite] += 1
    return counts


def task_needs_semantic_judge(task):
    return bool((task.get("expected") or {}).get("natural_language_assertions")) or task.get("type") == "scenario"


def load_eval_env_file():
    env_candidates = []
    if os.getenv("SEMA_EVAL_ENV_FILE"):
        env_candidates.append(Path(os.getenv("SEMA_EVAL_ENV_FILE")))
    env_candidates.extend(
        [
            Path.cwd() / ".env",
            ROOT_DIR / "my_agent" / ".env",
        ]
    )
    for env_path in env_candidates:
        if env_path.exists():
            _load_env_path(env_path)


def _load_env_path(env_path):
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def validate_semantic_judge_config(tasks, judge):
    if not any(task_needs_semantic_judge(task) for task in tasks):
        return
    if not judge:
        raise RuntimeError("Semantic eval requires a configured judge. Re-run without --no-judge.")
    load_eval_env_file()
    missing = []
    if not os.getenv("EVAL_JUDGE_API_KEY"):
        missing.append("EVAL_JUDGE_API_KEY")
    if not os.getenv("EVAL_JUDGE_BASE_URL"):
        missing.append("EVAL_JUDGE_BASE_URL")
    if missing:
        names = ", ".join(missing)
        raise RuntimeError(f"Semantic eval requires {names}. Set them in PowerShell or my_agent/.env.")


class BridgeClient:
    def __init__(
        self,
        workspace,
        sema_home,
        sema_command=None,
        turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
        event_callback=None,
    ):
        self.workspace = Path(workspace)
        self.sema_home = Path(sema_home)
        self.sema_command = sema_command
        self.turn_timeout = int(turn_timeout)
        self.event_callback = event_callback
        self.process = None
        self.stderr = ""
        self._stdout_queue = None
        self.events = []
        self.request_count = 0
        self.request_history = []
        self.usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "usage_events": 0,
        }

    def __enter__(self):
        command = build_bridge_command(self.workspace, self.sema_home, self.sema_command)
        env = build_bridge_environment()
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
        self._stdout_queue = Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        try:
            ready = self._read_json_line(timeout=self.turn_timeout)
            if ready.get("type") != "ready" or ready.get("protocol") != BRIDGE_PROTOCOL:
                raise RuntimeError(f"Sema eval bridge did not become ready: {ready}")
            return self
        except Exception:
            self._stop_process()
            raise

    def __exit__(self, _exc_type, _exc, _tb):
        if not self.process:
            return
        if self.process.poll() is None:
            try:
                self._write({"type": "close"})
                self._read_json_line(timeout=5, timeout_safe=True)
                self.process.wait(timeout=5)
            except (RuntimeError, BridgeTimeoutError):
                pass
            except subprocess.TimeoutExpired:
                self._stop_process()
        self.stderr = self.process.stderr.read() if self.process.stderr else ""

    def abort(self):
        """Stop a wedged bridge immediately so its worker cannot write later."""
        self._stop_process()
        if self.process and self.process.stderr:
            try:
                self.stderr = self.process.stderr.read()
            except (OSError, ValueError):
                pass

    def _stop_process(self):
        if not self.process or self.process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def turn(
        self,
        content,
        created_at=None,
        phase=None,
        request_id=None,
        wait_stable=False,
        quiet_seconds=1.0,
        stable_timeout_seconds=30.0,
        session_id=None,
    ):
        payload = {"type": "user", "content": content}
        if created_at is not None:
            payload["created_at"] = created_at
        if phase is not None:
            payload["phase"] = phase
        if request_id is not None:
            payload["request_id"] = request_id
        if session_id is not None:
            payload["session_id"] = session_id
        if wait_stable:
            payload.update(
                {
                    "wait_stable": True,
                    "quiet_seconds": quiet_seconds,
                    "stable_timeout_seconds": stable_timeout_seconds,
                }
            )
        self._write(payload)
        payload = self._read_json_line(timeout=self.turn_timeout)
        if payload.get("type") == "error":
            raise RuntimeError(payload.get("error") or "bridge error")
        if payload.get("type") != "turn_result":
            raise RuntimeError(f"unexpected bridge payload: {payload}")
        return payload

    def configure_budgets(self, stm=2000, wm=2000):
        self._write({"type": "configure_budgets", "stm": int(stm), "wm": int(wm)})
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "configure_budgets_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response.get("result") or {}

    def history_session(self, session_id, session_date, turns):
        self._write(
            {
                "type": "history_session",
                "session_id": session_id,
                "session_date": session_date,
                "turns": turns,
            }
        )
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "history_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response

    def process_session(
        self,
        session_id,
        session_date,
        turns,
        maintenance_timeout_seconds=180,
        stage_summary_max_tokens=None,
        formation_retrieval_tool_limit=None,
        operation_hard_timeout_seconds=None,
    ):
        self._write(
            {
                "type": "process_session",
                "session_id": session_id,
                "session_date": session_date,
                "turns": turns,
                "maintenance_timeout_seconds": int(maintenance_timeout_seconds),
                **({"stage_summary_max_tokens": int(stage_summary_max_tokens)} if stage_summary_max_tokens is not None else {}),
                **({"formation_retrieval_tool_limit": int(formation_retrieval_tool_limit)} if formation_retrieval_tool_limit is not None else {}),
                **({"operation_hard_timeout_seconds": int(operation_hard_timeout_seconds)} if operation_hard_timeout_seconds is not None else {}),
            }
        )
        response = self._read_json_line(
            timeout=max(self.turn_timeout, int(maintenance_timeout_seconds)),
            extend_timeout_on_event=True,
        )
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "session_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response

    def memory_search(self, query, top_k=20, purpose="qa"):
        self._write(
            {
                "type": "memory_search",
                "query": query,
                "top_k": int(top_k),
                "purpose": purpose,
            }
        )
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "memory_search_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response

    def history_complete(self):
        self._write({"type": "history_complete"})
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "history_complete":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response

    def context_build_preview(self):
        self._write({"type": "context_build_preview"})
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "context_build_preview":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response.get("preview") or {}

    def maintenance_status(self):
        self._write({"type": "maintenance_status"})
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "maintenance_status":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response

    def seed_messages(self, messages):
        self._write({"type": "seed_messages", "messages": messages})
        payload = self._read_json_line(timeout=self.turn_timeout)
        if payload.get("type") == "error":
            raise RuntimeError(payload.get("error") or "bridge error")
        if payload.get("type") != "seed_result":
            raise RuntimeError(f"unexpected bridge payload: {payload}")
        return payload

    def memory_snapshot(self, context_pressure=None):
        payload = {"type": "memory_snapshot"}
        if context_pressure is not None:
            payload["context_pressure"] = context_pressure
        self._write(payload)
        payload = self._read_json_line(timeout=self.turn_timeout)
        if payload.get("type") == "error":
            raise RuntimeError(payload.get("error") or "bridge error")
        if payload.get("type") != "memory_snapshot":
            raise RuntimeError(f"unexpected bridge payload: {payload}")
        return payload.get("snapshot") or {}

    def run_working_memory(self, decisions=None, force=True):
        payload = {"type": "run_working_memory", "force": bool(force)}
        if decisions is not None:
            payload["decisions"] = decisions
        self._write(payload)
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "working_memory_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response.get("result") or {}

    def stage_wm_ingress(self, context_pressure=None, commit=False):
        payload = {"type": "stage_wm_ingress", "commit": bool(commit)}
        if context_pressure is not None:
            payload["context_pressure"] = context_pressure
        self._write(payload)
        response = self._read_json_line(timeout=self.turn_timeout)
        if response.get("type") == "error":
            raise RuntimeError(response.get("error") or "bridge error")
        if response.get("type") != "wm_ingress_result":
            raise RuntimeError(f"unexpected bridge payload: {response}")
        return response.get("result") or {}

    def _write(self, payload):
        if not self.process or not self.process.stdin:
            raise RuntimeError("bridge process is not running")
        payload.setdefault("request_id", f"eval_bridge_{self.request_count + 1}")
        self.request_count += 1
        self.request_history.append(
            {
                "index": self.request_count,
                "type": payload.get("type"),
                "request_id": payload.get("request_id"),
                "session_id": payload.get("session_id"),
            }
        )
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _pump_stdout(self):
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self._stdout_queue.put(line)

    def _read_json_line(self, timeout=None, timeout_safe=False, extend_timeout_on_event=False):
        if not self.process or not self.process.stdout:
            raise RuntimeError("bridge process is not running")
        timeout_value = None if timeout is None else float(timeout)
        deadline = None if timeout_value is None else time.monotonic() + timeout_value
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                self.abort()
                if timeout_safe:
                    return {}
                raise BridgeTimeoutError(f"bridge response timed out after {timeout} seconds")
            try:
                line = self._stdout_queue.get(timeout=remaining)
            except Empty as error:
                self.abort()
                if timeout_safe:
                    return {}
                raise BridgeTimeoutError(f"bridge response timed out after {timeout} seconds") from error
            if not line:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                if timeout_safe:
                    return {}
                raise RuntimeError(f"bridge closed stdout before response. stderr={stderr}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"bridge returned non-JSON output: {line.strip()} ({error})")
            if payload.get("type") != "event":
                return payload
            event = payload.get("event") or {}
            self.events.append(event)
            self._collect_usage(event)
            if self.event_callback is not None:
                try:
                    self.event_callback(event)
                except Exception:
                    pass
            if extend_timeout_on_event and timeout_value is not None:
                deadline = time.monotonic() + timeout_value

    def _collect_usage(self, value):
        if not isinstance(value, dict) or value.get("type") != "provider_usage":
            return
        exchange_id = value.get("exchange_id")
        event_type = value.get("event_type")
        if not hasattr(self, "_provider_events_seen"):
            self._provider_events_seen = set()
        if exchange_id:
            identity = (exchange_id, event_type)
            if identity in self._provider_events_seen:
                return
            self._provider_events_seen.add(identity)
        else:
            self.usage["legacy_usage_events"] = self.usage.get("legacy_usage_events", 0) + 1
        counters = {
            "requested": "observed_model_requests",
            "responded": "model_responses",
            "failed": "model_failures",
        }
        if exchange_id and event_type in counters:
            key = counters[event_type]
            self.usage[key] = self.usage.get(key, 0) + 1
        usage = value.get("usage")
        if isinstance(usage, dict):
            found = False
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if usage.get(key) is None:
                    continue
                try:
                    self.usage[key] += int(usage[key])
                    found = True
                except (TypeError, ValueError):
                    pass
            if found:
                self.usage["usage_events"] += 1
        if event_type in {"responded", "failed"} and not usage:
            self.usage["responses_without_usage"] = self.usage.get("responses_without_usage", 0) + 1


def run_task(
    task,
    keep_workspace=False,
    keep_all_workspaces=False,
    judge=True,
    trial=1,
    progress=None,
    sema_command=None,
    turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
    transcript_stream=True,
    tool_result_chars=DEFAULT_TOOL_RESULT_CHARS,
):
    if progress:
        print_progress(
            progress,
            task["id"],
            "start",
            f"suite={task.get('suite', '')} trial={trial}",
        )
        print_progress(progress, task["id"], "workspace", "creating isolated workspace")
    workspace_root = Path(tempfile.mkdtemp(prefix=f"sema-eval-{task['id']}-"))
    workspace = workspace_root / "workspace"
    home = workspace_root / "home"
    workspace.mkdir()
    home.mkdir()
    _write_fixture_files(workspace, task.get("fixture", {}).get("files") or {})
    error = None
    bridge_payload = {}
    stderr = ""
    infrastructure_error = False
    try:
        if progress:
            print_progress(progress, task["id"], "bridge", "starting Sema subprocess")
        bridge = BridgeClient(workspace, home, sema_command=sema_command, turn_timeout=turn_timeout)
        with bridge:
            if progress:
                print_progress(progress, task["id"], "agent", "running Sema through eval bridge")
            bridge_payload = bridge.turn(task["input"])
        stderr = bridge.stderr
    except Exception as exc:
        error = str(exc)
        infrastructure_error = isinstance(exc, (BridgeTimeoutError, OSError, subprocess.SubprocessError))
        if progress:
            print_progress(progress, task["id"], "agent_error", error)
    result = {
        "task_id": task["id"],
        "suite": task.get("suite", ""),
        "trial": trial,
        "input": task.get("input", ""),
        "workspace": str(workspace),
        "sema_home": str(home),
        "messages": bridge_payload.get("messages") or [],
        "tool_steps": bridge_payload.get("tool_trace") or [],
        "events": bridge_payload.get("events") or [],
        "stdout": bridge_payload.get("stdout") or "",
        "stderr": stderr,
        "error": error,
        "infrastructure_error": infrastructure_error,
        "fixture_state": {},
        "visibility_audit": build_visibility_audit(task, workspace),
    }
    if bridge_payload.get("error") and not result["error"]:
        result["error"] = bridge_payload["error"]
    if task_needs_semantic_judge(task) and not result.get("infrastructure_error"):
        if progress:
            print_progress(progress, task["id"], "judge", "semantic judge")
        if judge:
            result["semantic_judge"] = judge_semantic_assertions(task, result)
        else:
            result["semantic_judge"] = {
                "status": "skipped",
                "reason": "semantic judge disabled for this run",
            }
        if progress:
            judge_status = result["semantic_judge"].get("status", "unknown")
            if result["semantic_judge"].get("parse_error"):
                judge_status = "parse_error"
            print_progress(progress, task["id"], "judge_done", judge_status)
    result["objective_commands"] = run_objective_commands(task, workspace, progress=progress)
    if progress:
        print_progress(progress, task["id"], "grade", "running objective graders")
    result["grade"] = grade_eval(task, result)
    apply_infrastructure_grade(result)
    if transcript_stream:
        print_task_transcript(result, tool_result_chars=tool_result_chars)
    passed = result["grade"]["passed"] and not result.get("error")
    if progress:
        warning_count = len(result.get("grade", {}).get("warnings", []))
        suffix = f"warnings={warning_count}" if warning_count else ""
        print_progress(progress, task["id"], result_status(result), suffix)
    if not keep_all_workspaces and (not keep_workspace or passed):
        result["workspace_removed"] = True
        shutil.rmtree(workspace_root, ignore_errors=True)
    else:
        result["workspace_removed"] = False
    return result


def run_scenario_task(
    task,
    keep_workspace=False,
    keep_all_workspaces=False,
    judge=True,
    trial=1,
    progress=None,
    sema_command=None,
    turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
    transcript_stream=True,
    tool_result_chars=DEFAULT_TOOL_RESULT_CHARS,
):
    scenario = task.get("scenario") or {}
    max_turns = int(scenario.get("max_turns", 3))
    if progress:
        print_progress(
            progress,
            task["id"],
            "start",
            f"suite={task.get('suite', '')} trial={trial} scenario_turns={max_turns}",
        )
        print_progress(progress, task["id"], "workspace", "creating isolated workspace")
    workspace_root = Path(tempfile.mkdtemp(prefix=f"sema-eval-{task['id']}-"))
    workspace = workspace_root / "workspace"
    home = workspace_root / "home"
    workspace.mkdir()
    home.mkdir()
    _write_fixture_files(workspace, task.get("fixture", {}).get("files") or {})
    first_input = scenario.get("initial_input") or task.get("input") or ""
    error = None
    turns = []
    all_tool_steps = []
    driver_events = []
    messages = []
    stdout_parts = []
    stderr = ""
    infrastructure_error = False
    try:
        if progress:
            print_progress(progress, task["id"], "bridge", "starting Sema subprocess")
        bridge = BridgeClient(workspace, home, sema_command=sema_command, turn_timeout=turn_timeout)
        with bridge:
            user_message = first_input
            for turn_index in range(1, max_turns + 1):
                if progress:
                    print_progress(progress, task["id"], "agent", f"turn={turn_index}/{max_turns}")
                try:
                    payload = bridge.turn(user_message)
                except Exception as exc:
                    error = str(exc)
                    infrastructure_error = isinstance(exc, (BridgeTimeoutError, OSError, subprocess.SubprocessError))
                    if progress:
                        print_progress(progress, task["id"], "agent_error", error)
                    break
                messages = payload.get("messages") or messages
                stdout_parts.append(payload.get("stdout") or "")
                if payload.get("error"):
                    error = payload.get("error")
                turn_tools = list(payload.get("tool_trace") or [])
                all_tool_steps.extend(turn_tools)
                turns.append(
                    {
                        "turn": turn_index,
                        "user": user_message,
                        "assistant": payload.get("final_answer") or final_answer({"messages": messages}),
                        "tool_steps": turn_tools,
                        "workspace_snapshot": workspace_snapshot(workspace),
                    }
                )
                if error:
                    break
                if turn_index >= max_turns:
                    break
                next_message = next_scenario_user_message(task, judge, turn_index, messages, turns)
                driver_events.append(next_message)
                if next_message.get("action") != "continue" or not next_message.get("message"):
                    break
                user_message = next_message["message"]
        stderr = bridge.stderr
    except Exception as exc:
        error = str(exc)
        infrastructure_error = isinstance(exc, (BridgeTimeoutError, OSError, subprocess.SubprocessError))
        if progress:
            print_progress(progress, task["id"], "agent_error", error)
    result = {
        "task_id": task["id"],
        "suite": task.get("suite", ""),
        "type": "scenario",
        "trial": trial,
        "input": first_input,
        "workspace": str(workspace),
        "sema_home": str(home),
        "messages": messages,
        "turns": turns,
        "scenario_driver": driver_events,
        "tool_steps": all_tool_steps,
        "stdout": "\n".join(stdout_parts),
        "stderr": stderr,
        "error": error,
        "infrastructure_error": infrastructure_error,
        "fixture_state": {},
        "visibility_audit": build_visibility_audit(task, workspace),
    }
    result["objective_commands"] = run_objective_commands(task, workspace, progress=progress)
    if progress:
        print_progress(progress, task["id"], "judge", "scenario judge")
    if result.get("infrastructure_error"):
        result["scenario_judge"] = {
            "status": "skipped",
            "reason": "bridge infrastructure error",
        }
    elif judge:
        result["scenario_judge"] = judge_scenario_final(task, result)
    else:
        result["scenario_judge"] = {
            "status": "skipped",
            "reason": "scenario judge disabled for this run",
        }
    if progress:
        judge_status = result["scenario_judge"].get("status", "unknown")
        if result["scenario_judge"].get("parse_error"):
            judge_status = "parse_error"
        print_progress(progress, task["id"], "judge_done", judge_status)
        print_progress(progress, task["id"], "grade", "running objective graders")
    result["grade"] = grade_eval(task, result)
    apply_infrastructure_grade(result)
    if transcript_stream:
        print_task_transcript(result, tool_result_chars=tool_result_chars)
    passed = result["grade"]["passed"] and not result.get("error")
    if progress:
        warning_count = len(result.get("grade", {}).get("warnings", []))
        suffix = f"warnings={warning_count}" if warning_count else f"turns={len(turns)}"
        print_progress(progress, task["id"], result_status(result), suffix)
    if not keep_all_workspaces and (not keep_workspace or passed):
        result["workspace_removed"] = True
        shutil.rmtree(workspace_root, ignore_errors=True)
    else:
        result["workspace_removed"] = False
    return result


def print_progress(progress, task_id, stage, detail=""):
    detail_text = f" - {detail}" if detail else ""
    print(
        f"[{progress}] {task_id} :: {stage}{detail_text}",
        file=sys.__stdout__,
        flush=True,
    )


def print_task_transcript(result, tool_result_chars=DEFAULT_TOOL_RESULT_CHARS):
    render_terminal_markdown(build_task_transcript_markdown(result, tool_result_chars=tool_result_chars))


def render_terminal_markdown(markdown_text):
    text = str(markdown_text or "")
    if MARKDOWN_CONSOLE and Markdown:
        try:
            MARKDOWN_CONSOLE.print(Markdown(text))
            return
        except UnicodeEncodeError:
            pass
    _safe_terminal_print(text)


def _safe_terminal_print(text):
    output = str(text or "") + "\n"
    stream = sys.__stdout__
    encoding = getattr(stream, "encoding", None) or "utf-8"
    if hasattr(stream, "buffer"):
        stream.buffer.write(output.encode(encoding, errors="replace"))
        stream.buffer.flush()
    else:
        print(
            output.encode(encoding, errors="replace").decode(encoding, errors="replace"),
            file=stream,
            flush=True,
        )


def build_transcript_markdown(results, suite, trials, tool_result_chars=DEFAULT_TOOL_RESULT_CHARS):
    lines = [
        "# Sema Eval Transcript",
        "",
        f"- suite: {suite}",
        "- mode: live",
        f"- trials: {trials}",
        "",
    ]
    for result in results:
        lines.append(build_task_transcript_markdown(result, tool_result_chars=tool_result_chars).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_task_transcript_markdown(result, tool_result_chars=DEFAULT_TOOL_RESULT_CHARS):
    lines = [
        "",
        f"## {result.get('task_id')} [{result_status(result)}]",
        "",
        f"- suite: {result.get('suite')}",
        f"- score: {task_score(result):.1f}/10" if result.get("grade") else "- score: n/a",
    ]
    if result.get("error"):
        lines.append(f"- error: {clip(result.get('error'), 240)}")
    lines.extend(
        [
            "",
            "### User Input",
            "",
            fenced(result.get("input") or latest_user_message(result.get("messages") or [])),
        ]
    )
    if result.get("type") == "scenario":
        lines.extend(["", "### Turns", ""])
        for turn in result.get("turns") or []:
            lines.extend(
                [
                    f"#### Turn {turn.get('turn')}",
                    "",
                    "User/Judge driver:",
                    "",
                    fenced(turn.get("user") or ""),
                    "",
                    "Sema:",
                    "",
                    fenced(turn.get("assistant") or ""),
                    "",
                ]
            )
            lines.extend(tool_trace_markdown(turn.get("tool_steps") or [], tool_result_chars))
        lines.extend(scenario_judge_markdown(result.get("scenario_judge") or {}))
    else:
        lines.extend(["", "### Sema Answer", "", fenced(final_answer(result))])
        lines.extend(tool_trace_markdown(result.get("tool_steps") or [], tool_result_chars))
        lines.extend(semantic_judge_markdown(result.get("semantic_judge") or {}))
    return "\n".join(lines)


def tool_trace_markdown(tool_steps, tool_result_chars):
    lines = ["", "### Tool Trace", ""]
    if not tool_steps:
        lines.append("(no tools)")
        return lines
    for index, step in enumerate(tool_steps, start=1):
        lines.extend(
            [
                f"{index}. `{step.get('tool')}`",
                f"   - args: `{clip(step.get('args'), 180)}`",
                f"   - result: {clip(step.get('result'), int(tool_result_chars))}",
            ]
        )
        if step.get("error"):
            lines.append(f"   - error: {clip(step.get('error'), 180)}")
    return lines


def semantic_judge_markdown(judge):
    if not judge:
        return ["", "### Semantic Judge", "", "(not used)"]
    lines = [
        "",
        "### Semantic Judge",
        "",
        f"- status: {judge.get('status', 'unknown')}",
    ]
    if judge.get("parse_error"):
        lines.append(f"- parse_error: {judge.get('improvement_note')}")
    for item in judge.get("results") or []:
        lines.append(f"- assertion {item.get('index')}: score={item.get('score')} category={item.get('category')} reason={item.get('reason')}")
    if judge.get("improvement_note"):
        lines.append(f"- improvement_note: {judge.get('improvement_note')}")
    if judge.get("error"):
        lines.append(f"- error: {judge.get('error')}")
    return lines


def scenario_judge_markdown(judge):
    if not judge:
        return ["", "### Final Judge", "", "(not used)"]
    lines = ["", "### Final Judge", "", f"- status: {judge.get('status', 'unknown')}"]
    if judge.get("score") is not None:
        lines.append(f"- score: {judge.get('score')}/10")
    if judge.get("dimensions"):
        lines.append(f"- dimensions: {json.dumps(judge.get('dimensions'), ensure_ascii=False)}")
    if judge.get("category"):
        lines.append(f"- category: {judge.get('category')}")
    if judge.get("reason"):
        lines.append(f"- reason: {judge.get('reason')}")
    if judge.get("improvement_note"):
        lines.append(f"- improvement_note: {judge.get('improvement_note')}")
    if judge.get("error"):
        lines.append(f"- error: {judge.get('error')}")
    return lines


def fenced(text):
    return "```text\n" + str(text or "").rstrip() + "\n```"


def apply_infrastructure_grade(result):
    if not result.get("infrastructure_error"):
        return
    grade = result.setdefault("grade", {"checks": [], "warnings": [], "scores": {}})
    grade["passed"] = False
    grade["inconclusive"] = True
    grade.setdefault("checks", []).append(
        {
            "name": "infrastructure_error",
            "passed": False,
            "detail": result.get("error") or "bridge infrastructure error",
            "category": "environment_issue",
        }
    )
    scores = grade.setdefault("scores", {})
    scores.setdefault("task", 0.0)
    scores.setdefault("objective", 0.0)


def run_objective_commands(task, workspace, progress=None):
    commands = (task.get("expected") or {}).get("objective_commands") or []
    results = []
    for index, command in enumerate(commands, start=1):
        command_text = command if isinstance(command, str) else command.get("command", "")
        if not command_text:
            results.append(
                {
                    "command": "",
                    "exit_code": None,
                    "stdout": "",
                    "stderr": "missing command",
                }
            )
            continue
        timeout = int(command.get("timeout", 30)) if isinstance(command, dict) else 30
        if progress:
            print_progress(
                progress,
                task["id"],
                "objective",
                f"[{index}/{len(commands)}] {command_text}",
            )
        try:
            completed = subprocess.run(
                command_text,
                cwd=workspace,
                shell=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
            )
            results.append(
                {
                    "command": command_text,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "timeout": False,
                }
            )
        except subprocess.TimeoutExpired as error:
            results.append(
                {
                    "command": command_text,
                    "exit_code": None,
                    "stdout": error.stdout or "",
                    "stderr": error.stderr or f"timed out after {timeout}s",
                    "timeout": True,
                }
            )
    return results


def run_memory_engine_task(
    task,
    keep_workspace=False,
    keep_all_workspaces=False,
    judge=True,
    trial=1,
    progress=None,
    sema_command=None,
    turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
    transcript_stream=True,
    tool_result_chars=DEFAULT_TOOL_RESULT_CHARS,
):
    del judge
    if progress:
        print_progress(
            progress,
            task["id"],
            "start",
            f"suite={task.get('suite', '')} trial={trial} memory_engine=true",
        )
        print_progress(progress, task["id"], "workspace", "creating isolated workspace")
    workspace_root = Path(tempfile.mkdtemp(prefix=f"sema-eval-{task['id']}-"))
    workspace = workspace_root / "workspace"
    home = workspace_root / "home"
    workspace.mkdir()
    home.mkdir()
    _write_fixture_files(workspace, task.get("fixture", {}).get("files") or {})
    error = None
    stderr = ""
    infrastructure_error = False
    snapshot = {}
    snapshots = {}
    operation_results = []
    seed_result = {}
    try:
        if progress:
            print_progress(progress, task["id"], "bridge", "starting Sema subprocess")
        bridge = BridgeClient(workspace, home, sema_command=sema_command, turn_timeout=turn_timeout)
        with bridge:
            engine_fixture = (task.get("fixture") or {}).get("memory_engine") or {}
            operations = engine_fixture.get("operations") or []
            if not operations:
                operations = [
                    {
                        "type": "seed_messages",
                        "messages": engine_fixture.get("seed_tail_messages") or [],
                    },
                    {
                        "type": "memory_snapshot",
                        "name": "final",
                        "context_pressure": engine_fixture.get("context_pressure"),
                    },
                ]
            for index, operation in enumerate(operations, start=1):
                operation_type = operation.get("type")
                operation_name = operation.get("name") or f"{operation_type}_{index}"
                if operation_type == "seed_messages":
                    seed_messages = _expand_memory_engine_messages(operation.get("messages") or [])
                    if seed_messages:
                        if progress:
                            print_progress(
                                progress,
                                task["id"],
                                "seed",
                                f"seeding {len(seed_messages)} event-stream messages",
                            )
                        seed_result = bridge.seed_messages(seed_messages)
                        operation_results.append(
                            {
                                "name": operation_name,
                                "type": operation_type,
                                "result": seed_result,
                            }
                        )
                    continue
                if operation_type == "memory_snapshot":
                    if progress:
                        print_progress(
                            progress,
                            task["id"],
                            "observer",
                            f"collecting snapshot {operation_name}",
                        )
                    snapshot = bridge.memory_snapshot(context_pressure=operation.get("context_pressure"))
                    snapshots[operation_name] = snapshot
                    operation_results.append(
                        {
                            "name": operation_name,
                            "type": operation_type,
                            "result": {"ok": True},
                        }
                    )
                    continue
                if operation_type == "run_working_memory":
                    if progress:
                        print_progress(progress, task["id"], "working_memory", operation_name)
                    wm_result = bridge.run_working_memory(
                        decisions=operation.get("decisions"),
                        force=operation.get("force", True),
                    )
                    operation_results.append(
                        {
                            "name": operation_name,
                            "type": operation_type,
                            "result": wm_result,
                        }
                    )
                    continue
                if operation_type == "stage_wm_ingress":
                    if progress:
                        print_progress(progress, task["id"], "wm_ingress", operation_name)
                    ingress_result = bridge.stage_wm_ingress(
                        context_pressure=operation.get("context_pressure"),
                        commit=operation.get("commit", False),
                    )
                    operation_results.append(
                        {
                            "name": operation_name,
                            "type": operation_type,
                            "result": ingress_result,
                        }
                    )
                    continue
                raise RuntimeError(f"unknown memory_engine operation: {operation_type}")
        stderr = bridge.stderr
    except Exception as exc:
        error = str(exc)
        infrastructure_error = isinstance(exc, (BridgeTimeoutError, OSError, subprocess.SubprocessError))
        if progress:
            print_progress(progress, task["id"], "agent_error", error)
    result = {
        "task_id": task["id"],
        "suite": task.get("suite", ""),
        "type": "memory_engine",
        "trial": trial,
        "input": task.get("input", ""),
        "workspace": str(workspace),
        "sema_home": str(home),
        "messages": [],
        "tool_steps": [],
        "events": [],
        "stdout": "",
        "stderr": stderr,
        "error": error,
        "infrastructure_error": infrastructure_error,
        "fixture_state": {"seed_result": seed_result},
        "visibility_audit": build_visibility_audit(task, workspace),
        "memory_engine": {
            "snapshot": snapshot,
            "snapshots": snapshots,
            "operations": operation_results,
        },
    }
    result["objective_commands"] = run_objective_commands(task, workspace, progress=progress)
    if progress:
        print_progress(progress, task["id"], "grade", "running memory engine graders")
    result["grade"] = grade_eval(task, result)
    apply_infrastructure_grade(result)
    if transcript_stream:
        print_task_transcript(result, tool_result_chars=tool_result_chars)
    passed = result["grade"]["passed"] and not result.get("error")
    if progress:
        warning_count = len(result.get("grade", {}).get("warnings", []))
        suffix = f"warnings={warning_count}" if warning_count else ""
        print_progress(progress, task["id"], result_status(result), suffix)
    if not keep_all_workspaces and (not keep_workspace or passed):
        result["workspace_removed"] = True
        shutil.rmtree(workspace_root, ignore_errors=True)
    else:
        result["workspace_removed"] = False
    return result


def _expand_memory_engine_messages(messages):
    expanded = []
    for message in messages:
        item = dict(message)
        repeat = item.pop("content_repeat", None)
        if repeat:
            item["content"] = str(repeat.get("text", "")) * int(repeat.get("count", 1))
        expanded.append(item)
    return expanded


def latest_user_message(messages):
    for message in reversed(messages):
        if message.get("role") == "user":
            return message.get("content") or ""
    return ""


def workspace_snapshot(workspace):
    files = []
    for path in sorted(Path(workspace).rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(workspace)), "size": path.stat().st_size})
    return {"files": files}


def next_scenario_user_message(task, judge, turn_index, messages, turns):
    scripted_turns = (task.get("scenario") or {}).get("scripted_turns") or []
    if turn_index - 1 < len(scripted_turns):
        return {
            "status": "ok",
            "action": "continue",
            "message": scripted_turns[turn_index - 1],
        }
    if not judge:
        return {
            "status": "skipped",
            "action": "stop",
            "reason": "scenario judge disabled",
        }
    return judge_scenario_next_user(task, messages, turns)


def run_tasks(
    tasks,
    trials=1,
    keep_failed_workspaces=False,
    keep_workspaces=False,
    judge=True,
    show_progress=False,
    sema_command=None,
    turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
    transcript_stream=True,
    tool_result_chars=DEFAULT_TOOL_RESULT_CHARS,
):
    results = []
    total = len(tasks) * trials
    counter = 0
    if show_progress:
        print(
            f"Running {total} eval trial(s): mode=live, trials={trials}, judge={str(judge).lower()}",
            flush=True,
        )
    for task in tasks:
        for trial in range(1, trials + 1):
            counter += 1
            if task.get("type") == "scenario":
                runner = run_scenario_task
            elif task.get("type") == "memory_engine":
                runner = run_memory_engine_task
            else:
                runner = run_task
            results.append(
                runner(
                    task,
                    keep_workspace=keep_failed_workspaces,
                    keep_all_workspaces=keep_workspaces,
                    judge=judge,
                    trial=trial,
                    progress=f"{counter}/{total}" if show_progress else None,
                    sema_command=sema_command,
                    turn_timeout=turn_timeout,
                    transcript_stream=transcript_stream,
                    tool_result_chars=tool_result_chars,
                )
            )
    return results


def _write_fixture_files(workspace, files):
    for relative_path, content in files.items():
        path = workspace / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def judge_semantic_assertions(task, result):
    load_eval_env_file()
    api_key = os.getenv("EVAL_JUDGE_API_KEY")
    model = os.getenv("EVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    base_url = os.getenv("EVAL_JUDGE_BASE_URL")
    if not api_key:
        return {
            "status": "error",
            "error": "Missing EVAL_JUDGE_API_KEY for semantic judge.",
        }
    if not base_url:
        return {
            "status": "error",
            "error": "Missing EVAL_JUDGE_BASE_URL for semantic judge.",
        }
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    attempts = [_judge_prompt(task, result), _judge_prompt(task, result, retry=True)]
    last_parsed = None
    for attempt_index, prompt in enumerate(attempts):
        response = _request_semantic_judge(url, api_key, model, prompt)
        if response.get("status") == "error":
            return response
        text = response.get("text", "")
        parsed = _extract_json_object(text, response.get("raw_response"))
        parsed["status"] = "ok"
        parsed["model"] = model
        parsed["text"] = text
        parsed["raw_response"] = response.get("raw_response")
        parsed["attempts"] = attempt_index + 1
        if not parsed.get("parse_error"):
            return parsed
        last_parsed = parsed
    return last_parsed


def _judge_prompt(task, result, retry=False):
    expected = task.get("expected") or {}
    prompt = {
        "task": task.get("input", ""),
        "assertions": expected.get("natural_language_assertions") or [],
        "semantic_score_threshold": int(expected.get("semantic_score_threshold", SEMANTIC_SCORE_THRESHOLD)),
        "tool_names": [step.get("tool") for step in result.get("tool_steps", [])],
        "final_answer": final_answer(result),
        "retry": bool(retry),
    }
    if expected.get("judge_reference"):
        prompt["hidden_reference_answer"] = expected.get("judge_reference")
    if expected.get("judge_rubric"):
        prompt["hidden_rubric"] = expected.get("judge_rubric")
    if expected.get("judge_max_tokens"):
        prompt["judge_max_tokens"] = int(expected.get("judge_max_tokens"))
    return prompt


def _request_semantic_judge(url, api_key, model, prompt):
    max_tokens = int(prompt.get("judge_max_tokens") or os.getenv("EVAL_JUDGE_MAX_TOKENS", DEFAULT_JUDGE_MAX_TOKENS))
    return _request_openai_compatible_judge(
        url,
        api_key,
        model,
        (
            "Return ONLY minified JSON, no analysis, no markdown. "
            'Schema: {"results":[{"index":0,"score":10,"category":"product_issue","reason":"short"}],"improvement_note":"short"}. '
            "For each assertion index, score semantic satisfaction 0-10. Passing is >=6. "
            "If hidden_reference_answer or hidden_rubric is provided, use it as the authoritative grading key; never require exact wording. "
            "Categories: product_issue, eval_issue, environment_issue. "
            "Do not copy assertions. Keep each reason under 12 words."
        ),
        prompt,
        max_tokens=max_tokens,
    )


def _request_openai_compatible_judge(
    url,
    api_key,
    model,
    instruction,
    prompt,
    max_tokens=1200,
    temperature=0,
    timeout=60,
    raw_prompt=False,
):
    prompt_content = prompt if raw_prompt else json.dumps(prompt, ensure_ascii=False)
    content = ((instruction + "\n") if instruction else "") + prompt_content
    body = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        return {
            "status": "error",
            "category": "environment_issue",
            "error": f"Semantic judge error {error.code}: {message}",
        }
    except (urllib.error.URLError, TimeoutError) as error:
        return {
            "status": "error",
            "category": "environment_issue",
            "error": f"Semantic judge request failed: {error}",
        }
    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    usage = data.get("usage")
    normalized_usage = {}
    if isinstance(usage, dict):
        aliases = {
            "prompt_tokens": ("prompt_tokens", "input_tokens"),
            "completion_tokens": ("completion_tokens", "output_tokens"),
            "total_tokens": ("total_tokens",),
        }
        for target, keys in aliases.items():
            for key in keys:
                if usage.get(key) is not None:
                    try:
                        normalized_usage[target] = int(usage[key])
                    except (TypeError, ValueError):
                        pass
                    break
    return {
        "status": "ok",
        "text": text,
        "raw_response": data,
        "usage": normalized_usage,
        "requested_model": model,
        "response_model": data.get("model") if isinstance(data, dict) else None,
    }


def judge_conversation(task, result):
    return judge_semantic_assertions(task, result)


def judge_scenario_next_user(task, messages, turns):
    response = request_judge_json(
        ('Return ONLY minified JSON. Schema: {"action":"continue","message":"short user follow-up","reason":"short"} or {"action":"stop","reason":"short"}. You are a benchmark user driver. Continue only if another user turn is needed to test the hidden goal.'),
        {
            "role": "user_driver",
            "task_id": task.get("id"),
            "hidden_goal": (task.get("scenario") or {}).get("hidden_goal", ""),
            "max_turns": (task.get("scenario") or {}).get("max_turns", 3),
            "turns_so_far": turns,
            "transcript": compact_transcript(messages),
        },
    )
    if response.get("status") == "error" or response.get("parse_error"):
        response.setdefault("action", "stop")
        return response
    action = response.get("action") if response.get("action") in {"continue", "stop"} else "stop"
    return {
        "status": "ok",
        "action": action,
        "message": response.get("message", ""),
        "reason": response.get("reason", ""),
        "raw": response,
    }


def judge_scenario_final(task, result):
    response = request_judge_json(
        (
            "Return ONLY minified JSON. "
            'Schema: {"score":8,"category":"product_issue","dimensions":{"task_success":8,'
            '"instruction_following":8,"tool_discipline":8,"recovery":8,"communication":8},'
            '"reason":"short","improvement_note":"short"}. '
            "Score the full scenario 0-10. Passing is >=6. "
            "Categories: product_issue, eval_issue, environment_issue."
        ),
        {
            "role": "final_judge",
            "task_id": task.get("id"),
            "hidden_goal": (task.get("scenario") or {}).get("hidden_goal", ""),
            "rubric": (task.get("scenario") or {}).get("rubric", []),
            "transcript": compact_transcript(result.get("messages") or []),
            "turns": result.get("turns") or [],
            "tool_names": [step.get("tool") for step in result.get("tool_steps", [])],
            "objective_commands": result.get("objective_commands") or [],
            "final_answer": final_answer(result),
        },
    )
    if response.get("status") == "error":
        return response
    if response.get("parse_error"):
        response["status"] = "ok"
        return response
    response["status"] = "ok"
    response.setdefault("category", "product_issue")
    response.setdefault("reason", "")
    response.setdefault("improvement_note", "")
    return response


def request_judge_json(instruction, prompt):
    load_eval_env_file()
    api_key = os.getenv("EVAL_JUDGE_API_KEY")
    model = os.getenv("EVAL_JUDGE_MODEL", DEFAULT_JUDGE_MODEL)
    base_url = os.getenv("EVAL_JUDGE_BASE_URL")
    if not api_key:
        return {
            "status": "error",
            "category": "environment_issue",
            "error": "Missing EVAL_JUDGE_API_KEY for scenario judge.",
        }
    if not base_url:
        return {
            "status": "error",
            "category": "environment_issue",
            "error": "Missing EVAL_JUDGE_BASE_URL for scenario judge.",
        }
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    response = _request_openai_compatible_judge(url, api_key, model, instruction, prompt, max_tokens=1400)
    if response.get("status") == "error":
        return response
    parsed = _extract_json_object(response.get("text", ""), response.get("raw_response"))
    parsed["status"] = "ok"
    parsed["model"] = model
    parsed["text"] = response.get("text", "")
    parsed["raw_response"] = response.get("raw_response")
    return parsed


def compact_transcript(messages):
    transcript = []
    for message in messages:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        content = clip(message.get("content", ""), 700)
        transcript.append({"role": role, "content": content})
    return transcript


def _extract_json_object(text, raw_response=None):
    if _judge_reasoning_exhausted(text, raw_response):
        return _judge_parse_error(
            "Judge exhausted completion tokens in reasoning_content before returning JSON.",
            text,
            category="judge_reasoning_exhausted",
        )
    candidate = _json_candidate(text)
    if not candidate:
        return _judge_parse_error("Judge response did not contain JSON.", text)
    for attempt in (candidate, _repair_json_candidate(candidate)):
        try:
            parsed = json.loads(attempt)
            return _normalize_judge_json(parsed)
        except json.JSONDecodeError:
            continue
    return _judge_parse_error("Judge response JSON could not be parsed.", text)


def _judge_reasoning_exhausted(text, raw_response):
    if str(text or "").strip():
        return False
    choice = ((raw_response or {}).get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return choice.get("finish_reason") == "length" and bool(message.get("reasoning_content"))


def _json_candidate(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + 1]


def _repair_json_candidate(candidate):
    import re

    return re.sub(
        r',?\s*"assertion"\s*:\s*".*?"\s*(?=,\s*"score")',
        "",
        candidate,
        flags=re.DOTALL,
    )


def _normalize_judge_json(parsed):
    if not isinstance(parsed, dict):
        return _judge_parse_error("Judge JSON root was not an object.", "")
    if isinstance(parsed.get("results"), list):
        parsed["results"] = [_normalize_judge_item(item, index) for index, item in enumerate(parsed["results"])]
        return parsed
    if isinstance(parsed.get("assertions"), list):
        parsed["results"] = [_normalize_judge_item(item, index) for index, item in enumerate(parsed["assertions"])]
        return parsed
    parsed["results"] = []
    return parsed


def _normalize_judge_item(item, index):
    if not isinstance(item, dict):
        return {
            "index": index,
            "score": 0,
            "category": "judge_parse_error",
            "reason": "judge result item was not an object",
        }
    normalized = dict(item)
    normalized.setdefault("index", index)
    normalized.setdefault("category", "product_issue")
    normalized.setdefault("reason", "")
    return normalized


def _judge_parse_error(message, text, category="judge_parse_error"):
    return {
        "parse_error": True,
        "category": category,
        "results": [],
        "assertions": [],
        "improvement_note": message,
        "raw_text": text,
    }


def write_results(results):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = RESULTS_DIR / f"{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
    return path


def write_run_artifacts(results, suite, trials, tool_result_chars=DEFAULT_TOOL_RESULT_CHARS):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = RUNS_DIR / stamp
    run_dir.mkdir(parents=True)
    results_path = run_dir / "results.jsonl"
    transcript_path = run_dir / "transcript.md"
    with results_path.open("w", encoding="utf-8") as file:
        for result in results:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
    transcript = build_transcript_markdown(results, suite, trials, tool_result_chars=tool_result_chars)
    summary = build_summary_markdown(results, suite, trials, transcript_path=transcript_path)
    codex = build_codex_brief(results, suite, trials)
    transcript_path.write_text(transcript, encoding="utf-8")
    (run_dir / "summary.md").write_text(summary, encoding="utf-8")
    (run_dir / "codex-brief.md").write_text(codex, encoding="utf-8")
    latest = {
        "run_id": stamp,
        "suite": suite,
        "mode": "live",
        "trials": trials,
        "run_dir": str(run_dir),
        "results_path": str(results_path),
        "transcript_path": str(transcript_path),
        "failed_task_ids": failed_task_ids(results),
    }
    LATEST_RUN_PATH.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return latest


def load_latest_run():
    if not LATEST_RUN_PATH.exists():
        raise RuntimeError("No eval run found. Run `sema-eval <suite>` first.")
    return json.loads(LATEST_RUN_PATH.read_text(encoding="utf-8-sig"))


def load_run_results(run_dir):
    path = Path(run_dir) / "results.jsonl"
    if not path.exists():
        raise RuntimeError(f"Missing eval results: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def failed_task_ids(results):
    failed = []
    for result in results:
        if result_status(result) == "FAIL":
            failed.append(result["task_id"])
    return sorted(set(failed))


def build_summary_markdown(results, suite, trials, transcript_path=None):
    passed = passing_count(results)
    failed = len(failed_results(results))
    inconclusive = inconclusive_count(results)
    total = len(results)
    semantic_total = semantic_count(results)
    objective_total = objective_count(results)
    lines = [
        "# Sema Benchmark Summary",
        "",
        f"- suite: {suite}",
        "- mode: live",
        f"- trials: {trials}",
        f"- result: {passed} PASS / {failed} FAIL / {inconclusive} INCONCLUSIVE ({total} total)",
        f"- average_score: {average_score(results, 'task'):.1f}/10",
        f"- objective_score: {average_score(results, 'objective'):.1f}/10",
        f"- semantic_score: {average_score(results, 'semantic'):.1f}/10",
        f"- objective_pass_rate: {objective_passing_count(results)}/{objective_total}",
        f"- semantic_pass_rate: {semantic_passing_count(results)}/{semantic_total}",
        f"- process_warnings: {process_warning_count(results)}",
        f"- average_scenario_turns: {average_scenario_turns(results):.1f}",
        f"- judge_parse_errors: {judge_parse_error_count(results)}",
        f"- judge_reasoning_exhausted: {judge_reasoning_exhausted_count(results)}",
        f"- inconclusive: {inconclusive}",
    ]
    if transcript_path:
        lines.append(f"- transcript: {transcript_path}")
    lines.extend(
        [
            "",
            "## Suites",
            "",
            "| Suite | PASS | FAIL | INCONCLUSIVE | Avg | Objective | Semantic | Warnings |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    by_suite = {}
    for result in results:
        by_suite.setdefault(result.get("suite") or "unknown", []).append(result)
    for name, suite_results in sorted(by_suite.items()):
        lines.append(
            f"| {name} | {passing_count(suite_results)} | "
            f"{len(failed_results(suite_results))} | "
            f"{inconclusive_count(suite_results)} | "
            f"{average_score(suite_results, 'task'):.1f} | "
            f"{average_score(suite_results, 'objective'):.1f} | "
            f"{format_optional_score(average_score(suite_results, 'semantic', none_if_empty=True))} | "
            f"{process_warning_count(suite_results)} |"
        )
    lines.extend(["", "## Tasks", ""])
    lines.extend(task_score_table(results))
    lines.extend(["", "## Lowest Scores", ""])
    lines.extend(lowest_score_table(results))
    if any(result.get("visibility_audit") for result in results):
        lines.extend(["", "## Visibility Audit", ""])
        lines.extend(visibility_audit_table(results))
    objective_failures = objective_command_failures(results)
    if objective_failures:
        lines.extend(["", "## Objective Check Failures", ""])
        lines.extend(objective_failure_table(objective_failures))
    scenario_results = [result for result in results if result.get("type") == "scenario"]
    if scenario_results:
        lines.extend(["", "## Multi-turn Transcript Summary", ""])
        lines.extend(scenario_transcript_table(scenario_results))
    failures = failed_results(results)
    if failures:
        lines.extend(["", "## Failed", ""])
        for result in failures:
            lines.append(f"- {result['task_id']} ({result.get('suite')})")
    return "\n".join(lines) + "\n"


def build_codex_brief(results, suite, trials):
    failures = failed_results(results)
    inconclusive = inconclusive_results(results)
    lines = [
        "# Codex Fix Brief",
        "",
        f"Suite: {suite}",
        "Mode: live",
        f"Trials: {trials}",
        f"Result: {passing_count(results)}/{len(results)} passed",
        f"Average score: {average_score(results, 'task'):.1f}/10",
        "",
    ]
    if not failures and not inconclusive:
        lines.append("No failures. No repair action needed.")
        return "\n".join(lines) + "\n"
    for category in ("judge_reasoning_exhausted", "judge_parse_error"):
        grouped = [result for result in inconclusive if failure_category(result) == category]
        if not grouped:
            continue
        lines.extend([f"## {category}", ""])
        for result in grouped:
            lines.extend(format_failure_block(result))
        lines.append("")
    for category in ("product_issue", "eval_issue", "environment_issue"):
        grouped = [result for result in [*failures, *inconclusive] if failure_category(result) == category]
        if not grouped:
            continue
        lines.extend(["", f"## {category}", ""])
        for result in grouped:
            lines.extend(format_failure_block(result))
    return "\n".join(lines)


def format_failure_block(result):
    lines = [
        f"### {result['task_id']}",
        "",
        f"- suite: {result.get('suite')}",
        f"- score: {task_score(result):.1f}/10",
        f"- objective_score: {component_score(result, 'objective'):.1f}/10",
        f"- semantic_score: {format_optional_score(component_score(result, 'semantic', none_if_empty=True))}/10",
        f"- suspected_subsystem: {suspected_subsystem(result)}",
        f"- rerun: `sema-eval {result.get('suite')} --failed-only`",
        f"- transcript: `transcript.md#{result.get('task_id')}`",
    ]
    if result.get("error"):
        lines.append(f"- error: {result['error']}")
    audit = result.get("visibility_audit") or {}
    if audit:
        lines.append(f"- visibility_policy: {audit.get('policy')}")
        if audit.get("violations"):
            lines.append(f"- visibility_violations: {audit.get('violations')}")
    failed_checks = [check for check in result.get("grade", {}).get("checks", []) if not check.get("passed")]
    if failed_checks:
        lines.append("- failed_checks:")
        for check in failed_checks:
            lines.append(f"  - {check.get('name')}: {check.get('detail')} [{check.get('category')}]")
    warnings = result.get("grade", {}).get("warnings", [])
    if warnings:
        lines.append("- process_warnings:")
        for warning in warnings:
            lines.append(f"  - {warning.get('name')}: {warning.get('detail')}")
    tool_names = [step.get("tool") for step in result.get("tool_steps", [])]
    lines.append(f"- tool_trace: {tool_names}")
    answer = final_answer(result)
    if answer:
        lines.append(f"- final_answer_excerpt: {clip(answer, 220)}")
    semantic = result.get("semantic_judge") or {}
    if semantic.get("attempts"):
        lines.append(f"- semantic_judge_attempts: {semantic.get('attempts')}")
    if semantic.get("parse_error"):
        lines.append(f"- judge_parse_error: {semantic.get('improvement_note')}")
        raw_text = semantic.get("raw_text") or semantic.get("text")
        if raw_text:
            lines.append(f"- judge_raw_excerpt: {clip(raw_text, 360)}")
        if semantic.get("raw_response"):
            lines.append(f"- judge_raw_response_excerpt: {clip(json.dumps(semantic.get('raw_response'), ensure_ascii=False), 500)}")
    if semantic.get("status") == "ok":
        lines.append(f"- semantic_judge: {clip(semantic.get('text') or json.dumps(semantic, ensure_ascii=False), 360)}")
    elif semantic.get("status") == "error":
        lines.append(f"- semantic_judge_error: {semantic.get('error')}")
    scenario_judge = result.get("scenario_judge") or {}
    if scenario_judge:
        lines.append(f"- scenario_judge_score: {scenario_judge.get('score', 'n/a')}/10")
        if scenario_judge.get("dimensions"):
            lines.append(f"- scenario_judge_dimensions: {json.dumps(scenario_judge.get('dimensions'), ensure_ascii=False)}")
        if scenario_judge.get("reason"):
            lines.append(f"- scenario_judge_reason: {clip(scenario_judge.get('reason'), 220)}")
    if result.get("turns"):
        lines.append("- key_turns:")
        for turn in result.get("turns", [])[:3]:
            lines.append(f"  - turn {turn.get('turn')}: user={clip(turn.get('user'), 120)} | assistant={clip(turn.get('assistant'), 160)}")
    failed_objectives = [item for item in result.get("objective_commands", []) if item.get("exit_code") not in (0, "0")]
    if failed_objectives:
        lines.append("- objective_command_failures:")
        for item in failed_objectives:
            lines.append(f"  - {item.get('command')}: exit_code={item.get('exit_code')} stderr={clip(item.get('stderr'), 180)}")
    lines.append("")
    return lines


def failed_results(results):
    return [result for result in results if result_status(result) == "FAIL"]


def inconclusive_results(results):
    return [result for result in results if result_status(result) == "INCONCLUSIVE"]


def passing_count(results):
    return sum(1 for result in results if result_status(result) == "PASS")


def task_score(result):
    return component_score(result, "task")


def component_score(result, name, none_if_empty=False):
    scores = (result.get("grade") or {}).get("scores") or {}
    value = scores.get(name)
    if value is None:
        return None if none_if_empty else 0.0
    return float(value)


def average_score(results, name, none_if_empty=False):
    values = []
    for result in results:
        value = component_score(result, name, none_if_empty=True)
        if value is not None:
            values.append(value)
    if not values:
        return None if none_if_empty else 0.0
    return sum(values) / len(values)


def format_optional_score(value):
    if value is None:
        return "n/a"
    return f"{float(value):.1f}"


def task_score_table(results):
    lines = [
        "| Task | Suite | Status | Score | Objective | Semantic | Warnings |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for result in results:
        lines.append(f"| {result.get('task_id')} | {result.get('suite')} | {result_status(result)} | {task_score(result):.1f} | {component_score(result, 'objective'):.1f} | {format_optional_score(component_score(result, 'semantic', none_if_empty=True))} | {len((result.get('grade') or {}).get('warnings') or [])} |")
    return lines


def lowest_score_table(results, limit=5):
    lines = [
        "| Task | Suite | Status | Score | Objective | Semantic |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for result in sorted(results, key=task_score)[:limit]:
        lines.append(f"| {result.get('task_id')} | {result.get('suite')} | {result_status(result)} | {task_score(result):.1f} | {component_score(result, 'objective'):.1f} | {format_optional_score(component_score(result, 'semantic', none_if_empty=True))} |")
    return lines


def visibility_audit_table(results):
    lines = [
        "| Task | Policy | SUT Visible | Grader Hidden | Violations |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in results:
        audit = result.get("visibility_audit") or {}
        lines.append(f"| {result.get('task_id')} | {audit.get('policy', 'n/a')} | {markdown_cell('; '.join(audit.get('sut_visible') or []) or 'none')} | {markdown_cell('; '.join(audit.get('grader_hidden') or []) or 'none')} | {markdown_cell('; '.join(audit.get('violations') or []) or 'none')} |")
    return lines


def markdown_cell(text):
    return str(text or "").replace("|", "\\|")


def objective_command_failures(results):
    failures = []
    for result in results:
        for item in result.get("objective_commands") or []:
            if item.get("exit_code") not in (0, "0"):
                failures.append((result, item))
    return failures


def objective_failure_table(failures):
    lines = [
        "| Task | Command | Exit | Stdout | Stderr |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result, item in failures:
        lines.append(f"| {result.get('task_id')} | `{clip(item.get('command'), 80)}` | {item.get('exit_code')} | {clip(item.get('stdout'), 120)} | {clip(item.get('stderr'), 120)} |")
    return lines


def scenario_transcript_table(results):
    lines = [
        "| Task | Turns | Final Score | Driver Events | Final Answer |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for result in results:
        lines.append(f"| {result.get('task_id')} | {len(result.get('turns') or [])} | {task_score(result):.1f} | {len(result.get('scenario_driver') or [])} | {clip(final_answer(result), 160)} |")
    return lines


def result_status(result):
    grade = result.get("grade", {})
    if grade.get("inconclusive"):
        return "INCONCLUSIVE"
    if result.get("error") or not grade.get("passed"):
        return "FAIL"
    return "PASS"


def objective_count(results):
    return len(results)


def objective_passing_count(results):
    count = 0
    for result in results:
        checks = [check for check in result.get("grade", {}).get("checks", []) if not check.get("name", "").startswith("semantic")]
        if not result.get("error") and all(check.get("passed") for check in checks):
            count += 1
    return count


def semantic_count(results):
    return sum(1 for result in results if result.get("semantic_judge") or result.get("scenario_judge"))


def semantic_passing_count(results):
    count = 0
    for result in results:
        if not result.get("semantic_judge") and not result.get("scenario_judge"):
            continue
        checks = [check for check in result.get("grade", {}).get("checks", []) if check.get("name", "").startswith("semantic")]
        if checks and all(check.get("passed") for check in checks):
            count += 1
            continue
        semantic_score = result.get("grade", {}).get("scores", {}).get("semantic")
        judge_category = result.get("semantic_judge", {}).get("category") or result.get("scenario_judge", {}).get("category")
        if not checks and semantic_score is not None and semantic_score >= 6 and judge_category != "judge_parse_error" and not result.get("grade", {}).get("inconclusive") and not result.get("error"):
            count += 1
    return count


def process_warning_count(results):
    return sum(len(result.get("grade", {}).get("warnings", [])) for result in results)


def judge_parse_error_count(results):
    return sum(1 for result in results if result.get("semantic_judge", {}).get("category") == "judge_parse_error" or result.get("scenario_judge", {}).get("category") == "judge_parse_error")


def judge_reasoning_exhausted_count(results):
    return sum(1 for result in results if result.get("semantic_judge", {}).get("category") == "judge_reasoning_exhausted" or result.get("scenario_judge", {}).get("category") == "judge_reasoning_exhausted")


def inconclusive_count(results):
    return sum(1 for result in results if result_status(result) == "INCONCLUSIVE")


def average_scenario_turns(results):
    counts = [len(result.get("turns") or []) for result in results if result.get("type") == "scenario"]
    if not counts:
        return 0.0
    return sum(counts) / len(counts)


def failure_category(result):
    failed_checks = [check for check in result.get("grade", {}).get("checks", []) if not check.get("passed")]
    categories = [check.get("category", "product_issue") for check in failed_checks]
    if result_status(result) == "INCONCLUSIVE":
        if "environment_issue" in categories:
            return "environment_issue"
        for category in ("judge_reasoning_exhausted", "judge_parse_error"):
            if category in categories:
                return category
        if categories and all(category == "eval_issue" for category in categories):
            return "eval_issue"
        return "product_issue"
    if result.get("error") and ("API_KEY" in result.get("error", "") or "TAVILY" in result.get("error", "") or "Semantic judge" in result.get("error", "")):
        return "environment_issue"
    for step in result.get("tool_steps", []):
        text = str(step.get("result") or "")
        if "TAVILY_API_KEY" in text or "Tavily" in text:
            return "environment_issue"
    if "environment_issue" in categories:
        return "environment_issue"
    if categories and all(category == "eval_issue" for category in categories):
        return "eval_issue"
    return "product_issue"


def final_answer(result):
    for message in reversed(result.get("messages") or []):
        if message.get("role") == "assistant":
            return message.get("content") or ""
    return ""


def clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def suspected_subsystem(result):
    suite = result.get("suite")
    if suite == "tool":
        return "tool routing"
    if suite == "file":
        return "file edit"
    if suite == "recovery":
        return "recovery"
    if suite == "conversation":
        return "conversation"
    if suite == "regression":
        return "regression"
    return "prompt/tool loop"


def run_benchmark(
    suite,
    trials=1,
    keep_failed_workspaces=False,
    keep_workspaces=False,
    judge=True,
    failed_only=False,
    sema_command=None,
    turn_timeout=DEFAULT_TURN_TIMEOUT_SECONDS,
    transcript_stream=True,
    tool_result_chars=DEFAULT_TOOL_RESULT_CHARS,
):
    tasks = load_suite_tasks(suite if suite != "failed" else "all")
    if failed_only or suite == "failed":
        latest = load_latest_run()
        wanted = set(latest.get("failed_task_ids") or [])
        tasks = [task for task in tasks if task["id"] in wanted]
        if not tasks:
            raise RuntimeError("Latest eval run has no failed tasks to rerun.")
    validate_semantic_judge_config(tasks, judge)
    results = run_tasks(
        tasks,
        trials=trials,
        keep_failed_workspaces=keep_failed_workspaces,
        keep_workspaces=keep_workspaces,
        judge=judge,
        show_progress=True,
        sema_command=sema_command,
        turn_timeout=turn_timeout,
        transcript_stream=transcript_stream,
        tool_result_chars=tool_result_chars,
    )
    print("Writing eval reports ...", flush=True)
    return write_run_artifacts(results, suite, trials, tool_result_chars=tool_result_chars)


def print_run_summary(latest):
    results = load_run_results(latest["run_dir"])
    render_terminal_markdown(build_terminal_run_summary_markdown(latest, results))


def build_terminal_run_summary_markdown(latest, results):
    passed = passing_count(results)
    failed = len(failed_results(results))
    inconclusive = inconclusive_count(results)
    total = len(results)
    lines = [
        "# Sema Eval Result",
        "",
        f"- result: **{passed} PASS / {failed} FAIL / {inconclusive} INCONCLUSIVE** ({total} total)",
        f"- average_score: **{average_score(results, 'task'):.1f}/10**",
        f"- objective_score: {average_score(results, 'objective'):.1f}/10",
        f"- semantic_score: {format_optional_score(average_score(results, 'semantic', none_if_empty=True))}/10",
        f"- average_scenario_turns: {average_scenario_turns(results):.1f}",
        f"- run_directory: `{latest['run_dir']}`",
        f"- summary: `{Path(latest['run_dir']) / 'summary.md'}`",
        f"- codex_brief: `{Path(latest['run_dir']) / 'codex-brief.md'}`",
    ]
    if latest.get("transcript_path"):
        lines.append(f"- transcript: `{latest['transcript_path']}`")
    lines.extend(["", "## Tasks", ""])
    lines.extend(["| Status | Task | Score |", "| --- | --- | ---: |"])
    for result in results:
        lines.append(f"| {result_status(result)} | `{result['task_id']}` | {task_score(result):.1f}/10 |")
    lines.extend(["", "## Lowest Scores", ""])
    for result in sorted(results, key=task_score)[:5]:
        lines.append(f"- `{result['task_id']}`: {task_score(result):.1f}/10 ({result_status(result)})")
    return "\n".join(lines) + "\n"


def regenerate_codex_report():
    latest = load_latest_run()
    results = load_run_results(latest["run_dir"])
    path = Path(latest["run_dir"]) / "codex-brief.md"
    path.write_text(
        build_codex_brief(results, latest["suite"], latest["trials"]),
        encoding="utf-8",
    )
    return path


def clear_eval_results():
    removed = []
    for directory in (RUNS_DIR, RESULTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        for path in directory.iterdir():
            if path.name == ".gitignore":
                continue
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
            elif path.is_file():
                path.unlink()
                removed.append(str(path))
    return {
        "removed_count": len(removed),
        "removed": removed,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run Sema agent eval tasks.")
    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(TASKS_DIR)],
        help="Task JSON files or directories.",
    )
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--sema-command")
    parser.add_argument("--turn-timeout", type=int, default=DEFAULT_TURN_TIMEOUT_SECONDS)
    parser.add_argument("--no-transcript-stream", action="store_true")
    parser.add_argument("--tool-result-chars", type=int, default=DEFAULT_TOOL_RESULT_CHARS)
    args = parser.parse_args(argv)
    tasks = load_tasks(args.paths)
    validate_semantic_judge_config(tasks, judge=True)
    results = run_tasks(
        tasks,
        keep_failed_workspaces=args.keep_workspace,
        keep_workspaces=args.keep_workspace,
        judge=True,
        sema_command=args.sema_command,
        turn_timeout=args.turn_timeout,
        transcript_stream=not args.no_transcript_stream,
        tool_result_chars=args.tool_result_chars,
    )
    path = write_results(results)
    passed = passing_count(results)
    failed = len(failed_results(results))
    inconclusive = inconclusive_count(results)
    total = len(results)
    print(f"Eval results: {passed} PASS / {failed} FAIL / {inconclusive} INCONCLUSIVE ({total} total)")
    print(f"Average score: {average_score(results, 'task'):.1f}/10")
    print(f"Results file: {path}")
    for result in results:
        print(f"{result_status(result)} {result['task_id']} score={task_score(result):.1f}/10")
        if result.get("error"):
            print(f"  error: {result['error']}")
        for check in result["grade"]["checks"]:
            marker = "ok" if check["passed"] else "fail"
            print(f"  {marker} {check['name']}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
