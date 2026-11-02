"""LTM Maintainer ReAct runner."""

import json
import hashlib
import time
from contextvars import ContextVar

from bridge.ui_events import emit_ui_event
from model.provider import call_chat_completion, parse_chat_completion_message
from shared.prompt_loader import load_prompt
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

from . import formation, ltm_store, maintenance_digest_cache, maintenance_intent_compiler, maintenance_observation
from . import memory_retrieval
from ..token_counter import count_text_tokens, truncate_text_tokens

_RETRIEVAL_STATE = ContextVar("maintainer_retrieval_state", default=None)


def _run_forgetting_cleanup(runtime_state, trajectory_context, run_id, root):
    emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id,
                  content="Refreshing Strength and applying logical deletion...", stage="completion")
    result = ltm_store.finish_forgetting_batch(run_id, root)
    from . import storage_management
    if isinstance(runtime_state, dict):
        runtime_state["ltm_storage_status"] = storage_management.status(root)
    summary = {key: value for key, value in result.items() if key not in {"deleted_object_ids", "provenance_ranges"}}
    record_event(trajectory_context, "maintainer.forgetting", status="completed", summary=summary)
    emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id,
                  content=f"Strength refreshed: {result['refreshed_count']}; logically deleted: {result['deleted_count']}.", stage="completion")
    return result


MAINTAINER_TIMEOUT_SECONDS = 120
MAX_MAINTAINER_MODEL_RETRIES = 2
MAX_CONSECUTIVE_PLAN_REJECTIONS = 5
MAX_CONSECUTIVE_TOOL_ARGUMENT_REJECTIONS = 5
MAX_CONSECUTIVE_STAGE_PROTOCOL_VIOLATIONS = 3
STAGE_SUMMARY_MAX_TOKENS = 500
STAGE_SUMMARY_OVERFLOW_TIER_ONE_REWRITES = 2
STAGE_SUMMARY_OVERFLOW_TIER_TWO_REWRITES = 4
MAINTENANCE_STAGES = ("recent_review", "global_duplicate_review", "abstraction", "episode_review", "completion")
HALUMEM_SESSION_STAGES = ("recent_review", "episode_review", "completion")
REPEATED_OBSERVATION_NOTICE = (
    "Runtime detected that you executed the same observation tool calls and received "
    "the same results for 3 consecutive rounds. Re-examine your current behavior, "
    "assess why it is not making progress, and adjust your next action."
)
RECOVERABLE_MAINTAINER_MODEL_ERROR_MARKERS = (
    "API request timed out",
    "request timed out",
    "Could not reach",
    "Could not complete",
    "API error 429",
    "engine_overloaded_error",
    "rate_limit",
    "IncompleteRead",
    "RemoteDisconnected",
    "ConnectionResetError",
    "WinError 10054",
    "UNEXPECTED_EOF_WHILE_READING",
)
INFRASTRUCTURE_PLAN_REJECTION_CODES = {"vector_sync"}
MAINTAINER_TOOL_SCHEMAS = maintenance_observation.maintainer_observation_tool_schemas() + [
    {"type": "function", "function": {
        "name": "retrieve_memory",
        "description": (
            "Delegate a bounded semantic memory-retrieval target to an independent Retriever Agent. "
            "It is more powerful than get_object for discovery across expressions or languages, "
            "related objects, relationships, and source evidence. "
            "Use query for a new target, continuation_id with message for a specific follow-up, "
            "and close to abandon the retrieval task. "
            "Results provide memory objects and evidence; you decide maintenance actions and "
            "results do not guarantee exhaustive coverage. Runtime closes Sessions before semantic writes."
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "continuation_id": {"type": "string"},
            "message": {"type": "string"}, "close": {"type": "boolean"}
        }, "additionalProperties": False}
    }},
    {"type":"function", "function":{"name":"finish_maintenance",
     "description":"Explicitly request completion after reviewing all slots through coverage_target_slot_seq. Must be the only tool call in this response. Runtime checks coverage and mechanical health.",
     "parameters":{"type":"object","properties":{},"additionalProperties":False}}},
    {"type":"function", "function":{"name":"complete_maintenance_stage",
     "description":"Report the current semantic stage as complete. Must be the only tool call in this response.",
     "parameters":{"type":"object","properties":{
         "stage":{"type":"string","enum":["recent_review","global_duplicate_review","abstraction","episode_review"]},
         "status":{"type":"string","enum":["completed","no_applicable_work"]},
         "stage_summary":{"type":"string"}},
         "required":["stage","status","stage_summary"],"additionalProperties":False}}},
    {
        "type": "function",
        "function": {
            "name": "submit_maintenance_plan",
            "description": "Submit a MaintainerActionPlan for deterministic compilation and atomic Store commit.",
            "parameters": maintenance_intent_compiler.maintenance_action_plan_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_maintenance_coverage",
            "description": "Mark a continuous Event Stream slot range as semantically covered by this run.",
            "parameters": {
                "type": "object",
                "properties": {"end_slot_seq": {"type": "integer"}},
                "required": ["end_slot_seq"],
                "additionalProperties": False,
            },
        },
    },
]


def _json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _maintainer_payload_diagnostics(messages):
    tool_result_chars = sum(
        len(str(message.get("content") or ""))
        for message in messages
        if message.get("role") == "tool"
    )
    content_chars = sum(len(str(message.get("content") or "")) for message in messages)
    return {
        "message_count": len(messages),
        "tool_message_count": sum(1 for message in messages if message.get("role") == "tool"),
        "tool_result_chars": tool_result_chars,
        "content_chars": content_chars,
        "tool_schema_count": len(MAINTAINER_TOOL_SCHEMAS),
        "approx_payload_chars": len(_json({"messages": messages, "tools": MAINTAINER_TOOL_SCHEMAS})),
    }


def _is_recoverable_maintainer_model_error(error):
    error_text = str(error)
    return any(marker in error_text for marker in RECOVERABLE_MAINTAINER_MODEL_ERROR_MARKERS)


def _call_maintainer_model(messages, runtime_state, run_id):
    last_error = None
    _sanitize_maintainer_messages(messages)
    for retry_index in range(MAX_MAINTAINER_MODEL_RETRIES + 1):
        try:
            parent = (runtime_state or {}).get("_ltm_maintenance_trajectory_context")
            observer = provider_exchange_observer(
                parent,
                purpose="ltm maintainer",
                agent_type="ltm_maintainer",
                metadata={"retry": retry_index, "run_id": run_id},
            )
            return call_chat_completion(
                messages,
                tools=MAINTAINER_TOOL_SCHEMAS,
                temperature=0,
                timeout_seconds=MAINTAINER_TIMEOUT_SECONDS,
                purpose="ltm maintainer",
                exchange_observer=observer,
                exchange_metadata={"retry": retry_index, "run_id": run_id},
            )
        except Exception as error:
            if not _is_recoverable_maintainer_model_error(error):
                raise
            last_error = error
            record_event(
                (runtime_state or {}).get("_ltm_maintenance_trajectory_context"),
                "model.retry",
                status="failed",
                summary={"retry": retry_index, "error": str(error)},
            )
            diagnostics = _maintainer_payload_diagnostics(messages)
            emit_ui_event(
                runtime_state,
                "ltm_maintenance_model_error",
                run_id=run_id,
                retry=retry_index,
                max_retries=MAX_MAINTAINER_MODEL_RETRIES,
                error=str(error),
                diagnostics=diagnostics,
            )
            if retry_index >= MAX_MAINTAINER_MODEL_RETRIES:
                raise
    raise last_error


def _tool_call_arguments(tool_call):
    function = tool_call.get("function") or {}
    try:
        args = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Invalid maintainer tool arguments JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno}, character {error.pos})"
        ) from error
    if not isinstance(args, dict):
        raise RuntimeError("Maintainer tool arguments must be a JSON object.")
    return function.get("name") or "", args


def _tool_argument_error_details(error):
    details = {"code": "invalid_tool_arguments_json", "message": str(error)}
    cause = error.__cause__
    if isinstance(cause, json.JSONDecodeError):
        start, end = max(0, cause.pos - 80), min(len(cause.doc), cause.pos + 80)
        details.update({
            "line": cause.lineno, "column": cause.colno, "position": cause.pos,
            "snippet": cause.doc[start:end], "snippet_start": start,
            "snippet_error_offset": cause.pos - start,
            "hint": "Line and column are 1-based; position and snippet offsets are 0-based Unicode character offsets. "
                    "The position is where parsing failed, not necessarily where the mistake began. "
                    "Inspect the surrounding syntax and regenerate valid JSON arguments matching the tool schema. "
                    "Do not add an arguments wrapper. No automatic repair was performed.",
        })
    return details


