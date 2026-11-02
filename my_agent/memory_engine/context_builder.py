import json
import copy
import threading
import uuid
from contextlib import contextmanager

import memory_engine.event_stream as event_stream
import memory_engine.focus as focus
from memory_engine.ltm import maintenance_pressure, memory_push_cache, memory_push_ranking
import memory_engine.wm.ingress as wm_ingress
from agents.temporal_state import CONTINUOUS_SESSION_GAP_SECONDS, ensure_process_temporal_state
from bridge.ui_events import emit_ui_event
from persistence.trajectory import complete_span, fail_span, record_event, start_span
from .ltm import ltm_store
from .dynamic_context_budget import compute_and_store_dynamic_context_budget
from .stm_projection import STM_TOKEN_BUDGET, build_stm_projection
from .token_counter import count_message_tokens, count_text_tokens








WM_TOKEN_BUDGET = 6000
FULL_REFRESH_INTERVAL = 10
TOOL_RESULT_LIKE_PROCESSING_CLASS = "tool_result_like"
_FULL_REFRESH_LOCK = threading.RLock()
_FULL_REFRESHES = {}
_MAIN_AGENT_CONTEXT_TIMING = "_main_agent_context_timing_enabled"
_CONTEXT_BUILD_SPAN = "_context_build_trajectory_context"


def _context_timing_enabled(runtime_state):
    return (
        isinstance(runtime_state, dict)
        and runtime_state.get(_MAIN_AGENT_CONTEXT_TIMING)
        and (runtime_state.get("trajectory_context") or {}).get("enabled")
    )


@contextmanager
def _context_timing_stage(runtime_state, name):
    parent = (runtime_state or {}).get(_CONTEXT_BUILD_SPAN) if isinstance(runtime_state, dict) else None
    if not (parent or {}).get("enabled"):
        yield
        return
    span = start_span(
        parent,
        "context_build_stage",
        name,
        metadata={"temporary_diagnostic": "main_agent_context_timing_v1"},
    )
    try:
        yield
    except Exception as error:
        fail_span(span, error, metadata={"temporary_diagnostic": "main_agent_context_timing_v1"})
        raise
    else:
        complete_span(span, metadata={"temporary_diagnostic": "main_agent_context_timing_v1"})



def build_event_stream_context(messages, runtime_state=None):
    if not _context_timing_enabled(runtime_state):
        return _build_event_stream_context_impl(messages, runtime_state=runtime_state)
    parent = runtime_state.get("trajectory_context")
    context_span = start_span(
        parent,
        "context_build",
        "Main Agent context build",
        metadata={"temporary_diagnostic": "main_agent_context_timing_v1"},
    )
    if not context_span.get("enabled"):
        return _build_event_stream_context_impl(messages, runtime_state=runtime_state)
    runtime_state[_CONTEXT_BUILD_SPAN] = context_span
    try:
        result = _build_event_stream_context_impl(messages, runtime_state=runtime_state)
    except Exception as error:
        fail_span(context_span, error, metadata={"temporary_diagnostic": "main_agent_context_timing_v1"})
        raise
    else:
        complete_span(context_span, metadata={"temporary_diagnostic": "main_agent_context_timing_v1"})
        return result
    finally:
        runtime_state.pop(_CONTEXT_BUILD_SPAN, None)


