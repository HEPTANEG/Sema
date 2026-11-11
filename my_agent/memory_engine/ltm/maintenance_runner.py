"""LTM Maintainer ReAct runner."""

import json
import hashlib
import copy
import re
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
    fail_trajectory_by_external_id,
    provider_exchange_observer,
    record_event,
    start_trajectory,
    start_span,
)
from runtime.context_repetition import ContextRepetitionDetector

from . import formation, ltm_store, maintenance_digest_cache, maintenance_intent_compiler, maintenance_observation
from . import memory_retrieval
from ..token_counter import count_text_tokens, truncate_text_tokens

_RETRIEVAL_STATE = ContextVar("maintainer_retrieval_state", default=None)
_MAINTENANCE_TRAJECTORY_CONTEXT = ContextVar("maintainer_trajectory_context", default=None)


def _record_plan_phase(run_id, stage, plan_index, phase, status, detail=None):
    context = _MAINTENANCE_TRAJECTORY_CONTEXT.get()
    if context is None:
        return
    record_event(
        context,
        "maintainer.plan_phase",
        status=status,
        summary={
            "run_id": run_id,
            "stage": stage,
            "plan_index": plan_index,
            "phase": phase,
            **(detail if isinstance(detail, dict) else {}),
        },
    )


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
STAGE_SUMMARY_PROMPT_TARGET_TOKENS = 500
STAGE_SUMMARY_RUNTIME_MAX_TOKENS = 1000
STAGE_SUMMARY_MAX_TOKENS = STAGE_SUMMARY_RUNTIME_MAX_TOKENS
STAGE_SUMMARY_OVERFLOW_TIER_ONE_REWRITES = 2
STAGE_SUMMARY_OVERFLOW_TIER_TWO_REWRITES = 4
MAINTENANCE_STAGES = ("recent_review", "global_duplicate_review", "abstraction", "episode_review", "completion")
HALUMEM_SESSION_STAGES = ("recent_review", "episode_review", "completion")
REPEATED_OBSERVATION_NOTICE = (
    "Runtime detected that you executed the same observation tool calls and received "
    "the same results for 3 consecutive rounds. Re-examine your current behavior, "
    "assess why it is not making progress, and adjust your next action."
)


def _repetition_notice(threshold, repeated_rounds):
    if threshold == 3:
        return (
            f"Runtime detected {repeated_rounds} highly similar Maintainer model outputs. "
            "Reassess whether the current repeated action is necessary."
        )
    if threshold == 5:
        return (
            f"Runtime detected {repeated_rounds} highly similar Maintainer model outputs. "
            "Before continuing, reassess the current retrieval target and whether this repetition is necessary."
        )
    return (
        f"Runtime detected {repeated_rounds} highly similar Maintainer model outputs. "
        "Before producing another similar output, decide whether continuing this repetition is necessary."
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
INFRASTRUCTURE_PLAN_REJECTION_CODES = {"vector_sync", "vector_sync_timeout"}
SUMMARY_REPAIR_ERROR_CODES = {"missing_summary", "unexpected_summary", "summary_without_payload"}
REPAIRABLE_PLAN_ERROR_CODES = (
    {"title_length"}
    | SUMMARY_REPAIR_ERROR_CODES
    | set(ltm_store.EPISODE_STRUCTURED_DATA_ERROR_CODES)
)
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
            "description": "Submit a MaintainerActionPlan for deterministic compilation and atomic Store commit. New object titles are short index labels of 1-50 Runtime-counted tokens; put detailed facts in the payload.",
            "parameters": maintenance_intent_compiler.maintenance_action_plan_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_maintenance_plan",
            "description": "Repair only fields marked by Runtime in a rejected Maintainer plan draft. Supported fields are title, summary, and Episode payload.structured_data; Runtime reapplies repairs to the saved plan and validates the complete plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                    "repairs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "target": {"type": "object", "properties": {"temp_id": {"type": "string"}, "object_id": {"type": "string"}}, "additionalProperties": False, "oneOf": [{"required": ["temp_id"]}, {"required": ["object_id"]}]},
                                        "field": {"type": "string", "enum": ["title"]},
                                        "value": {"type": "string", "description": "Replacement short index label of 1-50 Runtime-counted tokens."},
                                    },
                                    "required": ["target", "field", "value"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "target": {"type": "object", "properties": {"temp_id": {"type": "string"}, "object_id": {"type": "string"}}, "additionalProperties": False, "oneOf": [{"required": ["temp_id"]}, {"required": ["object_id"]}]},
                                        "field": {"type": "string", "enum": ["summary"]},
                                        "value": {"type": ["string", "null"], "description": "Replacement summary for the marked object."},
                                    },
                                    "required": ["target", "field", "value"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "target": {"type": "object", "properties": {"temp_id": {"type": "string"}, "object_id": {"type": "string"}}, "additionalProperties": False, "oneOf": [{"required": ["temp_id"]}, {"required": ["object_id"]}]},
                                        "field": {"type": "string", "enum": ["payload.structured_data"]},
                                        "value": maintenance_intent_compiler.episode_structured_data_schema(),
                                    },
                                    "required": ["target", "field", "value"],
                                    "additionalProperties": False,
                                },
                            ],
                        },
                    },
                },
                "required": ["draft_id", "repairs"],
                "additionalProperties": False,
            },
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


