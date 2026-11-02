"""HaluMem-Long adapter using Sema's memory runtime.
The public command follows HaluMem's per-user, per-session adapter contract:
historical dialogue is imported as data, Sema produces memory objects, and a
separate configured model answers and evaluates benchmark questions.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from sema_eval.benchmarks.halumem_response import decode_judgment, parse_judge_json
from sema_eval.benchmarks.halumem_official import (
    EVALUATOR_PROMPTS,
    OFFICIAL_COMMIT,
    PROMPTS,
    source_manifest,
    official_aggregate_eval_results,
    aggregate_question_answering,
)

try:
    from .. import runner
except ImportError:
    import runner
DATASET_URL = "https://huggingface.co/datasets/IAAR-Shanghai/HaluMem/resolve/main/HaluMem-Long.jsonl?download=true"
DEFAULT_DATA_DIR = Path(os.getenv("SEMA_EVAL_DATA_DIR", Path.home() / ".cache" / "sema-eval" / "halumem"))
DEFAULT_QUESTION_LIMIT = 5
DEFAULT_STM_BUDGET = 2000
DEFAULT_WM_BUDGET = 2000
DEFAULT_STABLE_TIMEOUT = 30
DEFAULT_MODEL_MAX_TOKENS = 16384
DEFAULT_MODEL_TEMPERATURE = 0
DEFAULT_MODEL_TIMEOUT = 300
DEFAULT_STAGE_SUMMARY_MAX_TOKENS = 2000
DEFAULT_FORMATION_RETRIEVAL_TOOL_LIMIT = 40
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_BACKOFF_SECONDS = (3, 10)
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "usage_events")
RUNTIME_COUNTERS = (
    "bridge_instances",
    "bridge_instances_started",
    "bridge_commands",
    *USAGE_KEYS,
    "observed_model_requests",
    "model_responses",
    "model_failures",
    "responses_without_usage",
    "legacy_usage_events",
)
OFFICIAL_QA_PROMPT = EVALUATOR_PROMPTS["EVALUATION_PROMPT_FOR_QUESTION"]
OFFICIAL_RESPONSE_PROMPT = PROMPTS["PROMPT_MEMOS"]
OFFICIAL_INTEGRITY_PROMPT = EVALUATOR_PROMPTS["EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY"]
OFFICIAL_ACCURACY_PROMPT = EVALUATOR_PROMPTS["EVALUATION_PROMPT_FOR_MEMORY_ACCURACY"]
OFFICIAL_UPDATE_PROMPT = EVALUATOR_PROMPTS["EVALUATION_PROMPT_FOR_UPDATE_MEMORY"]


def _event_stage(event):
    """Map runtime metadata to a small set of user-facing progress stages."""
    values = []
    for key in (
        "type",
        "event_type",
        "name",
        "agent_type",
        "process_type",
        "component",
        "source",
        "phase",
        "operation",
    ):
        value = event.get(key) if isinstance(event, dict) else None
        if value:
            values.append(str(value).lower())
    text = " ".join(values)
    if "retriever" in text:
        return "memory_retriever"
    if "ltm_maintainer" in text or "maintainer" in text or "maintenance" in text:
        return "ltm_maintainer"
    if "memory_push" in text:
        return "memory_push"
    if "formation" in text:
        return "formation"
    if "tool." in text or text.startswith("tool"):
        return "tool"
    if "main_agent" in text or "model" in text or "turn_start" in text:
        return "main_agent"
    if "wake" in text:
        return "wake"
    return ""


class ReplayProgress:
    """Print request-level progress without exposing benchmark content."""

    def __init__(self, history_total, question_total, heartbeat_seconds=30):
        self.history_total = int(history_total)
        self.question_total = int(question_total)
        self.total = self.history_total + self.question_total
        self.completed = 0
        self._lock = threading.Lock()
        self._current = None
        self._heartbeat_seconds = max(1, int(heartbeat_seconds))
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def _percentage(self):
        if not self.total:
            return 100.0
        return self.completed / self.total * 100

    def _line(self, message):
        return f"[{self.completed}/{self.total} {self._percentage():.1f}%] {message}"

    def _print_locked(self, message):
        print(self._line(message), flush=True)

    def print_plan(self):
        with self._lock:
            self._print_locked(f"plan history={self.history_total} questions={self.question_total} total_requests={self.total}")

    def start(self, kind, index, total, detail="", counted=True):
        self._stop_heartbeat()
        with self._lock:
            self._current = {
                "kind": kind,
                "index": index,
                "total": total,
                "started": time.monotonic(),
                "counted": counted,
                "last_stage": "",
            }
            suffix = f" {detail}" if detail else ""
            self._print_locked(f"{kind} [{index}/{total}] start{suffix}")
            stop = threading.Event()
            self._heartbeat_stop = stop
            thread = threading.Thread(target=self._heartbeat, args=(stop,), daemon=True)
            self._heartbeat_thread = thread
            thread.start()

    def on_event(self, event):
        stage = _event_stage(event)
        if not stage:
            return
        with self._lock:
            if not self._current or self._current["last_stage"] == stage:
                return
            self._current["last_stage"] = stage
            elapsed = int(time.monotonic() - self._current["started"])
            self._print_locked(f"{self._current['kind']} [{self._current['index']}/{self._current['total']}] stage={stage} elapsed={elapsed}s")

    def finish(self, status="completed", detail=""):
        self._stop_heartbeat()
        with self._lock:
            current = self._current
            if not current:
                return
            elapsed = int(time.monotonic() - current["started"])
            if current["counted"] and status == "completed":
                self.completed += 1
            suffix = f" {detail}" if detail else ""
            self._print_locked(f"{current['kind']} [{current['index']}/{current['total']}] {status} elapsed={elapsed}s{suffix}")
            self._current = None

    def notice(self, message):
        """Print a non-terminal status line for retries and recovery actions."""
        with self._lock:
            current = self._current
            if not current:
                self._print_locked(message)
                return
            elapsed = int(time.monotonic() - current["started"])
            self._print_locked(f"{current['kind']} [{current['index']}/{current['total']}] {message} elapsed={elapsed}s")

    def close(self):
        self._stop_heartbeat()
        with self._lock:
            self._current = None

    def _stop_heartbeat(self):
        stop = self._heartbeat_stop
        thread = self._heartbeat_thread
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)

    def _heartbeat(self, stop):
        while not stop.wait(self._heartbeat_seconds):
            with self._lock:
                if not self._current:
                    return
                elapsed = int(time.monotonic() - self._current["started"])
                self._print_locked(f"{self._current['kind']} [{self._current['index']}/{self._current['total']}] waiting stage={self._current['last_stage'] or 'runtime'} elapsed={elapsed}s")


def build_parser():
    parser = argparse.ArgumentParser(prog="sema-eval halumem")
    parser.add_argument("--dataset", choices=["long"], default="long")
    parser.add_argument("--data-file")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--user-id", help="Official HaluMem uuid; defaults to the first user")
    parser.add_argument("--question-id", action="append")
    parser.add_argument("--question-limit", type=int, default=None)
    parser.add_argument("--session-limit", type=int, default=None)
    parser.add_argument(
        "--session-index",
        type=int,
        default=None,
        help=("Run one zero-based session index as a diagnostic. For an index above 0, --resume must point to a run that already contains the preceding memory state."),
    )
    parser.add_argument(
        "--stm-budget",
        type=int,
        default=None,
        help="Optional explicit Sema pressure configuration.",
    )
    parser.add_argument(
        "--wm-budget",
        type=int,
        default=None,
        help="Optional explicit Sema pressure configuration.",
    )
    parser.add_argument("--maintenance-timeout", type=int, default=180)
    parser.add_argument(
        "--operation-hard-timeout",
        type=int,
        default=900,
        help="Maximum Bridge operation duration; activity heartbeats extend the idle timeout.",
    )
    parser.add_argument("--stage-summary-max-tokens", type=int, default=DEFAULT_STAGE_SUMMARY_MAX_TOKENS)
    parser.add_argument(
        "--formation-retrieval-tool-limit",
        type=int,
        default=DEFAULT_FORMATION_RETRIEVAL_TOOL_LIMIT,
        help="HaluMem adapter-only Formation retrieval budget; normal Sema remains unchanged.",
    )
    parser.add_argument("--resume", help="Resume an interrupted run directory.")
    parser.add_argument(
        "--repair-saved-judgments",
        metavar="RUN_DIRECTORY",
        help="Decode saved Judge JSON offline into a new revision; no API calls.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate inputs without starting Sema.",
    )
    parser.add_argument("--sema-command")
    parser.add_argument("--turn-timeout", type=int, default=runner.DEFAULT_TURN_TIMEOUT_SECONDS)
    parser.add_argument("--stable-timeout", type=int, default=DEFAULT_STABLE_TIMEOUT)
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--judge", dest="judge", action="store_true", default=True)
    parser.add_argument("--no-judge", dest="judge", action="store_false")
    return parser


def main(argv=None):
    args = build_parser().parse_args(list(argv or []))
    if args.repair_saved_judgments:
        if args.resume:
            raise RuntimeError("Offline repair cannot be combined with --resume.")
        from .halumem_repair import repair_saved_judgments

        repaired = repair_saved_judgments(args.repair_saved_judgments)
        print(json.dumps(repaired, ensure_ascii=False, indent=2))
        return 0 if repaired["inconclusive_count"] == 0 else 1
    if args.question_limit is not None and args.question_limit < 1:
        raise RuntimeError("--question-limit must be at least 1.")
    if (args.stm_budget is None) != (args.wm_budget is None):
        raise RuntimeError("--stm-budget and --wm-budget must be provided together.")
    if args.stm_budget is not None and (args.stm_budget < 2000 or args.wm_budget < 2000):
        raise RuntimeError("--stm-budget and --wm-budget must be at least 2000.")
    if args.session_limit is not None and args.session_limit < 1:
        raise RuntimeError("--session-limit must be at least 1.")
    if args.session_index is not None and args.session_index < 0:
        raise RuntimeError("--session-index must be zero or greater.")
    if args.session_index is not None and args.session_limit is not None:
        raise RuntimeError("--session-index cannot be combined with --session-limit.")
    if args.formation_retrieval_tool_limit < 1:
        raise RuntimeError("--formation-retrieval-tool-limit must be at least 1.")
    if args.operation_hard_timeout < 1:
        raise RuntimeError("--operation-hard-timeout must be at least 1.")
    resume_checkpoint = load_checkpoint(args.resume) if args.resume else None
    if resume_checkpoint:
        saved_scope = resume_checkpoint.get("scope") or {}
        if args.user_id is None:
            args.user_id = saved_scope.get("user_id")
        if args.session_limit is None:
            args.session_limit = saved_scope.get("session_limit")
        if args.question_limit is None:
            args.question_limit = saved_scope.get("question_limit")
        if not args.question_id:
            args.question_id = list(saved_scope.get("question_id") or []) or None
    data_file = args.data_file or (resume_checkpoint or {}).get("dataset_path")
    data_path = resolve_dataset_path(data_file, args.data_dir, args.download)
    users = load_users(data_path)
    user = select_user(users, args.user_id)
    questions = select_questions(user, args.question_limit, args.question_id)
    if args.session_index is not None:
        if args.session_index >= len(user.get("sessions") or []):
            raise RuntimeError(f"HaluMem session index out of range: {args.session_index}")
        if args.session_index > 0 and not resume_checkpoint:
            raise RuntimeError("--session-index above 0 requires --resume so the preceding sessions' memory state is preserved.")
        questions = [question for question in questions if int(question.get("session_index", -1)) == args.session_index]
    elif args.session_limit is not None:
        questions = [question for question in questions if int(question.get("session_index", -1)) < args.session_limit]
    if not questions and args.session_index is None:
        raise RuntimeError("No HaluMem questions matched the requested selection.")
    if not questions and args.session_index is not None and not (user.get("sessions") or [])[args.session_index].get("dialogue"):
        raise RuntimeError(f"HaluMem session {args.session_index} has no questions or dialogue to process.")
    if args.judge:
        validate_judge_config()
    if resume_checkpoint:
        validate_resume_checkpoint(resume_checkpoint, data_path, user, questions, args)
    preflight = run_preflight(user, questions, args, data_path)
    print(
        f"Preflight passed: sessions={preflight['session_count']} questions={preflight['question_count']} model={preflight['model']}",
        flush=True,
    )
    if args.preflight_only:
        return 0
    if args.session_index is not None:
        session_scope = f"target_session_index={args.session_index}"
    else:
        session_scope = f"sessions={args.session_limit or len(user.get('sessions') or [])}"
    print(
        f"Running HaluMem-Long official-compatible adapter: user={user['uuid']}, questions={len(questions)}, {session_scope}, judge={str(args.judge).lower()}",
        flush=True,
    )
    result = run_user(user, questions, args, data_path=data_path)
    result["preflight"] = preflight

    def save_evaluation_checkpoint():
        write_checkpoint(Path(result["run_dir"]), result, data_path, user, questions, args)

    evaluation = evaluate_all(result, judge=args.judge, checkpoint_callback=save_evaluation_checkpoint)
    refresh_request_stats(result, evaluation)
    save_evaluation_checkpoint()
    latest = write_artifacts(result, evaluation, data_path, args)
    print_summary(latest, result, evaluation)
    return 0 if result.get("status") == "COMPLETED" and evaluation.get("inconclusive_count", 0) == 0 else 1


def resolve_dataset_path(data_file=None, data_dir=None, download=False):
    if data_file:
        path = Path(data_file).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"HaluMem data file not found: {path}")
        return path
    path = Path(data_dir or DEFAULT_DATA_DIR).expanduser() / "HaluMem-Long.jsonl"
    if path.exists():
        return path
    if not download:
        raise RuntimeError(f"HaluMem-Long is missing: {path}. Re-run with --download or pass --data-file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    request = urllib.request.Request(DATASET_URL, headers={"User-Agent": "sema-eval/0.1"})
    try:
        with (
            urllib.request.urlopen(request, timeout=300) as response,
            temporary.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        temporary.replace(path)
    except (urllib.error.URLError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download HaluMem-Long: {error}") from error
    return path


def run_preflight(user, questions, args, data_path):
    """Validate an expensive run's immutable inputs without calling a model."""
    sessions = list(user.get("sessions") or [])
    if getattr(args, "session_index", None) is not None:
        session_index = int(args.session_index)
        if session_index < 0 or session_index >= len(sessions):
            raise RuntimeError(f"Preflight failed: session index out of range: {session_index}")
        sessions = [sessions[session_index]]
        session_indices = {session_index}
    elif args.session_limit is not None:
        sessions = sessions[: args.session_limit]
        session_indices = set(range(len(sessions)))
    else:
        session_indices = set(range(len(sessions)))
    if not sessions:
        raise RuntimeError("Preflight failed: selected user has no sessions in the requested scope.")
    if any(int(q.get("session_index", -1)) not in session_indices for q in questions):
        raise RuntimeError("Preflight failed: a question is outside the selected session scope.")
    try:
        runner.split_command(runner.resolve_sema_command(args.sema_command))
    except RuntimeError as error:
        raise RuntimeError(f"Preflight failed: invalid Sema command: {error}") from error
    config = halumem_model_config()
    if args.judge and not config["api_key"]:
        raise RuntimeError("Preflight failed: missing HALUMEM_MODEL_API_KEY or DEEPSEEK_API_KEY.")
    sema_key_env = os.getenv("HALUMEM_MODEL_API_KEY_ENV") or os.getenv("SEMA_API_KEY_ENV")
    if args.judge and sema_key_env and not os.getenv(sema_key_env) and not os.getenv("HALUMEM_MODEL_API_KEY"):
        raise RuntimeError(f"Preflight failed: Sema requires {sema_key_env}, but it is not set. Set that variable or provide HALUMEM_MODEL_API_KEY in this PowerShell.")
    if args.judge and not config["base_url"]:
        raise RuntimeError("Preflight failed: missing HALUMEM_MODEL_BASE_URL or SEMA_BASE_URL.")
    if args.judge and not config["base_url"].startswith(("http://", "https://")):
        raise RuntimeError(f"Preflight failed: invalid model base URL {config['base_url']!r}. Use a plain http:// or https:// URL, not a Markdown link.")
    try:
        free_bytes = shutil.disk_usage(Path(data_path).parent).free
    except OSError as error:
        raise RuntimeError(f"Preflight failed: cannot inspect free disk space: {error}") from error
    if free_bytes < 100 * 1024 * 1024:
        raise RuntimeError("Preflight failed: less than 100 MiB free near the dataset directory.")
    return {
        "dataset_path": str(data_path),
        "dataset_sha256": sha256_file(data_path),
        "user_id": str(user["uuid"]),
        "session_count": len(sessions),
        "question_count": len(questions),
        "model": config["model"],
        "model_source": config.get("model_source", "unknown"),
        "sema_command": runner.resolve_sema_command(args.sema_command),
        "free_disk_bytes": free_bytes,
    }


