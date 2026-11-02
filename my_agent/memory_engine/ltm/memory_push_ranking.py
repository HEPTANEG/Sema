"""Three-rank selection pipeline for Memory Push."""

import json
import time

from model.provider import call_chat_completion_stream
from persistence.trajectory import (
    complete_span,
    complete_trajectory,
    fail_span,
    fail_trajectory,
    provider_exchange_observer,
    record_event,
    start_span,
    start_trajectory,
)
from shared.prompt_loader import load_prompt
from bridge.ui_events import emit_ui_event

from . import (
    embedding_adapter,
    ltm_store,
    memory_push,
    memory_push_rank_1,
    memory_push_latency_diagnostics,
    object_references,
    reranker_adapter,
    retrieval_context_compiler,
)
from .meta_memory import system_owned_tags
from .reranker_adapter import rank_2_candidates
from ..token_counter import count_message_tokens, count_text_tokens


RANK_1_MAX_ITEMS = 400
RANK_2_MAX_ITEMS = 80
RANK_3_MAX_ITEMS = 40


STM_MAX_TOKENS = 1_024
CANDIDATE_MAX_TOKENS = 12_000
MODEL_CONTEXT_RESERVE = 1_024
MODEL_MAX_TOKENS = 32_768
RANK_2_QUERY_MAX_TOKENS = STM_MAX_TOKENS


def _memory_push_placeholder(message, stage, reason):
    return (
        "<long-term-memory>\n"
        f"{message}\n"
        f"stage: {stage}\n"
        f"reason: {reason}\n"
        "</long-term-memory>"
    )


def _empty_result(metrics, *, reason, candidate_pool=None):
    metrics["status"] = "empty"
    metrics["reason_code"] = reason
    return {
        "status": "ok",
        "content": _memory_push_placeholder(
            "No relevant long-term memory was found for this turn.",
            metrics.get("stage") or "memory_push",
            reason,
        ),
        "diagnostic_only": True,
        "candidate_pool": candidate_pool or {"status": "ok", "items": [], "errors": []},
        "selected_objects": [],
        "metrics": metrics,
    }


def _failure_reason(metrics, error):
    if metrics.get("reason_code"):
        return metrics["reason_code"]
    stage = metrics.get("stage") or "memory_push"
    message = str(error or "").lower()
    if stage == "query_compilation":
        return "query_compilation_failed"
    if stage == "candidate_pool":
        if "vector" in message:
            return "vector_index_unavailable"
        if "embed" in message:
            return "embedding_unavailable"
        return "candidate_retrieval_failed"
    if stage == "object_hydration":
        return "object_hydration_failed"
    if stage == "rank_1":
        return "rank_1_failed"
    if stage == "rank_2":
        return "rank_2_failed"
    if stage == "rank_3":
        if any(token in message for token in ("invalid", "unknown", "duplicate", "shape")):
            return "invalid_rank_3_output"
        return "rank_3_provider_failed"
    return "memory_push_failed"


def _emit_memory_push_stage(runtime_state, label):
    """Keep internal Memory Push phases out of the user-facing status line."""
    return None


def _message_text(message):
    role = str((message or {}).get("role") or "message")
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return f"[{role}] {content}"
    return f"[{role}] {json.dumps(content, ensure_ascii=False, sort_keys=True)}"


def _truncate_text(text, max_tokens):
    text = str(text or "")
    if count_text_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_text_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _trim_stm_messages(messages, max_tokens=STM_MAX_TOKENS):
    messages = [dict(message) for message in (messages or []) if isinstance(message, dict) and message.get("role") != "system"]
    while messages and count_text_tokens("\n".join(_message_text(message) for message in messages)) > max_tokens:
        if len(messages) == 1:
            message = dict(messages[0])
            content = message.get("content", "")
            role = str(message.get("role") or "message")
            prefix_tokens = count_text_tokens(f"[{role}] ")
            message["content"] = _truncate_text(content, max(1, max_tokens - prefix_tokens))
            messages[0] = message
            break
        messages.pop(0)
    return messages


