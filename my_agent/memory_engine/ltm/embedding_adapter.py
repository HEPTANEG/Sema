"""Process-local adapter for the project-local BGE-M3 embedding service."""

import atexit
import json
import math
import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .embedding_service import EXPECTED_DIMENSION, MODEL_NAME


DEFAULT_MODEL_PATH = Path.home() / ".cache" / "sema" / "models" / "bge-m3"
MODEL_PATH_ENV = "SEMA_EMBEDDING_MODEL_PATH"
STARTUP_TIMEOUT_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120
STARTUP_ATTEMPTS = 2
STARTUP_DIAGNOSTIC_LINES = 20
STARTUP_DIAGNOSTIC_CHARS = 2000


class EmbeddingServiceError(RuntimeError):
    """Raised when the local embedding service cannot fulfil a request."""


@dataclass(frozen=True)
class EmbeddingBatch:
    model: str
    dimension: int
    vectors: tuple[tuple[float, ...], ...]


def resolve_model_path(model_path=None):
    from runtime.setup_capabilities import service_value as value
    path = Path(model_path or value("embedding", "model_path") or DEFAULT_MODEL_PATH).expanduser()
    if not path.is_dir():
        raise EmbeddingServiceError(
            f"Embedding model is not installed at {path}; automatic download is disabled."
        )
    return path


def _prepend_python_path(environment):
    package_root = str(Path(__file__).resolve().parents[1])
    current = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = package_root + (os.pathsep + current if current else "")