def load_users(path):
    users = []
    try:
        with Path(path).open("r", encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    user = json.loads(line)
                    if not isinstance(user, dict) or not user.get("uuid") or not isinstance(user.get("sessions"), list):
                        raise RuntimeError("HaluMem data contains an invalid user record.")
                    users.append(user)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid HaluMem dataset: {path}: {error}") from error
    if not users:
        raise RuntimeError("HaluMem dataset is empty.")
    return users


def select_user(users, user_id=None):
    if user_id:
        for user in users:
            if str(user.get("uuid")) == str(user_id):
                return user
        raise RuntimeError(f"HaluMem user not found: {user_id}")
    return users[0]


def extract_user_name(persona_info):
    """Extract the dataset display name without sending persona data elsewhere."""
    if isinstance(persona_info, dict):
        name = persona_info.get("name") or persona_info.get("Name")
        if name:
            return str(name).strip()
    text = str(persona_info or "")
    match = re.search(r"(?:^|[;\n])\s*(?:\[[^\]]+\]\s*)?Name\s*:\s*([^;\n]+)", text, re.IGNORECASE)
    if match and match.group(1).strip():
        return match.group(1).strip()
    raise ValueError("persona_info does not contain a display name")


def halumem_model_config():
    """Resolve the benchmark model from explicit or active Sema settings.
    Benchmark-specific variables take precedence. When they are absent, use
    the active Sema runtime variables so the adapter cannot silently run a
    different model from the one selected for Sema.
    """
    runner.load_eval_env_file()
    explicit_key = os.getenv("HALUMEM_MODEL_API_KEY")
    key_env_name = os.getenv("HALUMEM_MODEL_API_KEY_ENV") or os.getenv("SEMA_API_KEY_ENV")
    api_key = explicit_key or (os.getenv(key_env_name, "") if key_env_name else "") or os.getenv("DEEPSEEK_API_KEY", "")
    if explicit_key:
        key_source = "HALUMEM_MODEL_API_KEY"
    elif key_env_name and os.getenv(key_env_name):
        key_source = key_env_name
    elif os.getenv("DEEPSEEK_API_KEY"):
        key_source = "DEEPSEEK_API_KEY"
    else:
        key_source = ""
    if os.getenv("HALUMEM_MODEL"):
        model_source = "HALUMEM_MODEL"
    elif os.getenv("SEMA_MODEL"):
        model_source = "SEMA_MODEL"
    elif os.getenv("DEEPSEEK_MODEL"):
        model_source = "DEEPSEEK_MODEL"
    else:
        model_source = "default"
    if os.getenv("HALUMEM_MODEL_BASE_URL"):
        base_url_source = "HALUMEM_MODEL_BASE_URL"
    elif os.getenv("SEMA_BASE_URL"):
        base_url_source = "SEMA_BASE_URL"
    else:
        base_url_source = "default"
    return {
        "api_key": api_key,
        "base_url": runner.normalize_base_url(os.getenv("HALUMEM_MODEL_BASE_URL") or os.getenv("SEMA_BASE_URL") or "https://api.deepseek.com"),
        "model": os.getenv("HALUMEM_MODEL") or os.getenv("SEMA_MODEL") or os.getenv("DEEPSEEK_MODEL") or "deepseek-chat",
        "max_tokens": int(os.getenv("HALUMEM_MODEL_MAX_TOKENS") or DEFAULT_MODEL_MAX_TOKENS),
        "temperature": float(os.getenv("HALUMEM_MODEL_TEMPERATURE") or DEFAULT_MODEL_TEMPERATURE),
        "timeout": int(os.getenv("HALUMEM_MODEL_TIMEOUT") or DEFAULT_MODEL_TIMEOUT),
        "model_source": model_source,
        "base_url_source": base_url_source,
        "api_key_source": key_source,
    }


def _model_url(config):
    url = str(config.get("base_url") or "").rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def select_questions(user, limit, question_ids=None):
    wanted = {str(item) for item in (question_ids or [])}
    selected = []
    for session_index, session in enumerate(user.get("sessions") or []):
        for question_index, question in enumerate(session.get("questions") or []):
            item = dict(question)
            item["session_index"] = session_index
            item["question_index"] = question_index
            if wanted and str(item.get("question_id") or item.get("id") or "") not in wanted:
                continue
            selected.append(item)
            if not wanted and limit is not None and len(selected) >= limit:
                return selected
    return selected[:limit] if not wanted and limit is not None else selected


def load_checkpoint(run_dir):
    path = Path(run_dir).expanduser().resolve() / "checkpoint.json"
    if not path.exists():
        raise RuntimeError(f"HaluMem checkpoint not found: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid HaluMem checkpoint: {path}: {error}") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RuntimeError(f"Unsupported HaluMem checkpoint: {path}")
    return value


def _checkpoint_question_ids(questions):
    return [str(q.get("question_id") or q.get("id") or f"{q.get('session_index')}:{q.get('question_index')}") for q in questions]


def _checkpoint_scope(user, questions, args):
    return {
        "user_id": str(user["uuid"]),
        "question_ids": _checkpoint_question_ids(questions),
        "session_limit": args.session_limit,
        "question_limit": args.question_limit,
        "question_id": list(args.question_id or []),
        "formation_retrieval_tool_limit": getattr(
            args,
            "formation_retrieval_tool_limit",
            DEFAULT_FORMATION_RETRIEVAL_TOOL_LIMIT,
        ),
        "stage_summary_max_tokens": getattr(args, "stage_summary_max_tokens", DEFAULT_STAGE_SUMMARY_MAX_TOKENS),
        "maintenance_timeout": getattr(args, "maintenance_timeout", 180),
        "operation_hard_timeout": getattr(args, "operation_hard_timeout", 900),
        "stm_budget": getattr(args, "stm_budget", None),
        "wm_budget": getattr(args, "wm_budget", None),
    }


def write_checkpoint(run_dir, result, data_path, user, questions, args):
    config = halumem_model_config()
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_path": str(data_path or result.get("dataset_path") or ""),
        "dataset_sha256": sha256_file(data_path) if data_path else "",
        "scope": result.get("_base_checkpoint_scope") or _checkpoint_scope(user, questions, args),
        "model": {key: config[key] for key in ("base_url", "model", "max_tokens", "temperature", "timeout")},
        "result": result,
    }
    write_json(Path(run_dir) / "checkpoint.json", payload)