def build_rank_2_query(stm_messages, max_tokens=RANK_2_QUERY_MAX_TOKENS):
    """Build the STM-only query used by Rank 2."""
    return "\n".join(_message_text(message) for message in _trim_stm_messages(stm_messages, max_tokens=max_tokens))


def _candidate_text(candidate, max_tokens=CANDIDATE_MAX_TOKENS):
    title = str(candidate.get("title") or "")
    summary = candidate.get("summary")
    text = title
    if summary:
        text += "\n" + str(summary)
    if count_text_tokens(text) > max_tokens:
        remaining = max(1, max_tokens - count_text_tokens(title))
        summary_text = _truncate_text(str(summary or ""), remaining)
        text = title + ("\n" + summary_text if summary_text else "")
    return text


def prepare_rank_2_candidates(candidates, max_tokens=CANDIDATE_MAX_TOKENS):
    prepared = []
    for candidate in list(candidates or [])[:RANK_1_MAX_ITEMS]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("object_id"), str):
            continue
        prepared.append({"object_id": candidate["object_id"], "text": _candidate_text(candidate, max_tokens=max_tokens)})
    return prepared


def _rank_3_json_response(runtime_state, messages):
    if isinstance(runtime_state, dict):
        responses = runtime_state.get("eval_memory_push_rank_3_responses")
        if isinstance(responses, list):
            index = int(runtime_state.get("eval_memory_push_rank_3_index") or 0)
            if index >= len(responses):
                raise RuntimeError("Memory Push Rank 3 eval response hook exhausted.")
            runtime_state["eval_memory_push_rank_3_index"] = index + 1
            response = responses[index]
            if isinstance(response, dict) and "content" in response:
                return str(response["content"])
            return json.dumps(response, ensure_ascii=False)
    context = (runtime_state or {}).get("_memory_push_trajectory_context")
    observer = provider_exchange_observer(context, purpose="memory push rank 3", agent_type="memory_push")
    profiler = (runtime_state or {}).get("_memory_push_diagnostic_profiler")
    provider_started = time.perf_counter()
    provider_timing = {
        "transport": "stream",
        "headers_ms": None,
        "first_content_ms": None,
        "output_ms": None,
        "complete_ms": None,
        "validation_ms": None,
        "content_delta_count": 0,
    }

    def on_headers():
        provider_timing["headers_ms"] = (time.perf_counter() - provider_started) * 1000

    def on_delta(_delta):
        provider_timing["content_delta_count"] += 1
        if provider_timing["first_content_ms"] is None:
            provider_timing["first_content_ms"] = (time.perf_counter() - provider_started) * 1000

    try:
        response = call_chat_completion_stream(
            messages,
            tools=None,
            temperature=0,
            timeout_seconds=120,
            purpose="memory push rank 3",
            on_delta=on_delta,
            on_headers=on_headers,
            exchange_observer=observer,
        )
        return response.get("content") if isinstance(response, dict) else ""
    finally:
        provider_timing["complete_ms"] = (time.perf_counter() - provider_started) * 1000
        if provider_timing["first_content_ms"] is not None:
            provider_timing["output_ms"] = provider_timing["complete_ms"] - provider_timing["first_content_ms"]
        if profiler is not None:
            profiler.record_provider_timing("rank_3", "provider", provider_timing)


def _validate_rank_3_result(raw_content, references, max_items=RANK_3_MAX_ITEMS):
    try:
        result = json.loads(str(raw_content or "").strip())
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Memory Push Rank 3 returned invalid JSON.") from error
    if not isinstance(result, dict) or set(result) != {"selected_refs"}:
        raise RuntimeError("Memory Push Rank 3 returned an invalid result shape.")
    selected = result.get("selected_refs")
    if not isinstance(selected, list):
        raise RuntimeError("Memory Push Rank 3 selected_refs is invalid.")
    try:
        resolved = references.resolve_many(selected)
        return resolved[:int(max_items)]
    except object_references.ObjectReferenceError as error:
        raise RuntimeError(f"Memory Push Rank 3 selected invalid refs: {error}.") from error