def _build_event_stream_context_impl(messages, runtime_state=None):
    from runtime.setup_capabilities import ltm_enabled, observe_window
    processing_class = runtime_state.get("turn_processing_class") if isinstance(runtime_state, dict) else "user_like"
    tool_result_like = processing_class == TOOL_RESULT_LIKE_PROCESSING_CLASS
    preflight = isinstance(runtime_state, dict) and runtime_state.get("_context_build_phase") == "preflight"
    if tool_result_like or preflight:



        focus_result = {"action": "no_change"}
    else:
        with _context_timing_stage(runtime_state, "focus_detector"):
            emit_ui_event(runtime_state, "focus_detector_started", path="context")
            try:
                focus_result = focus.run_focus_detector_before_context(messages, runtime_state) or {}
            except Exception as error:
                emit_ui_event(runtime_state, "focus_detector_finished", path="context", status="error", error=str(error))
                raise
            emit_ui_event(
                runtime_state,
                "focus_detector_finished",
                path="context",
                status="error" if focus_result.get("detector_error") else "ok",
                triggered=bool(focus_result.get("triggered")),
                action=focus_result.get("action", ""),
                reason=focus_result.get("reason", ""),
                reason_code=focus_result.get("reason_code", ""),
                error=focus_result.get("detector_error") or "",
                active_title=focus_result.get("active_title", ""),
            )
            if isinstance(runtime_state, dict):
                runtime_state["_focus_detector_action"] = focus_result.get("action", "")
                runtime_state["_focus_detector_result"] = {
                    "action": focus_result.get("action", ""),
                    "target_segment_id": focus_result.get("target_segment_id", ""),
                    "active_title": focus_result.get("active_title", ""),
                    "reason": focus_result.get("reason", ""),
                    "reason_code": focus_result.get("reason_code", ""),
                }
                try:
                    record_event(
                        runtime_state.get("trajectory_context"),
                        "focus_detector.completed",
                        status="error" if focus_result.get("detector_error") else "ok",
                        summary=runtime_state["_focus_detector_result"],
                    )
                except Exception:
                    pass
            if focus_result.get("action") in {"activated_inactive", "created_active"}:
                title = str(focus_result.get("active_title") or "").strip()
                if title:
                    emit_ui_event(
                        runtime_state,
                        "focus_shifted",
                        path="context",
                        title=title,
                        action=focus_result.get("action", ""),
                        target_segment_id=focus_result.get("target_segment_id", ""),
                    )
    with _context_timing_stage(runtime_state, "context_pressure"):
        pressure = compute_and_store_dynamic_context_budget(messages, runtime_state, emit_observer=True, stabilize=True)
    budgets = pressure.get("budgets") or {}
    stm_budget = int(budgets.get("stm") or STM_TOKEN_BUDGET)
    wm_budget = int(budgets.get("wm") or WM_TOKEN_BUDGET)
    with _context_timing_stage(runtime_state, "stm_projection"):
        watermark = wm_ingress.effective_wm_ingress_watermark(runtime_state)
        stm_projection = build_stm_projection(messages, budget=stm_budget, wm_ingress_watermark=watermark)
    with _context_timing_stage(runtime_state, "wm_ingress"):
        wm_ingress.stage_wm_ingress_from_projection(runtime_state, stm_projection)
    with _context_timing_stage(runtime_state, "wm_projection"):
        wm_records = _build_working_memory_projection(stm_projection, budget=wm_budget)
        wm_message = _build_working_memory_message(wm_records)
    with _context_timing_stage(runtime_state, "observe_window"):
        observe_window(messages, runtime_state)
    if tool_result_like or preflight:
        memory_push_result = {"status": "not_run", "content": None, "metrics": {}}
    else:
        if isinstance(runtime_state, dict):
            runtime_state["_memory_push_messages"] = _runtime_copy(messages)
        rank_3_context_messages = _rank_3_session_messages(runtime_state)
        memory_push_result = _memory_push_for_turn(
            stm_projection.get("messages") or [],
            [wm_message] if wm_message else [],
            runtime_state,
            root=None,
            focus_action=focus_result.get("action", "no_change"),
            source_revision=len(messages or []),
            rank_3_context_messages=rank_3_context_messages,
        )
    with _context_timing_stage(runtime_state, "ltm_result_processing"):
        outbound = []
        outbound.extend(_system_messages(stm_projection["messages"]))
        maintenance_notice = maintenance_pressure.maintenance_pressure_context_notice() if ltm_enabled() else "LTM is unavailable. STM and Working Memory remain active. Do not claim to retrieve or save long-term memories. The user can configure local memory models in Settings > Model."
        if maintenance_notice:
            outbound.append({"role": "system", "content": maintenance_notice})
        if wm_message:
            outbound.append(wm_message)
        if memory_push_result.get("content"):
            memory_push_result = memory_push_ranking.validate_push_result(memory_push_result)
        if memory_push_result.get("content"):
            outbound.append({"role": "user", "content": memory_push_result["content"]})
            if memory_push_result.get("status") == "ok":
                _record_memory_push_successful_uses(memory_push_result, runtime_state)
    with _context_timing_stage(runtime_state, "context_assembly"):
        outbound.extend(_non_system_messages(stm_projection["messages"]))
    with _context_timing_stage(runtime_state, "context_token_count"):
        total_tokens = sum(count_message_tokens(message) for message in outbound)
    return {
        "messages": outbound,
        "context_order": ["system", "working_memory", "long_term_memory", "short_term_memory"],
        "working_memory": wm_records,
        "short_term_memory": stm_projection,
        "context_pressure": pressure,
        "memory_push": {
            "status": memory_push_result.get("status"),
            "injected": bool(memory_push_result.get("content"))
            and memory_push_result.get("status") == "ok"
            and not memory_push_result.get("diagnostic_only"),
            "metrics": dict(memory_push_result.get("metrics") or {}),
        },
        "total_tokens": total_tokens,
    }



