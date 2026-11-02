"""Temporary real-runtime Memory Push latency diagnostics.

This module is intentionally temporary. It records metadata only and must be
removed after the current Memory Push optimization work is complete.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


TIMING_KEYS = (
    "query_compilation_ms",
    "candidate_pool_ms",
    "rank_1_ms",
    "rank_2_ms",
    "rank_3_provider_ms",
    "object_hydration_ms",
    "embedding_service_startup_ms",
    "reranker_service_startup_ms",
    "embedding_request_ms",
    "reranker_request_ms",
    "semantic_retrieval_ms",
    "lexical_retrieval_ms",
    "vector_index_search_ms",
)
COUNT_KEYS = (
    "compiler_query_count",
    "compiler_successful_channel_count",
    "semantic_query_count",
    "lexical_query_count",
    "embedding_request_count",
    "reranker_request_count",
    "candidate_pool_count",
    "rank_1_count",
    "rank_2_input_count",
    "rank_2_output_count",
    "rank_3_selected_count",
)


def _root_path(root=None):
    return Path(root or Path.cwd()).resolve()


def _diagnostic_paths(root):
    directory = _root_path(root) / ".sema" / "logs"
    return directory, directory / "memory-push-latency.jsonl", directory / "memory-push-latency-summary.json"


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class _ProcessState:
    def __init__(self, root):
        self.root = root
        self.turn_index = 0
        self.initialized = False
        self.summary = {
            "process_id": os.getpid(),
            "turn_count": 0,
            "status_counts": {"ok": 0, "error": 0, "empty": 0},
            "stages": {},
        }


_LOCK = threading.RLock()
_STATES = {}
_ACTIVE = threading.local()


def _state_for(root):
    key = str(_root_path(root))
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            state = _ProcessState(key)
            _STATES[key] = state
        if not state.initialized:
            state.initialized = True
            directory, record_path, summary_path = _diagnostic_paths(key)
            try:
                directory.mkdir(parents=True, exist_ok=True)
                record_path.unlink(missing_ok=True)
                summary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return state


class Profiler:
    def __init__(self, state):
        self.state = state
        self.started_at = time.perf_counter()
        self.timings = {key: 0.0 for key in TIMING_KEYS}
        self.counts = {key: 0 for key in COUNT_KEYS}
        self.cold_start = {"embedding": False, "reranker": False}
        self.provider_timings = {}

    @contextmanager
    def measure(self, key):
        started = time.perf_counter()
        try:
            yield
        finally:
            if key in self.timings:
                elapsed = (time.perf_counter() - started) * 1000
                with _LOCK:
                    self.timings[key] += elapsed

    def increment(self, key, amount=1):
        if key in self.counts:
            with _LOCK:
                self.counts[key] += int(amount)

    def set_count(self, key, value):
        if key in self.counts:
            with _LOCK:
                try:
                    self.counts[key] = int(value or 0)
                except (TypeError, ValueError):
                    self.counts[key] = 0

    def mark_cold_start(self, service):
        if service in self.cold_start:
            with _LOCK:
                self.cold_start[service] = True

    def record_provider_timing(self, stage, name, timing):
        if not stage or not name or not isinstance(timing, dict):
            return
        safe = {}
        for key in ("transport", "headers_ms", "first_content_ms", "output_ms", "complete_ms", "validation_ms", "content_delta_count"):
            if key not in timing:
                continue
            value = timing[key]
            if key == "content_delta_count":
                try:
                    safe[key] = int(value or 0)
                except (TypeError, ValueError):
                    safe[key] = 0
            elif key == "transport":
                safe[key] = str(value)
            elif value is not None:
                try:
                    safe[key] = round(float(value), 3)
                except (TypeError, ValueError):
                    continue
        with _LOCK:
            current = self.provider_timings.setdefault(str(stage), {}).setdefault(str(name), {})
            current.update(safe)

    def compiler_result(self, result):
        channels = result.get("channels") if isinstance(result, dict) else []
        if not isinstance(channels, list):
            return
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            queries = channel.get("queries")
            if isinstance(queries, list):
                self.increment("compiler_successful_channel_count")
                self.increment("compiler_query_count", len(queries))

    def _stop_reason(self, status):
        if status == "error":
            return "error"
        if self.counts["compiler_query_count"] == 0:
            return "no_compiled_queries"
        if self.counts["candidate_pool_count"] == 0:
            return "empty_candidate_pool"
        return "completed"

    def record(self, metrics):
        status = str((metrics or {}).get("status") or "ok")
        if status not in {"ok", "error", "empty"}:
            status = "ok"
        self.set_count("candidate_pool_count", (metrics or {}).get("candidate_pool_count"))
        self.set_count("rank_1_count", (metrics or {}).get("rank_1_count"))
        self.set_count("rank_2_input_count", (metrics or {}).get("rank_2_input_count"))
        self.set_count("rank_2_output_count", (metrics or {}).get("rank_2_output_count"))
        self.set_count("rank_3_selected_count", (metrics or {}).get("rank_3_selected_count"))
        with _LOCK:
            timings = {key: round(value, 3) for key, value in self.timings.items()}
            counts = dict(self.counts)
            cold_start = dict(self.cold_start)
            provider_timings = json.loads(json.dumps(self.provider_timings))
        timings["total_ms"] = round((time.perf_counter() - self.started_at) * 1000, 3)
        record = {
            "process_id": os.getpid(),
            "turn_index": self.state.turn_index,
            "status": status,
            "stop_reason": self._stop_reason(status),
            "error_type": str((metrics or {}).get("error_type") or ""),
            "timings_ms": timings,
            "counts": counts,
            "cold_start": cold_start,
        }
        if provider_timings:
            record["provider_timings"] = provider_timings
        _write_record(self.state, record)
        _update_summary(self.state, record)


class _NoopProfiler:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def measure(self, _key):
        return self

    def increment(self, *_args):
        return None

    def set_count(self, *_args):
        return None

    def mark_cold_start(self, *_args):
        return None

    def record_provider_timing(self, *_args):
        return None

    def compiler_result(self, *_args):
        return None

    def record(self, *_args):
        return None


NOOP = _NoopProfiler()


def begin_turn(root=None):
    try:
        state = _state_for(root)
        with _LOCK:
            state.turn_index += 1
        profiler = Profiler(state)
        _ACTIVE.profiler = profiler
        return profiler
    except Exception:
        return NOOP


def end_turn(profiler, metrics):
    try:
        profiler.record(metrics)
    except Exception:
        pass
    finally:
        if getattr(_ACTIVE, "profiler", None) is profiler:
            _ACTIVE.profiler = None


def current():
    return getattr(_ACTIVE, "profiler", NOOP)


def activate(profiler):
    """Temporarily expose a profiler to same-thread lower-level operations."""
    previous = getattr(_ACTIVE, "profiler", None)
    _ACTIVE.profiler = profiler

    @contextmanager
    def scope():
        try:
            yield profiler
        finally:
            _ACTIVE.profiler = previous

    return scope()


def _write_record(state, record):
    try:
        directory, record_path, _summary_path = _diagnostic_paths(state.root)
        directory.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with record_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def _stat(value):
    return {"count": value[0], "sum_ms": round(value[1], 3), "average_ms": round(value[1] / value[0], 3) if value[0] else 0.0, "min_ms": round(value[2], 3) if value[0] else 0.0, "max_ms": round(value[3], 3) if value[0] else 0.0}


def _update_summary(state, record):
    try:
        with _LOCK:
            summary = state.summary
            summary["turn_count"] += 1
            summary["status_counts"][record["status"]] = summary["status_counts"].get(record["status"], 0) + 1
            for key, value in record["timings_ms"].items():
                stats = summary["stages"].setdefault(key, [0, 0.0, 0.0, 0.0])
                numeric = float(value or 0.0)
                stats[0] += 1
                stats[1] += numeric
                stats[2] = numeric if stats[0] == 1 else min(stats[2], numeric)
                stats[3] = numeric if stats[0] == 1 else max(stats[3], numeric)
            output = dict(summary)
            output["stages"] = {key: _stat(value) for key, value in summary["stages"].items()}
            _directory, _record_path, summary_path = _diagnostic_paths(state.root)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