def _initial_messages(
    run,
    root,
    stage,
    previous_stage_summary=None,
    references=None,
    summary_material=None,
    summary_max_tokens=None,
):
    state = ltm_store.get_maintenance_runtime_state(root)
    health = maintenance_observation.get_overall_health({}, root)
    digest_max_tokens = int(summary_max_tokens or STAGE_SUMMARY_PROMPT_TARGET_TOKENS)
    prompt = _stage_prompt(stage) + (
        f"\n\nThe stage_summary MUST NOT exceed {digest_max_tokens} tokens. "
        "This is the normal output contract; do not rely on Runtime recovery tolerance. "
        "If necessary, write less.\n"
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


def _plan_diagnostics(plan, compiled=None, raw_tool_arguments=None, repair_applied=False):
    plan = plan if isinstance(plan, dict) else {}
    objects = (compiled or {}).get("objects") if isinstance(compiled, dict) else []
    objects = objects if isinstance(objects, list) else []
    payload_sizes = [
        ltm_store._payload_bytes(obj.get("payload"))
        for obj in objects
        if isinstance(obj, dict) and isinstance(obj.get("payload"), dict)
    ]
    diagnostics = {
        "raw_tool_arguments_bytes": len(str(raw_tool_arguments or "").encode("utf-8")) if raw_tool_arguments is not None else None,
        "plan_arguments_bytes": len(_json(plan).encode("utf-8")),
        "intent_count": len(plan.get("intents") or []) if isinstance(plan.get("intents") or [], list) else 0,
        "atomic_operation_count": len(plan.get("atomic_operations") or []) if isinstance(plan.get("atomic_operations") or [], list) else 0,
        "new_object_count": len(objects),
        "link_count": len((compiled or {}).get("links") or []) if isinstance(compiled, dict) else None,
        "update_count": len((compiled or {}).get("updates") or []) if isinstance(compiled, dict) else None,
        "payload_bytes_total": sum(payload_sizes),
        "max_payload_bytes": max(payload_sizes, default=0),
        "repair_applied": bool(repair_applied),
    }
    return diagnostics


def _attach_plan_diagnostics(result, diagnostics):
    if isinstance(result, dict):
        result["_plan_diagnostics"] = diagnostics
    return result


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


def _submit_maintenance_plan(args, run_id, counters, root, coverage_target=None, maintainable_frontier=None,
                              plan_repair_state=None, draft_id=None, raw_tool_arguments=None,
                              repair_applied=False, stage=None):
    normalized_summaries = []
    def finish(result, compiled=None):
        result_diagnostics = _plan_diagnostics(
            args,
            compiled=compiled,
            raw_tool_arguments=raw_tool_arguments,
            repair_applied=repair_applied,
        )
        if normalized_summaries:
            result_diagnostics["auto_normalized_summaries"] = normalized_summaries
        return _attach_plan_diagnostics(result, result_diagnostics)

    state = ltm_store.get_maintenance_runtime_state(root)
    plan_index = int(state.get("next_plan_index") or 0)
    _record_plan_phase(run_id, stage, plan_index, "compile", "started")
    try:
        compiled = maintenance_intent_compiler.compile_maintenance_action_plan(args, run_id, plan_index, root)
    except Exception as error:
        _record_plan_phase(run_id, stage, plan_index, "compile", "failed", {"error": str(error)})
        raise
    _record_plan_phase(run_id, stage, plan_index, "compile", "completed", {"status": compiled.get("status")})
    if compiled.get("status") != "compiled":
        return finish(compiled)
    if maintainable_frontier is not None:
        source_error = _source_range_error(compiled, maintainable_frontier)
        if source_error is not None:
            return finish(source_error, compiled)
    if _RETRIEVAL_STATE.get() is not None:
        _record_plan_phase(run_id, stage, plan_index, "close_retriever", "started")
        try:
            memory_retrieval.close_retrieval_session(_RETRIEVAL_STATE.get())
        except Exception as error:
            _record_plan_phase(run_id, stage, plan_index, "close_retriever", "failed", {"error": str(error)})
            raise
        _record_plan_phase(run_id, stage, plan_index, "close_retriever", "completed")
    _record_plan_phase(run_id, stage, plan_index, "commit_maintenance_plan", "started")
    commit_kwargs = {}
    if _MAINTENANCE_TRAJECTORY_CONTEXT.get() is not None:
        commit_kwargs["phase_callback"] = lambda phase, status, detail: _record_plan_phase(
            run_id, stage, plan_index, phase, status, detail
        )
    committed = ltm_store.commit_maintenance_plan(
        compiled,
        compiled["idempotency_key"],
        root,
        **commit_kwargs,
    )
    errors = committed.get("errors") or []
    if committed.get("status") == "rejected" and errors and all(
        isinstance(error, dict) and error.get("code") == "unexpected_summary"
        and isinstance(error.get("actual_payload_bytes"), int)
        and error["actual_payload_bytes"] <= ltm_store.SUMMARY_THRESHOLD_BYTES
        and re.fullmatch(r"objects\[(\d+)\]\.summary", str(error.get("path") or ""))
        for error in errors
    ):
        normalized = copy.deepcopy(compiled)
        for error in errors:
            index = int(re.fullmatch(r"objects\[(\d+)\]\.summary", error["path"])[1])
            normalized["objects"][index]["summary"] = None
            normalized_summaries.append({"path": error["path"], "actual_payload_bytes": error["actual_payload_bytes"]})
        committed = ltm_store.commit_maintenance_plan(normalized, normalized["idempotency_key"], root, **commit_kwargs)
        compiled = normalized
    _record_plan_phase(run_id, stage, plan_index, "commit_maintenance_plan", "completed", {"status": committed.get("status")})
    if committed.get("status") in {"committed", "already_committed"}:
        _merge_commit_counters(counters, compiled, committed)
        ltm_store.checkpoint_maintenance_run(
            run_id,
            plan_index + 1,
            last_committed_plan_index=plan_index,
            root=root,
        )
        _clear_plan_draft(plan_repair_state, draft_id)
        return finish(committed, compiled)
    if not _is_repairable_rejection(committed):
        return finish(committed, compiled)
    _annotate_repair_errors(committed, compiled)
    draft = _active_plan_draft(plan_repair_state, draft_id)
    if draft_id and draft is None:
        return finish({
            "status": "rejected",
            "errors": [{
                "code": "draft_not_found",
                "path": "draft_id",
                "message": "The maintenance plan draft is not available in this invocation.",
            }],
        }, compiled)
    if draft is None:
        draft = _make_plan_draft(args, committed, run_id, plan_index, plan_repair_state, attempts=1)
    else:
        draft["plan"] = copy.deepcopy(args)
        draft["attempts"] = int(draft.get("attempts") or 0) + 1
        draft["targets"] = _repairable_entries(committed)
        draft["errors"] = copy.deepcopy(committed.get("errors") or [])
        committed["draft_id"] = draft["draft_id"]
        committed["repair_targets"] = draft["targets"]
    if draft is not None and int(draft.get("attempts") or 0) >= 2:
        return finish(_partial_commit_plan(
            args, committed, draft, run_id, counters, root, maintainable_frontier, plan_repair_state,
        ), compiled)
    return finish(committed, compiled)


def _repair_maintenance_plan(args, run_id, counters, root, coverage_target=None, maintainable_frontier=None,
                              plan_repair_state=None, raw_tool_arguments=None, stage=None):
    if not isinstance(args, dict):
        return {"status": "rejected", "errors": [{"code": "invalid_arguments", "path": "plan", "message": "Arguments must be an object."}]}
    draft_id = args.get("draft_id")
    draft = _active_plan_draft(plan_repair_state, draft_id)
    if draft is None:
        return {"status": "rejected", "errors": [{"code": "draft_not_found", "path": "draft_id", "message": "The maintenance plan draft is not available in this invocation."}]}
    repairs = args.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        return {"status": "rejected", "errors": [{"code": "invalid_repairs", "path": "repairs", "message": "At least one repair is required."}]}
    allowed = {
        (_target_key(entry.get("target")), entry.get("field"))
        for entry in draft.get("targets") or []
        if isinstance(entry, dict) and _target_key(entry.get("target")) is not None
    }
    repaired_plan = copy.deepcopy(draft["plan"])
    seen = set()
    for index, repair in enumerate(repairs):
        target = _target_key(repair.get("target") if isinstance(repair, dict) else None)
        field = repair.get("field") if isinstance(repair, dict) else None
        value = repair.get("value") if isinstance(repair, dict) else None
        key = (target, field)
        if key not in allowed:
            return {"status": "rejected", "errors": [{"code": "repair_target_not_allowed", "path": f"repairs[{index}]", "message": "Only fields marked by Runtime may be repaired."}]}
        if key in seen:
            return {"status": "rejected", "errors": [{"code": "duplicate_repair_target", "path": f"repairs[{index}]", "message": "A target field may only be repaired once."}]}
        if field == "title" and not isinstance(value, str):
            return {"status": "rejected", "errors": [{"code": "invalid_repair_value", "path": f"repairs[{index}].value", "message": "title repair value must be a string."}]}
        if field == "payload.structured_data" and value is not None and not isinstance(value, dict):
            return {"status": "rejected", "errors": [{"code": "invalid_repair_value", "path": f"repairs[{index}].value", "message": "structured_data repair value must be an object or null."}]}
        seen.add(key)
        if not _replace_repair_target(repaired_plan, {target[0]: target[1]}, field, value):
            return {"status": "rejected", "errors": [{"code": "repair_target_not_found", "path": f"repairs[{index}].target", "message": "The repair target is not present in the saved draft."}]}
    return _submit_maintenance_plan(
        repaired_plan, run_id, counters, root, coverage_target=coverage_target,
        maintainable_frontier=maintainable_frontier, plan_repair_state=plan_repair_state, draft_id=draft_id,
        raw_tool_arguments=raw_tool_arguments, repair_applied=True, stage=stage,
    )


def _execute_tool(name, args, run_id, counters, root, coverage_target=None, maintainable_frontier=None,
                  plan_repair_state=None, raw_tool_arguments=None, stage=None):
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
        return _submit_maintenance_plan(
            args, run_id, counters, root, coverage_target=coverage_target,
            maintainable_frontier=maintainable_frontier, plan_repair_state=plan_repair_state,
            raw_tool_arguments=raw_tool_arguments, stage=stage,
        )
    if name == "repair_maintenance_plan":
        return _repair_maintenance_plan(
            args, run_id, counters, root, coverage_target=coverage_target,
            maintainable_frontier=maintainable_frontier, plan_repair_state=plan_repair_state,
            raw_tool_arguments=raw_tool_arguments,
            stage=stage,
        )
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


def _repair_errors(result):
    errors = result.get("errors") if isinstance(result, dict) else None
    return [error for error in errors or [] if isinstance(error, dict) and error.get("code") in REPAIRABLE_PLAN_ERROR_CODES]


def _is_repairable_rejection(result):
    errors = result.get("errors") if isinstance(result, dict) else None
    return bool(errors) and len(_repair_errors(result)) == len(errors)


def _annotate_repair_errors(result, compiled):
    targets = compiled.get("repair_targets") if isinstance(compiled, dict) else []
    if not targets and isinstance(compiled, dict):
        targets = compiled.get("title_targets") or []
    object_targets = {item.get("object_index"): item.get("target") for item in targets if item.get("kind") == "object"}
    update_targets = {item.get("update_index"): item.get("target") for item in targets if item.get("kind") == "update"}
    for error in result.get("errors") or []:
        if not isinstance(error, dict) or error.get("code") not in REPAIRABLE_PLAN_ERROR_CODES:
            continue
        path = str(error.get("path") or "")
        match = re.fullmatch(r"objects\[(\d+)\]\.(title|summary)", path)
        if match and int(match[1]) in object_targets:
            field = match[2]
            if error.get("code") == "title_length" and field == "title":
                error["repair_target"] = object_targets[int(match[1])]
                error["repair_field"] = "title"
            elif error.get("code") in SUMMARY_REPAIR_ERROR_CODES and field == "summary":
                error["repair_target"] = object_targets[int(match[1])]
                error["repair_field"] = "summary"
            continue
        match = re.fullmatch(r"updates\[(\d+)\](?:\.value|\.summary)?(?:\.payload)?", path)
        if match and int(match[1]) in update_targets:
            update_index = int(match[1])
            update = (compiled.get("updates") or [])[update_index]
            update_field = update.get("field") if isinstance(update, dict) else None
            if error.get("code") == "title_length" and update_field == "title":
                error["repair_target"] = update_targets[update_index]
                error["repair_field"] = "title"
            elif error.get("code") in SUMMARY_REPAIR_ERROR_CODES and update_field == "summary":
                error["repair_target"] = update_targets[update_index]
                error["repair_field"] = "summary"
            continue
        match = re.fullmatch(r"objects\[(\d+)\]\.payload\.structured_data(?:\..*)?", str(error.get("path") or ""))
        if match and int(match[1]) in object_targets:
            error["repair_target"] = object_targets[int(match[1])]
            error["repair_field"] = "payload.structured_data"
    return result


def _target_key(target):
    if not isinstance(target, dict) or len(target) != 1:
        return None
    key, value = next(iter(target.items()))
    if key not in {"temp_id", "object_id"} or not isinstance(value, str) or not value:
        return None
    return key, value


def _plan_references(value):
    references = set()
    if isinstance(value, dict):
        for key in ("temp_id", "object_id"):
            if isinstance(value.get(key), str) and value[key]:
                references.add((key, value[key]))
        for child in value.values():
            references.update(_plan_references(child))
    elif isinstance(value, list):
        for child in value:
            references.update(_plan_references(child))
    return references


def _plan_units(plan):
    units = []
    for index, intent in enumerate(plan.get("intents") or []):
        units.append({"section": "intents", "index": index, "value": intent, "references": _plan_references(intent)})
    for index, operation in enumerate(plan.get("atomic_operations") or []):
        units.append({"section": "atomic_operations", "index": index, "value": operation, "references": _plan_references(operation)})
    return units


def _group_plan_units(plan):
    units = _plan_units(plan)
    parent = list(range(len(units)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left in range(len(units)):
        for right in range(left + 1, len(units)):
            if units[left]["references"] & units[right]["references"]:
                union(left, right)
    groups = {}
    for index, unit in enumerate(units):
        groups.setdefault(find(index), []).append(unit)
    return list(groups.values())


def _subplan_from_units(groups):
    plan = {}
    intents = [unit["value"] for unit in groups if unit["section"] == "intents"]
    atomics = [unit["value"] for unit in groups if unit["section"] == "atomic_operations"]
    if intents:
        plan["intents"] = intents
    if atomics:
        plan["atomic_operations"] = atomics
    return plan


def _replace_title_target(plan, target, title):
    key = _target_key(target)
    if key is None:
        return False
    matches = []
    for intent in plan.get("intents") or []:
        intent_type = intent.get("intent_type") if isinstance(intent, dict) else None
        candidates = []
        if intent_type in {"replace", "merge", "abstract"}:
            candidates = [intent.get("new_object")]
        elif intent_type == "split":
            candidates = intent.get("new_objects") or []
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get(key[0]) == key[1]:
                matches.append(candidate)
        if intent_type == "update_title" and intent.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(intent)
    for operation in plan.get("atomic_operations") or []:
        if not isinstance(operation, dict):
            continue
        if operation.get("operation_type") == "create_object":
            candidate = operation.get("new_object")
            if isinstance(candidate, dict) and candidate.get(key[0]) == key[1]:
                matches.append(candidate)
        elif operation.get("operation_type") == "update_title" and operation.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(operation)
    if len(matches) != 1:
        return False
    candidate = matches[0]
    candidate["title"] = title if "title" in candidate else title
    if "new_title" in candidate:
        candidate["new_title"] = title
    return True


def _replace_repair_target(plan, target, field, value):
    key = _target_key(target)
    if key is None or field not in {"title", "summary", "payload.structured_data"}:
        return False
    matches = []
    for intent in plan.get("intents") or []:
        intent_type = intent.get("intent_type") if isinstance(intent, dict) else None
        candidates = []
        if intent_type in {"replace", "merge", "abstract"}:
            candidates = [intent.get("new_object")]
        elif intent_type == "split":
            candidates = intent.get("new_objects") or []
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get(key[0]) == key[1]:
                matches.append(candidate)
        if field == "title" and intent_type == "update_title" and intent.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(intent)
        if field == "summary" and intent_type == "update_summary" and intent.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(intent)
    for operation in plan.get("atomic_operations") or []:
        if not isinstance(operation, dict):
            continue
        if operation.get("operation_type") == "create_object":
            candidate = operation.get("new_object")
            if isinstance(candidate, dict) and candidate.get(key[0]) == key[1]:
                matches.append(candidate)
        elif field == "title" and operation.get("operation_type") == "update_title" and operation.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(operation)
        elif field == "summary" and operation.get("operation_type") == "update_summary" and operation.get("object_id") == key[1] and key[0] == "object_id":
            matches.append(operation)
    if len(matches) != 1:
        return False
    candidate = matches[0]
    if field == "title":
        if "new_title" in candidate:
            candidate["new_title"] = value
        else:
            candidate["title"] = value
        return True
    if field == "summary":
        if "new_summary" in candidate:
            candidate["new_summary"] = value
        else:
            candidate["summary"] = value
        return True
    payload = candidate.get("payload")
    if not isinstance(payload, dict) or candidate.get("object_type") != "episode":
        return False
    payload["structured_data"] = copy.deepcopy(value)
    return True


def _repairable_entries(result):
    targets = []
    for error in _repair_errors(result):
        target = error.get("repair_target")
        field = error.get("repair_field")
        entry = {"target": target, "field": field}
        if _target_key(target) is not None and field in {"title", "summary", "payload.structured_data"} and entry not in targets:
            targets.append(entry)
    return targets


def _make_plan_draft(plan, result, run_id, plan_index, plan_repair_state, attempts=1):
    if plan_repair_state is None or not _is_repairable_rejection(result):
        return None
    targets = _repairable_entries(result)
    if not targets:
        return None
    draft_id = f"maintenance_draft:{run_id}:{int(plan_index)}:{time.time_ns()}"
    draft = {"draft_id": draft_id, "plan": copy.deepcopy(plan), "attempts": attempts, "targets": targets, "errors": copy.deepcopy(result.get("errors") or [])}
    plan_repair_state.setdefault("drafts", {})[draft_id] = draft
    plan_repair_state["active_draft_id"] = draft_id
    result["draft_id"] = draft_id
    result["repair_targets"] = targets
    return draft


def _active_plan_draft(plan_repair_state, draft_id=None):
    if not isinstance(plan_repair_state, dict):
        return None
    drafts = plan_repair_state.setdefault("drafts", {})
    selected = draft_id or plan_repair_state.get("active_draft_id")
    return drafts.get(selected) if selected else None


def _clear_plan_draft(plan_repair_state, draft_id=None):
    if not isinstance(plan_repair_state, dict):
        return
    drafts = plan_repair_state.setdefault("drafts", {})
    selected = draft_id or plan_repair_state.get("active_draft_id")
    if selected:
        drafts.pop(selected, None)
    if plan_repair_state.get("active_draft_id") == selected:
        plan_repair_state.pop("active_draft_id", None)


def _partial_commit_plan(plan, result, draft, run_id, counters, root, maintainable_frontier, plan_repair_state):
    invalid_targets = {_target_key(error.get("repair_target")) for error in _repair_errors(result)}
    invalid_targets.discard(None)
    groups = _group_plan_units(plan)
    valid_groups, skipped_groups = [], []
    for group_index, group in enumerate(groups):
        if any(unit["references"] & invalid_targets for unit in group):
            skipped_groups.append({"group_id": f"group-{group_index}", "plan": _subplan_from_units(group), "errors": copy.deepcopy(result.get("errors") or [])})
        else:
            valid_groups.append((group_index, _subplan_from_units(group)))
    if not valid_groups:
        result["draft_id"] = draft["draft_id"] if draft else result.get("draft_id")
        return result
    state = ltm_store.get_maintenance_runtime_state(root)
    next_plan_index = int(state.get("next_plan_index") or 0)
    compiled_groups = []
    for group_index, subplan in valid_groups:
        compiled = maintenance_intent_compiler.compile_maintenance_action_plan(subplan, run_id, next_plan_index, root)
        if compiled.get("status") != "compiled":
            return result
        if maintainable_frontier is not None:
            source_error = _source_range_error(compiled, maintainable_frontier)
            if source_error is not None:
                return result
        compiled_groups.append((group_index, compiled))
        next_plan_index += 1
    if _RETRIEVAL_STATE.get() is not None:
        memory_retrieval.close_retrieval_session(_RETRIEVAL_STATE.get())
    committed_groups = []
    for group_index, compiled in compiled_groups:
        committed = ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], root)
        if committed.get("status") not in {"committed", "already_committed"}:
            skipped_groups.append({"group_id": f"group-{group_index}", "plan": _subplan_from_units(groups[group_index]), "errors": copy.deepcopy(committed.get("errors") or [])})
            continue
        _merge_commit_counters(counters, compiled, committed)
        ltm_store.checkpoint_maintenance_run(run_id, int(compiled["plan_index"]) + 1, last_committed_plan_index=int(compiled["plan_index"]), root=root)
        committed_groups.append({"group_id": f"group-{group_index}", "result": committed})
    if not committed_groups:
        return result
    skipped_plan_units = []
    for skipped in skipped_groups:
        skipped_plan_units.extend(_plan_units(skipped.get("plan") or {}))
    skipped_plan = _subplan_from_units(skipped_plan_units)
    if skipped_plan:
        draft_id = draft["draft_id"] if draft else _make_plan_draft(skipped_plan, result, run_id, next_plan_index, plan_repair_state, attempts=1)["draft_id"]
        if draft:
            draft["plan"] = copy.deepcopy(skipped_plan)
            draft["attempts"] = 1
            draft["errors"] = copy.deepcopy(result.get("errors") or [])
        result_draft_id = draft_id
    else:
        _clear_plan_draft(plan_repair_state, draft.get("draft_id") if draft else None)
        result_draft_id = None
    return {"status": "partial_committed", "draft_id": result_draft_id, "committed_groups": committed_groups, "skipped_groups": [{"group_id": item["group_id"], "errors": item["errors"]} for item in skipped_groups], "errors": copy.deepcopy(result.get("errors") or [])}


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
    status_callback = (runtime_state or {}).get("wake_enqueue_status_callback")
    trajectory_context = (runtime_state or {}).get("_ltm_maintenance_trajectory_context")
    if not callable(callback) and not callable(status_callback):
        record_event(trajectory_context, "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "reason": "wake queue callback is unavailable"})
        return False
    continuation = (runtime_state or {}).get("maintenance_continuation")
    if isinstance(continuation, dict) and continuation.get("status") == "running" and continuation.get("wake_prompt"):
        payload = {
            "maintenance_run_id": run_id,
            "status": "completed",
            "continuation_id": continuation.get("continuation_id"),
            "wake_prompt": continuation["wake_prompt"],
            "parent_turn_id": continuation.get("parent_turn_id"),
            "parent_request_id": continuation.get("parent_request_id"),
            "instruction": "Continue the suspended task from wake_prompt using the retained conversation history, then proceed with the stated next step.",
            "maintenance_summary_status": (summary_result or {}).get("status", "missing"),
            "maintenance_summary_id": (summary_result or {}).get("digest_id"),
        }
        try:
            enqueue = status_callback if callable(status_callback) else callback
            result = enqueue(
                "maintainer", "maintenance_task_resumed", payload,
                processing_class="tool_result_like",
                wake_id=f"wake_maintenance_resume_{continuation.get('continuation_id')}_{run_id}",
            )
            status = result if isinstance(result, str) else "queued" if result else "failed"
        except Exception as error:
            record_event(trajectory_context, "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "error": str(error)})
            return False
        accepted = status in {"queued", "already_queued", "already_in_flight", "already_delivered"}
        if accepted:
            record_event(trajectory_context, "maintenance.wake_queued", status="completed", summary={"run_id": run_id, "summary_status": summary_result.get("status"), "queue_status": status})
        else:
            record_event(trajectory_context, "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "queue_status": status, "reason": "wake queue did not accept the event"})
        return accepted
    payload = {
        "run_id": run_id,
        "status": "completed",
        "maintenance_summary": summary,
        "instruction": "你刚从一次类似睡眠的长期记忆维护中醒来。下面是这次梦中保留下来的内容，请用简短、自然的梦境回忆告诉用户。",
        "maintenance_summary_status": (summary_result or {}).get("status", "missing"),
        "maintenance_summary_id": (summary_result or {}).get("digest_id"),
    }
    try:
        enqueue = status_callback if callable(status_callback) else callback
        result = enqueue(
            "maintainer", "maintenance_completed", payload,
            processing_class="tool_result_like",
            wake_id=f"wake_maintenance_{run_id}",
        )
        status = result if isinstance(result, str) else "queued" if result else "failed"
    except Exception as error:
        record_event(trajectory_context, "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "error": str(error)})
        return False
    accepted = status in {"queued", "already_queued", "already_in_flight", "already_delivered"}
    if accepted:
        record_event(trajectory_context, "maintenance.wake_queued", status="completed", summary={"run_id": run_id, "summary_status": summary_result.get("status"), "queue_status": status})
    else:
        record_event(trajectory_context, "maintenance.wake_failed", status="failed", summary={"run_id": run_id, "queue_status": status, "reason": "wake queue did not accept the event"})
    return accepted


def _staged_tool_result_event(runtime_state, trajectory_context, run_id, tool, item_id, args, result, span):
    status = result.get("status", "ok") if isinstance(result, dict) else "ok"
    display_status = "error" if isinstance(result, dict) and result.get("kind") == "technical" else status
    event_status = "failed" if display_status in {"failed", "error"} else "rejected" if display_status == "rejected" else "completed"
    event_summary = {"tool": tool, "result_status": status}
    plan_diagnostics = result.get("_plan_diagnostics") if isinstance(result, dict) else None
    if plan_diagnostics is not None:
        event_summary["plan_diagnostics"] = plan_diagnostics
    record_event(
        span,
        "tool.completed" if event_status == "completed" else "validation.rejected" if event_status == "rejected" else "tool.failed",
        status=event_status,
        summary=event_summary,
        artifacts={"tool_arguments": args, "tool_result": result},
    )
    if event_status == "failed":
        fail_span(span, RuntimeError(str(result)))
    else:
        complete_span(span, metadata={"rejected": event_status == "rejected"})
    public_result = dict(result) if isinstance(result, dict) else result
    if isinstance(public_result, dict):
        public_result.pop("_plan_diagnostics", None)
    event_type = (
        "ltm_maintenance_plan_committed" if tool in {"submit_maintenance_plan", "repair_maintenance_plan"} and status in {"committed", "already_committed", "partial_committed"}
        else "ltm_maintenance_coverage_advanced" if tool == "mark_maintenance_coverage" and status == "advanced"
        else "ltm_maintenance_tool_finished"
    )
    emit_ui_event(runtime_state, event_type, run_id=run_id, tool=tool, status=display_status, item_id=item_id, result=public_result)
    return {"role": "tool", "tool_call_id": item_id, "content": _json(public_result)}


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
    if stage_summary_max_tokens is None:
        summary_max_tokens = STAGE_SUMMARY_RUNTIME_MAX_TOKENS
        prompt_summary_max_tokens = STAGE_SUMMARY_PROMPT_TARGET_TOKENS
    else:
        summary_max_tokens = int(stage_summary_max_tokens)
        prompt_summary_max_tokens = summary_max_tokens
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
    interrupted_run_id = run.get("interrupted_run_id")
    if interrupted_run_id:
        fail_trajectory_by_external_id(
            "ltm_maintainer",
            interrupted_run_id,
            RuntimeError("Maintenance process ended before the run completed."),
            metadata={"run_id": interrupted_run_id, "reason": "stale maintenance owner"},
            root=root,
        )
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
    plan_context_token = _MAINTENANCE_TRAJECTORY_CONTEXT.set(trajectory_context)
    record_event(trajectory_context, "process.invoked", status="running", summary={
        "run_id": run_id, "start_status": run.get("status"),
        "maintainable_frontier": maintainable_frontier,
        "coverage_target_slot_seq": coverage_target,
        "historical_slot_range": historical_slot_range,
    })
    counters = _empty_counters()
    plan_repair_state = {"drafts": {}}
    references = {"object_ids": set(), "link_ids": set(), "slot_seqs": set()}
    stage_summaries = dict(initial_stage_summaries or {})
    repetition_detector = ContextRepetitionDetector()
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
            repetition_detector.mark_stage_boundary(stage)
            if progress_callback is not None:
                progress_callback({"stage": stage, "stage_index": stage_index, "status": "started"})
            emit_ui_event(runtime_state, "ltm_maintenance_stage_started", run_id=run_id, stage=stage, stage_index=stage_index)
            record_event(trajectory_context, "maintainer.stage_started", status="running", summary={"stage": stage, "stage_index": stage_index})
            ltm_store.heartbeat_maintenance_run(run_id, root)
            messages = _initial_messages(
                run, root, stage, previous_stage_summary, _reference_payload(references),
                maintenance_digest_cache.build_maintenance_summary(stage_summaries),
                summary_max_tokens=prompt_summary_max_tokens,
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
            stage_summary = None
            stage_summary_overflow_attempts = 0
            while True:
                model_round += 1
                ltm_store.heartbeat_maintenance_run(run_id, root)
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
                                f"stage_summary has {summary_token_count} tokens. Rewrite it from scratch under "
                                f"{STAGE_SUMMARY_PROMPT_TARGET_TOKENS} tokens. Do not call observation tools again."
                            )
                            notice = "\n".join([warning] * warning_repetitions)
                            details = {
                                "code": "stage_summary_overflow",
                                "error": notice,
                                "stage": stage,
                                "token_count": summary_token_count,
                                "hard_limit": summary_max_tokens,
                                "prompt_target": STAGE_SUMMARY_PROMPT_TARGET_TOKENS,
                                "rewrite_attempt": stage_summary_overflow_attempts,
                                "max_rewrites": max_rewrites,
                            }
                            record_event(trajectory_context, "maintainer.stage_summary_overflow_warning", status="warning", summary=details)
                            messages.append(_assistant_history_message(assistant, tool_calls))
                            _append_rejected_tool_results(messages, tool_calls, notice)
                            messages.append({"role": "user", "content": notice})
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
                round_tool_span_ids = []
                round_tool_names = []
                model_output_accepted = True
                model_output_tool_calls = []
                for tool_call in tool_calls:
                    item_id = tool_call.get("id") or f"maintainer-tool-{time.time_ns()}"
                    raw_args = (tool_call.get("function") or {}).get("arguments") or ""
                    try:
                        name, args = _tool_call_arguments(tool_call)
                    except RuntimeError as error:
                        tool_argument_rejections += 1
                        model_output_accepted = False
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
                    round_tool_names.append(name)
                    model_output_tool_calls.append({
                        "id": item_id,
                        "function": {"name": name, "arguments": args},
                    })
                    emit_ui_event(runtime_state, "ltm_maintenance_tool_started", run_id=run_id, tool=name, item_id=item_id, args=args, stage=stage)
                    span = start_span(trajectory_context, "tool_call", name, metadata={"tool_call_id": item_id, "stage": stage})
                    record_event(span, "tool.started", status="running", summary={"tool": name, "stage": stage}, artifacts={"tool_arguments": args, "raw_tool_arguments": raw_args})
                    if name == "retrieve_memory" and _RETRIEVAL_STATE.get() is not None:
                        _RETRIEVAL_STATE.get()["trajectory_context"] = span
                    result = _execute_tool(
                        name, args, run_id, counters, root,
                        coverage_target=coverage_target,
                        maintainable_frontier=maintainable_frontier,
                        plan_repair_state=plan_repair_state,
                        raw_tool_arguments=raw_args,
                        stage=stage,
                    )
                    _collect_runtime_references(result, references)
                    round_tool_span_ids.append(span.get("span_id"))
                    messages.append(_staged_tool_result_event(runtime_state, trajectory_context, run_id, name, item_id, args, result, span))
                    if name in {"submit_maintenance_plan", "repair_maintenance_plan"} and result.get("status") == "rejected":
                        if _is_infrastructure_plan_rejection(result):
                            return _fail_run(run_id, counters, f"{name} infrastructure failure: {_first_error_message(result)}", root=root, runtime_state=runtime_state)
                        if result.get("status") == "rejected":

                            counters.setdefault("_plan_rejections", 0)
                            counters["_plan_rejections"] += 1
                            if counters["_plan_rejections"] >= MAX_CONSECUTIVE_PLAN_REJECTIONS:
                                return _fail_run(run_id, counters, f"submit_maintenance_plan rejected {MAX_CONSECUTIVE_PLAN_REJECTIONS} consecutive times; last error: {_first_error_message(result)}", root=root, runtime_state=runtime_state)
                    elif name in {"submit_maintenance_plan", "repair_maintenance_plan"} and result.get("status") in {"committed", "already_committed", "partial_committed"}:
                        counters.pop("_plan_rejections", None)

                if model_output_accepted and model_output_tool_calls:
                    signal = repetition_detector.observe(
                        {"content": assistant.get("content") or "", "tool_calls": model_output_tool_calls},
                        model_round,
                        stage=stage,
                        tool_names=round_tool_names,
                        tool_span_ids=round_tool_span_ids,
                    )
                    if signal is not None:
                        repetition_summary = signal.as_dict()
                        notice = _repetition_notice(signal.threshold, signal.repeated_rounds)
                        record_event(trajectory_context, "maintainer.observation_repetition_warning", status="warning", summary=repetition_summary)
                        messages.append({"role": "user", "content": notice})
                        emit_ui_event(
                            runtime_state,
                            "ltm_maintenance_repetition_warning",
                            run_id=run_id,
                            content=notice,
                            repeated_rounds=signal.repeated_rounds,
                            stage=stage,
                        )

            if stage == "completion":

                pass

            if stage != "completion":
                continue



    except Exception as error:
        return _fail_run(run_id, counters, str(error), root=root, runtime_state=runtime_state, final_check_status=final_check_status)
    finally:
        _MAINTENANCE_TRAJECTORY_CONTEXT.reset(plan_context_token)
    return _fail_run(run_id, counters, "maintenance completion protocol did not finish", root=root, runtime_state=runtime_state)
