"""Process-local adapter for the Memory Push reranker service."""

import atexit
import json
import math
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .reranker_service import MAX_CONTEXT_TOKENS, MODEL_NAME
from ..token_counter import count_text_tokens


DEFAULT_PYTHON = Path(__file__).resolve().parents[3] / ".venv-reranker" / "Scripts" / "python.exe"
DEFAULT_MODEL_PATH = Path.home() / ".cache" / "huggingface" / "hub" / "models--Qwen--Qwen3-Reranker-0.6B" / "snapshots"
PYTHON_ENV = "SEMA_RERANKER_PYTHON"
MODEL_PATH_ENV = "SEMA_RERANKER_MODEL_PATH"
STARTUP_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120


class RerankerServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rank2Batch:
    model: str
    scores: tuple[dict, ...]


def resolve_python_path(value=None):
    from runtime.setup_capabilities import service_value as configured_value
    path = Path(value or configured_value("reranker", "python_path") or DEFAULT_PYTHON).expanduser()
    if not path.is_file():
        raise RerankerServiceError(f"Reranker Python is not installed at {path}.")
    return path


def resolve_model_path(value=None):
    from runtime.setup_capabilities import service_value as configured_value
    configured = value or configured_value("reranker", "model_path")
    if configured:
        path = Path(configured).expanduser()
    else:
        snapshots = sorted(DEFAULT_MODEL_PATH.glob("*/config.json")) if DEFAULT_MODEL_PATH.is_dir() else []
        if not snapshots:
            raise RerankerServiceError("Reranker model is not installed; automatic download is disabled.")
        path = snapshots[-1].parent
    if not path.is_dir():
        raise RerankerServiceError(f"Reranker model does not exist at {path}.")
    return path


def _prepend_python_path(environment):
    package_root = str(Path(__file__).resolve().parents[1])
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = package_root + (os.pathsep + current if current else "")