def _is_empty_tool_call(tool_call):
    try:
        return _tool_call_arguments(tool_call)[1] == {}
    except RuntimeError:
        return False


def _stage_prompt(stage):
    prompt = load_prompt("ltm_maintainer")
    marker = f"## Stage: {stage}"
    start = prompt.find(marker)
    if start < 0:
        return prompt
    end = prompt.find("\n## Stage:", start + len(marker))
    section = prompt[start:] if end < 0 else prompt[start:end]
    common_end = prompt.find("## Stage:")
    common = prompt[:common_end] if common_end >= 0 else prompt
    return (common + section).strip()


def _initial_messages(run, root, stage, previous_stage_summary=None, references=None, summary_material=None, summary_max_tokens=None):
    state = ltm_store.get_maintenance_runtime_state(root)
    health = maintenance_observation.get_overall_health({}, root)
    digest_max_tokens = int(summary_max_tokens or STAGE_SUMMARY_MAX_TOKENS)
    prompt = _stage_prompt(stage) + (
        f"\n\nThe stage_summary MUST NOT exceed {digest_max_tokens} tokens. "
        "This is a hard Runtime contract. If necessary, write less.\n"
    )
    prior = maintenance_digest_cache.load_stage_summary(stage, root)
    return [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": _json(
                {
                    "maintenance_run": {
                        "run_id": run["run_id"],
                        "start_status": run["status"],
                        "runtime_state": state,
                        "current_stage": stage,
                        "previous_stage_summary": previous_stage_summary,
                        "previous_completed_stage_summary": prior,
                        "runtime_references": references or {},
                        "stage_summary_material": summary_material if stage == "completion" else None,
                    },
                    "overall_health": health,
                }
            ),
        },
    ]


def _assistant_history_message(assistant, tool_calls=None):
    message = {"role": "assistant", "content": assistant.get("content") or ""}



    if tool_calls:
        message["tool_calls"] = tool_calls
    if "reasoning_content" in assistant:
        message["reasoning_content"] = assistant.get("reasoning_content") or ""
    return message


def _append_rejected_tool_results(messages, tool_calls, reason):
    """Keep provider history valid when a tool-call turn is rejected by Runtime."""
    for index, tool_call in enumerate(tool_calls or []):
        call_id = tool_call.get("id") or f"rejected-tool-{index}-{time.time_ns()}"
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": _json({"status": "rejected", "error": reason}),
        })


def _sanitize_maintainer_messages(messages):
    """Remove invalid empty tool-call arrays from replayed history.

    The normal history builder already omits them. This second guard also
    protects retries and history assembled by older paths before it is sent
    to a strict provider.
    """
    for message in messages:
        if isinstance(message, dict) and message.get("tool_calls") == []:
            message.pop("tool_calls", None)
    return messages


def _emit_assistant_visibility(runtime_state, run_id, assistant):
    reasoning = assistant.get("reasoning_content") or ""
    if str(reasoning).strip():
        emit_ui_event(
            runtime_state,
            "ltm_maintenance_reasoning_available",
            run_id=run_id,
            content=reasoning,
        )
    content = assistant.get("content") or ""
    if str(content).strip():
        emit_ui_event(
            runtime_state,
            "ltm_maintenance_message_delta",
            run_id=run_id,
            content=content,
        )


def _empty_counters():
    return {
        "plan_commit_count": 0,
        "created_object_count": 0,
        "created_link_count": 0,
        "deactivated_link_count": 0,
        "updated_title_count": 0,
        "updated_summary_count": 0,
        "supersedes_count": 0,
        "contradiction_count": 0,
    }


def _merge_commit_counters(counters, compiled, committed):
    if committed.get("status") not in {"committed", "already_committed"}:
        return
    counters["plan_commit_count"] += 1
    counters["created_object_count"] += len(committed.get("object_ids") or [])
    counters["created_link_count"] += len(committed.get("link_ids") or [])
    counters["deactivated_link_count"] += len(committed.get("deactivated_link_ids") or [])
    counters["updated_title_count"] += sum(1 for update in compiled.get("updates") or [] if update.get("field") == "title")
    counters["updated_summary_count"] += sum(1 for update in compiled.get("updates") or [] if update.get("field") == "summary")
    counters["supersedes_count"] += sum(1 for link in compiled.get("links") or [] if link.get("relation_type") == "supersedes")
    counters["contradiction_count"] += sum(1 for link in compiled.get("links") or [] if link.get("relation_type") == "contradicts")


def _health_passes(health):
    if health.get("status") not in {"ok", "not_initialized"}:
        return False
    derived = health.get("derived") or {}
    links = health.get("links") or {}
    fts = health.get("fts") or {}
    vector = health.get("vector") or {}
    event_stream = health.get("event_stream") or {}
    checks = [
        int(derived.get("missing", 0)) == 0,
        int(derived.get("stale_formula", 0)) == 0,
        int(links.get("dangling_active", 0)) == 0,
        int(fts.get("missing_objects", 0)) == 0,
        int(fts.get("unknown_objects", 0)) == 0,
        int(vector.get("missing_objects", 0)) == 0,
        int(vector.get("unknown_objects", 0)) == 0,
        event_stream.get("status") in {None, "ok", "not_initialized"},
    ]
    return all(checks)


def _source_range_error(compiled, maintainable_frontier):
    for index, link in enumerate(compiled.get("links") or []):
        if link.get("relation_type") != "sourced_from":
            continue
        target = link.get("target") or {}
        slots = target.get("event_slots") if isinstance(target, dict) else None
        if not isinstance(slots, dict) or slots.get("start") is None:
            continue
        start = int(slots["start"])
        end = int(slots.get("end", start))
        if end > int(maintainable_frontier):
            return {
                "status": "rejected",
                "errors": [{
                    "code": "maintenance_source_not_eligible",
                    "path": f"links[{index}].target.event_slots",
                    "message": (
                        f"Event Stream source range {start}-{end} exceeds the Maintainer "
                        f"maintainable frontier {maintainable_frontier}."
                    ),
                    "maintainable_frontier": int(maintainable_frontier),
                    "requested_start_slot_seq": start,
                    "requested_end_slot_seq": end,
                }],
                "maintainable_frontier": int(maintainable_frontier),
            }
    return None


def _execute_tool(name, args, run_id, counters, root, coverage_target=None, maintainable_frontier=None):
    if name == "retrieve_memory":
        try:
            return memory_retrieval.run_retrieve_memory(
                args, runtime_state=_RETRIEVAL_STATE.get(),
                caller_profile=memory_retrieval.MAINTAINER_CALLER, root=root,
            )
        except Exception as error:
            return {"kind": "technical", "message": str(error), "error": {"code": type(error).__name__, "message": str(error)}}
    if name in maintenance_observation.OBSERVATION_TOOLS:
        return maintenance_observation.run_maintenance_observation_tool(name, args, root)
    if name == "submit_maintenance_plan":
        state = ltm_store.get_maintenance_runtime_state(root)
        plan_index = int(state.get("next_plan_index") or 0)
        compiled = maintenance_intent_compiler.compile_maintenance_action_plan(args, run_id, plan_index, root)
        if compiled.get("status") != "compiled":
            return compiled
        if maintainable_frontier is not None:
            source_error = _source_range_error(compiled, maintainable_frontier)
            if source_error is not None:
                return source_error
        if _RETRIEVAL_STATE.get() is not None:
            memory_retrieval.close_retrieval_session(_RETRIEVAL_STATE.get())
        committed = ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], root)
        if committed.get("status") in {"committed", "already_committed"}:
            _merge_commit_counters(counters, compiled, committed)
            ltm_store.checkpoint_maintenance_run(
                run_id,
                plan_index + 1,
                last_committed_plan_index=plan_index,
                root=root,
            )
        return committed
    if name == "mark_maintenance_coverage":
        if "end_slot_seq" not in args:
            return {"status": "rejected", "error": {"code": "end_slot_seq", "message": "end_slot_seq is required."}}
        end_slot_seq = int(args["end_slot_seq"])


        if maintainable_frontier is not None and end_slot_seq > int(maintainable_frontier):
            state = ltm_store.get_maintenance_runtime_state(root)
            return {
                "status": "rejected",
                "error": {
                    "code": "coverage_exceeds_maintainable_frontier",
                    "path": "end_slot_seq",
                    "message": (
                        f"Coverage end slot {end_slot_seq} exceeds the fixed maintainable "
                        f"frontier {maintainable_frontier} for this invocation."
                    ),
                    "maintainable_frontier": maintainable_frontier,
                },
                "coverage_cursor": state.get("maintenance_coverage_cursor"),
                "coverage_target_slot_seq": (
                    int(coverage_target)
                    if coverage_target is not None
                    else max(int(state.get("maintenance_coverage_cursor") or -1), int(maintainable_frontier))
                ),
                "maintainable_frontier": maintainable_frontier,
                "requested_end_slot_seq": end_slot_seq,
            }
        result = ltm_store.advance_maintenance_coverage(run_id, end_slot_seq, root)
        state = ltm_store.get_maintenance_runtime_state(root)
        ltm_store.checkpoint_maintenance_run(
            run_id,
            int(state.get("next_plan_index") or 0),
            last_checkpoint_slot_seq=int(result["maintenance_coverage_cursor"]),
            root=root,
        )
        return result
    return {"status": "rejected", "error": {"code": "unknown_tool", "message": name}}