def validate_resume_checkpoint(checkpoint, data_path, user, questions, args):
    if checkpoint.get("dataset_sha256") != sha256_file(data_path):
        raise RuntimeError("Cannot resume: dataset hash does not match the checkpoint.")
    scope = checkpoint.get("scope") or {}
    current = _checkpoint_scope(user, questions, args)
    targeted_resume = getattr(args, "session_index", None) is not None
    for key in (
        "user_id",
        "question_ids",
        "session_limit",
        "question_limit",
        "question_id",
        "formation_retrieval_tool_limit",
        "stage_summary_max_tokens",
        "maintenance_timeout",
        "operation_hard_timeout",
        "stm_budget",
        "wm_budget",
    ):
        if targeted_resume and key in {
            "question_ids",
            "session_limit",
            "question_limit",
            "question_id",
            "formation_retrieval_tool_limit",
        }:
            continue
        if (
            key
            in {
                "formation_retrieval_tool_limit",
                "stage_summary_max_tokens",
                "maintenance_timeout",
                "operation_hard_timeout",
                "stm_budget",
                "wm_budget",
            }
            and key not in scope
        ):
            continue
        if scope.get(key) != current.get(key):
            raise RuntimeError(f"Cannot resume: benchmark scope field {key!r} does not match the checkpoint.")
    if targeted_resume:
        target = int(args.session_index)
        saved_result = checkpoint.get("result") or {}
        completed = {int(item.get("session_index", index)) for index, item in enumerate(saved_result.get("sessions") or [])}
        execution = saved_result.get("execution") or {}
        execution_index = execution.get("session_index")
        if target not in completed and execution_index is not None and int(execution_index) != target:
            raise RuntimeError(f"Cannot target session index {target}: checkpoint is currently at session index {execution_index}.")
    saved_model = checkpoint.get("model") or {}
    current_model = halumem_model_config()
    for key in ("base_url", "model", "max_tokens", "temperature", "timeout"):
        if saved_model.get(key) != current_model.get(key):
            raise RuntimeError(f"Cannot resume: model configuration field {key!r} does not match the checkpoint.")


def _classify_recovery_error(error):
    """Classify a failure without deciding to skip the continuous session."""
    text = str(error or "").lower()
    if isinstance(
        error,
        (runner.BridgeTimeoutError, TimeoutError, OSError, subprocess.SubprocessError),
    ):
        return "transient"
    transient_fragments = (
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "connection aborted",
        "temporarily unavailable",
        " 429",
        " 500:",
        " 502:",
        " 503:",
        " 504:",
        "rate limit",
        "server error",
        "insufficient tool messages following tool_calls",
    )
    if any(fragment in text for fragment in transient_fragments):
        return "transient"
    if "stage_summary exceeds" in text:
        return "summary_overflow"
    if "reached the" in text and "tool-call limit" in text:
        return "tool_limit"
    return "permanent"


def refresh_request_stats(result, evaluation):
    """Rebuild totals, rather than charge cached judgments again on resume."""
    runtime = result.get("runtime_request_stats") or {}
    evaluator = evaluation.get("request_stats") or {}
    stats = {**runtime, **evaluator, "accounting_version": 2}
    for key in USAGE_KEYS:
        stats[key] = int(runtime.get(key, 0)) + int(evaluator.get(key, 0))
    stats["observed_model_requests"] = int(runtime.get("observed_model_requests", 0)) + int(evaluator.get("judge_requests", 0)) + int(evaluator.get("hypothesis_requests", 0))
    stats["coverage"] = "legacy_incomplete" if result.get("legacy_request_stats") else "observed_only"
    legacy = result.get("legacy_request_stats") or {}
    for key in ("bridge_commands", "bridge_instances", "bridge_instances_started"):
        stats[key] = int(runtime.get(key, 0)) + int(legacy.get(key, 0))
    result["request_stats"] = stats
    result["actual_requests"] = None
    result["retry_requests"] = len(result.get("recovery_attempts") or []) + int(evaluator.get("judge_retry_requests", 0)) + int(evaluator.get("hypothesis_retry_requests", 0))


def _merge_bridge_stats(total, bridge):
    if bridge is None:
        return
    total["bridge_instances"] += 1
    total["bridge_commands"] += int(getattr(bridge, "request_count", 0))
    usage = getattr(bridge, "usage", {}) or {}
    for key in RUNTIME_COUNTERS:
        if key.startswith("bridge_"):
            continue
        total[key] = int(total.get(key, 0)) + int(usage.get(key, 0) or 0)
    if getattr(bridge, "stderr", ""):
        total.setdefault("stderr", []).append(str(bridge.stderr))


def _close_bridge(bridge, total, abort=False):
    if bridge is None:
        return
    try:
        if abort:
            bridge.abort()
    finally:
        try:
            bridge.__exit__(None, None, None)
        finally:
            _merge_bridge_stats(total, bridge)


def _open_halumem_bridge(workspace, home, args, progress, stats, event_callback=None):
    bridge = runner.BridgeClient(
        workspace,
        home,
        sema_command=args.sema_command,
        turn_timeout=args.turn_timeout,
        event_callback=event_callback or progress.on_event,
    )
    try:
        bridge.__enter__()
        stats["bridge_instances_started"] += 1
        if args.stm_budget is not None:
            progress.notice("configuring explicit memory budgets")
            bridge.configure_budgets(args.stm_budget, args.wm_budget)
        return bridge
    except Exception:
        _close_bridge(bridge, stats, abort=True)
        raise