def context_status(messages, runtime_state=None):
    runtime_copy = _runtime_copy(runtime_state)
    pressure = compute_and_store_dynamic_context_budget(messages, runtime_copy, emit_observer=False, stabilize=False)
    context = _build_event_stream_context_with_pressure(messages, pressure, runtime_state=runtime_copy)
    wm = context["working_memory"]
    stm = context["short_term_memory"]
    scores = pressure.get("scores") or {}
    budgets = pressure.get("budgets") or {}
    return {
        "context_order": context["context_order"],
        "budget_source": pressure.get("budget_source") or "fixed",
        "pressure_scores": scores,
        "stm_min": budgets.get("stm_min", 0),
        "stm_max": budgets.get("stm_max", 0),
        "wm_min": budgets.get("wm_min", 0),
        "wm_max": budgets.get("wm_max", 0),
        "wm_budget": wm["budget"],
        "wm_tokens": wm["selected_tokens"],
        "wm_selected_count": len(wm["selected_records"]),
        "wm_skipped_count": wm["skipped_count"],
        "wm_candidate_count": wm["candidate_count"],
        "wm_selected_range": _record_range(wm["selected_records"]),
        "wm_projection_mode": wm.get("projection_mode", "legacy"),
        "wm_metadata_tokens": wm.get("metadata_tokens", 0),
        "wm_active_detail_tokens": wm.get("active_detail_tokens", 0),
        "wm_unowned_detail_tokens": wm.get("unowned_detail_tokens", 0),
        "wm_active_detail_count": wm.get("active_detail_count", 0),
        "wm_unowned_detail_count": wm.get("unowned_detail_count", 0),
        "wm_inactive_summary_count": wm.get("inactive_summary_count", 0),
        "wm_resolved_title_count": wm.get("resolved_title_count", 0),
        "wm_resident_slot_count": wm.get("resident_slot_count", 0),
        "wm_resident_raw_token_count": wm.get("resident_raw_token_count", 0),
        "stm_budget": stm["budget"],
        "stm_raw_budget": budgets.get("stm_raw", budgets.get("stm", 0)),
        "wm_raw_budget": budgets.get("wm_raw", budgets.get("wm", 0)),
        "wm_ingress_watermark": stm.get("wm_ingress_watermark", -1),
        "watermark_locked_omitted_block_count": stm.get("watermark_locked_omitted_block_count", 0),
        "stm_selected_tokens": stm["selected_tokens"],
        "stm_selected_block_count": len(stm["selected_blocks"]),
        "stm_omitted_block_count": len(stm["omitted_blocks"]),
        "total_context_tokens": context["total_tokens"],
    }



