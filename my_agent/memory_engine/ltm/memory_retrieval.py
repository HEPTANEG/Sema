"""Session-oriented, query-driven Long-Term Memory Retrieval.

The Retriever is an isolated provider-native Agent.  Its tool loop is
deliberately small: it can generate service-specific candidate searches,
inspect/rank Session-local pools, and observe source material through the
Maintainer's read-only primitives.  A returned checkpoint is resumable; it is
not an implicit stop signal.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from contextvars import ContextVar
import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque

from model.provider import call_chat_completion
from persistence.trajectory import (
    complete_span,
    complete_trajectory,
    fail_span,
    fail_trajectory,
    provider_exchange_observer,
    record_event,
    start_trajectory,
    start_span,
)
from runtime.context_snapshot import context_snapshot_for_agent
from shared.prompt_loader import load_prompt
from bridge.ui_events import emit_ui_event

from . import candidate_generation, ltm_retrieval, ltm_store, memory_push_rank_1, object_references, reranker_adapter, retrieval_observation
from .storage_gate import memory_task
from .reranker_adapter import rank_2_candidates_for_target
from .ltm_retrieval import RetrievalProfile
from ..token_counter import count_text_tokens


RETRIEVAL_TIMEOUT_SECONDS = 120
RETRIEVAL_SYNC_WINDOW_SECONDS = 5
RETRIEVAL_MAX_TOOL_CALLS = 20
RETRIEVAL_MAX_MODEL_ROUNDS = 32
RETRIEVAL_MAX_RESULT_TOKENS = 12_000
RETRIEVAL_SESSION_TTL_SECONDS = 30 * 60
RETRIEVAL_MAX_POOL_ITEMS = 400
RETRIEVAL_MAX_NO_PROGRESS_BATCHES = 2
MEMORY_OBJECT_ID_RE = re.compile(r"^mem_[A-Za-z0-9_-]+$")
SESSION_KEY = "memory_retrieval_session"
LOCK_KEY = "_memory_retrieval_lock"
CLOSED_KEY = "_memory_retrieval_closed_results"
_TRAJECTORY_CONTEXT = ContextVar("memory_retrieval_trajectory_context", default=None)


def _set_retriever_background_status(session, runtime_state, active, *, kind=None, reason=None):
    """Publish only the Retriever lifecycle visible to clients.

    The session flag prevents a synchronous retrieval from emitting a finish
    event without a corresponding background start, and keeps feedback alive
    while the worker continues searching.
    """
    if not isinstance(session, dict) or not isinstance(runtime_state, dict):
        return
    was_active = bool(session.get("_retriever_ui_active"))
    if active:
        if was_active:
            return
        session["_retriever_ui_active"] = True
        emit_ui_event(
            runtime_state,
            "retriever_started",
            continuation_id=session.get("continuation_id"),
        )
        return
    if not was_active:
        return
    session["_retriever_ui_active"] = False
    fields = {"continuation_id": session.get("continuation_id")}
    if kind:
        fields["kind"] = kind
    if reason:
        fields["reason"] = reason
    emit_ui_event(runtime_state, "retriever_finished", **fields)


@dataclass(frozen=True)
class RetrievalCallerProfile:
    name: str
    synchronous: bool = False
    record_usage: bool = True
    result_kinds: tuple = ("feedback", "checkpoint", "need_clue", "need_context", "technical")


MAIN_AGENT_CALLER = RetrievalCallerProfile("main_agent")
MAINTAINER_CALLER = RetrievalCallerProfile(
    "ltm_maintainer", True, False, ("checkpoint", "need_clue", "technical"),
)


def close_retrieval_session(runtime_state):
    manager = RetrievalSessionManager(runtime_state)
    session = manager.current()
    if session is not None:
        manager.close(session)


def _caller(session):
    return session.get("caller_profile", MAIN_AGENT_CALLER)


class RetrievalRuntimeError(RuntimeError):
    def __init__(self, code, message, *, continuation_id=None):
        self.code = str(code)
        self.continuation_id = continuation_id
        super().__init__(f"{self.code}: {message}")


def _now():
    return time.monotonic()


def _lock_for(runtime_state):
    if not isinstance(runtime_state, dict):
        return threading.RLock()
    lock = runtime_state.get(LOCK_KEY)
    if lock is None:
        lock = threading.RLock()
        runtime_state[LOCK_KEY] = lock
    return lock


def _new_continuation():
    return "ret_" + uuid.uuid4().hex


def _fingerprint(request):
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _technical(code, message, continuation_id=None, details=None):
    result = {"kind": "technical", "message": str(message)}
    if continuation_id:
        result["continuation_id"] = continuation_id
        if code not in {"busy", "retrieval_timeout"}:
            result["message"] += (
                " To continue, send continuation_id with a new message describing the next action."
                " An identical request replays its cached result."
            )
    result["error"] = {"code": str(code)}
    if isinstance(details, dict):
        result["error"].update(details)
    return result


def _retrieval_timeout_result(session, command):
    return _technical(
        "retrieval_timeout",
        "Maintainer retrieval exceeded its overall technical deadline before producing a converged result.",
        session.get("continuation_id"),
        details={
            "batch_count": int(command.get("batch_count") or 0),
            "tool_call_count": int(command.get("tool_call_count") or 0),
        },
    )


def _observer(runtime_state):
    if not isinstance(runtime_state, dict):
        return None
    return runtime_state.setdefault("memory_engine_retrieval_observer", {"retrieval_request_count": 0, "last_result": {}})


def retrieval_observer_snapshot(runtime_state=None):
    value = (runtime_state or {}).get("memory_engine_retrieval_observer") or {}
    allowed = {
        "tool_call_count", "model_round_count", "context_snapshot_used", "terminal_kind",
        "error_code", "successful_use_object_count", "successful_use_status", "successful_use_error_type",
        "elapsed_ms", "kind", "continuation_id",
    }
    return {
        "retrieval_request_count": int(value.get("retrieval_request_count") or 0),
        "last_result": {key: item for key, item in (value.get("last_result") or {}).items() if key in allowed},
    }


def _record_observer(runtime_state, result, metrics):
    observer = _observer(runtime_state)
    if observer is None:
        return
    observer["retrieval_request_count"] = int(observer.get("retrieval_request_count") or 0) + 1
    observer["last_result"] = {**metrics, "kind": result.get("kind"), "continuation_id": result.get("continuation_id")}


class RetrievalSessionManager:
    """Own the one process-local Retrieval Session stored in runtime_state."""

    def __init__(self, runtime_state=None, *, ttl_seconds=RETRIEVAL_SESSION_TTL_SECONDS, clock=None):
        self.runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock or _now
        self.lock = _lock_for(self.runtime_state)

    def _expire_locked(self):
        session = self.runtime_state.get(SESSION_KEY)
        if not isinstance(session, dict):
            return None
        worker = session.get("worker")
        if worker is not None and worker.is_active:
            return session
        if self.clock() - float(session.get("last_activity") or 0) >= self.ttl_seconds:
            self.runtime_state.pop(SESSION_KEY, None)
            return None
        return session

    def current(self):
        with self.lock:
            return self._expire_locked()

    def create(self, query, message=None):
        with self.lock:
            if self._expire_locked() is not None:
                raise RetrievalRuntimeError("session_active", "An unclosed Retrieval Session already exists; close it first.")
            session = {
                "query": query,
                "continuation_id": _new_continuation(),
                "history": [],
                "candidate_pools": {},
                "object_references": object_references.ObjectReferenceMap(),
                "observed_object_ids": set(),
                "evidence_object_ids": set(),
                "candidate_object_ids": set(),
                "seen_search_signatures": set(),
                "observed_evidence_keys": set(),
                "no_progress_batches": 0,
                "_batch_progress": None,
                "successful_use_object_ids": set(),
                "guidance": [message] if message else [],
                "status": "running",
                "worker": None,
                "worker_generation": 0,
                "last_activity": self.clock(),
                "last_request_fingerprint": None,
                "last_result": None,
                "tool_call_count": 0,




                "retrieval_invocation_seq": 0,
            }
            self.runtime_state[SESSION_KEY] = session
            return session

    def get(self, continuation_id):
        with self.lock:
            session = self._expire_locked()
            if session is None or session.get("continuation_id") != continuation_id:
                raise RetrievalRuntimeError("invalid_continuation", "continuation_id is unknown or expired.")
            return session

    def close(self, session):
        with self.lock:
            worker = session.get("worker")
            if worker is not None:
                worker.close()
            _set_retriever_background_status(session, self.runtime_state, False, reason="closed")
            session["worker"] = None
            session["status"] = "closed"
            result = {"kind": "closed", "message": "Retrieval Session closed."}
            self.runtime_state.pop(SESSION_KEY, None)
            return result


def _validate_public_request(request):
    if not isinstance(request, dict):
        raise RetrievalRuntimeError("invalid_arguments", "retrieve_memory arguments must be an object.")
    unknown = set(request) - {"query", "continuation_id", "message", "include_context", "close"}
    if unknown:
        raise RetrievalRuntimeError("invalid_arguments", f"unknown retrieve_memory arguments: {sorted(unknown)}")
    query = request.get("query")
    continuation_id = request.get("continuation_id")
    message = request.get("message")
    include_context = request.get("include_context", False)
    close = request.get("close", False)
    if include_context is None:
        include_context = False
    if close is None:
        close = False
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise RetrievalRuntimeError("invalid_arguments", "query must be a non-empty string when provided.")
    if continuation_id is not None and (not isinstance(continuation_id, str) or not continuation_id.strip()):
        raise RetrievalRuntimeError("invalid_arguments", "continuation_id must be a non-empty string when provided.")
    if message is not None and not isinstance(message, str):
        raise RetrievalRuntimeError("invalid_arguments", "message must be a string when provided.")
    if not isinstance(include_context, bool):
        raise RetrievalRuntimeError("invalid_arguments", "include_context must be boolean.")
    if not isinstance(close, bool):
        raise RetrievalRuntimeError("invalid_arguments", "close must be boolean.")
    if query is not None and continuation_id is not None:
        raise RetrievalRuntimeError("invalid_arguments", "query and continuation_id are mutually exclusive.")
    if query is None and continuation_id is None:
        raise RetrievalRuntimeError("invalid_arguments", "query or continuation_id is required.")
    if close and continuation_id is None:
        raise RetrievalRuntimeError("invalid_arguments", "close requires continuation_id.")
    if close and (message is not None or include_context):
        raise RetrievalRuntimeError("invalid_arguments", "close cannot include message or context.")
    return {
        "query": query.strip() if isinstance(query, str) else None,
        "continuation_id": continuation_id.strip() if isinstance(continuation_id, str) else None,
        "message": message.strip() if isinstance(message, str) else None,
        "include_context": include_context,
        "close": close,
    }


def _assistant_history(message, tool_calls=None):



    value = {"role": "assistant", "content": "" if tool_calls is not None else message.get("content") or ""}
    if tool_calls is not None:
        value["tool_calls"] = tool_calls
    if "reasoning_content" in message:
        value["reasoning_content"] = message.get("reasoning_content") or ""
    return value


def _normalize_tool_calls(raw_calls, start):
    calls = []
    for index, raw in enumerate(raw_calls or [], start=1):
        function = raw.get("function") if isinstance(raw, dict) and isinstance(raw.get("function"), dict) else {}
        calls.append({
            "id": str((raw or {}).get("id") or f"retrieve_tool_{start + index}"),
            "type": "function",
            "function": {"name": str(function.get("name") or ""), "arguments": function.get("arguments") or "{}"},
        })
    return calls


def _parse_arguments(call):
    raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        value = raw if isinstance(raw, dict) else json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as error:
        raise RetrievalRuntimeError("invalid_tool_arguments", str(error)) from error
    if not isinstance(value, dict):
        raise RetrievalRuntimeError("invalid_tool_arguments", "tool arguments must be an object.")
    return value


def _operation_signature(name, args):
    """Return a stable, mechanical signature for progress detection."""
    value = dict(args or {})
    if name == "search_candidates":
        value["query"] = re.sub(r"\s+", " ", str(value.get("query") or "").strip())
        value["service"] = str(value.get("service") or "embedding").strip().lower()
    return json.dumps({"tool": name, "arguments": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _start_batch_progress(session):
    session["_batch_progress"] = {
        "new_search_signatures": 0,
        "new_candidate_count": 0,
        "new_evidence_count": 0,
    }


def _batch_progress(session):
    value = session.get("_batch_progress")
    if not isinstance(value, dict):
        _start_batch_progress(session)
        value = session["_batch_progress"]
    return value


def _record_operation_progress(session, name, args):
    if name != "search_candidates":
        return
    signature = _operation_signature(name, args)
    seen = session.setdefault("seen_search_signatures", set())
    progress = _batch_progress(session)
    if signature not in seen:
        progress["new_search_signatures"] += 1
        seen.add(signature)


def _record_candidate_items(session, items):
    known = session.setdefault("candidate_object_ids", set())
    progress = _batch_progress(session)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        object_id = item.get("object_id")
        if object_id and object_id not in known:
            known.add(object_id)
            progress["new_candidate_count"] += 1


def _take_batch_progress(session):
    value = session.get("_batch_progress")
    session["_batch_progress"] = None
    if not isinstance(value, dict):
        return {"new_search_signatures": 0, "new_candidate_count": 0, "new_evidence_count": 0}
    return value


def _batch_made_progress(progress):
    return bool(
        progress.get("new_candidate_count")
        or progress.get("new_evidence_count")
        or progress.get("new_search_signatures")
    )


def _make_no_progress_result(session):
    last = session.get("last_result")
    if isinstance(last, dict) and last.get("evidence"):
        return {
            "kind": "checkpoint",
            "message": "Retrieval stopped after repeated searches produced no new information.",
            "evidence": copy.deepcopy(last["evidence"]),
            "continuation_id": session.get("continuation_id"),
        }
    return _technical(
        "no_progress",
        "Retrieval stopped because repeated search batches produced no new information.",
        session.get("continuation_id"),
    )


def _pool_id():
    return "cp_" + uuid.uuid4().hex[:16]


def _object_ref(session, object_id):
    try:
        return session["object_references"].register(object_id)
    except (KeyError, object_references.ObjectReferenceError) as error:
        raise RetrievalRuntimeError("invalid_reference", "MemoryObject reference could not be created.") from error


def _modelize_result(value, session):
    """Replace MemoryObject IDs in a tool result with session-local refs."""
    if isinstance(value, list):
        return [_modelize_result(item, session) for item in value]
    if not isinstance(value, dict):
        return value
    output = {}
    for key, item in value.items():
        if key == "object_id" and isinstance(item, str):
            output["ref"] = _object_ref(session, item)
        elif key == "source_object_id" and isinstance(item, str):
            output["source_ref"] = _object_ref(session, item)
        elif key == "target_object_id" and isinstance(item, str):
            output["target_ref"] = _object_ref(session, item)
        else:
            output[key] = _modelize_result(item, session)
    return output


def _resolve_model_args(name, args, session):
    """Translate model-facing object refs to backend object ID arguments."""
    resolved = dict(args or {})
    fields = {
        "object_ref": "object_id",
        "object_refs": "object_ids",
        "linked_to_object_ref": "linked_to_object_id",
        "tag_object_refs": "tag_object_ids",
        "endpoint_object_refs": "endpoint_object_ids",
        "source_object_refs": "source_object_ids",
        "target_object_refs": "target_object_ids",
        "start_object_refs": "start_object_ids",
    }
    reference_map = session.get("object_references")
    if reference_map is None:
        if any(key in resolved for key in fields):
            raise RetrievalRuntimeError("invalid_tool_arguments", f"{name} requires a Retrieval Session reference map.")
        return resolved
    for model_key, backend_key in fields.items():
        if model_key not in resolved:
            continue
        value = resolved.pop(model_key)
        try:
            resolved[backend_key] = reference_map.resolve(value) if model_key.endswith("_ref") else reference_map.resolve_many(value)
        except object_references.ObjectReferenceError as error:
            raise RetrievalRuntimeError("invalid_tool_arguments", f"{name}.{model_key} contains an invalid MemoryObject reference.") from error
    return resolved


def _reference_observation_tool_schemas():
    """Build observation schemas whose object arguments are explicitly refs."""
    schemas = copy.deepcopy(retrieval_observation.tool_schemas())
    replacements = {
        "linked_to_object_id": "linked_to_object_ref",
        "tag_object_ids": "tag_object_refs",
        "endpoint_object_ids": "endpoint_object_refs",
        "source_object_ids": "source_object_refs",
        "target_object_ids": "target_object_refs",
        "start_object_ids": "start_object_refs",
        "object_ids": "object_refs",
        "object_id": "object_ref",
    }

    def rewrite(node):
        if isinstance(node, list):
            for item in node:
                rewrite(item)
            return
        if not isinstance(node, dict):
            return
        if isinstance(node.get("description"), str):
            description = node["description"]
            for old, new in (
                ("endpoint_object_ids", "endpoint_object_refs"),
                ("source_object_ids", "source_object_refs"),
                ("target_object_ids", "target_object_refs"),
                ("start_object_ids", "start_object_refs"),
                ("object_ids", "object_refs"),
                ("object_id", "object_ref"),
                ("MemoryObject IDs", "temporary refs"),
                ("MemoryObject ID", "temporary ref"),
            ):
                description = description.replace(old, new)
            node["description"] = description
        properties = node.get("properties")
        if isinstance(properties, dict):
            renamed = {}
            for key, value in properties.items():
                new_key = replacements.get(key, key)
                if new_key.endswith("_refs"):
                    converted = {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 1},
                        "minItems": value.get("minItems", 1),
                        "maxItems": value.get("maxItems", RETRIEVAL_MAX_POOL_ITEMS),
                        "description": "Temporary session-local integer MemoryObject references.",
                    }
                elif new_key.endswith("_ref"):
                    converted = {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Temporary session-local integer MemoryObject reference.",
                    }
                else:
                    converted = value
                    rewrite(converted)
                renamed[new_key] = converted
            node["properties"] = renamed
        required = node.get("required")
        if isinstance(required, list):
            node["required"] = [replacements.get(key, key) for key in required]
        for key, value in node.items():
            if key not in {"properties", "required"}:
                rewrite(value)

    rewrite(schemas)
    return schemas


def _pool_projection(session, pool, projection, *, cursor=0, limit=80):
    if projection not in {"count_only", "title", "title_summary", "candidate"}:
        raise RetrievalRuntimeError("invalid_projection", "projection must be count_only, title, title_summary, or candidate.")
    items = list(pool.get("items") or [])
    if projection == "count_only":
        return {
            "status": pool.get("status", "ok"), "pool_id": pool["pool_id"],
            "candidate_count": len(items), "actual_projection": "count_only", "items": [],
            "errors": list(pool.get("errors") or []),
        }
    limit = max(1, min(int(limit), RETRIEVAL_MAX_POOL_ITEMS))
    cursor = max(0, int(cursor or 0))
    selected = items[cursor: cursor + limit]
    output = []
    for item in selected:
        if projection == "title":
            output.append({"ref": _object_ref(session, item.get("object_id")), "title": item.get("title")})
        elif projection == "title_summary":
            output.append(_modelize_result({
                key: item.get(key)
                for key in (
                    "object_id", "object_type", "title", "summary",
                    "rank_1_score", "rank_1_position", "relevance_score",
                )
                if key in item
            }, session))
        else:
            output.append(_modelize_result({key: value for key, value in item.items() if not str(key).startswith("_")}, session))
    return {
        "status": pool.get("status", "ok"),
        "pool_id": pool["pool_id"],
        "candidate_count": len(items),
        "actual_projection": projection,
        "items": output,
        "next_cursor": cursor + len(selected) if cursor + len(selected) < len(items) else None,
        "errors": list(pool.get("errors") or []),
    }


def _invalidate_deleted_candidates(session):
    generation = ltm_store.forgetting_generation(session.get("root"))
    if generation != session.get("_forgetting_generation", 0):
        session["candidate_pools"] = {}
    session["_forgetting_generation"] = generation


def _stored_pool(session, pool_id):
    _invalidate_deleted_candidates(session)
    pool = (session.get("candidate_pools") or {}).get(pool_id)
    if not isinstance(pool, dict):
        raise RetrievalRuntimeError("unknown_candidate_pool", f"Unknown Candidate Pool: {pool_id}")
    return pool


def _merge_pools(session, pool_ids):
    merged = {}
    query_evidence = []
    for pool_id in pool_ids:
        pool = _stored_pool(session, pool_id)
        for item in pool.get("items") or []:
            if isinstance(item, dict) and item.get("object_id"):
                merged.setdefault(item["object_id"], dict(item))
        query_evidence.extend(pool.get("query_evidence") or [])
    return {"status": "ok", "items": list(merged.values()), "query_evidence": query_evidence, "errors": []}


def _record_observed(session, result):
    progress = _batch_progress(session)
    evidence_keys = set()
    for item in (result or {}).get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("object_id"):
            object_id = item["object_id"]
            session.setdefault("observed_object_ids", set()).add(object_id)
            evidence_keys.add(("object", object_id))
        if item.get("link_id"):
            evidence_keys.add(("link", item["link_id"]))
        if item.get("slot_seq") is not None:
            evidence_keys.add(("slot", item["slot_seq"]))
    for item in (result or {}).get("existing_sourced_objects") or []:
        if isinstance(item, dict) and item.get("object_id"):
            object_id = item["object_id"]
            session.setdefault("observed_object_ids", set()).add(object_id)
            evidence_keys.add(("object", object_id))
    seen = session.setdefault("observed_evidence_keys", set())
    new_keys = evidence_keys - seen
    seen.update(evidence_keys)
    progress["new_evidence_count"] += len(new_keys)


RETRIEVAL_OBSERVATION_PROFILE = retrieval_observation.PROFILE


def _run_internal_tool(name, args, session):
    _invalidate_deleted_candidates(session)
    args = dict(args or {})
    _record_operation_progress(session, name, args)
    if name == "search_candidates":
        query = str(args.get("query") or "").strip()
        service = str(args.get("service") or "embedding").strip().lower()
        if not query:
            raise RetrievalRuntimeError("invalid_tool_arguments", "search_candidates.query is required.")
        if service not in {"lexical", "embedding", "both"}:
            raise RetrievalRuntimeError("invalid_tool_arguments", "search_candidates.service must be lexical, embedding, or both.")
        result = candidate_generation.build_candidate_pool(
            [{"query": query, "hypothesis": args.get("hypothesis") or "retrieval", "service": service}],
            profile=ltm_retrieval.MEMORY_RETRIEVAL_PROFILE,
            root=session.get("root"),
        )
        pool_id = _pool_id()
        pool = {"pool_id": pool_id, **result, "created_at": time.time()}
        session.setdefault("candidate_pools", {})[pool_id] = pool
        _record_candidate_items(session, result.get("items"))
        projection = args.get("projection") or ("count_only" if len(result.get("items") or []) > 80 else "title_summary")
        return _pool_projection(session, pool, projection, cursor=args.get("cursor", 0), limit=args.get("limit", 80))
    if name == "inspect_candidate_pool":
        pool = _stored_pool(session, args.get("pool_id"))
        return _pool_projection(session, pool, args.get("projection", "title_summary"), cursor=args.get("cursor", 0), limit=args.get("limit", 80))
    if name in {"rank_1_candidate_pools", "rank_2_candidate_pools"}:
        pool_ids = args.get("pool_ids") or args.get("candidate_pool_ids") or []
        if not isinstance(pool_ids, list) or not pool_ids:
            raise RetrievalRuntimeError("invalid_tool_arguments", f"{name}.pool_ids must be a non-empty array.")
        merged = _merge_pools(session, pool_ids)
        if name == "rank_1_candidate_pools":
            result = memory_push_rank_1.rank_1_candidate_pool(merged, max_items=args.get("max_items", 400))
        else:
            target = session["query"] + "\n" + "\n".join(session.get("guidance") or [])
            result = rank_2_candidates_for_target(target, merged["items"], max_items=args.get("max_items", 80))
            source_by_id = {
                item.get("object_id"): item
                for item in merged["items"]
                if isinstance(item, dict) and item.get("object_id")
            }
            result["items"] = [
                {**source_by_id.get(item.get("object_id"), {}), **item}
                for item in result.get("items") or []
            ]
        pool_id = _pool_id()
        pool = {"pool_id": pool_id, **result, "query_evidence": merged.get("query_evidence", []), "created_at": time.time()}
        session.setdefault("candidate_pools", {})[pool_id] = pool
        _record_candidate_items(session, result.get("items"))
        return _pool_projection(session, pool, args.get("projection", "title_summary"), limit=args.get("limit", 80))
    if name in {"get_object", "get_links", "traverse_objects", "read_event_slots"}:
        try:
            result = retrieval_observation.run(name, _resolve_model_args(name, args, session), profile=RETRIEVAL_OBSERVATION_PROFILE, root=session.get("root"))
        except ValueError as error:
            raise RetrievalRuntimeError("invalid_tool_arguments", str(error)) from error
        _record_observed(session, result)
        return _modelize_result(result, session)
    raise RetrievalRuntimeError("unknown_tool", f"Unknown Retrieval tool: {name}")


def _extract_json(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise RetrievalRuntimeError("invalid_result", "Retriever did not return a JSON object.")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as error:
        raise RetrievalRuntimeError("invalid_result", f"invalid RetrievalResult JSON: {error}") from error
    if not isinstance(value, dict):
        raise RetrievalRuntimeError("invalid_result", "RetrievalResult must be an object.")
    return value


def _validate_source_refs(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 20:
        raise RetrievalRuntimeError("invalid_result", "source_refs must be a bounded array.")
    refs = []
    for ref in value:
        if not isinstance(ref, dict) or not isinstance(ref.get("kind"), str):
            raise RetrievalRuntimeError("invalid_result", "source_refs entries require kind.")
        allowed = {"kind", "link_id", "slot_start", "slot_end", "excerpt"}
        if set(ref) - allowed:
            raise RetrievalRuntimeError("invalid_result", "source_refs contains unknown fields.")
        if ref["kind"] not in {"link", "event_slot"}:
            raise RetrievalRuntimeError("invalid_result", "source_refs.kind must be link or event_slot.")
        if "link_id" in ref and (not isinstance(ref["link_id"], (str, int)) or isinstance(ref["link_id"], bool)):
            raise RetrievalRuntimeError("invalid_result", "source_refs.link_id must be a string or integer.")
        for key in ("slot_start", "slot_end"):
            if key in ref and (not isinstance(ref[key], int) or isinstance(ref[key], bool)):
                raise RetrievalRuntimeError("invalid_result", f"source_refs.{key} must be an integer.")
        if "excerpt" in ref and (not isinstance(ref["excerpt"], str) or len(ref["excerpt"]) > 2_000):
            raise RetrievalRuntimeError("invalid_result", "source_refs.excerpt must be a bounded string.")
        refs.append(dict(ref))
    return refs


def _validate_evidence(evidence, session):
    if evidence is None:
        return []
    if not isinstance(evidence, list) or len(evidence) > RETRIEVAL_MAX_POOL_ITEMS:
        raise RetrievalRuntimeError("invalid_result", "evidence must be a bounded array.")
    output, seen = [], set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}] must be an object.")
        allowed = {"object_id", "title", "summary", "body", "structured_data", "source_refs"}
        if set(item) - allowed:
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}] has unknown fields.")
        object_id = item.get("object_id")
        if not isinstance(object_id, str) or not MEMORY_OBJECT_ID_RE.fullmatch(object_id):
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}].object_id is invalid.")
        if object_id in seen:
            raise RetrievalRuntimeError("invalid_result", "evidence contains duplicate object_id values.")
        if object_id not in session.get("observed_object_ids", set()):
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}] was not observed through a source tool.")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}].title is required.")
        for key in ("summary", "body"):
            if key in item and not isinstance(item[key], str):
                raise RetrievalRuntimeError("invalid_result", f"evidence[{index}].{key} must be a string.")
        if item.get("structured_data") is not None and not isinstance(item["structured_data"], dict):
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}].structured_data must be an object or null.")
        normalized = {key: value for key, value in item.items() if key != "source_refs"}
        refs = _validate_source_refs(item.get("source_refs"))
        if refs is not None:
            normalized["source_refs"] = refs
        output.append(normalized)
        seen.add(object_id)
    return output


def _resolve_terminal_evidence_refs(raw, session):
    """Translate model-facing evidence refs before normal ID validation."""
    normalized = copy.deepcopy(raw)
    evidence = normalized.get("evidence")
    if not isinstance(evidence, list):
        return normalized
    reference_map = session["object_references"]
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or "ref" not in item:
            continue
        if "object_id" in item:
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}] must use ref or object_id, not both.")
        try:
            item["object_id"] = reference_map.resolve(item.pop("ref"))
        except object_references.ObjectReferenceError as error:
            raise RetrievalRuntimeError("invalid_result", f"evidence[{index}].ref is invalid.") from error
    return normalized


def _validate_terminal(raw, session):
    raw = _resolve_terminal_evidence_refs(raw, session)
    allowed = {"kind", "message", "evidence", "error"}
    unknown = set(raw) - allowed
    if unknown:
        raise RetrievalRuntimeError("invalid_result", f"RetrievalResult has unknown fields: {sorted(unknown)}")
    kind = raw.get("kind")
    if kind not in {"feedback", "checkpoint", "need_clue", "need_context", "technical"}:
        raise RetrievalRuntimeError("invalid_result", "kind must be feedback, checkpoint, need_clue, need_context, or technical.")
    if kind not in _caller(session).result_kinds:
        raise RetrievalRuntimeError("invalid_result", f"This caller accepts only {_caller(session).result_kinds}.")
    message = raw.get("message")
    if not isinstance(message, str) or not message.strip():
        raise RetrievalRuntimeError("invalid_result", "message must be a non-empty string.")
    if kind == "technical":
        error = raw.get("error")
        if not isinstance(error, dict) or set(error) != {"code"} or not isinstance(error.get("code"), str):
            raise RetrievalRuntimeError("invalid_result", "technical results must include an error.code.")
        if error["code"] != "unsupported_query":
            raise RetrievalRuntimeError("invalid_result", "Retriever technical results may only use unsupported_query.")
        if raw.get("evidence"):
            raise RetrievalRuntimeError("invalid_result", "technical results must not include evidence.")
        return {"kind": kind, "message": message.strip(), "error": {"code": error["code"]}}
    if "error" in raw:
        raise RetrievalRuntimeError("invalid_result", "Only technical results may include error.")
    evidence = _validate_evidence(raw.get("evidence"), session)
    if kind in {"need_clue", "need_context"} and evidence:
        raise RetrievalRuntimeError("invalid_result", f"{kind} must not include evidence.")
    return {"kind": kind, "message": message.strip(), **({"evidence": evidence} if evidence else {})}


def _eval_message(runtime_state):
    if not isinstance(runtime_state, dict):
        return None
    responses = runtime_state.get("eval_retrieve_memory_responses")
    index_key = "eval_retrieve_memory_response_index"
    if not isinstance(responses, list):
        responses = runtime_state.get("eval_retrieval_responses")
        index_key = "eval_retrieval_response_index"
    if not isinstance(responses, list):
        return None
    index = int(runtime_state.get(index_key) or 0)
    if index >= len(responses):
        raise RetrievalRuntimeError("provider_error", "retrieval evaluation response hook exhausted.")
    runtime_state[index_key] = index + 1
    response = responses[index]
    if isinstance(response, dict) and ("role" in response or "tool_calls" in response or "content" in response):
        return {"role": "assistant", **response}
    return {"role": "assistant", "content": json.dumps(response, ensure_ascii=False)}


def _call_agent(messages, timeout_seconds, *, tools_enabled=True):
    trajectory_context = _TRAJECTORY_CONTEXT.get()
    observer = provider_exchange_observer(
        trajectory_context,
        purpose="ltm memory retrieval",
        agent_type="memory_retriever",
    )
    response = call_chat_completion(
        messages, tools=RETRIEVAL_TOOL_SCHEMAS if tools_enabled else None, temperature=0, timeout_seconds=timeout_seconds, purpose="ltm memory retrieval",
        exchange_observer=observer,
    )
    try:
        choice = response["choices"][0]
        return {**choice["message"], "finish_reason": choice.get("finish_reason")}
    except (KeyError, IndexError, TypeError) as error:
        raise RetrievalRuntimeError("provider_error", "provider response is missing an assistant message.") from error


def _check_runtime(deadline, check_cancel):
    if check_cancel:
        try:
            check_cancel()
        except Exception as error:
            raise RetrievalRuntimeError("cancelled", str(error)) from error
    if time.monotonic() >= deadline:
        raise RetrievalRuntimeError("timeout", "Retrieval exceeded its technical deadline.")


def _terminal_response(response, session):
    if response.get("finish_reason") == "length":
        raise RetrievalRuntimeError("output_truncated", "Provider output reached its generation limit; no complete checkpoint was delivered.")
    if response.get("tool_calls"):
        raise RetrievalRuntimeError("invalid_result", "Format correction must return a checkpoint, not tool calls.")
    result = _validate_terminal(_extract_json(response.get("content")), session)
    if count_text_tokens(json.dumps(result, ensure_ascii=False, sort_keys=True)) > RETRIEVAL_MAX_RESULT_TOKENS:
        raise RetrievalRuntimeError("result_too_large", f"Retrieval checkpoint exceeds the {RETRIEVAL_MAX_RESULT_TOKENS}-token technical limit.")
    session.setdefault("evidence_object_ids", set()).update(item["object_id"] for item in result.get("evidence", []))
    return result


def _correct_terminal(session, runtime_state, deadline, check_cancel, metrics, error, trajectory_context):
    _check_runtime(deadline, check_cancel)


    session["history"].append({"role": "user", "content": (
        f"Runtime format correction: {error.code}: {error}. "
        "This is the single format-only correction attempt for this run. For this response only, search tools are unavailable. "
        f"Return one short JSON result with kind in {_caller(session).result_kinds} using only source observations already in this history. "
        "Omit evidence if none is verified. Do not narrate plans, invent source fields, or request further searches."
    )})
    record_event(trajectory_context, "retriever.format_correction", summary={"reason": error.code})
    response = _eval_message(runtime_state) or _call_agent(
        session["history"], max(1, int(deadline - time.monotonic() + 0.999)), tools_enabled=False,
    )
    metrics["model_round_count"] += 1
    _check_runtime(deadline, check_cancel)
    result = _terminal_response(response, session)
    session["history"].append(_assistant_history(response))
    return result


def _run_agent(session, runtime_state, context_snapshot, message, check_cancel=None, trajectory_context=None, deadline=None):
    if deadline is None:
        deadline = session.get("_active_command_deadline")
    if deadline is None:
        deadline = time.monotonic() + RETRIEVAL_TIMEOUT_SECONDS
    _start_batch_progress(session)
    if not session.get("history"):
        caller_instructions = (
            "Your caller is Maintainer. Return a consolidated checkpoint or ask a targeted question with need_clue. "
            "Do not return feedback or need_context. Caller messages supply any needed materials. "
            "You are a general retrieval tool, not a duplicate adjudicator or maintenance planner."
            if _caller(session).synchronous else
            "Your caller is Main Agent. Use need_context if its full conversation is needed. "
            "You may return provisional feedback and continue searching; checkpoint yields to the caller."
        )
        session["history"].append({"role": "system", "content": load_prompt("memory_retrieval") + "\n\n" + caller_instructions})
        session["history"].append({
            "role": "user",
            "content": json.dumps({"query": session["query"], "message": message or "", "context_snapshot": context_snapshot_for_agent(context_snapshot) if context_snapshot else []}, ensure_ascii=False),
        })
    else:
        session.setdefault("guidance", []).append(message) if message else None
        session["history"].append({
            "role": "user",
            "content": json.dumps({"query": session["query"], "message": message or "", "context_snapshot": context_snapshot_for_agent(context_snapshot) if context_snapshot else []}, ensure_ascii=False),
        })
    metrics = {"tool_call_count": 0, "model_round_count": 0, "context_snapshot_used": bool(context_snapshot), "terminal_kind": "", "error_code": ""}
    session["_active_batch_metrics"] = metrics
    for _round in range(RETRIEVAL_MAX_MODEL_ROUNDS):
        _check_runtime(deadline, check_cancel)
        response = _eval_message(runtime_state) or _call_agent(session["history"], max(1, int(deadline - time.monotonic() + 0.999)))
        metrics["model_round_count"] += 1
        _check_runtime(deadline, check_cancel)
        if response.get("finish_reason") == "length":
            result = _correct_terminal(session, runtime_state, deadline, check_cancel, metrics,
                                       RetrievalRuntimeError("output_truncated", "Provider output was truncated."), trajectory_context)
            metrics["terminal_kind"] = result["kind"]
            return result, metrics
        calls = _normalize_tool_calls(response.get("tool_calls") or [], metrics["tool_call_count"])
        if calls:
            session["history"].append(_assistant_history(response, tool_calls=calls))
            for index, call in enumerate(calls):
                tool_name = (call.get("function") or {}).get("name") or "unknown"
                tool_span = start_span(trajectory_context, "tool_call", tool_name, metadata={"tool_call_id": call.get("id")})
                try:
                    _check_runtime(deadline, check_cancel)
                    if metrics["tool_call_count"] >= RETRIEVAL_MAX_TOOL_CALLS:
                        raise RetrievalRuntimeError("tool_limit", f"Retrieval reached the {RETRIEVAL_MAX_TOOL_CALLS} tool-call technical limit.", continuation_id=session.get("continuation_id"))
                except RetrievalRuntimeError as error:


                    for pending in calls[index:]:
                        session["history"].append({
                            "role": "tool", "tool_call_id": pending["id"],
                            "content": json.dumps({
                                "status": "not_executed", "reason": error.code,
                                "message": "Runtime interrupted before execution; no search result is available. Decide whether to issue this call again when resumed.",
                            }),
                        })
                    fail_span(tool_span, error)
                    raise
                metrics["tool_call_count"] += 1
                try:
                    args = _parse_arguments(call)
                    raw_arguments = (call.get("function") or {}).get("arguments") or ""
                    record_event(tool_span, "tool.started", status="running", summary={"tool": tool_name}, artifacts={"raw_tool_arguments": raw_arguments, "tool_arguments": args})
                    result = _run_internal_tool(tool_name, args, session)
                    content = json.dumps(result, ensure_ascii=False, sort_keys=True)
                    record_event(tool_span, "tool.completed", status="completed", summary={"tool": tool_name}, artifacts={"tool_arguments": args, "tool_result": result})
                    complete_span(tool_span)
                except RetrievalRuntimeError as error:
                    metrics["error_code"] = error.code
                    result = {"status": "error", "errors": [{"code": error.code, "message": str(error)}]}
                    content = json.dumps(result)
                    record_event(tool_span, "validation.rejected", status="rejected", summary={"tool": tool_name, "error": str(error)}, artifacts={"raw_tool_arguments": (call.get("function") or {}).get("arguments") or "", "tool_result": result})
                    complete_span(tool_span)
                except Exception as error:
                    metrics["error_code"] = type(error).__name__
                    result = {"status": "error", "errors": [{"code": "tool_failed", "message": str(error)}]}
                    content = json.dumps(result)
                    record_event(tool_span, "tool.failed", status="failed", summary={"tool": tool_name, "error": str(error)}, artifacts={"tool_result": result})
                    fail_span(tool_span, error)
                session["history"].append({"role": "tool", "tool_call_id": call["id"], "content": content})
            continue
        try:
            result = _terminal_response(response, session)
        except RetrievalRuntimeError as error:
            result = _correct_terminal(session, runtime_state, deadline, check_cancel, metrics, error, trajectory_context)
        else:
            session["history"].append(_assistant_history(response))
        metrics["terminal_kind"] = result["kind"]
        return result, metrics
    raise RetrievalRuntimeError("model_round_limit", "Retrieval reached its model-round technical limit.", continuation_id=session.get("continuation_id"))


def _record_successful_uses(session, evidence, runtime_state, metrics):
    ids = sorted({item.get("object_id") for item in evidence if item.get("object_id")})
    fresh = [object_id for object_id in ids if object_id not in session.setdefault("successful_use_object_ids", set())]
    metrics["successful_use_object_count"] = len(fresh)
    if not fresh:
        metrics["successful_use_status"] = "not_needed"
        return
    key = f"successful_use:retrieve:{session['continuation_id']}:{len(session['successful_use_object_ids']) + 1}"
    try:
        record = ltm_store.record_successful_uses(fresh, key, root=session.get("root"))
        metrics["successful_use_status"] = str(record.get("status") or "rejected")
        if record.get("status") in {"committed", "already_committed"}:
            session["successful_use_object_ids"].update(fresh)
        else:
            metrics["successful_use_error_type"] = "store_rejected"
    except Exception as error:
        metrics["successful_use_status"] = "error"
        metrics["successful_use_error_type"] = type(error).__name__


class RetrievalWorker:
    """A single Session-owned worker which can outlive a public tool call.

    The worker deliberately keeps the existing Retriever tool loop intact.  A
    public invocation only supplies a command and waits for the short
    synchronous window; subsequent results are delivered through the Runtime
    wake callback when one is installed.
    """

    def __init__(self, manager, session, runtime_state):
        self.manager = manager
        self.session = session
        self.runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
        self._condition = threading.Condition(manager.lock)
        self._commands = deque()
        self._current_cancel = None
        self._active_command = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="memory-retrieval-worker", daemon=True)
        self._thread.start()

    @property
    def is_alive(self):
        return self._thread.is_alive() and not self._closed

    @property
    def is_active(self):
        with self._condition:
            return self.is_alive and (self._current_cancel is not None or bool(self._commands))

    def submit(self, command):
        with self._condition:
            if self._closed:
                raise RetrievalRuntimeError("closed", "Retrieval Session is closed.", continuation_id=self.session.get("continuation_id"))
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._commands.append(command)
            self.session["status"] = "running"
            self.session["last_activity"] = self.manager.clock()
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            if self._current_cancel is not None:
                self._current_cancel.set()
            commands = list(self._commands)
            if self._active_command is not None:
                commands.append(self._active_command)
            for command in commands:
                command["result"] = _technical("cancelled", "Retrieval Session closed.", self.session["continuation_id"])
                command["event"].set()
            self._commands.clear()
            self.session["status"] = "closed"
            discard = self.runtime_state.get("wake_discard_session")
            if callable(discard):
                discard(self.session.get("continuation_id"))
            self._condition.notify_all()

    def _next_command(self):
        with self._condition:
            while not self._closed and not self._commands:
                self._condition.wait(timeout=0.5)
            if self._closed:
                return None
            command = self._commands.popleft()
            self._active_command = command
            self._current_cancel = threading.Event()
            return command, self._current_cancel

    def _wake(self, result):
        if _caller(self.session).synchronous or self._closed:
            return False
        result = _revalidate_delivery(result, self.session.get("root"))
        callback = self.runtime_state.get("wake_callback")
        if callable(callback):
            try:
                return bool(
                    callback(
                        "retriever",
                        result.get("kind"),
                        {**result, "continuation_id": self.session.get("continuation_id")},
                        processing_class="tool_result_like",
                    )
                )
            except Exception:
                return False
        self.runtime_state.setdefault("memory_retrieval_wake_results", []).append(copy.deepcopy(result))
        return True

    def _publish(self, command, result):
        if _caller(self.session).synchronous:
            with self.manager.lock:
                self._publish_locked(command, result)
        else:
            self._publish_locked(command, result)

    def _publish_locked(self, command, result):
        if self._closed:
            return
        result = {**result, "continuation_id": self.session.get("continuation_id")}
        result = _revalidate_delivery(result, self.session.get("root"))
        if _caller(self.session).record_usage and result.get("kind") == "checkpoint" and result.get("evidence"):
            metrics = {"successful_use_object_count": 0, "successful_use_status": "not_applicable"}
            _record_successful_uses(self.session, result["evidence"], self.runtime_state, metrics)
        with self.manager.lock:
            self.session["last_result"] = copy.deepcopy(result)
            self.session["status"] = "awaiting"
            self.session["last_activity"] = self.manager.clock()
            if result.get("kind") != "feedback":
                _set_retriever_background_status(
                    self.session,
                    self.runtime_state,
                    False,
                    kind=result.get("kind"),
                    reason="result",
                )
        if not command.get("detached") and not command["event"].is_set():
            command["result"] = copy.deepcopy(result)
            command["event"].set()
            command["delivered"] = True
        else:
            if result.get("kind") == "feedback":


                while not self._closed and not self._wake(result):
                    time.sleep(0.05)
            else:
                self._wake(result)

    def _publish_timeout(self, command, trajectory_context, invocation_span, session_id, invocation_seq):
        details = {
            "batch_count": int(command.get("batch_count") or 0),
            "tool_call_count": int(command.get("tool_call_count") or 0),
        }
        error = RetrievalRuntimeError(
            "retrieval_timeout",
            "Maintainer retrieval exceeded its overall technical deadline before producing a converged result.",
            continuation_id=self.session.get("continuation_id"),
        )
        record_event(trajectory_context, "retriever.timeout", status="failed", summary=details)
        fail_span(invocation_span, error)
        fail_trajectory(
            trajectory_context,
            error,
            metadata={"session_id": session_id, "invocation_seq": invocation_seq, **details},
        )
        self._publish(command, _retrieval_timeout_result(self.session, command))

    @memory_task
    def _run_command(self, command, cancel_event):


        command.setdefault("batch_count", 0)
        command.setdefault("tool_call_count", 0)
        deadline = command.get("deadline")
        while not self._closed:
            def check_cancel():
                if cancel_event.is_set() or self._closed:
                    raise RetrievalRuntimeError("preempted", "Retrieval was preempted by newer guidance.")
                if command.get("check_cancel"):
                    command["check_cancel"]()

            with self.manager.lock:
                self.session["retrieval_invocation_seq"] = int(self.session.get("retrieval_invocation_seq") or 0) + 1
                invocation_seq = self.session["retrieval_invocation_seq"]
            session_id = self.session["continuation_id"]



            trajectory_external_id = f"{session_id}:invocation:{invocation_seq}"
            caller = (self.runtime_state or {}).get("trajectory_context")
            trajectory_metadata = {
                "session_id": session_id,
                "invocation_seq": invocation_seq,
                "background": bool(command.get("detached")),
            }
            if deadline is not None:
                trajectory_metadata.update({
                    "command_started_monotonic": command.get("started_at"),
                    "command_deadline_monotonic": deadline,
                    "command_batch_count": int(command.get("batch_count") or 0) + 1,
                })
            command["batch_count"] = int(command.get("batch_count") or 0) + 1
            trajectory_context = start_trajectory(
                "memory_retriever",
                trajectory_external_id,
                caller=caller,
                metadata=trajectory_metadata,
                root=self.session.get("root"),
            )
            invocation_span = start_span(trajectory_context, "invocation", "retrieve_memory", metadata=trajectory_metadata)
            try:
                self.session["_active_command_deadline"] = deadline
                token = _TRAJECTORY_CONTEXT.set(trajectory_context)
                try:
                    result, metrics = _run_agent(
                        self.session, self.runtime_state, command.get("context_snapshot"),
                        command.get("message"), check_cancel, trajectory_context,
                    )
                finally:
                    _TRAJECTORY_CONTEXT.reset(token)
                self.session.pop("_active_command_deadline", None)
                self.session.pop("_active_batch_metrics", None)
                command["tool_call_count"] += int(metrics.get("tool_call_count") or 0)
                _take_batch_progress(self.session)
                self.session["no_progress_batches"] = 0
                result = {**result, "continuation_id": self.session["continuation_id"]}
                if cancel_event.is_set() or self._closed:
                    cancelled = RetrievalRuntimeError("cancelled", "Retrieval invocation was cancelled.")
                    fail_span(invocation_span, cancelled)
                    fail_trajectory(
                        trajectory_context,
                        cancelled,
                        metadata={"session_id": session_id, "invocation_seq": invocation_seq, "cancelled": True},
                    )
                    return
                record_event(trajectory_context, "checkpoint.saved", status="completed", summary={"kind": result.get("kind")}, artifacts={"result": result, "metrics": metrics})
                complete_span(invocation_span, metadata={"kind": result.get("kind")})
                complete_trajectory(
                    trajectory_context,
                    metadata={
                        "kind": result.get("kind"),
                        "session_id": session_id,
                        "invocation_seq": invocation_seq,
                    },
                )
                self._publish(command, result)



                if result.get("kind") != "feedback":
                    return
            except RetrievalRuntimeError as error:
                batch_metrics = self.session.pop("_active_batch_metrics", {})
                self.session.pop("_active_command_deadline", None)
                command["tool_call_count"] += int(batch_metrics.get("tool_call_count") or 0)
                if error.code == "timeout" and deadline is not None:
                    self._publish_timeout(command, trajectory_context, invocation_span, session_id, invocation_seq)
                    return
                if error.code == "tool_limit" and not cancel_event.is_set() and not self._closed:
                    progress = _take_batch_progress(self.session)
                    made_progress = _batch_made_progress(progress)
                    if made_progress:
                        self.session["no_progress_batches"] = 0
                    else:
                        self.session["no_progress_batches"] = int(self.session.get("no_progress_batches") or 0) + 1
                    should_stop = self.session["no_progress_batches"] >= RETRIEVAL_MAX_NO_PROGRESS_BATCHES
                    result = _make_no_progress_result(self.session) if should_stop else None



                    if should_stop:
                        record_event(
                            trajectory_context,
                            "checkpoint.saved",
                            status="completed",
                            summary={"kind": result.get("kind"), "reason": "no_progress"},
                            artifacts={"result": result},
                        )
                    complete_span(invocation_span, metadata={
                        "kind": "no_progress" if should_stop else "tool_limit",
                        "no_progress_batches": self.session["no_progress_batches"],
                        **progress,
                        **trajectory_metadata,
                    })
                    complete_trajectory(
                        trajectory_context,
                        metadata={
                            "kind": "no_progress" if should_stop else "tool_limit",
                            "no_progress_batches": self.session["no_progress_batches"],
                            **progress,
                            **trajectory_metadata,
                        },
                    )
                    if should_stop:
                        self._publish(command, result)
                        return
                    continue
                if self._closed or (error.code in {"preempted", "cancelled"} and cancel_event.is_set()):
                    fail_span(invocation_span, error)
                    fail_trajectory(
                        trajectory_context,
                        error,
                        metadata={"session_id": session_id, "invocation_seq": invocation_seq, "cancelled": True},
                    )
                    return
                result = _technical(error.code, str(error), self.session["continuation_id"])
                fail_span(invocation_span, error)
                fail_trajectory(trajectory_context, error, metadata={"session_id": session_id, "invocation_seq": invocation_seq})
                self._publish(command, result)
                return
            except Exception as error:
                batch_metrics = self.session.pop("_active_batch_metrics", {})
                self.session.pop("_active_command_deadline", None)
                command["tool_call_count"] += int(batch_metrics.get("tool_call_count") or 0)
                if deadline is not None and time.monotonic() >= deadline:
                    self._publish_timeout(command, trajectory_context, invocation_span, session_id, invocation_seq)
                    return
                if self._closed:
                    return
                result = _technical("provider_or_runtime_error", str(error), self.session["continuation_id"])
                fail_span(invocation_span, error)
                fail_trajectory(trajectory_context, error, metadata={"session_id": session_id, "invocation_seq": invocation_seq})
                self._publish(command, result)
                return

    def _run(self):
        while not self._closed:
            item = self._next_command()
            if item is None:
                return
            command, cancel_event = item
            try:
                self._run_command(command, cancel_event)
            finally:
                with self._condition:
                    self._current_cancel = None
                    self._active_command = None
                    self.session["last_activity"] = self.manager.clock()


def _running_result(session):
    return {
        "kind": "running",
        "message": "Retrieval is continuing in the background.",
        "continuation_id": session.get("continuation_id"),
    }


def _revalidate_delivery(result, root=None):
    if any(ltm_store.get_object(item["object_id"], root=root) is None for item in result.get("evidence", [])):
        return {"kind": "technical", "message": "Retrieved memory was deleted before delivery; start a new retrieval.",
                "continuation_id": result.get("continuation_id"), "error": {"code": "stale_evidence"}}
    return result


def run_retrieve_memory(request, context_snapshot=None, runtime_state=None, check_cancel=None, *, caller_profile=MAIN_AGENT_CALLER, root=None):
    """Submit retrieval with caller-bound synchronous or Main Agent Wake delivery."""
    started = time.perf_counter()
    runtime_state = runtime_state if isinstance(runtime_state, dict) else {}
    configured_ttl = runtime_state.get("memory_retrieval_ttl_seconds")
    manager = RetrievalSessionManager(
        runtime_state,
        ttl_seconds=configured_ttl if configured_ttl is not None else RETRIEVAL_SESSION_TTL_SECONDS,
    )
    try:
        if caller_profile not in (MAIN_AGENT_CALLER, MAINTAINER_CALLER):
            raise RetrievalRuntimeError("invalid_profile", "A trusted caller profile is required.")
        if caller_profile.synchronous and isinstance(request, dict) and "include_context" in request:
            raise RetrievalRuntimeError("invalid_arguments", "include_context is unavailable for this caller.")
        request = _validate_public_request(request)
        if request["query"] is not None:
            session = manager.create(request["query"], request.get("message"))
            session.update(caller_profile=caller_profile, root=root)
        else:
            session = manager.get(request["continuation_id"])
            if _caller(session) != caller_profile or session.get("root") != root:
                raise RetrievalRuntimeError("invalid_continuation", "Session belongs to another caller or root.")
        fingerprint = _fingerprint(request)

        if request["close"]:
            result = manager.close(session)
            closed_results = runtime_state.setdefault(CLOSED_KEY, {})
            closed_results[session["continuation_id"]] = {"fingerprint": fingerprint, "result": copy.deepcopy(result)}
            while len(closed_results) > 8:
                closed_results.pop(next(iter(closed_results)))
            return result

        if request["include_context"] and context_snapshot is None:
            result = _technical("context_unavailable", "include_context requested but no provider Context Snapshot was captured.", session.get("continuation_id"))
            session["last_request_fingerprint"], session["last_result"] = fingerprint, copy.deepcopy(result)
            return result

        with manager.lock:
            worker = session.get("worker")
            if session.get("last_request_fingerprint") == fingerprint and session.get("last_result") is not None:
                return _revalidate_delivery(copy.deepcopy(session["last_result"]), root)
            if worker is None or not worker.is_alive:
                worker = RetrievalWorker(manager, session, runtime_state)
                session["worker"] = worker
                session["worker_generation"] = int(session.get("worker_generation") or 0) + 1
            command = {
                "event": threading.Event(),
                "result": None,
                "detached": False,
                "delivered": False,
                "message": request.get("message"),
                "context_snapshot": context_snapshot if request["include_context"] else None,
                "check_cancel": check_cancel,
                "fingerprint": fingerprint,
                "started_at": time.monotonic(),
                "deadline": (
                    time.monotonic() + RETRIEVAL_TIMEOUT_SECONDS
                    if caller_profile is MAINTAINER_CALLER else None
                ),
                "batch_count": 0,
                "tool_call_count": 0,
            }
            session["last_request_fingerprint"] = fingerprint
            session["last_result"] = _running_result(session)
            worker.submit(command)

        if caller_profile.synchronous:
            try:
                while not command["event"].wait(0.1):
                    if check_cancel:
                        check_cancel()
                    if not worker.is_alive:
                        return _technical("worker_stopped", "Retrieval worker stopped.", session["continuation_id"])
            except BaseException:
                manager.close(session)
                raise
            return _revalidate_delivery(command.get("result") or _technical("missing_result", "No retrieval result.", session["continuation_id"]), root)
        if command["event"].wait(RETRIEVAL_SYNC_WINDOW_SECONDS):
            result = command.get("result") or _running_result(session)
            return _revalidate_delivery(result, root)
        with manager.lock:
            command["detached"] = True
            last_result = session.get("last_result")
            if not command["event"].is_set() and not (
                isinstance(last_result, dict) and last_result.get("kind") not in {None, "running", "feedback"}
            ):
                _set_retriever_background_status(session, runtime_state, True, reason="sync_window_elapsed")
        return _running_result(session)
    except RetrievalRuntimeError as error:
        if error.code == "invalid_continuation" and isinstance(request, dict) and request.get("continuation_id"):
            closed = (runtime_state.get(CLOSED_KEY) or {}).get(request.get("continuation_id"))
            if isinstance(closed, dict) and closed.get("fingerprint") == _fingerprint(request):
                return copy.deepcopy(closed.get("result"))
        session = manager.current()
        continuation = error.continuation_id or (session.get("continuation_id") if isinstance(session, dict) else None)
        return _technical(error.code, str(error), continuation)
    finally:
        observer = _observer(runtime_state)
        if observer is not None:
            observer["retrieval_request_count"] = int(observer.get("retrieval_request_count") or 0) + 1
            observer["last_result"] = {"elapsed_ms": round((time.perf_counter() - started) * 1000), **observer.get("last_result", {})}


def run_retrieval_memory(*args, **kwargs):
    """Spelling alias used by a few dependency-injection callers."""
    return run_retrieve_memory(*args, **kwargs)


RETRIEVAL_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search_candidates", "description": "Generate service-specific lexical or embedding candidates and store a Session-local Candidate Pool.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "service": {"type": "string", "enum": ["lexical", "embedding", "both"]}, "hypothesis": {"type": "string"}, "projection": {"type": "string", "enum": ["count_only", "title", "title_summary", "candidate"]}, "limit": {"type": "integer"}}, "required": ["query", "service"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "inspect_candidate_pool", "description": "Inspect a stored Candidate Pool without searching again.", "parameters": {"type": "object", "properties": {"pool_id": {"type": "string"}, "projection": {"type": "string", "enum": ["count_only", "title", "title_summary", "candidate"]}, "cursor": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["pool_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rank_1_candidate_pools", "description": "Apply shared deterministic Rank 1 to one or more Candidate Pools.", "parameters": {"type": "object", "properties": {"pool_ids": {"type": "array", "items": {"type": "string"}}, "max_items": {"type": "integer"}, "projection": {"type": "string", "enum": ["count_only", "title", "title_summary", "candidate"]}, "limit": {"type": "integer"}}, "required": ["pool_ids"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rank_2_candidate_pools", "description": "Apply caller-neutral model Rank 2 using the original Query and Session guidance.", "parameters": {"type": "object", "properties": {"pool_ids": {"type": "array", "items": {"type": "string"}}, "max_items": {"type": "integer"}, "projection": {"type": "string", "enum": ["count_only", "title", "title_summary", "candidate"]}, "limit": {"type": "integer"}}, "required": ["pool_ids"], "additionalProperties": False}}},
] + _reference_observation_tool_schemas()