def _full_objects(items, root=None):
    object_ids = list(dict.fromkeys(
        item.get("object_id")
        for item in items
        if isinstance(item, dict) and item.get("object_id")
    ))
    root_ids = {item["object_id"] for item in system_owned_tags(root)}
    stored_objects = ltm_store.get_objects(object_ids, include_payload=True, root=root)
    objects = {}
    for object_id in object_ids:
        if not object_id or object_id in root_ids:
            continue
        memory_object = stored_objects.get(object_id)
        strength = ((memory_object or {}).get("derived") or {}).get("strength")
        if memory_object and isinstance(strength, (int, float)) and not isinstance(strength, bool) and strength >= ltm_store.PUSH_STRENGTH_THRESHOLD:
            objects[object_id] = memory_object
    return objects


def _rank_3_messages(context_messages, objects):
    context_messages = [
        dict(message)
        for message in (context_messages or [])
        if isinstance(message, dict) and message.get("role") != "system"
    ]
    references = object_references.ObjectReferenceMap()
    candidates = []
    for object_id, memory_object in objects.items():
        candidate = {
            "ref": references.register(object_id),
            "title": memory_object.get("title"),
            "payload": memory_object.get("payload"),
        }
        candidates.append(candidate)
    messages = [
        {"role": "system", "content": load_prompt("memory_push_rank_3")},
        {
            "role": "user",
            "content": (
                "[CURRENT_CONTEXT]\n"
                + json.dumps(context_messages, ensure_ascii=False)
                + "\n[/CURRENT_CONTEXT]\n\n[CANDIDATE_MEMORIES]\n"
                + json.dumps(candidates, ensure_ascii=False)
                + "\n[/CANDIDATE_MEMORIES]"
            ),
        },
    ]
    return messages, references


def _provenance_sort_key(memory_object, root=None):
    links = ltm_store.get_links(memory_object["object_id"], "outgoing", relation_type="sourced_from", root=root)
    slots = [
        (int(link["slot_seq_start"]), int(link["slot_seq_end"]))
        for link in links
        if link.get("target_kind") == "event_slot" and link.get("slot_seq_start") is not None
    ]
    if slots:
        start, end = min(slots)
        return (0, start, end, "", memory_object["object_id"])
    return (1, 0, 0, str(memory_object.get("created_at") or ""), memory_object["object_id"])


def _render_memory_material(memory_object):
    payload = memory_object.get("payload")
    if payload is None:
        return str(memory_object.get("title") or "")
    body = payload.get("body") or ""
    structured = payload.get("structured_data")
    if structured is None:
        return str(body)
    if body:
        return body + "\n" + json.dumps(structured, ensure_ascii=False, sort_keys=True)
    return json.dumps(structured, ensure_ascii=False, sort_keys=True)


def render_memory_push(objects):
    materials = [_render_memory_material(memory_object) for memory_object in objects]
    materials = [material for material in materials if material]
    if not materials:
        return None
    return "<long-term-memory>\n" + "\n\n".join(materials) + "\n</long-term-memory>"


def _record_metrics(runtime_state, metrics):
    if not isinstance(runtime_state, dict):
        return
    observer = runtime_state.setdefault("memory_engine_memory_push_observer", {"memory_push_request_count": 0, "last_result": {}})
    observer["memory_push_request_count"] = int(observer.get("memory_push_request_count") or 0) + 1
    observer["last_result"] = dict(metrics)


def _trace_metrics(runtime_state, metrics, status="running"):
    if not isinstance(runtime_state, dict):
        return
    stage = metrics.get("stage") or ""
    event_type = f"memory_push.{stage}" if stage in {"rank_1", "rank_2", "rank_3"} else "memory_push.state"
    record_event(
        runtime_state.get("_memory_push_trajectory_context"),
        event_type,
        status="failed" if status == "error" else status,
        summary={
            "stage": stage,
            "candidate_pool_count": int(metrics.get("candidate_pool_count") or 0),
            "rank_1_count": int(metrics.get("rank_1_count") or 0),
            "rank_2_input_count": int(metrics.get("rank_2_input_count") or 0),
            "rank_2_output_count": int(metrics.get("rank_2_output_count") or 0),
            "rank_3_selected_count": int(metrics.get("rank_3_selected_count") or 0),
            "error_type": metrics.get("error_type") or "",
            "reason_code": metrics.get("reason_code") or "",
        },
    )