def _first_error_message(result):
    errors = result.get("errors") if isinstance(result, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        code = first.get("code") or "error"
        message = first.get("message") or str(first)
        return f"{code}: {message}"
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or "error"
        message = error.get("message") or str(error)
        return f"{code}: {message}"
    return str(result)


def _first_error_code(result):
    errors = result.get("errors") if isinstance(result, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("code") or ""
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        return error.get("code") or ""
    return ""


def _is_infrastructure_plan_rejection(result):
    return result.get("status") == "rejected" and _first_error_code(result) in INFRASTRUCTURE_PLAN_REJECTION_CODES


def _run_counters(run_id, fallback_counters, root=None):
    try:
        return ltm_store.aggregate_maintenance_run_counters(run_id, root)
    except Exception:
        return dict(fallback_counters)


def _fail_run(run_id, counters, reason, root=None, runtime_state=None, slot_seq=None, final_check_status="not_run"):
    maintenance_digest_cache.clear_pending_digest(root)
    if slot_seq is None:
        state = ltm_store.get_maintenance_runtime_state(root)
        slot_seq = state.get("maintenance_coverage_cursor", -1)
    try:
        ltm_store.record_maintenance_failure(run_id, slot_seq, reason, root)
    except Exception:
        pass
    failed_state = ltm_store.get_maintenance_runtime_state(root)
    failed = {
        "status": "failed",
        "run_id": run_id,
        "state": failed_state,
        "failure_reason": reason,
        "final_check_status": final_check_status,
        **_run_counters(run_id, counters, root),
    }
    emit_ui_event(runtime_state, "ltm_maintenance_failed", **failed)
    fail_trajectory(
        (runtime_state or {}).get("_ltm_maintenance_trajectory_context"),
        RuntimeError(reason),
        metadata={
            "run_id": run_id,
            "coverage_cursor": failed_state.get("maintenance_coverage_cursor"),
            "maintainable_frontier": failed_state.get("maintainable_frontier"),
            "coverage_target_slot_seq": failed_state.get("coverage_target_slot_seq"),
        },
    )
    return failed


def _digest_report_fields(digest_result):
    status = (digest_result or {}).get("status") or "missing"
    return {
        "semantic_digest_status": status if status in {"written", "missing", "failed"} else "failed",
        "semantic_digest_id": (digest_result or {}).get("digest_id"),
        "semantic_digest_token_count": int((digest_result or {}).get("token_count") or 0),
    }


def _report_from_counters(counters, state, final_check_status="passed", failure=None, digest_result=None,
                          maintainable_frontier=None, coverage_target=None):
    report = dict(counters)
    start = int(state.get("run_start_cursor") if state.get("run_start_cursor") is not None else -1)
    end = int(state.get("maintenance_coverage_cursor", start))
    report.update(
        {
            "start_cursor": start,
            "end_cursor": end,
            "coverage_status": "completed" if failure is None else "failed",
            "processed_slot_count": max(0, end - start),
            "final_check_status": final_check_status,
            "maintainable_frontier": (
                state.get("maintainable_frontier") if maintainable_frontier is None else maintainable_frontier
            ),
            "coverage_target_slot_seq": (
                state.get("coverage_target_slot_seq") if coverage_target is None else coverage_target
            ),
            **_digest_report_fields(digest_result),
        }
    )
    if failure:
        report["failure_slot_seq"] = failure.get("slot_seq")
        report["failure_reason"] = failure.get("reason")
    return report


def format_ltm_maintenance_result(result):
    state = result.get("state") or {}
    lines = [
        "LTM Maintenance:",
        f"status: {result.get('status')}",
        f"run_id: {result.get('run_id', '')}",
        f"coverage_cursor: {state.get('maintenance_coverage_cursor')}",
        f"maintainable_frontier: {result.get('maintainable_frontier', state.get('maintainable_frontier', ''))}",
        f"coverage_target: {result.get('coverage_target_slot_seq', state.get('coverage_target_slot_seq', ''))}",
        f"final_check_status: {result.get('final_check_status', '')}",
        f"plan_commit_count: {result.get('plan_commit_count', 0)}",
        f"created_objects: {result.get('created_object_count', 0)}",
        f"created_links: {result.get('created_link_count', 0)}",
        f"deactivated_links: {result.get('deactivated_link_count', 0)}",
        f"updated_titles: {result.get('updated_title_count', 0)}",
        f"updated_summaries: {result.get('updated_summary_count', 0)}",
        f"maintenance_summary_status: {result.get('maintenance_summary_status', result.get('semantic_digest_status', ''))}",
    ]
    if result.get("maintenance_summary_id", result.get("semantic_digest_id")):
        lines.append(f"maintenance_summary_id: {result.get('maintenance_summary_id', result.get('semantic_digest_id'))}")
    if result.get("maintenance_summary_token_count", result.get("semantic_digest_token_count")):
        lines.append(f"maintenance_summary_token_count: {result.get('maintenance_summary_token_count', result.get('semantic_digest_token_count'))}")
    if result.get("failure_reason"):
        lines.append(f"failure_reason: {result['failure_reason']}")
    return "\n".join(lines)


from .storage_gate import memory_task


@memory_task
def run_ltm_maintenance(
    runtime_state=None,
    root=None,
    force=False,
    stages=None,
    stage_summary_max_tokens=None,
    start_stage_index=0,
    initial_stage_summaries=None,
    progress_callback=None,
    historical_slot_range=None,
):
    from runtime.setup_capabilities import require_ltm
    from runtime.agent_phase import maintainer_phase
    require_ltm()
    with maintainer_phase(root):
        retrieval_state = {}
        token = _RETRIEVAL_STATE.set(retrieval_state)
        try:
            kwargs = {}
            if stages is not None:
                kwargs["stages"] = stages
            if stage_summary_max_tokens is not None:
                kwargs["stage_summary_max_tokens"] = stage_summary_max_tokens
            if start_stage_index:
                kwargs["start_stage_index"] = int(start_stage_index)
            if initial_stage_summaries:
                kwargs["initial_stage_summaries"] = dict(initial_stage_summaries)
            if progress_callback is not None:
                kwargs["progress_callback"] = progress_callback
            if historical_slot_range is not None:
                kwargs["historical_slot_range"] = dict(historical_slot_range)
            return _run_ltm_maintenance(runtime_state, root, force, **kwargs)
        finally:
            try:
                memory_retrieval.close_retrieval_session(retrieval_state)
            finally:
                _RETRIEVAL_STATE.reset(token)


def _collect_runtime_references(value, references):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"object_id", "link_id"} and isinstance(item, str):
                references.setdefault("object_ids" if key == "object_id" else "link_ids", set()).add(item)
            if key in {"slot_seq", "start_slot_seq", "end_slot_seq", "maintenance_coverage_cursor"}:
                try:
                    references.setdefault("slot_seqs", set()).add(int(item))
                except (TypeError, ValueError):
                    pass
            _collect_runtime_references(item, references)
    elif isinstance(value, list):
        for item in value:
            _collect_runtime_references(item, references)


def _reference_payload(references):
    return {key: sorted(values) for key, values in references.items() if values}


def _stage_has_evidence(stage, tool_names):
    if stage == "recent_review":
        return bool(set(tool_names) & {"read_event_slots", "get_object", "get_links", "traverse_objects"})
    if stage == "global_duplicate_review":
        return bool(set(tool_names) & {"retrieve_memory", "get_object", "get_links", "traverse_objects"})
    if stage == "abstraction":
        return bool(set(tool_names) & {"get_object", "get_links", "traverse_objects", "retrieve_memory"})
    if stage == "episode_review":
        return bool(set(tool_names) & {"read_event_slots", "get_object", "get_links", "traverse_objects", "retrieve_memory"})
    return True


def _summary_result_fields(summary_result):
    status = (summary_result or {}).get("status") or "missing"
    status = status if status in {"written", "missing", "failed"} else "failed"
    return {
        "maintenance_summary_status": status,
        "maintenance_summary_id": (summary_result or {}).get("digest_id"),
        "maintenance_summary_token_count": int((summary_result or {}).get("token_count") or 0),

        "semantic_digest_status": status,
        "semantic_digest_id": (summary_result or {}).get("digest_id"),
        "semantic_digest_token_count": int((summary_result or {}).get("token_count") or 0),
    }


def _enqueue_maintenance_wake(runtime_state, run_id, summary, summary_result):
    callback = (runtime_state or {}).get("wake_callback")
    if not callable(callback):
        return False
    payload = {
        "run_id": run_id,
        "status": "completed",
        "maintenance_summary": summary,
        "instruction": "你刚从一次类似睡眠的长期记忆维护中醒来。下面是这次梦中保留下来的内容，请用简短、自然的梦境回忆告诉用户。",
        "maintenance_summary_status": (summary_result or {}).get("status", "missing"),
        "maintenance_summary_id": (summary_result or {}).get("digest_id"),
    }
    try:
        return bool(callback(
            "maintainer", "maintenance_completed", payload,
            processing_class="tool_result_like",
            wake_id=f"wake_maintenance_{run_id}",
        ))
    except Exception as error:
        record_event(
            (runtime_state or {}).get("_ltm_maintenance_trajectory_context"),
            "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "error": str(error)},
        )
        return False


def _staged_tool_result_event(runtime_state, trajectory_context, run_id, tool, item_id, args, result, span):
    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    display_status = "error" if isinstance(result, dict) and result.get("kind") == "technical" else status
    event_status = "failed" if display_status in {"failed", "error"} else "rejected" if display_status == "rejected" else "completed"
    record_event(
        span,
        "tool.completed" if event_status == "completed" else "validation.rejected" if event_status == "rejected" else "tool.failed",
        status=event_status,
        summary={"tool": tool, "result_status": status},
        artifacts={"tool_arguments": args, "tool_result": result},
    )
    if event_status == "failed":
        fail_span(span, RuntimeError(str(result)))
    else:
        complete_span(span, metadata={"rejected": event_status == "rejected"})
    event_type = (
        "ltm_maintenance_plan_committed" if tool == "submit_maintenance_plan" and status in {"committed", "already_committed"}
        else "ltm_maintenance_coverage_advanced" if tool == "mark_maintenance_coverage" and status == "advanced"
        else "ltm_maintenance_tool_finished"
    )
    emit_ui_event(runtime_state, event_type, run_id=run_id, tool=tool, status=display_status, item_id=item_id, result=result)
    return {"role": "tool", "tool_call_id": item_id, "content": _json(result)}


def _run_ltm_maintenance(
    runtime_state=None,
    root=None,
    force=False,
    stages=None,
    stage_summary_max_tokens=None,
    start_stage_index=0,
    initial_stage_summaries=None,
    progress_callback=None,
    historical_slot_range=None,
):
    del force
    selected_stages = tuple(stages or MAINTENANCE_STAGES)
    summary_max_tokens = int(stage_summary_max_tokens or STAGE_SUMMARY_MAX_TOKENS)
    if not selected_stages or selected_stages[-1] != "completion":
        raise ValueError("Maintainer stages must be non-empty and end with completion.")
    if any(stage not in MAINTENANCE_STAGES for stage in selected_stages):
        raise ValueError("Maintainer stages contain an unknown stage.")
    if len(set(selected_stages)) != len(selected_stages):
        raise ValueError("Maintainer stages must not contain duplicates.")
    start_stage_index = int(start_stage_index or 0)
    if not 0 <= start_stage_index <= len(selected_stages):
        raise ValueError("start_stage_index is outside the selected maintenance stages.")
    run = ltm_store.start_or_resume_maintenance_run(root)
    run_id = run["run_id"]
    state_at_start = ltm_store.get_maintenance_runtime_state(root)
    if historical_slot_range is None:
        maintainable_frontier = formation.maintainable_slot_frontier(root=root)
    else:
        maintainable_frontier = formation.maintainable_slot_frontier(
            root=root, historical_slot_range=historical_slot_range
        )
    coverage_target = max(int(state_at_start.get("maintenance_coverage_cursor") or -1), maintainable_frontier)
    caller = (runtime_state or {}).get("trajectory_context")
    trajectory_context = start_trajectory("ltm_maintainer", run_id, caller=caller, root=root)
    if isinstance(runtime_state, dict):
        runtime_state["_ltm_maintenance_trajectory_context"] = trajectory_context
    record_event(trajectory_context, "process.invoked", status="running", summary={
        "run_id": run_id, "start_status": run.get("status"),
        "maintainable_frontier": maintainable_frontier,
        "coverage_target_slot_seq": coverage_target,
        "historical_slot_range": historical_slot_range,
    })
    counters = _empty_counters()
    references = {"object_ids": set(), "link_ids": set(), "slot_seqs": set()}
    stage_summaries = dict(initial_stage_summaries or {})
    previous_stage_summary = None
    if stage_summaries:
        previous_stage_summary = stage_summaries.get(selected_stages[start_stage_index - 1]) if start_stage_index else None
    final_check_status = "not_run"
    emit_ui_event(
        runtime_state,
        "ltm_maintenance_started",
        run_id=run_id,
        start_status=run["status"],
        maintainable_frontier=maintainable_frontier,
        coverage_target_slot_seq=coverage_target,
        historical_slot_range=historical_slot_range,
    )
    try:
        for stage_index, stage in enumerate(selected_stages):
            if stage_index < start_stage_index:
                continue
            if progress_callback is not None:
                progress_callback({"stage": stage, "stage_index": stage_index, "status": "started"})
            emit_ui_event(runtime_state, "ltm_maintenance_stage_started", run_id=run_id, stage=stage, stage_index=stage_index)
            record_event(trajectory_context, "maintainer.stage_started", status="running", summary={"stage": stage, "stage_index": stage_index})
            messages = _initial_messages(
                run, root, stage, previous_stage_summary, _reference_payload(references),
                maintenance_digest_cache.build_maintenance_summary(stage_summaries),
                summary_max_tokens=summary_max_tokens,
            )
            messages.append({"role": "user", "content": _json({
                "current_stage": stage,
                "maintainable_frontier": maintainable_frontier,
                "coverage_target_slot_seq": coverage_target,
                "newer_slots_are_reference_only": historical_slot_range is None,
                "historical_session_finalization": historical_slot_range is not None,
                "stage_instruction": "Complete this stage with complete_maintenance_stage alone." if stage != "completion" else "Complete coverage, then call finish_maintenance alone.",
            })})
            model_round = 0
            protocol_violations = 0
            tool_argument_rejections = 0
            stage_tool_names = []
            observation_signature = None
            observation_rounds = []
            stage_summary = None
            stage_summary_overflow_attempts = 0
            while True:
                model_round += 1
                emit_ui_event(runtime_state, "ltm_maintenance_model_turn", run_id=run_id, stage=stage, stage_index=stage_index)
                data = _call_maintainer_model(messages, runtime_state, run_id)
                assistant = parse_chat_completion_message(data)
                _emit_assistant_visibility(runtime_state, run_id, assistant)
                tool_calls = assistant.get("tool_calls") or []
                finish_calls = [call for call in tool_calls if (call.get("function") or {}).get("name") == "finish_maintenance"]
                stage_calls = [call for call in tool_calls if (call.get("function") or {}).get("name") == "complete_maintenance_stage"]
                protocol_error = None
                if assistant.get("finish_reason") not in {"stop", "tool_calls"}:
                    protocol_error = "Response not accepted: unsupported or missing finish_reason. No tools were executed."
                elif not tool_calls:
                    protocol_error = "No tool action received. Continue the current stage and report it explicitly when ready."
                elif (stage != "completion" and finish_calls) or (stage == "completion" and stage_calls):
                    protocol_error = (
                        "Semantic stages require complete_maintenance_stage alone."
                        if stage != "completion" else "The completion stage requires finish_maintenance alone."
                    )
                elif finish_calls and (len(tool_calls) != 1 or not _is_empty_tool_call(finish_calls[0])):
                    protocol_error = "finish_maintenance requires empty arguments and must be the only tool call. No tools were executed."
                elif stage_calls and len(tool_calls) != 1:
                    protocol_error = "complete_maintenance_stage must be the only tool call. No tools were executed."
                if protocol_error:
                    protocol_violations += 1
                    details = {"message": protocol_error, "stage": stage, "finish_reason": assistant.get("finish_reason"),
                               "consecutive_violations": protocol_violations, "model_round": model_round}
                    record_event(trajectory_context, "maintainer.completion_protocol_rejected", status="rejected", summary=details)
                    if protocol_violations >= MAX_CONSECUTIVE_STAGE_PROTOCOL_VIOLATIONS:
                        return _fail_run(run_id, counters, "maintenance_stage_protocol: " + protocol_error, root=root, runtime_state=runtime_state, final_check_status=final_check_status)
                    messages.append(_assistant_history_message(assistant, tool_calls))
                    _append_rejected_tool_results(messages, tool_calls, protocol_error)
                    messages.append({"role": "user", "content": _json(details)})
                    emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id, content=protocol_error, stage=stage)
                    continue

                if finish_calls:
                    finish_call = finish_calls[0]
                    finish_id = finish_call.get("id") or f"maintainer-finish-{time.time_ns()}"
                    messages.append(_assistant_history_message(assistant, tool_calls))
                    finish_span = start_span(trajectory_context, "tool", "finish_maintenance", metadata={"stage": stage})
                    record_event(finish_span, "tool.started", status="running", summary={"tool": "finish_maintenance", "stage": stage}, artifacts={"tool_arguments": {}})
                    emit_ui_event(runtime_state, "ltm_maintenance_tool_started", run_id=run_id, tool="finish_maintenance", item_id=finish_id, args={}, stage=stage)
                    state = ltm_store.get_maintenance_runtime_state(root)
                    if state["maintenance_coverage_cursor"] < coverage_target:
                        result = {"status": "rejected", "error": {"code": "coverage_incomplete", "message": "Review remaining maintainable slots and mark coverage before finishing."}, "coverage_cursor": state["maintenance_coverage_cursor"], "coverage_target_slot_seq": coverage_target, "maintainable_frontier": maintainable_frontier, "remaining_start_slot_seq": state["maintenance_coverage_cursor"] + 1}
                        messages.append({"role": "tool", "tool_call_id": finish_id, "content": _json(result)})
                        record_event(finish_span, "validation.rejected", status="rejected", summary=result, artifacts={"tool_result": result})
                        complete_span(finish_span, metadata={"rejected": True})
                        emit_ui_event(runtime_state, "ltm_maintenance_tool_finished", run_id=run_id, tool="finish_maintenance", item_id=finish_id, status="rejected", result=result, stage=stage)
                        record_event(trajectory_context, "maintainer.coverage_incomplete", status="rejected", summary=result)
                        continue
                    _run_forgetting_cleanup(runtime_state, trajectory_context, run_id, root)
                    emit_ui_event(runtime_state, "ltm_maintenance_final_check_started", run_id=run_id)
                    final_check_span = start_span(trajectory_context, "final_check", "mechanical consistency")
                    health = maintenance_observation.get_overall_health({}, root)
                    if not _health_passes(health):
                        final_check_status = "failed"
                        record_event(final_check_span, "maintainer.final_check", status="failed", summary={"passed": False}, artifacts={"health": health})
                        fail_span(final_check_span, RuntimeError("Mechanical consistency check failed."))
                        emit_ui_event(runtime_state, "ltm_maintenance_final_check_failed", run_id=run_id, health=health)
                        result = {"status": "rejected", "final_check": "failed", "overall_health": health}
                        messages.append({"role": "tool", "tool_call_id": finish_id, "content": _json(result)})
                        record_event(finish_span, "validation.rejected", status="rejected", summary=result, artifacts={"tool_result": result})
                        complete_span(finish_span, metadata={"rejected": True})
                        emit_ui_event(runtime_state, "ltm_maintenance_tool_finished", run_id=run_id, tool="finish_maintenance", item_id=finish_id, status="rejected", result=result, stage=stage)
                        continue
                    final_check_status = "passed"
                    record_event(final_check_span, "maintainer.final_check", status="completed", summary={"passed": True}, artifacts={"health": health})
                    complete_span(final_check_span)
                    digest_stages = tuple(stage for stage in selected_stages if stage != "completion")
                    summary = maintenance_digest_cache.build_maintenance_summary(stage_summaries, digest_stages)
                    summary_result = maintenance_digest_cache.persist_stage_summaries(
                        run_id,
                        stage_summaries,
                        _reference_payload(references),
                        root,
                        required_stages=digest_stages,
                        max_tokens=summary_max_tokens,
                    )
                    state = ltm_store.get_maintenance_runtime_state(root)
                    report = _report_from_counters(
                        _run_counters(run_id, counters, root), state,
                        final_check_status="passed", digest_result=summary_result,
                        maintainable_frontier=maintainable_frontier, coverage_target=coverage_target,
                    )
                    ltm_store.complete_maintenance_run(run_id, report, root)
                    final_state = ltm_store.get_maintenance_runtime_state(root)
                    result = {
                        "status": "completed",
                        "run_id": run_id,
                        "final_check_status": "passed",
                        "state": final_state,
                        "maintainable_frontier": maintainable_frontier,
                        "coverage_target_slot_seq": coverage_target,
                        "historical_slot_range": historical_slot_range,
                        "maintenance_summary": summary,
                        **_run_counters(run_id, counters, root),
                        **_summary_result_fields(summary_result),
                    }
                    finish_result = {"status": "completed", "coverage_cursor": final_state["maintenance_coverage_cursor"]}
                    messages.append({"role": "tool", "tool_call_id": finish_id, "content": _json(finish_result)})
                    record_event(finish_span, "tool.completed", status="completed", summary={"tool": "finish_maintenance"}, artifacts={"tool_result": finish_result})
                    complete_span(finish_span)
                    emit_ui_event(runtime_state, "ltm_maintenance_tool_finished", run_id=run_id, tool="finish_maintenance", item_id=finish_id, status="completed", result=finish_result, stage=stage)
                    emit_ui_event(runtime_state, "ltm_maintenance_completed", **result, storage_status=(runtime_state or {}).get("ltm_storage_status"))
                    record_event(trajectory_context, "maintenance.wake_queued", status="completed", summary={"run_id": run_id, "summary_status": summary_result.get("status")})
                    _enqueue_maintenance_wake(runtime_state, run_id, summary, summary_result)
                    complete_trajectory(trajectory_context, metadata={"run_id": run_id, "coverage_cursor": final_state.get("maintenance_coverage_cursor")})
                    return result


                if stage_calls:
                    try:
                        report_name, report_args = _tool_call_arguments(stage_calls[0])
                    except RuntimeError as error:
                        report_name, report_args = "complete_maintenance_stage", {}
                        protocol_error = str(error)
                    if protocol_error:
                        protocol_violations += 1
                        messages.append(_assistant_history_message(assistant, tool_calls))
                        _append_rejected_tool_results(messages, tool_calls, protocol_error)
                        messages.append({"role": "user", "content": _json({"error": protocol_error, "stage": stage})})
                        emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id, content=protocol_error, stage=stage)
                        if protocol_violations >= MAX_CONSECUTIVE_STAGE_PROTOCOL_VIOLATIONS:
                            return _fail_run(run_id, counters, "maintenance_stage_protocol: " + protocol_error, root=root, runtime_state=runtime_state)
                        continue
                    summary_text = str(report_args.get("stage_summary") or "").strip()
                    valid = report_name == "complete_maintenance_stage" and report_args.get("stage") == stage and report_args.get("status") in {"completed", "no_applicable_work"} and bool(summary_text)
                    if report_args.get("stage") != stage:
                        protocol_error = f"Stage report must name the current stage ({stage})."
                    if not _stage_has_evidence(stage, stage_tool_names) and not protocol_error:
                        valid = False
                        protocol_error = f"Stage {stage} requires relevant observation or retrieval evidence before it can be reported."
                    summary_token_count = count_text_tokens(summary_text)
                    summary_overflow = (
                        valid
                        and summary_token_count > summary_max_tokens
                    )
                    if not valid:
                        protocol_violations += 1
                        protocol_error = protocol_error or "Invalid complete_maintenance_stage arguments for the current stage."
                        details = {"error": protocol_error, "stage": stage, "consecutive_violations": protocol_violations}
                        record_event(trajectory_context, "maintainer.stage_rejected", status="rejected", summary=details)
                        messages.append(_assistant_history_message(assistant, tool_calls))
                        _append_rejected_tool_results(messages, tool_calls, protocol_error)
                        messages.append({"role": "user", "content": _json(details)})
                        emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id, content=protocol_error, stage=stage)
                        if protocol_violations >= MAX_CONSECUTIVE_STAGE_PROTOCOL_VIOLATIONS:
                            return _fail_run(run_id, counters, "maintenance_stage_protocol: " + protocol_error, root=root, runtime_state=runtime_state)
                        continue
                    if summary_overflow:
                        stage_summary_overflow_attempts += 1
                        if summary_token_count <= summary_max_tokens * 2:
                            max_rewrites = STAGE_SUMMARY_OVERFLOW_TIER_ONE_REWRITES
                            warning_repetitions = 1
                        else:
                            max_rewrites = STAGE_SUMMARY_OVERFLOW_TIER_TWO_REWRITES
                            warning_repetitions = 3
                        if stage_summary_overflow_attempts <= max_rewrites:
                            warning = (
                                f"stage_summary has {summary_token_count} tokens; the hard limit is "
                                f"{summary_max_tokens}. Rewrite it from scratch under {summary_max_tokens} "
                                "tokens. Do not call observation tools again."
                            )
                            notice = "\n".join([warning] * warning_repetitions)
                            details = {
                                "code": "stage_summary_overflow",
                                "error": notice,
                                "stage": stage,
                                "token_count": summary_token_count,
                                "hard_limit": summary_max_tokens,
                                "rewrite_attempt": stage_summary_overflow_attempts,
                                "max_rewrites": max_rewrites,
                            }
                            record_event(trajectory_context, "maintainer.stage_summary_overflow_warning", status="warning", summary=details)
                            messages.append(_assistant_history_message(assistant, tool_calls))
                            _append_rejected_tool_results(messages, tool_calls, notice)
                            messages.append({"role": "user", "content": _json(details)})
                            emit_ui_event(runtime_state, "ltm_maintenance_runtime_notice", run_id=run_id, content=notice, stage=stage)
                            continue
                        stage_summary = truncate_text_tokens(summary_text, summary_max_tokens)
                        summary_truncated = True
                    else:
                        stage_summary = summary_text
                        summary_truncated = False
                    stage_summaries[stage] = stage_summary
                    _collect_runtime_references(report_args, references)
                    report = {"status": "completed", "stage": stage, "stage_summary": stage_summary}
                    accepted_token_count = count_text_tokens(stage_summary)
                    record_event(trajectory_context, "maintainer.stage_completed", status="completed", summary={"stage": stage, "status": report_args.get("status"), "token_count": accepted_token_count, "summary_truncated": summary_truncated, "summary_overflow_attempts": stage_summary_overflow_attempts, "references": _reference_payload(references)})
                    emit_ui_event(runtime_state, "ltm_maintenance_stage_completed", run_id=run_id, stage=stage, stage_index=stage_index, status=report_args.get("status"), stage_summary=stage_summary, token_count=accepted_token_count, summary_truncated=summary_truncated)
                    previous_stage_summary = stage_summary
                    if progress_callback is not None:
                        progress_callback({
                            "stage": stage,
                            "stage_index": stage_index,
                            "status": "completed",
                            "stage_summary": stage_summary,
                            "token_count": accepted_token_count,
                            "summary_truncated": summary_truncated,
                        })
                    break

                messages.append(_assistant_history_message(assistant, tool_calls))
                round_observations = []
                round_tool_span_ids = []
                observation_only = True
                for tool_call in tool_calls:
                    item_id = tool_call.get("id") or f"maintainer-tool-{time.time_ns()}"
                    raw_args = (tool_call.get("function") or {}).get("arguments") or ""
                    try:
                        name, args = _tool_call_arguments(tool_call)
                    except RuntimeError as error:
                        tool_argument_rejections += 1
                        observation_only = False
                        result = {"status": "rejected", "error": _tool_argument_error_details(error)}
                        name, args = (tool_call.get("function") or {}).get("name") or "unknown_tool", {}
                        span = start_span(trajectory_context, "tool_call", name, metadata={"tool_call_id": item_id})
                        record_event(span, "validation.rejected", status="rejected", summary={"tool": name}, artifacts={"raw_tool_arguments": raw_args, "tool_result": result})
                        complete_span(span, metadata={"rejected": True})
                        emit_ui_event(runtime_state, "ltm_maintenance_tool_started", run_id=run_id, tool=name, item_id=item_id, args=args, stage=stage)
                        messages.append({"role": "tool", "tool_call_id": item_id, "content": _json(result)})
                        emit_ui_event(runtime_state, "ltm_maintenance_tool_finished", run_id=run_id, tool=name, item_id=item_id, status="rejected", result=result, stage=stage)
                        if tool_argument_rejections >= MAX_CONSECUTIVE_TOOL_ARGUMENT_REJECTIONS:
                            return _fail_run(run_id, counters, f"Maintainer tool arguments were invalid JSON {MAX_CONSECUTIVE_TOOL_ARGUMENT_REJECTIONS} consecutive times; last error: {error}", root=root, runtime_state=runtime_state)
                        continue
                    tool_argument_rejections = 0
                    protocol_violations = 0
                    stage_tool_names.append(name)
                    if name not in maintenance_observation.OBSERVATION_TOOLS:
                        observation_only = False
                    emit_ui_event(runtime_state, "ltm_maintenance_tool_started", run_id=run_id, tool=name, item_id=item_id, args=args, stage=stage)
                    span = start_span(trajectory_context, "tool_call", name, metadata={"tool_call_id": item_id, "stage": stage})
                    record_event(span, "tool.started", status="running", summary={"tool": name, "stage": stage}, artifacts={"tool_arguments": args, "raw_tool_arguments": raw_args})
                    if name == "retrieve_memory" and _RETRIEVAL_STATE.get() is not None:
                        _RETRIEVAL_STATE.get()["trajectory_context"] = span
                    result = _execute_tool(
                        name, args, run_id, counters, root,
                        coverage_target=coverage_target,
                        maintainable_frontier=maintainable_frontier,
                    )
                    _collect_runtime_references(result, references)
                    if name in maintenance_observation.OBSERVATION_TOOLS:
                        round_observations.append({"tool": name, "args": args, "result": result})
                        round_tool_span_ids.append(span.get("span_id"))
                    messages.append(_staged_tool_result_event(runtime_state, trajectory_context, run_id, name, item_id, args, result, span))
                    if name == "submit_maintenance_plan" and result.get("status") == "rejected":
                        if _is_infrastructure_plan_rejection(result):
                            return _fail_run(run_id, counters, f"submit_maintenance_plan infrastructure failure: {_first_error_message(result)}", root=root, runtime_state=runtime_state)
                        if result.get("status") == "rejected":

                            counters.setdefault("_plan_rejections", 0)
                            counters["_plan_rejections"] += 1
                            if counters["_plan_rejections"] >= MAX_CONSECUTIVE_PLAN_REJECTIONS:
                                return _fail_run(run_id, counters, f"submit_maintenance_plan rejected {MAX_CONSECUTIVE_PLAN_REJECTIONS} consecutive times; last error: {_first_error_message(result)}", root=root, runtime_state=runtime_state)
                    elif name == "submit_maintenance_plan" and result.get("status") in {"committed", "already_committed"}:
                        counters.pop("_plan_rejections", None)

                if not observation_only or not round_observations:
                    observation_signature, observation_rounds = None, []
                else:
                    signature = hashlib.sha256(_json(round_observations).encode("utf-8")).hexdigest()
                    if signature != observation_signature:
                        observation_rounds = []
                    observation_signature = signature
                    observation_rounds.append({"model_round": model_round, "tool_names": [item["tool"] for item in round_observations], "tool_span_ids": round_tool_span_ids})
                    repetitions = len(observation_rounds)
                    repetition_summary = {"repeated_rounds": repetitions, "tools": [item["tool"] for item in round_observations], "signature": signature, "rounds": observation_rounds}
                    if repetitions == 3:
                        record_event(trajectory_context, "maintainer.observation_repetition_warning", status="warning", summary=repetition_summary)
                        messages.append({"role": "user", "content": REPEATED_OBSERVATION_NOTICE})
                        emit_ui_event(runtime_state, "ltm_maintenance_repetition_warning", run_id=run_id, content=REPEATED_OBSERVATION_NOTICE, repeated_rounds=repetitions, stage=stage)
                    elif repetitions == 5:
                        record_event(trajectory_context, "maintainer.observation_repetition_stopped", status="failed", summary=repetition_summary)
                        return _fail_run(run_id, counters, f"repeated_observation_no_progress: {repetitions} consecutive rounds; tools: {', '.join(repetition_summary['tools'])}", root=root, runtime_state=runtime_state, final_check_status=final_check_status)

            if stage == "completion":

                pass

            if stage != "completion":
                continue



    except Exception as error:
        return _fail_run(run_id, counters, str(error), root=root, runtime_state=runtime_state, final_check_status=final_check_status)
    return _fail_run(run_id, counters, "maintenance completion protocol did not finish", root=root, runtime_state=runtime_state)