def format_context_status(status):
    status = status or {}
    order = status.get("context_order") or []
    lines = [
        "Context Builder status:",
        f"context_order: {' -> '.join(order) if order else '(none)'}",
        f"budget_source: {status.get('budget_source') or '(none)'}",
        f"pressure_I: {(status.get('pressure_scores') or {}).get('I', 0)}",
        f"pressure_S: {(status.get('pressure_scores') or {}).get('S', 0)}",
        f"pressure_R: {(status.get('pressure_scores') or {}).get('R', 0)}",
        f"pressure_L: {(status.get('pressure_scores') or {}).get('L', 0)}",
        f"pressure_P_context: {(status.get('pressure_scores') or {}).get('P_context', 0)}",
        f"wm_budget: {status.get('wm_budget', 0)}",
        f"wm_raw_budget: {status.get('wm_raw_budget', 0)}",
        f"wm_budget_range: {status.get('wm_min', 0)}..{status.get('wm_max', 0)}",
        f"wm_tokens: {status.get('wm_tokens', 0)}",
        f"wm_selected_count: {status.get('wm_selected_count', 0)}",
        f"wm_skipped_count: {status.get('wm_skipped_count', 0)}",
        f"wm_candidate_count: {status.get('wm_candidate_count', 0)}",
        f"wm_selected_range: {status.get('wm_selected_range') or '(none)'}",
        f"wm_projection_mode: {status.get('wm_projection_mode', 'legacy')}",
        f"wm_metadata_tokens: {status.get('wm_metadata_tokens', 0)}",
        f"wm_active_detail_tokens: {status.get('wm_active_detail_tokens', 0)}",
        f"wm_unowned_detail_tokens: {status.get('wm_unowned_detail_tokens', 0)}",
        f"wm_active_detail_count: {status.get('wm_active_detail_count', 0)}",
        f"wm_unowned_detail_count: {status.get('wm_unowned_detail_count', 0)}",
        f"wm_inactive_summary_count: {status.get('wm_inactive_summary_count', 0)}",
        f"wm_resolved_title_count: {status.get('wm_resolved_title_count', 0)}",
        f"wm_resident_slot_count: {status.get('wm_resident_slot_count', 0)}",
        f"wm_resident_raw_token_count: {status.get('wm_resident_raw_token_count', 0)}",
        f"stm_budget: {status.get('stm_budget', 0)}",
        f"stm_raw_budget: {status.get('stm_raw_budget', 0)}",
        f"stm_budget_range: {status.get('stm_min', 0)}..{status.get('stm_max', 0)}",
        f"wm_ingress_watermark: {status.get('wm_ingress_watermark', -1)}",
        f"watermark_locked_omitted_block_count: {status.get('watermark_locked_omitted_block_count', 0)}",
        f"stm_selected_tokens: {status.get('stm_selected_tokens', 0)}",
        f"stm_selected_block_count: {status.get('stm_selected_block_count', 0)}",
        f"stm_omitted_block_count: {status.get('stm_omitted_block_count', 0)}",
        f"total_context_tokens: {status.get('total_context_tokens', 0)}",
    ]
    return "\n".join(lines)


def _build_event_stream_context_with_pressure(messages, pressure, runtime_state=None):
    budgets = pressure.get("budgets") or {}
    stm_budget = int(budgets.get("stm") or STM_TOKEN_BUDGET)
    wm_budget = int(budgets.get("wm") or WM_TOKEN_BUDGET)
    watermark = wm_ingress.effective_wm_ingress_watermark(runtime_state)
    stm_projection = build_stm_projection(messages, budget=stm_budget, wm_ingress_watermark=watermark)
    wm_records = _build_working_memory_projection(stm_projection, budget=wm_budget)
    wm_message = _build_working_memory_message(wm_records)
    outbound = []
    outbound.extend(_system_messages(stm_projection["messages"]))
    maintenance_notice = maintenance_pressure.maintenance_pressure_context_notice()
    if maintenance_notice:
        outbound.append({"role": "system", "content": maintenance_notice})
    if wm_message:
        outbound.append(wm_message)
    outbound.extend(_non_system_messages(stm_projection["messages"]))
    return {
        "messages": outbound,
        "context_order": ["system", "working_memory", "long_term_memory", "short_term_memory"],
        "working_memory": wm_records,
        "short_term_memory": stm_projection,
        "context_pressure": pressure,
        "memory_push": {"status": "not_run", "injected": False, "metrics": {}},
        "total_tokens": sum(count_message_tokens(message) for message in outbound),
    }


def _rank_3_session_messages(runtime_state, root=None, max_slot_seq=None):
    temporal_state = ensure_process_temporal_state(runtime_state)
    return event_stream.load_current_session_messages(
        process_started_at=temporal_state.get("process_started_at"),
        gap_seconds=CONTINUOUS_SESSION_GAP_SECONDS,
        max_slot_seq=max_slot_seq,
        root=root,
    )