def memory_push_observer_snapshot(runtime_state=None):
    source = (runtime_state or {}).get("memory_engine_memory_push_observer") or {}
    return {"memory_push_request_count": int(source.get("memory_push_request_count") or 0), "last_result": dict(source.get("last_result") or {})}


def validate_push_result(result, root=None):
    """Rehydrate cached selections immediately before injection."""
    from . import ltm_store
    result = dict(result)
    object_ids = list(result.get("used_object_ids") or [])
    objects_by_id = ltm_store.get_objects(object_ids, include_payload=True, root=root)
    selected = []
    for object_id in object_ids:
        obj = objects_by_id.get(object_id)
        if obj and obj.get("derived", {}).get("strength", 0) >= ltm_store.PUSH_STRENGTH_THRESHOLD:
            selected.append(obj)
    result.update(selected_objects=selected, used_object_ids=[obj["object_id"] for obj in selected],
                  content=render_memory_push(selected))
    return result


def _baseline_result(entry):
    if not isinstance(entry, dict):
        return None
    return {
        "status": entry.get("status") or "empty",
        "content": entry.get("content"),
        "used_object_ids": list(entry.get("used_object_ids") or []),
        "candidate_pool": entry.get("candidate_pool") or {},
        "selected_objects": entry.get("selected_objects") or [],
        "metrics": {"mode": entry.get("mode") or "cache_reuse", "cache_reused": True},
    }


def _merge_candidate_pool_items(existing_items, new_items):
    """Union cached and incremental candidates without duplicate object IDs."""
    merged = {}
    for item in [*(existing_items or []), *(new_items or [])]:
        if not isinstance(item, dict) or not item.get("object_id"):
            continue
        object_id = item["object_id"]


        merged[object_id] = dict(item)
    return list(merged.values())


def _merge_incremental_result(baseline, new_objects, selected_ids, root=None):
    old_objects = {
        item.get("object_id"): item
        for item in (baseline or {}).get("selected_objects") or []
        if isinstance(item, dict) and item.get("object_id")
    }
    for object_id in selected_ids:
        if object_id in new_objects:
            old_objects[object_id] = new_objects[object_id]
    ordered = list(old_objects.values())
    if len(ordered) > RANK_3_MAX_ITEMS:
        ordered = ordered[-RANK_3_MAX_ITEMS:]
    ordered.sort(key=lambda item: _provenance_sort_key(item, root))
    content = render_memory_push(ordered)
    return {
        "status": "ok",
        "content": content,
        "used_object_ids": [item["object_id"] for item in ordered if _render_memory_material(item)],
        "selected_objects": ordered,
    }


from .storage_gate import memory_task