class EmbeddingAdapter:
    def __init__(self, model_path=None, startup_timeout=STARTUP_TIMEOUT_SECONDS, request_timeout=REQUEST_TIMEOUT_SECONDS):
        self.model_path = resolve_model_path(model_path)
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self._process = None
        self._base_url = None
        self._lock = threading.RLock()
        self._stderr_tail = deque(maxlen=STARTUP_DIAGNOSTIC_LINES)
        self._stderr_thread = None

    def _read_ready_line(self, output, queue):
        try:
            queue.put(output.readline())
        except Exception as error:
            queue.put(error)

    def _drain_stderr(self, output):
        try:
            for line in iter(output.readline, ""):
                line = line.strip()
                if line:
                    self._stderr_tail.append(line)
        except Exception:
            return

    def _startup_diagnostics(self):
        thread = self._stderr_thread
        process = self._process
        if thread is not None and process is not None and process.poll() is not None:
            thread.join(timeout=0.2)
        detail = " | ".join(self._stderr_tail)
        if len(detail) > STARTUP_DIAGNOSTIC_CHARS:
            detail = detail[-STARTUP_DIAGNOSTIC_CHARS:]
        return detail

    def _startup_error(self, message):
        detail = self._startup_diagnostics()
        return EmbeddingServiceError(f"{message} Startup diagnostics: {detail}" if detail else message)

    def _start_once(self):
        environment = os.environ.copy()
        environment["HF_HUB_OFFLINE"] = "1"
        _prepend_python_path(environment)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._stderr_tail.clear()
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "memory_engine.ltm.embedding_service",
                "--model-path",
                str(self.model_path),
                "--port",
                "0",
                "--shutdown-on-stdin-eof",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self._process.stderr,),
            daemon=True,
        )
        self._stderr_thread.start()
        ready_queue = Queue(maxsize=1)
        threading.Thread(
            target=self._read_ready_line,
            args=(self._process.stdout, ready_queue),
            daemon=True,
        ).start()
        try:
            line = ready_queue.get(timeout=self.startup_timeout)
        except Empty as error:
            raise self._startup_error("Embedding Service did not become ready in time.") from error
        if isinstance(line, Exception):
            raise self._startup_error("Embedding Service readiness pipe failed.") from line
        if not line:
            exit_code = self._process.poll()
            suffix = f" (exit code {exit_code})" if exit_code is not None else ""
            raise self._startup_error(f"Embedding Service exited before readiness{suffix}.")
        try:
            message = json.loads(line)
            port = int(message["port"])
            if message.get("event") != "ready" or not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            preview = line.strip()[:200]
            raise self._startup_error(
                f"Embedding Service returned an invalid readiness message: {preview!r}."
            ) from error
        self._base_url = f"http://127.0.0.1:{port}"
        health = self._request_json("GET", "/health")
        if health.get("status") != "ready" or health.get("dimension") != EXPECTED_DIMENSION:
            raise self._startup_error("Embedding Service health check failed.")

    def _ensure_started(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None and self._base_url:
                return
            last_error = None
            for _attempt in range(STARTUP_ATTEMPTS):
                try:
                    self._start_once()
                    return
                except EmbeddingServiceError as error:
                    last_error = error
                    self.close()
            if last_error is not None:
                raise EmbeddingServiceError(
                    f"Embedding Service failed to start after {STARTUP_ATTEMPTS} attempts. {last_error}"
                ) from last_error
            raise EmbeddingServiceError("Embedding Service failed to start.")

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
            message = detail.get("error", {}).get("message", str(error))
            raise EmbeddingServiceError(message) from error
        except (URLError, OSError, json.JSONDecodeError) as error:
            raise EmbeddingServiceError(f"Embedding Service request failed: {error}") from error

    def _embed(self, texts, input_kind, _diagnostic_profiler=None):
        if not isinstance(texts, (list, tuple)) or not texts or any(not isinstance(text, str) for text in texts):
            raise ValueError("texts must be a non-empty sequence of strings.")
        with self._lock:
            process = self._process
            process_running = process is not None and (
                not hasattr(process, "poll") or process.poll() is None
            )
            was_started = bool(process_running and self._base_url)
            if _diagnostic_profiler is not None:
                if not was_started:
                    _diagnostic_profiler.mark_cold_start("embedding")
                with _diagnostic_profiler.measure("embedding_service_startup_ms"):
                    self._ensure_started()
                _diagnostic_profiler.increment("embedding_request_count")
                with _diagnostic_profiler.measure("embedding_request_ms"):
                    result = self._request_json("POST", "/embed", {"texts": list(texts), "input_kind": input_kind})
            else:
                self._ensure_started()
                result = self._request_json("POST", "/embed", {"texts": list(texts), "input_kind": input_kind})
        if result.get("model") != MODEL_NAME or result.get("dimension") != EXPECTED_DIMENSION:
            raise EmbeddingServiceError("Embedding Service returned incompatible model metadata.")
        rows = result.get("vectors")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingServiceError("Embedding Service returned an unexpected vector count.")
        vectors = []
        for row in rows:
            if not isinstance(row, list) or len(row) != EXPECTED_DIMENSION:
                raise EmbeddingServiceError("Embedding Service returned an unexpected vector dimension.")
            values = tuple(float(value) for value in row)
            norm = math.sqrt(sum(value * value for value in values))
            if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
                raise EmbeddingServiceError("Embedding Service returned a non-normalized vector.")
            vectors.append(values)
        return EmbeddingBatch(MODEL_NAME, EXPECTED_DIMENSION, tuple(vectors))

    def embed_documents(self, texts, _diagnostic_profiler=None):
        return self._embed(texts, "document", _diagnostic_profiler=_diagnostic_profiler)

    def embed_queries(self, texts, _diagnostic_profiler=None):
        return self._embed(texts, "query", _diagnostic_profiler=_diagnostic_profiler)

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            self._base_url = None
            if process is None:
                return
            if process.poll() is None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for stream_name in ("stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


_default_adapter = None
_default_lock = threading.Lock()


def _get_default_adapter():
    global _default_adapter
    with _default_lock:
        if _default_adapter is None:
            _default_adapter = EmbeddingAdapter()
        return _default_adapter


def embed_documents(texts, _diagnostic_profiler=None):
    adapter = _get_default_adapter()
    if _diagnostic_profiler is None:
        return adapter.embed_documents(texts)
    return adapter.embed_documents(texts, _diagnostic_profiler=_diagnostic_profiler)


def embed_queries(texts, _diagnostic_profiler=None):
    adapter = _get_default_adapter()
    if _diagnostic_profiler is None:
        return adapter.embed_queries(texts)
    return adapter.embed_queries(texts, _diagnostic_profiler=_diagnostic_profiler)


def close_default_adapter():
    global _default_adapter
    with _default_lock:
        adapter, _default_adapter = _default_adapter, None
    if adapter is not None:
        adapter.close()


atexit.register(close_default_adapter)