def _memory_push_for_turn(
    stm_messages,
    wm_messages,
    runtime_state,
    root=None,
    focus_action="no_change",
    source_revision=0,
    rank_3_context_messages=None,
):
    """Select the Focus-aware route and persist its reusable result."""
    from runtime.setup_capabilities import ltm_enabled
    if not ltm_enabled():
        return {"status": "unavailable", "content": None, "metrics": {}}
    if not isinstance(runtime_state, dict):
        return {"status": "not_run", "content": None, "metrics": {}}
    if not runtime_state.get("_memory_push_enabled"):
        return {"status": "not_run", "content": None, "metrics": {}}
    if runtime_state.get("_memory_push_attempted"):
        return dict(runtime_state.get("_memory_push_result") or {"status": "empty", "content": None, "metrics": {}})


    runtime_state["_memory_push_attempted"] = True
    if focus_action in {"activated_inactive", "created_active"}:
        mode = "focus_shift_sync"
    else:
        mode = "incremental" if memory_push_cache.load_latest(root) else "full"
    baseline = memory_push_cache.load_latest(root) if mode == "incremental" else None
    result = memory_push_ranking.run_memory_push(
        stm_messages,
        wm_messages,
        root=root,
        runtime_state=runtime_state,
        mode=mode,
        baseline=baseline,
        rank_3_context_messages=rank_3_context_messages,
    )
    if result.get("status") == "ok":
        memory_push_cache.save(
            root,
            revision=source_revision,
            focus_state="shift" if mode == "focus_shift_sync" else "stable",
            mode=mode,
            result=result,
        )
        state = memory_push_cache.load_state(root)
        stable_turn_count = 0 if mode == "focus_shift_sync" else int(state.get("stable_turn_count") or 0) + 1
        memory_push_cache.save_state(root, stable_turn_count=stable_turn_count, latest_revision=int(source_revision))
        if mode != "focus_shift_sync" and stable_turn_count >= FULL_REFRESH_INTERVAL:
            schedule_memory_push_refresh(runtime_state.get("_memory_push_messages") or [], runtime_state=runtime_state, root=root)
    runtime_state["_memory_push_result"] = dict(result)
    return result


def schedule_memory_push_refresh(messages, runtime_state=None, root=None):
    """Schedule one coalescing full refresh using a snapshot of current messages."""
    from runtime.setup_capabilities import ltm_enabled
    if not ltm_enabled():
        return None
    snapshot = [dict(message) for message in (messages or []) if isinstance(message, dict)]
    revision = len(snapshot)
    runtime_snapshot = _runtime_copy(runtime_state)
    key = str(event_stream.memory_root(root))
    with _FULL_REFRESH_LOCK:
        existing = _FULL_REFRESHES.get(key)
        if existing and existing.get("thread") and existing["thread"].is_alive():
            existing["pending"] = (snapshot, revision, runtime_snapshot)
            return existing["thread"]
        record = {"pending": None}
        _FULL_REFRESHES[key] = record

        def worker():
            from runtime.agent_phase import get_agent_phase
            phase = get_agent_phase(root)
            phase.acquire_main(blocking=True)
            emit_ui_event(runtime_snapshot, "memory_push_started", path="background", mode="full_background")
            result = None
            try:
                pressure = compute_and_store_dynamic_context_budget(snapshot, runtime_snapshot, emit_observer=False, stabilize=False)
                stm_projection = build_stm_projection(
                    snapshot,
                    budget=int((pressure.get("budgets") or {}).get("stm") or STM_TOKEN_BUDGET),
                    wm_ingress_watermark=wm_ingress.effective_wm_ingress_watermark(runtime_snapshot),
                )
                wm_records = _build_working_memory_projection(
                    stm_projection,
                    budget=int((pressure.get("budgets") or {}).get("wm") or WM_TOKEN_BUDGET),
                )
                wm_message = _build_working_memory_message(wm_records)
                rank_3_context_messages = _rank_3_session_messages(
                    runtime_snapshot,
                    root=root,
                    max_slot_seq=revision - 1,
                )
                result = memory_push_ranking.run_memory_push(
                    stm_projection.get("messages") or [],
                    [wm_message] if wm_message else [],
                    root=root,
                    runtime_state=runtime_snapshot,
                    mode="full_background",
                    rank_3_context_messages=rank_3_context_messages,
                )
                current_revision = int(memory_push_cache.load_state(root).get("latest_revision") or -1)
                if result.get("status") == "ok" and revision >= current_revision:
                    memory_push_cache.save(root, revision=revision, focus_state="stable", mode="full_background", result=result)
                    memory_push_cache.save_state(root, stable_turn_count=0, latest_revision=revision)
            except Exception as error:
                result = {"status": "error", "error": str(error)}
            finally:
                emit_ui_event(
                    runtime_snapshot,
                    "memory_push_finished",
                    path="background",
                    mode="full_background",
                    status="error" if (result or {}).get("status") == "error" else "ok",
                    error=(result or {}).get("error") or "",
                )
                with _FULL_REFRESH_LOCK:
                    pending = _FULL_REFRESHES.get(key, {}).get("pending")
                    _FULL_REFRESHES.pop(key, None)
                try:
                    if pending:
                        schedule_memory_push_refresh(pending[0], runtime_state=pending[2], root=root)
                finally:
                    phase.release_main()

        thread = threading.Thread(target=worker, daemon=True)
        from runtime.setup_capabilities import register_thread
        register_thread(thread)
        record["thread"] = thread
        thread.start()
        return thread