class RerankerAdapter:
    def __init__(self, python_path=None, model_path=None, startup_timeout=STARTUP_TIMEOUT_SECONDS, request_timeout=REQUEST_TIMEOUT_SECONDS):
        self.python_path = resolve_python_path(python_path)
        self.model_path = resolve_model_path(model_path)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._process = None
        self._base_url = None
        self._lock = threading.RLock()

    def _read_ready_line(self, output, queue):
        try:
            queue.put(output.readline())
        except Exception as error:
            queue.put(error)

    def _ensure_started(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._base_url:
                return
            environment = os.environ.copy()
            environment["HF_HUB_OFFLINE"] = "1"
            _prepend_python_path(environment)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._process = subprocess.Popen(
                [
                    str(self.python_path),
                    "-m",
                    "memory_engine.ltm.reranker_service",
                    "--model-path",
                    str(self.model_path),
                    "--port",
                    "0",
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=creationflags,
            )
            ready_queue = Queue(maxsize=1)
            threading.Thread(target=self._read_ready_line, args=(self._process.stdout, ready_queue), daemon=True).start()
            try:
                line = ready_queue.get(timeout=self.startup_timeout)
            except Empty as error:
                self.close()
                raise RerankerServiceError("Reranker Service did not become ready in time.") from error
            if isinstance(line, Exception):
                self.close()
                raise RerankerServiceError("Reranker Service readiness pipe failed.") from line
            try:
                message = json.loads(line)
                port = int(message["port"])
                if message.get("event") != "ready" or not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                self.close()
                raise RerankerServiceError("Reranker Service returned an invalid readiness message.") from error
            self._base_url = f"http://127.0.0.1:{port}"
            health = self._request_json("GET", "/health")
            if health.get("status") != "ready" or health.get("model") != MODEL_NAME or health.get("max_context_tokens") != MAX_CONTEXT_TOKENS:
                self.close()
                raise RerankerServiceError("Reranker Service health check failed.")

    def _request_json(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = {"error": {"message": str(error)}}
            raise RerankerServiceError(detail.get("error", {}).get("message", str(error))) from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            raise RerankerServiceError(f"Reranker Service request failed: {error}") from error

    def rerank(self, query, candidates, _diagnostic_profiler=None):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if not isinstance(candidates, (list, tuple)) or not candidates:
            raise ValueError("candidates must be a non-empty sequence.")
        payload = {"query": query, "candidates": list(candidates)}
        with self._lock:
            process = self._process
            process_running = process is not None and (
                not hasattr(process, "poll") or process.poll() is None
            )
            was_started = bool(process_running and self._base_url)
            if _diagnostic_profiler is not None:
                if not was_started:
                    _diagnostic_profiler.mark_cold_start("reranker")
                with _diagnostic_profiler.measure("reranker_service_startup_ms"):
                    self._ensure_started()
                _diagnostic_profiler.increment("reranker_request_count")
                with _diagnostic_profiler.measure("reranker_request_ms"):
                    result = self._request_json("POST", "/rerank", payload)
            else:
                self._ensure_started()
                result = self._request_json("POST", "/rerank", payload)
        if result.get("model") != MODEL_NAME or not isinstance(result.get("scores"), list):
            raise RerankerServiceError("Reranker Service returned incompatible metadata.")
        if len(result["scores"]) != len(candidates):
            raise RerankerServiceError("Reranker Service returned an unexpected score count.")
        for candidate, score in zip(candidates, result["scores"]):
            if not isinstance(score, dict) or score.get("object_id") != candidate.get("object_id"):
                raise RerankerServiceError("Reranker Service returned scores in an invalid order.")
            value = score.get("relevance_score")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                raise RerankerServiceError("Reranker Service returned an invalid relevance score.")
        return Rank2Batch(MODEL_NAME, tuple(result["scores"]))

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            self._base_url = None
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


_default_adapter = None
_default_lock = threading.Lock()


def _get_default_adapter():
    global _default_adapter
    with _default_lock:
        if _default_adapter is None:
            _default_adapter = RerankerAdapter()
        return _default_adapter


def rank_2_candidates(stm_messages, candidates, *, max_items=80, _diagnostic_profiler=None):
    from .memory_push_ranking import build_rank_2_query, prepare_rank_2_candidates
    from .memory_push_ranking import RANK_2_QUERY_MAX_TOKENS

    query = build_rank_2_query(stm_messages, max_tokens=RANK_2_QUERY_MAX_TOKENS)
    available_candidate_tokens = max(
        1,
        MAX_CONTEXT_TOKENS - 1024 - count_text_tokens(query),
    )
    prepared = prepare_rank_2_candidates(
        candidates,
        max_tokens=min(12_000, available_candidate_tokens),
    )
    adapter = _get_default_adapter()
    if _diagnostic_profiler is None:
        batch = adapter.rerank(query, prepared)
    else:
        batch = adapter.rerank(query, prepared, _diagnostic_profiler=_diagnostic_profiler)
    by_id = {item["object_id"]: item for item in batch.scores}
    items = []
    for candidate in prepared:
        score = by_id.get(candidate["object_id"], {}).get("relevance_score")
        if isinstance(score, (int, float)):
            items.append({"object_id": candidate["object_id"], "relevance_score": float(score)})
    items.sort(key=lambda item: (-item["relevance_score"], item["object_id"]))
    return {"status": "ok", "items": items[:max_items], "errors": []}


def rank_2_candidates_for_target(target, candidates, *, max_items=80, _diagnostic_profiler=None):
    """Caller-neutral Rank 2 entry point.

    Memory Push keeps its STM-specific wrapper above.  Retrieval supplies its
    immutable original Query plus Session guidance through this entry point.
    Unlike the Push compatibility wrapper, Retrieval never silently truncates
    Rank 1 output: callers receive an explicit over-cap error for inputs above
    the shared 400-item technical baseline.
    """
    from .memory_push_ranking import RANK_1_MAX_ITEMS, CANDIDATE_MAX_TOKENS, _candidate_text

    if not isinstance(target, str) or not target.strip():
        return {"status": "error", "items": [], "errors": [{"code": "invalid_target", "message": "target must be non-empty."}]}
    if not isinstance(candidates, (list, tuple)):
        return {"status": "error", "items": [], "errors": [{"code": "invalid_candidates", "message": "candidates must be an array."}]}
    if len(candidates) > RANK_1_MAX_ITEMS:
        return {"status": "error", "items": [], "errors": [{"code": "rank_2_input_over_cap", "message": f"Rank 2 accepts at most {RANK_1_MAX_ITEMS} candidates."}]}
    prepared = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("object_id"), str):
            continue
        prepared.append({"object_id": candidate["object_id"], "text": _candidate_text(candidate, max_tokens=CANDIDATE_MAX_TOKENS)})
    if not prepared:
        return {"status": "ok", "items": [], "errors": []}
    adapter = _get_default_adapter()
    batch = adapter.rerank(
        target,
        prepared,
        **({"_diagnostic_profiler": _diagnostic_profiler} if _diagnostic_profiler is not None else {}),
    )
    by_id = {item["object_id"]: item for item in batch.scores}
    items = [
        {"object_id": candidate["object_id"], "relevance_score": float(by_id[candidate["object_id"]]["relevance_score"])}
        for candidate in prepared if candidate["object_id"] in by_id
    ]
    items.sort(key=lambda item: (-item["relevance_score"], item["object_id"]))
    return {"status": "ok", "items": items[: max(1, int(max_items))], "errors": []}


def close_default_adapter():
    global _default_adapter
    with _default_lock:
        adapter, _default_adapter = _default_adapter, None
    if adapter is not None:
        adapter.close()


atexit.register(close_default_adapter)