def run_user(user, questions, args, data_path=None):
    run_started = time.monotonic()
    if args.resume:
        run_dir = Path(args.resume).expanduser().resolve()
        root = run_dir
        workspace = root / "workspace"
        home = root / "sema_home"
        checkpoint = load_checkpoint(run_dir)
        saved = checkpoint.get("result") or {}
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = runner.RUNS_DIR / "halumem" / stamp
        root = run_dir
        workspace = root / "workspace"
        home = root / "sema_home"
        checkpoint = {}
        saved = {}
    prior_request_stats = dict(saved.get("runtime_request_stats") or {})
    workspace.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    sessions = list(user.get("sessions") or [])
    if args.session_limit is not None:
        sessions = sessions[: args.session_limit]
    if getattr(args, "session_index", None) is not None:
        target_session_index = int(args.session_index)
        if target_session_index >= len(sessions):
            raise RuntimeError(f"Target session index {target_session_index} is outside the resumed session scope.")
        selected_session_indices = [target_session_index]
    else:
        target_session_index = None
        selected_session_indices = list(range(len(sessions)))
    selected_session_set = set(selected_session_indices)
    question_keys = {(q["session_index"], q["question_index"]): q for q in questions if q["session_index"] in selected_session_set}
    selected_sessions = [sessions[index] for index in selected_session_indices]
    planned_update_requests = sum(1 for session in selected_sessions for memory in session.get("memory_points") or [] if str(memory.get("is_update")).strip().lower() == "true" and memory.get("original_memories") and "memories_from_system" not in memory)
    planned_request_count = len(selected_sessions) + len(question_keys) + planned_update_requests
    progress = ReplayProgress(len(selected_sessions), len(question_keys))
    progress.print_plan()
    result = {
        "user_id": user["uuid"],
        "persona_info": user.get("persona_info") or "",
        "question_count": len(questions),
        "session_count": len(sessions) if saved else len(selected_sessions),
        "workspace": str(workspace),
        "sema_home": str(home),
        "status": "INCONCLUSIVE",
        "run_quality": "inconclusive",
        "history_user_turns": 0,
        "history_total": sum(len(sessions[index].get("dialogue") or []) for index in selected_session_indices),
        "total_requests": planned_request_count,
        "planned_requests": planned_request_count,
        "actual_requests": 0,
        "retry_requests": 0,
        "history_assistant_turns_generated": 0,
        "sessions": [],
        "questions": [],
        "events": [],
        "errors": [],
        "current_error": "",
        "recovered_errors": [],
        "unresolved_errors": [],
        "recovery_attempts": [],
        "question_checkpoints": {},
        "request_stats": {},
        "execution": {"operation": "initializing", "status": "pending"},
        "unavailable_extraction_count": 0,
        "budget": {"stm": args.stm_budget, "wm": args.wm_budget},
        "run_dir": str(run_dir),
        "dataset_path": str(data_path) if data_path else checkpoint.get("dataset_path", ""),
    }
    if target_session_index is not None:
        result["diagnostic_target_session_index"] = target_session_index
        if args.resume:
            result["diagnostic_source_run"] = str(run_dir)
    if saved:
        if target_session_index is not None:
            result["_base_checkpoint_scope"] = checkpoint.get("scope") or {}
        historical_errors = list(saved.get("errors") or [])
        result.update(saved)
        if "runtime_request_stats" not in saved:
            result["legacy_request_stats"] = dict(saved.get("request_stats") or {})
        result["run_dir"] = str(run_dir)
        result["workspace"] = str(workspace)
        result["sema_home"] = str(home)
        result["errors"] = []
        result["current_error"] = ""
        result["unresolved_errors"] = []
        result.setdefault("question_checkpoints", {})
        if target_session_index is not None:
            result["_base_checkpoint_scope"] = checkpoint.get("scope") or {}
            result["diagnostic_target_session_index"] = target_session_index
        if historical_errors:
            result.setdefault("resume_history", []).append({"errors": historical_errors})
    checkpoint_lock = threading.Lock()

    def save_checkpoint():
        with checkpoint_lock:
            write_checkpoint(run_dir, result, data_path, user, questions, args)

    def on_bridge_event(event):
        progress.on_event(event)
        event_type = str((event or {}).get("type") or "")
        payload = (event or {}).get("payload") or {}
        if event_type in {
            "ltm_maintenance_stage_started",
            "ltm_maintenance_stage_completed",
        }:
            execution = result.get("execution") or {}
            result["execution"] = {
                "operation": "process_session",
                "session_id": execution.get("session_id", ""),
                "stage": payload.get("stage") or "",
                "stage_status": "running" if event_type.endswith("started") else "completed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            save_checkpoint()

    save_checkpoint()
    completed_sessions = {int(item.get("session_index", index)) for index, item in enumerate(result.get("sessions") or [])}
    if target_session_index is not None:
        completed_sessions.discard(target_session_index)
    completed_questions = {(int(item.get("session_index", -1)), int(item.get("question_index", -1))) for item in result.get("questions") or []}
    if target_session_index is not None:
        completed_questions = {key for key in completed_questions if key[0] != target_session_index}
        result["question_checkpoints"] = {key: value for key, value in (result.get("question_checkpoints") or {}).items() if not str(key).startswith(f"{target_session_index}:")}
    bridge = None
    bridge_stats = {
        "bridge_instances": 0,
        "bridge_instances_started": 0,
        "bridge_commands": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_events": 0,
        "stderr": [],
    }

    def start_bridge():
        return _open_halumem_bridge(workspace, home, args, progress, bridge_stats, on_bridge_event)

    def restart_bridge(reason):
        nonlocal bridge
        progress.notice(f"bridge restart reason={reason}")
        _close_bridge(bridge, bridge_stats, abort=True)
        bridge = None
        bridge = start_bridge()

    def start_bridge_with_recovery():
        """Start the supervisor-managed bridge with bounded startup retries."""
        nonlocal bridge
        attempts = []
        for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
            try:
                bridge = start_bridge()
                if attempts:
                    result["recovered_errors"].extend(attempts)
                    save_checkpoint()
                return bridge
            except Exception as error:
                kind = _classify_recovery_error(error)
                item = {
                    "operation": "bridge_start",
                    "attempt": attempt,
                    "kind": kind,
                    "error": str(error),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                attempts.append(item)
                result["recovery_attempts"].append(item)
                result["current_error"] = str(error)
                save_checkpoint()
                if kind not in {"transient", "summary_overflow"} or attempt >= MAX_RECOVERY_ATTEMPTS:
                    result["unresolved_errors"].append(item)
                    raise
                result["retry_requests"] += 1
                progress.notice(f"retry bridge_start attempt={attempt + 1}/{MAX_RECOVERY_ATTEMPTS} kind={kind}")
                time.sleep(RECOVERY_BACKOFF_SECONDS[min(attempt - 1, len(RECOVERY_BACKOFF_SECONDS) - 1)])
        raise RuntimeError("bridge_start failed after recovery attempts")

    def run_with_recovery(label, operation, summary_limit=None):
        """Retry only recoverable failures while preserving the same run state."""
        nonlocal bridge
        attempts = []
        effective_summary_limit = summary_limit
        for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
            try:
                value = operation(bridge, effective_summary_limit)
                if attempts:
                    result["recovered_errors"].extend(attempts)
                    save_checkpoint()
                return value
            except Exception as error:
                kind = _classify_recovery_error(error)
                item = {
                    "operation": label,
                    "attempt": attempt,
                    "kind": kind,
                    "error": str(error),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                attempts.append(item)
                result["recovery_attempts"].append(item)
                save_checkpoint()
                can_retry = kind in {"transient", "summary_overflow"} and attempt < MAX_RECOVERY_ATTEMPTS
                if not can_retry:
                    result["current_error"] = str(error)
                    result["unresolved_errors"].append(item)
                    raise
                result["retry_requests"] += 1
                progress.notice(f"retry {label} attempt={attempt + 1}/{MAX_RECOVERY_ATTEMPTS} kind={kind}")
                if kind == "summary_overflow" and effective_summary_limit is not None:
                    effective_summary_limit = max(int(effective_summary_limit) * 2, 2000)
                    item["next_stage_summary_max_tokens"] = effective_summary_limit
                restart_bridge(str(error))
                time.sleep(RECOVERY_BACKOFF_SECONDS[min(attempt - 1, len(RECOVERY_BACKOFF_SECONDS) - 1)])
        raise RuntimeError(f"{label} failed after recovery attempts")

    try:
        start_bridge_with_recovery()
        for session_ordinal, session_index in enumerate(selected_session_indices, start=1):
            session = sessions[session_index]
            session_id = f"halumem-session-{session_index}"
            if session_index in completed_sessions:
                progress.start(
                    "session",
                    session_ordinal,
                    len(selected_session_indices),
                    f"session={session_id} resumed",
                )
                progress.finish("completed", "checkpoint")
                session_record = next(item for item in result["sessions"] if int(item.get("session_index", -1)) == session_index)
                processed = session_record.get("adapter_session_result") or {}
            else:
                session_started = time.monotonic()
                progress.start(
                    "session",
                    session_ordinal,
                    len(selected_session_indices),
                    f"session={session_id}",
                )
                result["execution"] = {
                    "operation": "process_session",
                    "session_id": session_id,
                    "session_index": session_index,
                    "status": "running",
                }
                save_checkpoint()
                turns = [
                    {
                        "role": turn.get("role"),
                        "content": turn.get("content") or "",
                        "timestamp": turn.get("timestamp") or session.get("start_time"),
                    }
                    for turn in session.get("dialogue") or []
                ]
                processed = run_with_recovery(
                    f"session:{session_id}",
                    lambda active_bridge, summary_limit: active_bridge.process_session(
                        session_id,
                        session.get("start_time"),
                        turns,
                        maintenance_timeout_seconds=args.maintenance_timeout,
                        stage_summary_max_tokens=summary_limit,
                        formation_retrieval_tool_limit=args.formation_retrieval_tool_limit,
                        operation_hard_timeout_seconds=args.operation_hard_timeout,
                    ),
                    summary_limit=args.stage_summary_max_tokens,
                )
                session_record = {
                    "session_index": session_index,
                    "memory_points": json.loads(json.dumps(session.get("memory_points") or [], ensure_ascii=False)),
                    "dialogue": session.get("dialogue") or [],
                    "extracted_memories": [item.get("memory") for item in processed.get("extracted_memories") or [] if item.get("memory")],
                    "extracted_memory_records": processed.get("extracted_memories") or [],
                    "unavailable_changed_objects": processed.get("unavailable_changed_objects") or [],
                    "add_dialogue_duration_ms": 0,
                    "adapter_session_result": processed,
                    "digest_status": (processed.get("maintenance") or {}).get("maintenance_summary_status") or (processed.get("maintenance") or {}).get("semantic_digest_status", "missing"),
                    "digest_stage_count": (processed.get("maintenance") or {}).get("digest_stage_count", 0),
                    "previous_digest_consumed": bool((processed.get("maintenance") or {}).get("previous_digest_consumed")),
                    "duration_ms": int((time.monotonic() - session_started) * 1000),
                }
                if session.get("is_generated_qa_session", False):
                    session_record["is_generated_qa_session"] = True
                existing_index = next(
                    (index for index, item in enumerate(result["sessions"]) if int(item.get("session_index", -1)) == session_index),
                    None,
                )
                if existing_index is None:
                    result["sessions"].append(session_record)
                else:
                    result["sessions"][existing_index] = session_record
                completed_sessions.add(session_index)
                result["history_user_turns"] += len([t for t in turns if t.get("role") == "user"])
                result["unavailable_extraction_count"] += int(processed.get("unavailable_changed_object_count") or 0)
                progress.finish("completed")
                result["execution"] = {
                    "operation": "session",
                    "session_id": session_id,
                    "session_index": session_index,
                    "status": "completed",
                }
                save_checkpoint()
                for memory in session_record["memory_points"]:
                    is_update = str(memory.get("is_update")).strip().lower() == "true"
                    if not is_update or not memory.get("original_memories"):
                        continue
                    if "memories_from_system" in memory:
                        continue
                    update_search = run_with_recovery(
                        f"update_search:session:{session_index}",
                        lambda active_bridge, _summary_limit: active_bridge.memory_search(
                            memory.get("memory_content") or "",
                            top_k=10,
                            purpose="update",
                        ),
                    )
                    memory["memories_from_system"] = [f"{item.get('timestamp')}: {item.get('memory')}" for item in update_search.get("memories") or []]
                    save_checkpoint()
                for key, question in question_keys.items():
                    if key[0] != session_index:
                        continue
                    if key in completed_questions:
                        continue
                    question_index = sum(1 for item in result["questions"] if int(item.get("session_index", -1)) == session_index) + 1
                    question_started = time.monotonic()
                    checkpoint_key = f"{key[0]}:{key[1]}"
                    question_checkpoint = result["question_checkpoints"].get(checkpoint_key) or {}
                    progress.start(
                        "question",
                        question_index,
                        len(question_keys),
                        f"session={session_index}",
                    )
                    result["execution"] = {
                        "operation": "question",
                        "question_id": question.get("question_id") or key,
                        "session_index": session_index,
                        "status": "running",
                    }
                    save_checkpoint()
                    if question_checkpoint.get("search_status") == "completed":
                        search = question_checkpoint.get("search") or {}
                    else:
                        search = run_with_recovery(
                            f"qa_search:{question.get('question_id') or key}",
                            lambda active_bridge, _summary_limit: active_bridge.memory_search(
                                question.get("question") or "",
                                top_k=20,
                                purpose="qa",
                            ),
                        )
                        question_checkpoint.update({"search_status": "completed", "search": search})
                        result["question_checkpoints"][checkpoint_key] = question_checkpoint
                        save_checkpoint()
                    if question_checkpoint.get("hypothesis_status") == "completed":
                        hypothesis = str(question_checkpoint.get("hypothesis") or "")
                        hypothesis_retry_count = int(question_checkpoint.get("hypothesis_retry_count", 0) or 0)
                        hypothesis_usage = question_checkpoint.get("hypothesis_usage") or {}
                    else:
                        hypothesis, hypothesis_attempts, hypothesis_usage = _generate_answer(
                            search.get("context") or "[]",
                            question.get("question") or "",
                        )
                        hypothesis_retry_count = len(hypothesis_attempts)
                        question_checkpoint.update(
                            {
                                "hypothesis_status": "completed",
                                "hypothesis": hypothesis,
                                "hypothesis_retry_count": hypothesis_retry_count,
                                "hypothesis_usage": hypothesis_usage,
                            }
                        )
                        result["question_checkpoints"][checkpoint_key] = question_checkpoint
                        save_checkpoint()
                    question_result = {
                        "question_id": question.get("question_id") or question.get("id") or f"{session_index}:{key[1]}",
                        "session_index": session_index,
                        "question_index": key[1],
                        "question": question.get("question") or "",
                        "hypothesis": hypothesis,
                        "hypothesis_retry_count": hypothesis_retry_count,
                        "hypothesis_usage": hypothesis_usage,
                        "search": search,
                        "events": [],
                        "stability": {"stable": True},
                        "turn_id": "",
                        "error": "",
                        "status": "COMPLETED",
                        "duration_ms": int((time.monotonic() - question_started) * 1000),
                    }
                    result["questions"].append({**question_result, "reference": question})
                    question_checkpoint["status"] = "completed"
                    result["question_checkpoints"][checkpoint_key] = question_checkpoint
                    completed_questions.add(key)
                    progress.finish("completed")
                    result["execution"] = {
                        "operation": "question",
                        "question_id": question_result["question_id"],
                        "session_index": session_index,
                        "status": "completed",
                    }
                    save_checkpoint()
        result["status"] = "COMPLETED"
        result["run_quality"] = "recovered" if result.get("recovery_attempts") else "clean"
    except Exception as error:
        progress.finish("INCONCLUSIVE", str(error))
        result["current_error"] = str(error)
        result["errors"].append(str(error))
        if not result.get("unresolved_errors"):
            result["unresolved_errors"].append(
                {
                    "operation": "run",
                    "kind": _classify_recovery_error(error),
                    "error": str(error),
                }
            )
        result["status"] = "INCONCLUSIVE"
    finally:
        progress.close()
        _close_bridge(bridge, bridge_stats)
        result["stderr"] = "\n".join(bridge_stats.get("stderr") or [])
        bridge_stats.pop("stderr", None)
        cumulative_stats = dict(prior_request_stats)
        for key in RUNTIME_COUNTERS:
            cumulative_stats[key] = int(cumulative_stats.get(key, 0) or 0) + int(bridge_stats.get(key, 0) or 0)
        if bridge_stats.get("stderr"):
            cumulative_stats["stderr"] = list(cumulative_stats.get("stderr") or []) + list(bridge_stats["stderr"])
        result["runtime_request_stats"] = cumulative_stats
        refresh_request_stats(result, {})
        result["duration_ms"] = int((time.monotonic() - run_started) * 1000)
        result["workspace_removed"] = False
        save_checkpoint()
    return result


def _generate_answer(context, question):
    config = halumem_model_config()
    response, _attempts = _model_request_with_retries(
        _model_url(config),
        config["api_key"],
        config["model"],
        "",
        OFFICIAL_RESPONSE_PROMPT.format(context=context, question=question),
        max_tokens=config["max_tokens"],
        temperature=config["temperature"],
        timeout=config["timeout"],
    )
    if response.get("status") == "error":
        raise RuntimeError(response.get("error") or "HaluMem QA model request failed")
    return (
        str(response.get("text") or "").strip(),
        _attempts,
        response.get("usage") or {},
    )


def _model_request_with_retries(url, api_key, model, instruction, prompt, *, max_tokens, temperature, timeout):
    attempts = []
    for attempt in range(1, MAX_RECOVERY_ATTEMPTS + 1):
        response = runner._request_openai_compatible_judge(
            url,
            api_key,
            model,
            instruction,
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            raw_prompt=True,
        )
        if response.get("status") != "error":
            return response, attempts
        kind = _classify_recovery_error(response.get("error"))
        item = {"attempt": attempt, "kind": kind, "error": response.get("error", "")}
        attempts.append(item)
        if kind not in {"transient"} or attempt >= MAX_RECOVERY_ATTEMPTS:
            return response, attempts
        time.sleep(RECOVERY_BACKOFF_SECONDS[min(attempt - 1, len(RECOVERY_BACKOFF_SECONDS) - 1)])
    return response, attempts


def visible_answer(replay):
    answers = []
    for event in replay.get("events") or []:
        if event.get("type") != "final_answer":
            continue
        payload = event.get("payload") or {}
        content = str(payload.get("content") or "").strip()
        if content and content not in answers:
            answers.append(content)
    if answers:
        return "\n\n".join(answers)
    return replay.get("final_answer") or ""


def validate_judge_config():
    config = halumem_model_config()
    missing = []
    if not config["api_key"]:
        missing.append("HALUMEM_MODEL_API_KEY (or DEEPSEEK_API_KEY)")
    if not config["base_url"]:
        missing.append("HALUMEM_MODEL_BASE_URL (or SEMA_BASE_URL)")
    if missing:
        raise RuntimeError("HaluMem DeepSeek scoring requires " + ", ".join(missing) + ". Set them in PowerShell or my_agent/.env.")


def _evaluation_cache_key(*parts):
    return hashlib.sha256(
        json.dumps(
            {"evaluator_revision": OFFICIAL_COMMIT, "parts": parts},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def evaluate_questions(
    questions,
    judge=True,
    checkpoint=None,
    checkpoint_callback=None,
    config=None,
    allow_requests=True,
):
    if not judge:
        return {
            "status": "skipped",
            "items": [],
            "inconclusive_count": 0,
            "request_count": 0,
            "metrics": {"accuracy": None},
        }
    config = config if config is not None else halumem_model_config()
    model = config["model"]
    max_tokens = config["max_tokens"]
    items = []
    request_items = []
    cached_items = checkpoint if isinstance(checkpoint, dict) else {}
    for question in questions:
        question_id = question["question_id"]
        reference = question.get("reference") or {}
        evidence = "\n".join(str(item.get("memory_content") or "") for item in reference.get("evidence") or [])
        prompt = OFFICIAL_QA_PROMPT.format(
            question=reference.get("question") or question.get("question") or "",
            reference_answer=reference.get("answer") or "",
            key_memory_points=evidence,
            response=question.get("hypothesis") or "",
        )
        input_sha256 = _evaluation_cache_key(
            "qa",
            question_id,
            prompt,
            config.get("model"),
            config.get("max_tokens"),
            config.get("temperature"),
        )
        cached = cached_items.get(question_id)
        if isinstance(cached, dict) and cached.get("status") in {"SCORED", "INCONCLUSIVE"} and cached.get("input_sha256") == input_sha256:
            items.append(dict(cached))
            continue
        if not allow_requests:
            raise RuntimeError(f"Offline repair requires cached judgment for QA: {question_id}")
        item = _judge_json(prompt, schema="qa", config=config)
        item.update(
            {
                "question_id": question_id,
                "judge_model": model,
                "input_sha256": input_sha256,
            }
        )
        if item.get("status") == "SCORED":
            item["label"] = item["evaluation_result"]
        items.append(item)
        request_items.append(item)
        if isinstance(checkpoint, dict):
            checkpoint[question_id] = dict(item)
            if checkpoint_callback is not None:
                checkpoint_callback()
    scored = [item for item in items if item.get("status") == "SCORED"]
    return {
        "status": "completed" if len(scored) == len(items) else "partial",
        "judge_model": model,
        "judge_response_models": observed_response_models(items),
        "judge_max_tokens": max_tokens,
        "request_count": len(request_items),
        "actual_request_count": sum(1 + int(item.get("retry_count", 0) or 0) for item in request_items),
        "retry_requests": sum(int(item.get("retry_count", 0) or 0) for item in items),
        "usage": usage_totals(items),
        "items": items,
        "inconclusive_count": len(items) - len(scored),
        "metrics": {
            "accuracy": (sum(item.get("label") == "Correct" for item in items) / len(items)) if items else None,
            "hallucination_rate": (sum(item.get("label") == "Hallucination" for item in items) / len(items)) if items else None,
            "omission_rate": (sum(item.get("label") == "Omission" for item in items) / len(items)) if items else None,
            "accuracy_valid": (sum(item.get("label") == "Correct" for item in scored) / len(scored)) if scored else None,
            "hallucination_rate_valid": (sum(item.get("label") == "Hallucination" for item in scored) / len(scored)) if scored else None,
            "omission_rate_valid": (sum(item.get("label") == "Omission" for item in scored) / len(scored)) if scored else None,
        },
    }


def _judge_json(prompt, max_tokens=None, *, schema="integrity", config=None):
    config = config if config is not None else halumem_model_config()
    attempts = []
    response_models = set()
    for attempt in range(MAX_RECOVERY_ATTEMPTS):
        response = runner._request_openai_compatible_judge(
            _model_url(config),
            config["api_key"],
            config["model"],
            "",
            prompt,
            max_tokens=max_tokens or config["max_tokens"],
            temperature=config["temperature"],
            timeout=config["timeout"],
            raw_prompt=True,
        )
        response_model = response.get("response_model")
        if response_model:
            response_models.add(str(response_model))
        record = {
            "raw_response": response.get("raw_response"),
            "usage": response.get("usage") or {},
            "requested_model": config["model"],
            "response_model": response_model,
        }
        if response.get("status") == "error":
            error = response.get("error") or "judge request failed"
            category = response.get("category", "environment_issue")
            retryable = _classify_recovery_error(error) == "transient"
            parsed, source = None, None
        else:
            parsed, source, error = decode_judgment(response, schema)
            category, retryable = "judge_parse_error", True
        record.update({"error": error, "response_source": source})
        attempts.append(record)
        item = {
            "status": "INCONCLUSIVE",
            "category": category,
            "error": error,
            "raw_response": response.get("raw_response"),
            "response_source": source,
            "requested_model": config["model"],
            "response_model": response_model,
            "response_models": sorted(response_models),
            "request_count": len(attempts),
            "retry_count": len(attempts) - 1,
            "usage": usage_totals(attempts),
            "attempts": list(attempts),
        }
        if parsed is not None:
            item.update(parsed)
            item["status"] = "SCORED"
            item.pop("category", None)
            item.pop("error", None)
            return item
        if not retryable or attempt + 1 == MAX_RECOVERY_ATTEMPTS:
            return item
        time.sleep(RECOVERY_BACKOFF_SECONDS[min(attempt, len(RECOVERY_BACKOFF_SECONDS) - 1)])


def usage_totals(items):
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "usage_events": 0,
    }
    for item in items or []:
        usage = item.get("usage") if isinstance(item, dict) else None
        if not isinstance(usage, dict) or not usage:
            continue
        found = False
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                value = usage.get(key)
                if value is not None:
                    totals[key] += int(value)
                    found = True
            except (TypeError, ValueError):
                pass
        if found:
            totals["usage_events"] += int(usage.get("usage_events", 1))
    return totals


def observed_response_models(items):
    """Return provider model labels seen in judge responses, including retries."""
    models = set()
    for item in items or []:
        models.update(str(value) for value in item.get("response_models") or [] if value)
        if item.get("response_model"):
            models.add(str(item["response_model"]))
        for attempt in item.get("attempts") or []:
            if attempt.get("response_model"):
                models.add(str(attempt["response_model"]))
    return sorted(models)


def evaluate_memory_operations(
    result,
    judge=True,
    checkpoint=None,
    checkpoint_callback=None,
    config=None,
    allow_requests=True,
):
    if not judge:
        return {
            "status": "skipped",
            "memory_integrity": [],
            "memory_accuracy": [],
            "memory_update": [],
            "request_count": 0,
            "metrics": {},
        }
    config = config if config is not None else halumem_model_config()
    model = config["model"]
    integrity = []
    accuracy = []
    updates = []
    request_items = []
    cached_items = checkpoint if isinstance(checkpoint, dict) else {}
    for session_index, session in enumerate(result.get("sessions") or []):
        points = session.get("memory_points") or []
        extracted = [item for item in session.get("extracted_memories") or [] if item]
        extracted_text = "\n".join(extracted)
        dialogue_text = "\n".join(f"[{turn.get('timestamp')}] {turn.get('role')}: {turn.get('content')}" for turn in session.get("dialogue") or [])
        golden_text = "\n".join(str(item.get("memory_content") or "") for item in points if item.get("memory_source") != "interference")
        for point_index, point in enumerate(points):
            is_update = str(point.get("is_update")).strip().lower() == "true"
            cache_key = f"update:{session_index}:{point_index}" if is_update and point.get("original_memories") else f"integrity:{session_index}:{point_index}"
            if is_update and point.get("memories_from_system"):
                operation_prompt = OFFICIAL_UPDATE_PROMPT.format(
                    memories="\n".join(point.get("memories_from_system") or []),
                    updated_memory=point.get("memory_content") or "",
                    original_memory="\n".join(point.get("original_memories") or []),
                )
            else:
                operation_prompt = OFFICIAL_INTEGRITY_PROMPT.format(
                    memories=extracted_text,
                    expected_memory_point=point.get("memory_content") or "",
                )
            input_sha256 = _evaluation_cache_key(
                "operation",
                cache_key,
                operation_prompt,
                config.get("model"),
                config.get("max_tokens"),
                config.get("temperature"),
            )
            cached = cached_items.get(cache_key) or {}
            was_cached = cached.get("status") in {"SCORED", "INCONCLUSIVE"} and cached.get("input_sha256") == input_sha256
            if not was_cached and not allow_requests:
                raise RuntimeError(f"Offline repair requires cached judgment: {cache_key}")
            if is_update and point.get("memories_from_system"):
                judgment = dict(cached)
                if judgment.get("status") not in {"SCORED", "INCONCLUSIVE"}:
                    judgment = _judge_json(operation_prompt, schema="update", config=config)
                    judgment["input_sha256"] = input_sha256
                    if isinstance(checkpoint, dict):
                        checkpoint[cache_key] = dict(judgment)
                        if checkpoint_callback is not None:
                            checkpoint_callback()
                judgment.update({"session_index": session_index, "memory": point})
                updates.append(judgment)
                if not was_cached:
                    request_items.append(judgment)
            else:
                judgment = dict(cached)
                if judgment.get("status") not in {"SCORED", "INCONCLUSIVE"}:
                    judgment = _judge_json(operation_prompt, config=config)
                    judgment["input_sha256"] = input_sha256
                    if isinstance(checkpoint, dict):
                        checkpoint[cache_key] = dict(judgment)
                        if checkpoint_callback is not None:
                            checkpoint_callback()
                judgment.update({"session_index": session_index, "memory": point})
                integrity.append(judgment)
                if not was_cached:
                    request_items.append(judgment)
        for candidate_index, candidate in enumerate(extracted):
            cache_key = f"accuracy:{session_index}:{candidate_index}"
            operation_prompt = OFFICIAL_ACCURACY_PROMPT.format(
                dialogue=dialogue_text,
                golden_memories=golden_text,
                candidate_memory=candidate,
            )
            input_sha256 = _evaluation_cache_key(
                "operation",
                cache_key,
                operation_prompt,
                config.get("model"),
                config.get("max_tokens"),
                config.get("temperature"),
            )
            cached = cached_items.get(cache_key) or {}
            was_cached = cached.get("status") in {"SCORED", "INCONCLUSIVE"} and cached.get("input_sha256") == input_sha256
            if not was_cached and not allow_requests:
                raise RuntimeError(f"Offline repair requires cached judgment: {cache_key}")
            judgment = dict(cached)
            if judgment.get("status") not in {"SCORED", "INCONCLUSIVE"}:
                judgment = _judge_json(operation_prompt, schema="accuracy", config=config)
                judgment["input_sha256"] = input_sha256
                if isinstance(checkpoint, dict):
                    checkpoint[cache_key] = dict(judgment)
                    if checkpoint_callback is not None:
                        checkpoint_callback()
            judgment.update({"session_index": session_index, "candidate": candidate})
            accuracy.append(judgment)
            if not was_cached:
                request_items.append(judgment)

    def ratio(items, predicate):
        valid = [item for item in items if item.get("status") == "SCORED"]
        return (sum(1 for item in valid if predicate(item)) / len(valid)) if valid else None

    return {
        "status": "partial" if any(item.get("status") != "SCORED" for item in [*integrity, *accuracy, *updates]) else "completed",
        "judge_model": model,
        "judge_response_models": observed_response_models([*integrity, *accuracy, *updates]),
        "judge_max_tokens": config["max_tokens"],
        "request_count": len(request_items),
        "actual_request_count": sum(1 + int(item.get("retry_count", 0) or 0) for item in request_items),
        "retry_requests": sum(int(item.get("retry_count", 0) or 0) for item in [*integrity, *accuracy, *updates]),
        "memory_integrity": integrity,
        "memory_accuracy": accuracy,
        "memory_update": updates,
        "usage": usage_totals([*integrity, *accuracy, *updates]),
        "metrics": {
            "memory_integrity_recall": ratio(integrity, lambda item: str(item.get("score")) == "2"),
            "memory_accuracy": ratio(accuracy, lambda item: str(item.get("accuracy_score")) == "2"),
            "memory_update_correct": ratio(updates, lambda item: item.get("evaluation_result") == "Correct"),
            "memory_integrity_inconclusive": sum(item.get("status") != "SCORED" for item in integrity),
            "memory_accuracy_inconclusive": sum(item.get("status") != "SCORED" for item in accuracy),
            "memory_update_inconclusive": sum(item.get("status") != "SCORED" for item in updates),
        },
    }


def _official_eval_input(result, qa, operations):
    """Build the official evaluator shape without changing Sema records."""
    memory_types = {
        "Event Memory": {
            "memory_integrity_acc": 0,
            "memory_update_acc": 0,
            "total_num": 0,
        },
        "Persona Memory": {
            "memory_integrity_acc": 0,
            "memory_update_acc": 0,
            "total_num": 0,
        },
        "Relationship Memory": {
            "memory_integrity_acc": 0,
            "memory_update_acc": 0,
            "total_num": 0,
        },
    }
    payload = {
        "overall_score": {
            "memory_integrity": {},
            "memory_accuracy": {},
            "memory_extraction_f1": 0,
            "memory_update": {},
            "question_answering": {},
            "memory_type_accuracy": memory_types,
        },
        "memory_integrity_records": [],
        "memory_accuracy_records": [],
        "memory_update_records": [],
        "question_answering_records": [],
    }

    def memory_type(value):
        return value if value in memory_types else "Event Memory"

    def importance(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 1.0

    for item in operations.get("memory_integrity") or []:
        point = item.get("memory") or {}
        payload["memory_integrity_records"].append(
            {
                "memory_source": point.get("memory_source", "target"),
                "importance": importance(point.get("importance")),
                "memory_type": memory_type(point.get("memory_type")),
                "memory_integrity_score": (int(item["score"]) if item.get("status") == "SCORED" and str(item.get("score")) in {"0", "1", "2"} else None),
            }
        )
    for item in operations.get("memory_accuracy") or []:
        payload["memory_accuracy_records"].append(
            {
                "memory_accuracy_score": (int(item["accuracy_score"]) if item.get("status") == "SCORED" and str(item.get("accuracy_score")) in {"0", "1", "2"} else None),
                "is_included_in_golden_memories": str(item.get("is_included_in_golden_memories", "false")),
            }
        )
    for item in operations.get("memory_update") or []:
        point = item.get("memory") or {}
        payload["memory_update_records"].append(
            {
                "memory_update_type": item.get("evaluation_result") if item.get("status") == "SCORED" else None,
                "importance": importance(point.get("importance")),
                "memory_type": memory_type(point.get("memory_type")),
            }
        )
    for item in qa.get("items") or []:
        payload["question_answering_records"].append(
            {
                "result_type": item.get("label") if item.get("status") == "SCORED" else None,
            }
        )
    return payload


def _run_official_aggregation(result, qa, operations):
    """Run the pinned evaluator when its complete input contract is available."""
    payload = _official_eval_input(result, qa, operations)
    try:
        aggregated = official_aggregate_eval_results(payload)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        return {
            "status": "completed_qa_only",
            "scope": "question_answering",
            "full_aggregation_status": "unavailable_empty_or_unmapped_category",
            "full_aggregation_error": str(error),
            "result": {"overall_score": {"question_answering": aggregate_question_answering(payload["question_answering_records"])}},
            "input": payload,
        }
    return {
        "status": "completed",
        "scope": "full",
        "result": aggregated,
        "input": payload,
    }


def evaluate_all(result, judge=True, checkpoint_callback=None, config=None, allow_requests=True):
    evaluation_checkpoint = result.setdefault("evaluation_checkpoint", {"qa": {}, "operations": {}})
    qa = evaluate_questions(
        result.get("questions") or [],
        judge=judge,
        checkpoint=evaluation_checkpoint.setdefault("qa", {}),
        checkpoint_callback=checkpoint_callback,
        config=config,
        allow_requests=allow_requests,
    )
    operations = evaluate_memory_operations(
        result,
        judge=judge,
        checkpoint=evaluation_checkpoint.setdefault("operations", {}),
        checkpoint_callback=checkpoint_callback,
        config=config,
        allow_requests=allow_requests,
    )
    hypothesis_usage = usage_totals([{"usage": item.get("hypothesis_usage") or {}} for item in result.get("questions") or []])
    qa_usage = qa.get("usage") or {}
    operation_usage = operations.get("usage") or {}
    usage = {
        key: int(hypothesis_usage.get(key, 0) or 0) + int(qa_usage.get(key, 0) or 0) + int(operation_usage.get(key, 0) or 0)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "usage_events",
        )
    }
    official_aggregation = _run_official_aggregation(result, qa, operations) if judge else {"status": "skipped"}
    qa_metrics = qa.get("metrics") or {}
    diagnostic_metrics = operations.get("metrics") or {}
    return {
        "status": "skipped" if not judge else qa.get("status", "partial"),
        "judge_model": qa.get("judge_model"),
        "judge_response_models": sorted(
            {
                str(model_name)
                for model_name in [
                    *(qa.get("judge_response_models") or []),
                    *(operations.get("judge_response_models") or []),
                ]
                if model_name
            }
        ),
        "judge_max_tokens": qa.get("judge_max_tokens"),
        "request_stats": {
            "judge_requests": sum(
                int(item.get("request_count", 1 + int(item.get("retry_count", 0))))
                for item in [
                    *(qa.get("items") or []),
                    *(operations.get("memory_integrity") or []),
                    *(operations.get("memory_accuracy") or []),
                    *(operations.get("memory_update") or []),
                ]
            ),
            "hypothesis_requests": sum(1 + int(item.get("hypothesis_retry_count", 0) or 0) for item in result.get("questions") or []),
            "hypothesis_retry_requests": sum(int(item.get("hypothesis_retry_count", 0) or 0) for item in result.get("questions") or []),
            "judge_retry_requests": int(qa.get("retry_requests", 0)) + int(operations.get("retry_requests", 0)),
            **usage,
        },
        "items": qa.get("items") or [],
        "inconclusive_count": int(qa.get("inconclusive_count", 0)),
        "diagnostic_inconclusive_count": sum(
            diagnostic_metrics.get(key, 0)
            for key in (
                "memory_integrity_inconclusive",
                "memory_accuracy_inconclusive",
                "memory_update_inconclusive",
            )
        ),
        "metrics": dict(qa_metrics),
        "qa_metrics": dict(qa_metrics),
        "diagnostic_metrics": dict(diagnostic_metrics),
        "official_aggregation": official_aggregation,
        "operations": operations,
    }


def write_artifacts(result, evaluation, data_path, args):
    stamp = Path(result.get("run_dir") or "").name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = Path(result.get("run_dir") or runner.RUNS_DIR / "halumem" / stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    model_config = halumem_model_config()
    metadata = {
        "benchmark": "HaluMem-Long",
        "evaluation_mode": "halumem-official-compatible-adapter",
        "dataset_path": str(data_path),
        "dataset_sha256": sha256_file(data_path),
        "user_id": result["user_id"],
        "question_count": result["question_count"],
        "session_count": result["session_count"],
        "questions_completed": len(result.get("questions") or []),
        "sessions_completed": len(result.get("sessions") or []),
        "run_status": result.get("status"),
        "run_quality": result.get("run_quality", "unknown"),
        "diagnostic_target_session_index": result.get("diagnostic_target_session_index"),
        "diagnostic_source_run": result.get("diagnostic_source_run"),
        "errors": result.get("errors") or [],
        "current_error": result.get("current_error", ""),
        "recovered_errors": result.get("recovered_errors") or [],
        "unresolved_errors": result.get("unresolved_errors") or [],
        "bridge_stderr": result.get("stderr") or "",
        "sema_command": runner.resolve_sema_command(args.sema_command),
        "model": model_config["model"],
        "model_source": model_config.get("model_source", "unknown"),
        "judge_model": evaluation.get("judge_model", model_config["model"]),
        "judge_response_models": evaluation.get("judge_response_models", []),
        "model_base_url": model_config["base_url"],
        "judge_base_url": model_config["base_url"],
        "model_max_tokens": model_config["max_tokens"],
        "model_temperature": model_config["temperature"],
        "model_timeout": model_config["timeout"],
        "stm_budget": args.stm_budget,
        "wm_budget": args.wm_budget,
        "maintainer_profile": "recent_review,episode_review,completion",
        "historical_session_finalization": True,
        "historical_session_boundary": "current session slot range",
        "adapter_contract": "HaluMem official eval input/output contract",
        "evaluator_source": "MemTensor/HaluMem official vendored source",
        "evaluator_revision": OFFICIAL_COMMIT,
        "evaluator_source_manifest": source_manifest(),
        "evaluator_logic_sha256": hashlib.sha256(json.dumps(source_manifest(), sort_keys=True).encode("utf-8")).hexdigest(),
        "qa_track": "official_memOS_prompt_and_question_evaluator",
        "extraction_update_track": "sema_diagnostic_mapping_unvalidated",
        "not_official_leaderboard_submission": True,
        "official_answer_prompt_sha256": hashlib.sha256(OFFICIAL_RESPONSE_PROMPT.encode("utf-8")).hexdigest(),
        "official_question_evaluator_prompt_sha256": hashlib.sha256(OFFICIAL_QA_PROMPT.encode("utf-8")).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sema_commit": git_revision(),
        "adapter_commit": git_revision(),
        "run_duration_ms": result.get("duration_ms", 0),
        "request_plan_count": result.get("total_requests", 0),
        "checkpoint_path": str(run_dir / "checkpoint.json"),
        "workspace_path": str(result.get("workspace") or run_dir / "workspace"),
        "sema_home_path": str(result.get("sema_home") or run_dir / "sema_home"),
        "digest_stage_count": 3,
        "stage_summary_max_tokens": args.stage_summary_max_tokens,
        "operation_hard_timeout": args.operation_hard_timeout,
        "formation_retrieval_tool_limit": args.formation_retrieval_tool_limit,
        "planned_requests": result.get("planned_requests", result.get("total_requests", 0)),
        "actual_bridge_requests": (result.get("request_stats") or {}).get("bridge_commands", 0),
        "retry_requests": result.get("retry_requests", 0),
        "request_stats": result.get("request_stats") or {},
        "actual_requests": result.get("actual_requests", 0),
        "execution_checkpoint": result.get("execution") or {},
        "preflight": result.get("preflight") or {},
        "official_aggregation_status": (evaluation.get("official_aggregation") or {}).get("status"),
    }
    write_json(run_dir / "metadata.json", metadata)
    predictions = [{"question_id": q["question_id"], "hypothesis": q.get("hypothesis", "")} for q in result["questions"]]
    write_jsonl(run_dir / "predictions.jsonl", predictions)
    user_name = result["user_id"]
    if result.get("persona_info"):
        try:
            user_name = extract_user_name(result["persona_info"])
        except ValueError:
            pass
    official_sessions = []
    questions_by_session = {}
    for question in result.get("questions") or []:
        questions_by_session.setdefault(int(question.get("session_index", -1)), []).append(question)
    for session_index, session in enumerate(result.get("sessions") or []):
        official_session = {
            "memory_points": session.get("memory_points") or [],
            "dialogue": session.get("dialogue") or [],
            "extracted_memories": session.get("extracted_memories") or [],
        }
        session_questions = []
        for question in questions_by_session.get(session_index, []):
            reference = dict(question.get("reference") or {})
            reference["context"] = (question.get("search") or {}).get("context") or ""
            reference["system_response"] = question.get("hypothesis") or ""
            session_questions.append(reference)
        if session_questions:
            official_session["questions"] = session_questions
        official_sessions.append(official_session)
    write_jsonl(
        run_dir / "halumem_eval_results.jsonl",
        [
            {
                "uuid": result["user_id"],
                "user_name": user_name,
                "sessions": official_sessions,
            }
        ],
    )
    write_json(run_dir / "evaluation.json", evaluation)
    write_jsonl(run_dir / "per_question.jsonl", merge_questions(result["questions"], evaluation))
    diagnostics = [
        {
            "question_id": q["question_id"],
            "session_index": q["session_index"],
            "turn_id": q.get("turn_id"),
            "stability": q.get("stability"),
            "event_types": [e.get("type") for e in q.get("events") or []],
            "search_metrics": (q.get("search") or {}).get("metrics") or {},
            "status": q.get("status"),
            "error": q.get("error", ""),
        }
        for q in result["questions"]
    ]
    if result.get("errors"):
        diagnostics.append(
            {
                "type": "adapter_error",
                "status": result.get("status"),
                "errors": result.get("errors") or [],
                "stderr": result.get("stderr") or "",
            }
        )
    diagnostics.extend(
        {
            "session_index": index,
            "type": "session",
            "session_result": item.get("adapter_session_result") or {},
        }
        for index, item in enumerate(result.get("sessions") or [])
    )
    write_jsonl(run_dir / "diagnostics.jsonl", diagnostics)
    (run_dir / "transcript.md").write_text(build_transcript(result), encoding="utf-8")
    (run_dir / "summary.md").write_text(build_summary(metadata, result, evaluation), encoding="utf-8")
    latest = {
        "benchmark": "halumem",
        "run_id": stamp,
        "run_dir": str(run_dir),
        "status": result.get("status"),
        "checkpoint_path": str(run_dir / "checkpoint.json"),
        "workspace_path": str(result.get("workspace") or run_dir / "workspace"),
        "sema_home_path": str(result.get("sema_home") or run_dir / "sema_home"),
        "predictions_path": str(run_dir / "predictions.jsonl"),
        "evaluation_path": str(run_dir / "evaluation.json"),
        "halumem_results_path": str(run_dir / "halumem_eval_results.jsonl"),
        "summary_path": str(run_dir / "summary.md"),
    }
    latest_dir = runner.RUNS_DIR / "halumem"
    latest_dir.mkdir(parents=True, exist_ok=True)
    write_json(latest_dir / "latest.json", latest)
    return latest


def merge_questions(questions, evaluation):
    judged = {item.get("question_id"): item for item in evaluation.get("items") or []}
    rows = []
    for question in questions:
        row = {
            key: question.get(key)
            for key in (
                "question_id",
                "session_index",
                "question_index",
                "question",
                "hypothesis",
                "status",
                "error",
            )
        }
        row.update(
            judged.get(
                question["question_id"],
                {"status": "INCONCLUSIVE", "error": "missing evaluation"},
            )
        )
        rows.append(row)
    return rows


def build_transcript(result):
    lines = [
        "# HaluMem-Long Sema Adapter Transcript",
        "",
        "Historical assistant turns are imported as data and are not regenerated by Sema.",
        "",
    ]
    for index, session in enumerate(result.get("sessions") or []):
        processed = session.get("adapter_session_result") or {}
        lines.extend(
            [
                f"## Session {index} [{processed.get('session_id', '')}]",
                "",
                f"- historical session finalization: {str(bool(processed.get('historical_session_finalization'))).lower()}",
                f"- slot range: {json.dumps(processed.get('session_slot_range') or {}, ensure_ascii=False)}",
                f"- maintainable frontier: {processed.get('maintenance', {}).get('maintainable_frontier', '')}",
                f"- coverage target: {processed.get('maintenance', {}).get('coverage_target_slot_seq', '')}",
                f"- Formation: {processed.get('formation', {}).get('reason', '')}",
                f"- Maintainer: {processed.get('maintenance', {}).get('status', '')}",
                f"- extracted memories: {len(session.get('extracted_memories') or [])}",
                "",
            ]
        )
    for question in result["questions"]:
        lines.extend(
            [
                f"## Question {question['question_id']} [{question.get('status')} ]",
                "",
                "### Question",
                "",
                fenced(question.get("question")),
                "",
                "### System response",
                "",
                fenced(question.get("hypothesis")),
                "",
                f"- session_index: {question.get('session_index')}",
                f"- search metrics: {json.dumps((question.get('search') or {}).get('metrics') or {}, ensure_ascii=False)}",
                "",
            ]
        )
    return "\n".join(lines)


def request_summary_lines(result):
    stats = result.get("request_stats") or {}
    return [
        f"- planned adapter operations (not API requests): {result.get('planned_requests', result.get('total_requests', 0))}",
        f"- Bridge commands (not API requests): {stats.get('bridge_commands', 0)}",
        f"- observed model API attempts: {stats.get('observed_model_requests', 'unavailable')}",
        f"- request/token coverage: {stats.get('coverage', 'legacy_unverified')}; not a billing total",
        f"- responses without token usage: {stats.get('responses_without_usage', 'unavailable')}",
    ]


def build_summary(metadata, result, evaluation):
    metrics = evaluation.get("qa_metrics") or evaluation.get("metrics") or {}
    diagnostic_metrics = evaluation.get("diagnostic_metrics") or {}
    official_aggregation = evaluation.get("official_aggregation") or {}
    request_stats = result.get("request_stats") or {}
    lines = [
        "# HaluMem-Long Official-Compatible Adapter Summary",
        "",
        "**This is a reproducible HaluMem-Long single-user subset experiment, not an official leaderboard submission.**",
        "",
        f"- run status: {metadata.get('run_status')}",
        f"- run quality: {metadata.get('run_quality')}",
        f"- user_id: {metadata['user_id']}",
        f"- sessions completed: {metadata.get('sessions_completed', 0)}/{metadata['session_count']}",
        f"- questions completed: {metadata.get('questions_completed', 0)}/{metadata['question_count']}",
        f"- model / judge: {metadata.get('model')} / {metadata.get('judge_model')}",
        f"- dialogue messages imported: {result.get('history_total', 0)}",
        f"- extraction unavailable objects: {result.get('unavailable_extraction_count', 0)}",
        f"- evaluator revision: {metadata['evaluator_revision']}",
        f"- Maintainer profile: {metadata.get('maintainer_profile')}",
        f"- historical session finalization: {str(bool(metadata.get('historical_session_finalization'))).lower()}",
        f"- historical boundary: {metadata.get('historical_session_boundary')}",
        f"- operation idle timeout: {metadata.get('maintenance_timeout', 180)}s",
        f"- operation hard timeout: {metadata.get('operation_hard_timeout', 900)}s",
        f"- QA accuracy (official denominator): {format_metric(metrics.get('accuracy'))}",
        f"- QA hallucination rate (official denominator): {format_metric(metrics.get('hallucination_rate'))}",
        f"- QA omission rate (official denominator): {format_metric(metrics.get('omission_rate'))}",
        f"- QA accuracy (valid only): {format_metric(metrics.get('accuracy_valid'))}",
        f"- evaluator inconclusive: {evaluation.get('inconclusive_count', 0)}",
        f"- diagnostic inconclusive: {evaluation.get('diagnostic_inconclusive_count', 0)}",
        f"- official aggregation status: {official_aggregation.get('status', 'unavailable')}",
        f"- full aggregation status: {official_aggregation.get('full_aggregation_status', 'not_applicable')}",
        f"- official aggregation scope: {official_aggregation.get('scope', 'unknown')}",
        f"- judge requests: {request_stats.get('judge_requests', 0)}",
        f"- retries: {result.get('retry_requests', 0)}",
        f"- observed prompt tokens: {request_stats.get('prompt_tokens', 0)}",
        f"- observed completion tokens: {request_stats.get('completion_tokens', 0)}",
        f"- observed total tokens: {request_stats.get('total_tokens', 0)}",
        f"- usage events observed: {request_stats.get('usage_events', 0)}",
    ]
    lines.extend(request_summary_lines(result))
    if result.get("current_error") or result.get("unresolved_errors"):
        lines.extend(
            [
                "",
                "## Current Unresolved Errors",
                "",
                *[f"- {item.get('error', item)}" if isinstance(item, dict) else f"- {item}" for item in (result.get("unresolved_errors") or [result.get("current_error")])],
            ]
        )
    if result.get("recovered_errors"):
        lines.extend(
            [
                "",
                "## Recovered Errors",
                "",
                *[f"- {item.get('operation', '')}: {item.get('error', item)}" if isinstance(item, dict) else f"- {item}" for item in result["recovered_errors"]],
            ]
        )
    lines.extend(["", "| Question | Adapter status | Judge |", "| --- | --- | --- |"])
    judged = {item.get("question_id"): item for item in evaluation.get("items") or []}
    for question in result["questions"]:
        item = judged.get(question["question_id"], {})
        lines.append(f"| {question['question_id']} | {question.get('status')} | {item.get('label', item.get('status', 'INCONCLUSIVE'))} |")
    lines.extend(
        [
            "",
            "## Diagnostic Track",
            "",
            "Extraction and update metrics below are Sema diagnostics only; their cross-session mapping is not validated as an official HaluMem score.",
            "",
            f"- memory integrity recall: {format_metric(diagnostic_metrics.get('memory_integrity_recall'))}",
            f"- memory accuracy: {format_metric(diagnostic_metrics.get('memory_accuracy'))}",
            f"- memory update correct: {format_metric(diagnostic_metrics.get('memory_update_correct'))}",
            f"- mapping status: {metadata.get('extraction_update_track')}",
            "",
            "## Session Diagnostics",
            "",
            "| Session | Digest | Previous digest consumed | Unavailable provenance |",
            "| ---: | --- | --- | ---: |",
        ]
    )
    for index, session in enumerate(result.get("sessions") or []):
        lines.append(f"| {index} | {session.get('digest_status', 'missing')} ({session.get('digest_stage_count', 0)} stages) | {str(bool(session.get('previous_digest_consumed'))).lower()} | {len(session.get('unavailable_changed_objects') or [])} |")
    unavailable = [item for session in result.get("sessions") or [] for item in session.get("unavailable_changed_objects") or []]
    if unavailable:
        lines.extend(
            [
                "",
                "## Unavailable Provenance Objects",
                "",
                "| Object ID | Title | Reason |",
                "| --- | --- | --- |",
            ]
        )
        for item in unavailable:
            lines.append(f"| {item.get('object_id', '')} | {item.get('title', '')} | {item.get('reason', 'unavailable_provenance')} |")
    lines.extend(
        [
            "",
            "Runtime diagnostics are in diagnostics.jsonl; full event details are in per_question.jsonl. The pinned official source manifest is in metadata.json.",
            "",
        ]
    )
    return "\n".join(lines)


def print_summary(latest, result, evaluation):
    metrics = evaluation.get("qa_metrics") or evaluation.get("metrics") or {}
    request_stats = result.get("request_stats") or {}
    official_aggregation = evaluation.get("official_aggregation") or {}
    lines = [
        "# HaluMem-Long Official-Compatible Result",
        "",
        f"- status: **{result.get('status')}**",
        f"- run quality: **{result.get('run_quality', 'unknown')}**",
        f"- user: **{result['user_id']}**",
        f"- sessions: **{len(result.get('sessions') or [])}/{result['session_count']}**",
        f"- questions: **{len(result['questions'])}/{result['question_count']}**",
        f"- QA accuracy: **{format_metric(metrics.get('accuracy'))}**",
        f"- QA hallucination rate: **{format_metric(metrics.get('hallucination_rate'))}**",
        f"- QA omission rate: **{format_metric(metrics.get('omission_rate'))}**",
        f"- evaluator inconclusive: **{evaluation.get('inconclusive_count', 0)}**",
        f"- diagnostic inconclusive: **{evaluation.get('diagnostic_inconclusive_count', 0)}**",
        f"- official aggregation: **{official_aggregation.get('status', 'unavailable')}**",
        f"- full aggregation: **{official_aggregation.get('full_aggregation_status', 'not_applicable')}**",
        f"- aggregation scope: **{official_aggregation.get('scope', 'unknown')}**",
        f"- observed prompt / completion / total tokens: **{request_stats.get('prompt_tokens', 0)} / {request_stats.get('completion_tokens', 0)} / {request_stats.get('total_tokens', 0)}**",
        f"- retries: **{result.get('retry_requests', 0)}**",
    ]
    lines.extend(request_summary_lines(result))
    if result.get("current_error"):
        lines.append(f"- current error: **{result['current_error']}**")
    lines.extend(
        [
            f"- run directory: {latest['run_dir']}",
            f"- summary: {latest['summary_path']}",
            f"- predictions: {latest['predictions_path']}",
            f"- HaluMem results: {latest.get('halumem_results_path', '')}",
        ]
    )
    text = "\n".join(lines)
    runner.render_terminal_markdown(text)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    with Path(path).open("w", encoding="utf-8") as output:
        for value in values:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision():
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=runner.ROOT_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unavailable"


def fenced(value):
    marker = chr(96) * 3
    return marker + "text\n" + str(value or "").rstrip() + "\n" + marker


def format_metric(value):
    return "n/a" if value is None else f"{float(value) * 100:.2f}%"