def _memory_push_usage_key(runtime_state):
    trace_id = str(runtime_state.get("trace_id") or "").strip()
    if not trace_id:
        trace_id = runtime_state.setdefault("_ltm_usage_run_id", uuid.uuid4().hex)
    return f"successful_use:memory_push:{trace_id}"


def _record_memory_push_successful_uses(memory_push_result, runtime_state):
    """Record only memories that were actually appended to provider context."""
    if not isinstance(runtime_state, dict) or runtime_state.get("_memory_push_usage_recorded"):
        return
    runtime_state["_memory_push_usage_recorded"] = True
    metrics = memory_push_result.setdefault("metrics", {})
    object_ids = sorted(set(memory_push_result.get("used_object_ids") or []))
    metrics["successful_use_object_count"] = len(object_ids)
    if not object_ids:
        metrics["successful_use_status"] = "not_applicable"
        return
    try:
        result = ltm_store.record_successful_uses(object_ids, _memory_push_usage_key(runtime_state))
        metrics["successful_use_status"] = str(result.get("status") or "rejected")
        if result.get("status") not in {"committed", "already_committed"}:
            metrics["successful_use_error_type"] = "store_rejected"
    except Exception as error:
        metrics["successful_use_status"] = "error"
        metrics["successful_use_error_type"] = type(error).__name__
    try:
        context = runtime_state.get("trajectory_context")
        if (context or {}).get("enabled"):
            record_event(
                context,
                "memory_push_successful_use",
                status="ok" if metrics.get("successful_use_status") in {"committed", "already_committed"} else "error",
                summary={
                    "object_count": len(object_ids),
                    "usage_status": metrics.get("successful_use_status"),
                    "error_type": metrics.get("successful_use_error_type", ""),
                },
            )
    except Exception:
        pass


def _runtime_copy(runtime_state):
    if not isinstance(runtime_state, dict):
        return {}
    try:
        return copy.deepcopy(runtime_state)
    except Exception:
        return dict(runtime_state)


def _select_working_memory_records(stm_projection, budget=WM_TOKEN_BUDGET, root=None):
    records = _working_memory_candidate_records(stm_projection, root=root)
    selected = []
    selected_tokens = 0
    for record in reversed(records):
        rendered = _render_working_memory_record(record)
        token_count = count_text_tokens(rendered)
        if token_count > budget:
            raise RuntimeError(
                f"Context Builder WM slot exceeds budget: "
                f"slot_seq={record['slot_seq']}, tokens={token_count}, budget={budget}."
            )
        next_tokens = selected_tokens + token_count
        if next_tokens > budget:
            break
        record = dict(record)
        record["rendered"] = rendered
        record["rendered_tokens"] = token_count
        selected.append(record)
        selected_tokens = next_tokens
    selected.reverse()
    return {
        "budget": budget,
        "candidate_count": len(records),
        "selected_records": selected,
        "selected_tokens": selected_tokens,
        "skipped_count": max(0, len(records) - len(selected)),
    }