@memory_task
def run_memory_push(
    stm_messages,
    wm_messages,
    *,
    root=None,
    runtime_state=None,
    mode="full",
    baseline=None,
    max_selected_items=None,
    rank_3_context_messages=None,
):
    started_at = time.perf_counter()
    parent_context = (runtime_state or {}).get("_context_build_trajectory_context") or (runtime_state or {}).get("trajectory_context")
    owned_root = None
    if not (parent_context or {}).get("enabled"):
        owned_root = start_trajectory("memory_push", metadata={"mode": mode}, root=root)
        parent_context = owned_root
    focus_action = ""
    if mode != "full_background" and isinstance(runtime_state, dict):
        focus_action = str(runtime_state.get("_focus_detector_action") or "")
    push_span = start_span(
        parent_context,
        "memory_push",
        "memory push",
        metadata={"mode": mode, "focus_action": focus_action},
    )
    profiler = memory_push_latency_diagnostics.begin_turn(root)
    if isinstance(runtime_state, dict):
        runtime_state["_memory_push_trajectory_context"] = push_span
        runtime_state["_memory_push_diagnostic_profiler"] = profiler
    metrics = {
        "focus_action": focus_action,
        "candidate_pool_count": 0,
        "rank_1_count": 0,
        "rank_2_input_count": 0,
        "rank_2_output_count": 0,
        "rank_3_selected_count": 0,
        "status": "",
        "stage": "",
        "injected": False,
    }
    selected_limit = RANK_3_MAX_ITEMS if max_selected_items is None else min(RANK_3_MAX_ITEMS, max(1, int(max_selected_items)))
    rank_3_context = stm_messages if rank_3_context_messages is None else rank_3_context_messages
    emit_lifecycle = mode != "full_background" and isinstance(runtime_state, dict) and runtime_state.get("ui_event_consumer") != "eval"
    if emit_lifecycle:
        emit_ui_event(runtime_state, "memory_push_started", path="foreground", mode=mode, status="running")
    try:
        metrics["mode"] = mode
        metrics["stage"] = "query_compilation"
        _trace_metrics(runtime_state, metrics)
        _emit_memory_push_stage(runtime_state, "Generating memory retrieval queries")
        contexts = retrieval_context_compiler.build_channel_contexts(stm_messages, wm_messages)
        with profiler.measure("query_compilation_ms"):
            channels = {
                "incremental": ("latest_semantic", "entity"),
                "full_background": ("general_semantic", "entity"),
                "focus_shift_sync": ("general_semantic", "latest_semantic", "entity"),
            }.get(mode)
            if channels:
                compiled = retrieval_context_compiler.compile_retrieval_queries(
                    contexts,
                    channels=channels,
                    trajectory_context=push_span,
                    _diagnostic_profiler=profiler,
                )
            else:
                compiled = retrieval_context_compiler.compile_retrieval_queries(
                    contexts,
                    trajectory_context=push_span,
                    _diagnostic_profiler=profiler,
                )
        profiler.compiler_result(compiled)
        metrics["compiler_query_count"] = sum(
            len(channel.get("queries") or [])
            for channel in (compiled.get("channels") or [])
            if isinstance(channel, dict)
        )
        metrics["compiler_successful_channel_count"] = sum(
            1
            for channel in (compiled.get("channels") or [])
            if isinstance(channel, dict) and isinstance(channel.get("queries"), list)
        )
        metrics["stage"] = "candidate_pool"
        _trace_metrics(runtime_state, metrics)
        _emit_memory_push_stage(runtime_state, "Searching long-term memory")
        with profiler.measure("candidate_pool_ms"):
            pool = memory_push.build_candidate_pool(compiled, root=root, _diagnostic_profiler=profiler)
        metrics["candidate_pool_count"] = len(pool.get("items") or [])
        record_event(
            push_span,
            "memory_push.candidate_pool.built",
            status="failed" if pool.get("status") == "error" else "completed",
            summary={"candidate_count": metrics["candidate_pool_count"], "error_count": len(pool.get("errors") or [])},
        )
        for channel_result in compiled.get("channels") or []:
            if not isinstance(channel_result, dict):
                continue
            channel = channel_result.get("channel") or "unknown"
            channel_hits = sum(
                1
                for item in pool.get("items") or []
                if channel in (((item or {}).get("retrieval_evidence") or {}).get("channels") or [])
            )
            record_event(
                push_span,
                "memory_push.channel.executed",
                status="completed",
                summary={"channel": channel, "query_count": len(channel_result.get("queries") or []), "candidate_count": channel_hits},
            )
        _trace_metrics(runtime_state, metrics)
        if pool.get("status") == "error":
            metrics["reason_code"] = "candidate_retrieval_failed"
            raise RuntimeError("Memory Push candidate retrieval failed.")
        if not pool.get("items"):
            if mode == "incremental" and baseline:
                cached = _baseline_result(baseline)
                if cached:
                    cached["metrics"].update(metrics)
                    cached["metrics"]["status"] = "ok"
                    return cached
            reason = "no_compiled_queries" if not metrics.get("compiler_query_count") else "empty_candidate_pool"
            return _empty_result(metrics, reason=reason, candidate_pool=pool)

        if mode == "incremental":
            previous_selected_ids = {
                item.get("object_id")
                for item in (baseline or {}).get("selected_objects") or []
                if isinstance(item, dict) and item.get("object_id")
            }
            recheck_items = [
                item
                for item in pool.get("items") or []
                if item.get("object_id") not in previous_selected_ids
            ]
            if not recheck_items:
                cached = _baseline_result(baseline)
                if cached:
                    cached["candidate_pool"] = {
                        "status": "ok",
                        "items": ((baseline or {}).get("candidate_pool") or {}).get("items") or [],
                        "query_evidence": [],
                        "errors": [],
                    }
                    cached["metrics"].update(metrics)
                    cached["metrics"]["status"] = "ok"
                    return cached
                return _empty_result(metrics, reason="empty_incremental_result", candidate_pool=pool)
            metrics["stage"] = "object_hydration"
            with profiler.measure("object_hydration_ms"):
                recheck_objects = _full_objects([{"object_id": item["object_id"]} for item in recheck_items], root=root)
            if not recheck_objects:
                return _baseline_result(baseline) or _empty_result(metrics, reason="no_hydrated_objects", candidate_pool=pool)
            metrics["stage"] = "rank_3"
            _trace_metrics(runtime_state, metrics)
            _emit_memory_push_stage(runtime_state, "Selecting long-term memory")
            rank_3_messages, references = _rank_3_messages(rank_3_context, recheck_objects)
            with profiler.measure("rank_3_provider_ms"):
                rank_3_result = _rank_3_json_response(runtime_state, rank_3_messages)
            selected_ids = _validate_rank_3_result(rank_3_result, references, max_items=selected_limit)
            record_event(push_span, "memory_push.final_selection", status="completed", summary={"selected_count": len(selected_ids), "mode": mode})
            metrics["rank_3_selected_count"] = len(selected_ids)
            merged = _merge_incremental_result(baseline, recheck_objects, selected_ids, root=root)
            metrics["status"] = "ok"
            metrics["injected"] = bool(merged.get("content"))
            merged["candidate_pool"] = {
                "status": pool.get("status", "ok"),
                "items": _merge_candidate_pool_items(
                    ((baseline or {}).get("candidate_pool") or {}).get("items") or [],
                    recheck_items,
                ),
                "query_evidence": [],
                "errors": pool.get("errors") or [],
            }
            merged["metrics"] = metrics
            return merged
        pool_items = list(pool.get("items") or [])
        if len(pool_items) <= RANK_2_MAX_ITEMS:
            rank_2_ids = [item["object_id"] for item in pool_items if isinstance(item, dict) and item.get("object_id")]
            metrics["rank_2_input_count"] = 0
            metrics["rank_2_output_count"] = 0
        else:
            if len(pool_items) <= RANK_1_MAX_ITEMS:
                rank_2_input = pool_items
            else:
                metrics["stage"] = "rank_1"
                _trace_metrics(runtime_state, metrics)
                with profiler.measure("rank_1_ms"):
                    ranked = memory_push_rank_1.rank_1_candidate_pool(pool, max_items=RANK_1_MAX_ITEMS)
                rank_2_input = list(ranked.get("items") or [])
                metrics["rank_1_count"] = len(rank_2_input)
                _trace_metrics(runtime_state, metrics)
            metrics["stage"] = "rank_2"
            metrics["rank_2"] = "started"
            metrics["rank_2_input_count"] = len(rank_2_input)
            _trace_metrics(runtime_state, metrics)
            _emit_memory_push_stage(runtime_state, "Rank 2: Ranking long-term memory")
            with profiler.measure("rank_2_ms"):
                rank_2 = rank_2_candidates(
                    stm_messages,
                    rank_2_input,
                    max_items=RANK_2_MAX_ITEMS,
                    _diagnostic_profiler=profiler,
                )
            if rank_2.get("status") != "ok":
                raise RuntimeError("Memory Push Rank 2 failed.")
            rank_2_ids = [item["object_id"] for item in rank_2.get("items") or []]
            metrics["rank_2_output_count"] = len(rank_2_ids)
            _trace_metrics(runtime_state, metrics)
        if not rank_2_ids:
            return _empty_result(metrics, reason="empty_rank_2_result", candidate_pool=pool)
        metrics["stage"] = "object_hydration"
        with profiler.measure("object_hydration_ms"):
            objects = _full_objects([{"object_id": object_id} for object_id in rank_2_ids], root=root)
        if not objects:
            return _empty_result(metrics, reason="no_hydrated_objects", candidate_pool=pool)
        metrics["stage"] = "rank_3"
        _trace_metrics(runtime_state, metrics)
        _emit_memory_push_stage(runtime_state, "Rank 3: Selecting long-term memory")
        rank_3_messages, references = _rank_3_messages(rank_3_context, objects)
        with profiler.measure("rank_3_provider_ms"):
            rank_3_result = _rank_3_json_response(runtime_state, rank_3_messages)
        validation_started = time.perf_counter()
        try:
            selected_ids = _validate_rank_3_result(rank_3_result, references, max_items=selected_limit)
        finally:
            profiler.record_provider_timing(
                "rank_3",
                "provider",
                {"validation_ms": (time.perf_counter() - validation_started) * 1000},
            )
        record_event(push_span, "memory_push.final_selection", status="completed", summary={"selected_count": len(selected_ids), "mode": mode})
        metrics["rank_3_selected_count"] = len(selected_ids)
        selected_objects = sorted((objects[object_id] for object_id in selected_ids), key=lambda item: _provenance_sort_key(item, root))
        rendered_object_ids = [
            memory_object["object_id"]
            for memory_object in selected_objects
            if _render_memory_material(memory_object)
        ]
        content = render_memory_push(selected_objects)
        metrics["status"] = "ok"
        metrics["injected"] = bool(content)
        if not selected_ids:
            return _empty_result(metrics, reason="no_selected_memory", candidate_pool=pool)
        return {
            "status": "ok",
            "content": content,
            "used_object_ids": rendered_object_ids if content else [],
            "candidate_pool": pool,
            "selected_objects": selected_objects,
            "metrics": metrics,
        }
    except Exception as error:
        metrics["status"] = "error"
        metrics["stage"] = metrics.get("stage") or "memory_push"
        metrics["error_type"] = type(error).__name__
        metrics["reason_code"] = _failure_reason(metrics, error)
        _trace_metrics(runtime_state, metrics, status="error")
        return {
            "status": "error",
            "content": _memory_push_placeholder(
                "Memory Push failed; no long-term memory was injected for this turn.",
                metrics["stage"],
                metrics["reason_code"],
            ),
            "diagnostic_only": True,
            "metrics": metrics,
        }
    finally:
        metrics["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000)
        if metrics.get("status") == "ok":
            _trace_metrics(runtime_state, metrics, status="ok")
        _record_metrics(runtime_state, metrics)
        memory_push_latency_diagnostics.end_turn(profiler, metrics)
        if metrics.get("status") == "error":
            error = RuntimeError(metrics.get("reason_code") or "Memory Push failed.")
            fail_span(push_span, error, metadata=metrics)
            if owned_root:
                fail_trajectory(owned_root, error, metadata=metrics)
        else:
            complete_span(push_span, metadata=metrics)
            if owned_root:
                complete_trajectory(owned_root, metadata=metrics)
        if isinstance(runtime_state, dict):
            runtime_state.pop("_memory_push_trajectory_context", None)
            runtime_state.pop("_memory_push_diagnostic_profiler", None)
        if emit_lifecycle:
            emit_ui_event(
                runtime_state,
                "memory_push_finished",
                path="foreground",
                mode=mode,
                status="error" if metrics.get("status") == "error" else "ok",
                error=metrics.get("reason_code") or "",
            )