def _run_ltm_maintenance_legacy(runtime_state=None, root=None, force=False):
    del force
    run = ltm_store.start_or_resume_maintenance_run(root)
    run_id = run["run_id"]
    state_at_start = ltm_store.get_maintenance_runtime_state(root)
    maintainable_frontier = formation.maintainable_slot_frontier(root=root)
    coverage_target = max(int(state_at_start.get("maintenance_coverage_cursor") or -1), maintainable_frontier)
    caller = (runtime_state or {}).get("trajectory_context")
    trajectory_context = start_trajectory("ltm_maintainer", run_id, caller=caller, root=root)
    if isinstance(runtime_state, dict):
        runtime_state["_ltm_maintenance_trajectory_context"] = trajectory_context
    record_event(
        trajectory_context,
        "process.invoked",
        status="running",
        summary={
            "run_id": run_id,
            "start_status": run.get("status"),
            "maintainable_frontier": maintainable_frontier,
            "coverage_target_slot_seq": coverage_target,
            "caller_trajectory_id": (caller or {}).get("trajectory_id"),
            "caller_span_id": (caller or {}).get("span_id"),
        },
    )
    counters = _empty_counters()
    consecutive_plan_rejections = 0
    consecutive_tool_argument_rejections = 0
    observation_signature = None
    observation_rounds = []
    model_round = 0
    protocol_violations = 0
    final_check_status = "not_run"
    messages = _initial_messages(run, root)
    messages.append({"role":"user", "content":_json({"maintainable_frontier":maintainable_frontier,
        "coverage_target_slot_seq":coverage_target,
        "completion_protocol":"Call finish_maintenance alone after reaching this coverage target. Text-only replies do not finish maintenance."})})
    emit_ui_event(runtime_state, "ltm_maintenance_started", run_id=run_id, start_status=run["status"])
    try:
        while True:
            model_round += 1
            emit_ui_event(runtime_state, "ltm_maintenance_model_turn", run_id=run_id)
            data = _call_maintainer_model(messages, runtime_state, run_id)
            assistant = parse_chat_completion_message(data)
            _emit_assistant_visibility(runtime_state, run_id, assistant)
            tool_calls = assistant.get("tool_calls") or []
            finish_calls = [call for call in tool_calls if (call.get("function") or {}).get("name") == "finish_maintenance"]
            protocol_error = None
            argument_error = None
            if assistant.get("finish_reason") not in {"stop", "tool_calls"}:
                protocol_error = "Response not accepted: unsupported or missing finish_reason. No tools were executed."
            elif not tool_calls:
                protocol_error = "No tool action received. Continue with tools, or call finish_maintenance alone when ready."
            elif finish_calls:
                try:
                    if len(tool_calls) != 1 or _tool_call_arguments(finish_calls[0])[1] != {}:
                        protocol_error = "finish_maintenance requires empty arguments and must be the only tool call. No tools were executed."
                except RuntimeError as error:
                    protocol_error = "Invalid finish_maintenance arguments. Use {} alone. No tools were executed."
                    argument_error = _tool_argument_error_details(error)
            if protocol_error:
                observation_signature, observation_rounds = None, []
                protocol_violations += 1
                details = {"message":protocol_error,"finish_reason":assistant.get("finish_reason"),
                           "consecutive_violations":protocol_violations,"model_round":model_round,
                           "coverage_target_slot_seq":coverage_target}
                if argument_error is not None:
                    details["argument_error"] = argument_error
                record_event(trajectory_context,"maintainer.completion_protocol_rejected",status="rejected",summary=details)
                if protocol_violations >= 3:
                    return _fail_run(run_id,counters,"maintenance_completion_protocol: " + protocol_error,
                                     root=root,runtime_state=runtime_state,final_check_status=final_check_status)
                messages.append(_assistant_history_message(assistant, tool_calls))
                _append_rejected_tool_results(messages, tool_calls, protocol_error)
                messages.append({"role":"user","content":_json(details)})
                emit_ui_event(runtime_state,"ltm_maintenance_runtime_notice",run_id=run_id,content=protocol_error)
                continue
            protocol_violations = 0
            if finish_calls:
                observation_signature, observation_rounds = None, []
                finish_id = finish_calls[0].get("id") or f"maintainer-finish-{time.time_ns()}"
                tool_calls[0]["id"] = finish_id
                messages.append(_assistant_history_message(assistant,tool_calls))
                finish_span = start_span(trajectory_context,"tool","finish_maintenance")
                record_event(finish_span,"tool.started",status="running",summary={"tool":"finish_maintenance","model_round":model_round},artifacts={"tool_arguments":{}})
                emit_ui_event(runtime_state,"ltm_maintenance_tool_started",run_id=run_id,tool="finish_maintenance",item_id=finish_id,args={})
                def finish_result(result):
                    messages.append({"role":"tool","tool_call_id":finish_id,"content":_json(result)})
                    record_event(finish_span,"tool.completed",status=result["status"],summary={"tool":"finish_maintenance"},artifacts={"tool_result":result})
                    complete_span(finish_span)
                    emit_ui_event(runtime_state,"ltm_maintenance_tool_finished",run_id=run_id,tool="finish_maintenance",item_id=finish_id,status=result["status"],result=result)
                state = ltm_store.get_maintenance_runtime_state(root)
                record_event(trajectory_context,"maintainer.completion_requested",status="running",
                             summary={"model_round":model_round,"finish_reason":assistant.get("finish_reason"),
                                      "coverage_cursor":state["maintenance_coverage_cursor"],
                                      "coverage_target_slot_seq":coverage_target,"tool_span_id":finish_span.get("span_id")})
                if state["maintenance_coverage_cursor"] < coverage_target:
                    rejection = {"status":"rejected","error":{"code":"coverage_incomplete",
                        "message":"Review the remaining slots and explicitly mark coverage before requesting completion."},
                        "coverage_cursor":state["maintenance_coverage_cursor"],"coverage_target_slot_seq":coverage_target,
                        "remaining_start_slot_seq":state["maintenance_coverage_cursor"]+1}
                    finish_result(rejection)
                    record_event(trajectory_context,"maintainer.coverage_incomplete",status="rejected",summary=rejection)
                    continue
                _run_forgetting_cleanup(runtime_state, trajectory_context, run_id, root)
                emit_ui_event(runtime_state, "ltm_maintenance_final_check_started", run_id=run_id)
                final_check_span = start_span(trajectory_context, "final_check", "mechanical consistency")
                health = maintenance_observation.get_overall_health({}, root)
                if _health_passes(health):
                    final_check_status = "passed"
                    record_event(final_check_span, "maintainer.final_check", status="completed", summary={"passed": True}, artifacts={"health": health})
                    complete_span(final_check_span)
                    state = ltm_store.get_maintenance_runtime_state(root)
                    durable_counters = _run_counters(run_id, counters, root)
                    digest_result = maintenance_digest_cache.persist_pending_digest(run_id, root)
                    report = _report_from_counters(durable_counters, state, final_check_status="passed", digest_result=digest_result)
                    ltm_store.complete_maintenance_run(run_id, report, root)
                    final_state = ltm_store.get_maintenance_runtime_state(root)
                    finish_result({"status":"completed","coverage_cursor":final_state["maintenance_coverage_cursor"]})
                    result = {"status": "completed", "run_id": run_id, "final_check_status": "passed", "state": final_state, **durable_counters, **_digest_report_fields(digest_result)}
                    emit_ui_event(runtime_state, "ltm_maintenance_completed", **result, storage_status=(runtime_state or {}).get("ltm_storage_status"))
                    complete_trajectory(trajectory_context, metadata={"run_id": run_id, "coverage_cursor": final_state.get("maintenance_coverage_cursor")})
                    return result
                record_event(final_check_span, "maintainer.final_check", status="failed", summary={"passed": False}, artifacts={"health": health})
                final_check_status = "failed"
                fail_span(final_check_span, RuntimeError("Mechanical consistency check failed."))
                emit_ui_event(runtime_state, "ltm_maintenance_final_check_failed", run_id=run_id, health=health)
                finish_result({"status":"rejected","final_check":"failed","overall_health":health})
                continue
            messages.append(_assistant_history_message(assistant, tool_calls))
            observations, observation_spans = [], []
            observation_only = True
            for tool_call in tool_calls:
                tool_call_id = tool_call.get("id") or f"maintainer-tool-{time.time_ns()}"
                raw_tool_arguments = ((tool_call.get("function") or {}).get("arguments") or "")
                try:
                    name, args = _tool_call_arguments(tool_call)
                except RuntimeError as error:
                    observation_only = False
                    function = tool_call.get("function") or {}
                    name = function.get("name") or "unknown_tool"
                    args = {"invalid_arguments": True}
                    result = {
                        "status": "rejected",
                        "error": _tool_argument_error_details(error),
                    }
                    tool_span = start_span(trajectory_context, "tool_call", name, metadata={"tool_call_id": tool_call_id})
                    record_event(
                        tool_span,
                        "validation.rejected",
                        status="rejected",
                        summary={"tool": name, "code": "invalid_tool_arguments_json"},
                        artifacts={"raw_tool_arguments": raw_tool_arguments, "tool_result": result},
                    )
                    complete_span(tool_span, metadata={"rejected": True})
                    emit_ui_event(
                        runtime_state,
                        "ltm_maintenance_tool_started",
                        run_id=run_id,
                        tool=name,
                        args=args,
                        item_id=tool_call_id,
                    )
                    emit_ui_event(
                        runtime_state,
                        "ltm_maintenance_tool_finished",
                        run_id=run_id,
                        tool=name,
                        status="rejected",
                        item_id=tool_call_id,
                        result=result,
                    )
                    messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": _json(result)})
                    consecutive_tool_argument_rejections += 1
                    if consecutive_tool_argument_rejections >= MAX_CONSECUTIVE_TOOL_ARGUMENT_REJECTIONS:
                        reason = (
                            "Maintainer tool arguments were invalid JSON "
                            f"{MAX_CONSECUTIVE_TOOL_ARGUMENT_REJECTIONS} consecutive times; last error: {error}"
                        )
                        return _fail_run(run_id, counters, reason, root=root, runtime_state=runtime_state, final_check_status=final_check_status)
                    continue
                consecutive_tool_argument_rejections = 0
                emit_ui_event(runtime_state, "ltm_maintenance_tool_started", run_id=run_id, tool=name, args=args, item_id=tool_call_id)
                tool_span = start_span(trajectory_context, "tool_call", name, metadata={"tool_call_id": tool_call_id})
                record_event(tool_span, "tool.started", status="running", summary={"tool": name}, artifacts={"raw_tool_arguments": raw_tool_arguments, "tool_arguments": args})
                try:
                    if name == "retrieve_memory" and _RETRIEVAL_STATE.get() is not None:
                        _RETRIEVAL_STATE.get()["trajectory_context"] = tool_span
                    result = _execute_tool(name, args, run_id, counters, root,
                                           coverage_target=coverage_target,
                                           maintainable_frontier=maintainable_frontier)
                except Exception as error:
                    record_event(tool_span, "tool.failed", status="failed", summary={"tool": name, "error": str(error)}, artifacts={"tool_arguments": args, "error": str(error)})
                    fail_span(tool_span, error)
                    raise
                result_status = "error" if name == "retrieve_memory" and result.get("kind") == "technical" else result.get("status", "ok")
                if name in maintenance_observation.OBSERVATION_TOOLS:
                    observations.append({"tool": name, "args": args, "result": result})
                    observation_spans.append(tool_span.get("span_id"))
                else:
                    observation_only = False
                event_status = "failed" if result_status in {"failed", "error"} else "rejected" if result_status == "rejected" else "completed"
                record_event(tool_span, "tool.completed" if event_status == "completed" else "validation.rejected" if event_status == "rejected" else "tool.failed", status=event_status, summary={"tool": name, "result_status": result_status}, artifacts={"tool_arguments": args, "tool_result": result})
                if event_status in {"completed", "rejected"}:
                    complete_span(tool_span, metadata={"rejected": event_status == "rejected"})
                else:
                    fail_span(tool_span, RuntimeError(str(result)))
                event_type = "ltm_maintenance_plan_committed" if name == "submit_maintenance_plan" and result.get("status") in {"committed", "already_committed"} else "ltm_maintenance_coverage_advanced" if name == "mark_maintenance_coverage" and result.get("status") == "advanced" else "ltm_maintenance_tool_finished"
                emit_ui_event(runtime_state, event_type, run_id=run_id, tool=name, status=result_status, item_id=tool_call_id, result=result)
                messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": _json(result)})
                if name == "submit_maintenance_plan":
                    if result.get("status") == "rejected":
                        if _is_infrastructure_plan_rejection(result):
                            reason = f"submit_maintenance_plan infrastructure failure: {_first_error_message(result)}"
                            return _fail_run(run_id, counters, reason, root=root, runtime_state=runtime_state, final_check_status=final_check_status)
                        consecutive_plan_rejections += 1
                        if consecutive_plan_rejections >= MAX_CONSECUTIVE_PLAN_REJECTIONS:
                            reason = (
                                "submit_maintenance_plan rejected "
                                f"{MAX_CONSECUTIVE_PLAN_REJECTIONS} consecutive times; last error: {_first_error_message(result)}"
                            )
                            return _fail_run(run_id, counters, reason, root=root, runtime_state=runtime_state, final_check_status=final_check_status)
                    elif result.get("status") in {"committed", "already_committed"}:
                        consecutive_plan_rejections = 0
            if not observation_only or not observations:
                observation_signature, observation_rounds = None, []
                continue
            signature = hashlib.sha256(_json(observations).encode("utf-8")).hexdigest()
            if signature != observation_signature:
                observation_rounds = []
            observation_signature = signature
            observation_rounds.append({"model_round": model_round, "tool_span_ids": observation_spans})
            repetitions = len(observation_rounds)
            names = [item["tool"] for item in observations]
            summary = {"repeated_rounds": repetitions, "tools": names, "signature": signature,
                       "rounds": observation_rounds}
            if repetitions == 3:
                record_event(trajectory_context, "maintainer.observation_repetition_warning", status="warning", summary=summary)
                messages.append({"role": "user", "content": REPEATED_OBSERVATION_NOTICE})
                emit_ui_event(runtime_state, "ltm_maintenance_repetition_warning", run_id=run_id,
                              content=REPEATED_OBSERVATION_NOTICE, repeated_rounds=repetitions)
            elif repetitions == 5:
                reason = f"repeated_observation_no_progress: {repetitions} consecutive rounds; tools: {', '.join(names)}"
                record_event(trajectory_context, "maintainer.observation_repetition_stopped", status="failed", summary=summary)
                return _fail_run(run_id, counters, reason, root=root, runtime_state=runtime_state, final_check_status=final_check_status)
    except Exception as error:
        return _fail_run(run_id, counters, str(error), root=root, runtime_state=runtime_state, final_check_status=final_check_status)