def _working_memory_candidate_records(stm_projection, root=None):
    stm_start = _stm_start_index(stm_projection)
    records = (
        event_stream.load_active_trunk_records()
        if root is None
        else event_stream.load_active_trunk_records(root)
    )
    return [
        record
        for record in records
        if int(record["slot_seq"]) < stm_start and record.get("source") != "system"
    ]


def _build_working_memory_projection(stm_projection, budget=WM_TOKEN_BUDGET, root=None):
    candidates = _working_memory_candidate_records(stm_projection, root=root)
    focused = focus.build_working_memory_projection(
        candidates,
        _render_working_memory_record,
        budget,
    )
    if focused is not None:
        return focused

    selected = _select_working_memory_records(stm_projection, budget=budget, root=root)
    selected_records = list(selected["selected_records"])
    content = _legacy_working_memory_content(selected_records)
    while selected_records and count_text_tokens(content) > budget:
        if len(selected_records) == 1:
            required_tokens = count_text_tokens(content)
            raise RuntimeError(
                "Context Builder WM slot exceeds budget: "
                f"slot_seq={selected_records[0]['slot_seq']}, tokens={required_tokens}, budget={budget}."
            )
        selected_records.pop(0)
        content = _legacy_working_memory_content(selected_records)
    selected_tokens = count_text_tokens(content) if content else 0
    return {
        **selected,
        "selected_records": selected_records,
        "content": content,
        "selected_tokens": selected_tokens,
        "projection_mode": "legacy",
        "selected_slot_seqs": [
            int(record["slot_seq"]) for record in selected_records
        ],
        "metadata_tokens": 0,
        "active_detail_tokens": selected_tokens,
        "unowned_detail_tokens": 0,
        "active_detail_count": len(selected_records),
        "unowned_detail_count": 0,
        "inactive_summary_count": 0,
        "resolved_title_count": 0,
        "resident_slot_count": len(candidates),
        "resident_raw_token_count": sum(
            int(record.get("active_token_count") or 0) for record in candidates
        ),
        "skipped_count": max(0, len(candidates) - len(selected_records)),
    }


def _stm_start_index(stm_projection):
    selected = stm_projection.get("selected_blocks") or []
    if not selected:
        return int(stm_projection.get("wm_ingress_watermark", -1)) + 1
    return min(int(block["start_index"]) for block in selected)


def _build_working_memory_message(projection):
    content = (projection or {}).get("content")
    if not content:
        return None
    return {
        "role": "user",
        "content": content,
    }


def _legacy_working_memory_content(records):
    if not records:
        return None
    lines = [
        "<working-memory>",
        "Older active working-memory timeline. Some entries are summaries; raw source remains traceable locally.",
    ]
    for record in records:
        lines.append(record["rendered"])
    lines.append("</working-memory>")
    return "\n".join(lines)


def _render_working_memory_record(record):
    slot_seq = int(record["slot_seq"])
    kind = record.get("active_kind", "")
    content = record.get("content")
    if kind == "sum":
        source_role = _source_label((content or {}).get("source_role") or record.get("source"))
        summary = _single_line((content or {}).get("summary", ""))
        return f"[{slot_seq}] {source_role} summary: {summary}"

    if isinstance(content, dict):
        source_role = _source_label(content.get("role") or record.get("source"))
        text = _single_line(_message_content_text(content))
        return f"[{slot_seq}] {source_role}: {text}"

    source_role = _source_label(record.get("source"))
    return f"[{slot_seq}] {source_role}: {_single_line(content)}"


def _message_content_text(message):
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _source_label(value):
    value = str(value or "").strip().lower()
    if value == "assistant":
        return "sema"
    if value in {"user", "sema", "tool", "file", "system"}:
        return value
    return value or "unknown"


def _single_line(value):
    return " ".join(str(value or "").split())


def _system_messages(messages):
    return [message for message in messages if message.get("role") == "system"]


def _non_system_messages(messages):
    return [message for message in messages if message.get("role") != "system"]


def _record_range(records):
    if not records:
        return ""
    return f"{records[0]['slot_seq']}..{records[-1]['slot_seq']}"
