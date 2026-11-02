"""User-requested LTM memorization for Event Stream."""

import copy
import hashlib
import json
import threading
import time
import uuid

from model.provider import call_chat_completion
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
from .. import event_stream
from . import formation, ltm_retrieval, ltm_store, meta_memory
from runtime.context_snapshot import context_snapshot_for_agent
from bridge.ui_events import emit_ui_event


FORMATION_TIMEOUT_SECONDS = formation.FORMATION_TIMEOUT_SECONDS
FORMATION_MAX_TOOL_CALLS = formation.FORMATION_MAX_TOOL_CALLS
FORMATION_MAX_MODEL_ROUNDS = formation.FORMATION_MAX_MODEL_ROUNDS


class MemorizeError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _metrics():
    return {
        "model_round_count": 0,
        "retrieval_call_count": 0,
        "retrieval_success_count": 0,
        "retrieval_error_count": 0,
        "retrieval_partial_count": 0,
        "validation_status": "not_run",
        "commit_status": "not_run",
        "context_snapshot_used": False,
        "provenance_slot_count": 0,
        "elapsed_ms": 0,
    }


def _observer(runtime_state):
    if not isinstance(runtime_state, dict):
        return None
    return runtime_state.setdefault(
        "memory_engine_memorize_observer",
        {"memorize_request_count": 0, "last_result": {}},
    )


def memorize_observer_snapshot(runtime_state=None):
    state = (runtime_state or {}).get("memory_engine_memorize_observer") or {}
    return {
        "memorize_request_count": int(state.get("memorize_request_count") or 0),
        "last_result": dict(state.get("last_result") or {}),
    }


def _record(
    runtime_state,
    result,
    metrics,
    reason_code,
    *,
    observer_state=None,
    memorize_id=None,
    started_at=None,
    count_request=True,
):
    observer = _observer(observer_state if observer_state is not None else runtime_state)
    if observer is None:
        return
    if count_request:
        observer["memorize_request_count"] = int(observer.get("memorize_request_count") or 0) + 1
    observer["last_result"] = {
        "memorize_id": memorize_id,
        "status": result.get("status"),
        "reason_code": reason_code,
        "started_at": started_at,
        "completed_at": time.time(),
        **{key: metrics.get(key) for key in metrics},
    }


def _begin_async_observer(runtime_state, memorize_id, started_at):
    observer = _observer(runtime_state)
    if observer is None:
        return
    observer["memorize_request_count"] = int(observer.get("memorize_request_count") or 0) + 1
    observer["last_result"] = {
        "memorize_id": memorize_id,
        "status": "running",
        "reason_code": "in_progress",
        "started_at": started_at,
        "completed_at": None,
    }


def _extract_json(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("terminal_result_invalid")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("terminal_result_invalid")
    return parsed


def _tool_arguments(tool_call):
    raw = (tool_call.get("function") or {}).get("arguments", "{}")
    try:
        value = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _history_message(message, tool_calls=None):
    result = {"role": "assistant", "content": message.get("content") or ""}
    if tool_calls is not None:
        result["tool_calls"] = tool_calls
    if "reasoning_content" in message:
        result["reasoning_content"] = message.get("reasoning_content") or ""
    return result


def _call(messages, timeout_seconds, trajectory_context=None):
    observer = provider_exchange_observer(trajectory_context, purpose="ltm memorize", agent_type="memorize")
    response = call_chat_completion(
        messages,
        tools=ltm_retrieval.FORMATION_RETRIEVAL_TOOL_SCHEMAS,
        temperature=0,
        timeout_seconds=timeout_seconds,
        purpose="ltm memorize",
        exchange_observer=observer,
    )
    return response["choices"][0]["message"]


def _projection(instruction, context_snapshot, evidence_projection, roots):
    return {
        "user_instruction": instruction,
        "context_snapshot": context_snapshot_for_agent(context_snapshot) if context_snapshot else [],
        "evidence_projection": evidence_projection,
        "system_root_tags": [
            {"object_id": item["object_id"], "tag_key": item["tag_key"], "title": item["title"]}
            for item in roots
        ],
    }


def _decision(projection, runtime_state, metrics, check_cancel=None):
    deadline = time.monotonic() + FORMATION_TIMEOUT_SECONDS

    def check_runtime():
        if check_cancel:
            try:
                check_cancel()
            except Exception as error:
                raise MemorizeError("cancelled", str(error)) from error
        if time.monotonic() >= deadline:
            raise MemorizeError("timeout", "Memorize exceeded its 60 second deadline.")

    def remaining_seconds():
        check_runtime()
        return max(1, int(deadline - time.monotonic() + 0.999))

    prompt = [
        {"role": "system", "content": load_prompt("memorize")},
        {"role": "user", "content": json.dumps(projection, ensure_ascii=False)},
    ]
    responses = runtime_state.get("eval_memorize_responses") if isinstance(runtime_state, dict) else None
    response_index = 0
    for _ in range(FORMATION_MAX_MODEL_ROUNDS):
        check_runtime()
        if isinstance(responses, list):
            if response_index >= len(responses):
                raise RuntimeError("model_error")
            message = responses[response_index]
            response_index += 1
            message = {"role": "assistant", **message} if isinstance(message, dict) else {"role": "assistant", "content": str(message)}
        else:
            message = _call(prompt, remaining_seconds(), (runtime_state or {}).get("_memorize_trajectory_context"))
        metrics["model_round_count"] += 1
        check_runtime()
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            normalized = []
            for index, raw in enumerate(tool_calls):
                function = raw.get("function") if isinstance(raw, dict) else {}
                normalized.append(
                    {
                        "id": str((raw or {}).get("id") or f"memorize_{metrics['retrieval_call_count'] + index + 1}"),
                        "type": "function",
                        "function": {
                            "name": str((function or {}).get("name") or ""),
                            "arguments": (function or {}).get("arguments") or "{}",
                        },
                    }
                )
            prompt.append(_history_message(message, tool_calls=normalized))
            for tool_call in normalized:
                check_runtime()
                if metrics["retrieval_call_count"] >= FORMATION_MAX_TOOL_CALLS:
                    raise RuntimeError("tool_limit")
                metrics["retrieval_call_count"] += 1
                tool_name = (tool_call.get("function") or {}).get("name") or "unknown"
                tool_span = start_span((runtime_state or {}).get("_memorize_trajectory_context"), "tool_call", tool_name, metadata={"tool_call_id": tool_call.get("id")})
                args = _tool_arguments(tool_call)
                if args is None:
                    result = {
                        "status": "error",
                        "items": [],
                        "next_cursor": None,
                        "truncation_reason": None,
                        "errors": [{"code": "invalid_arguments", "path": "arguments", "message": "arguments must be an object."}],
                    }
                else:
                    result = ltm_retrieval.run_formation_retrieval_tool(
                        tool_name,
                        args,
                    )
                event_status = "failed" if result.get("status") == "error" else "completed"
                record_event(tool_span, "tool.completed" if event_status == "completed" else "tool.failed", status=event_status, summary={"tool": tool_name}, artifacts={"raw_tool_arguments": (tool_call.get("function") or {}).get("arguments") or "", "tool_arguments": args or {}, "tool_result": result})
                if event_status == "completed":
                    complete_span(tool_span)
                else:
                    fail_span(tool_span, RuntimeError(json.dumps(result, ensure_ascii=False)))
                status = result.get("status")
                if status in {"ok", "partial"}:
                    metrics["retrieval_success_count"] += 1
                if status == "partial":
                    metrics["retrieval_partial_count"] += 1
                if status == "error":
                    metrics["retrieval_error_count"] += 1
                prompt.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
                    }
                )
                check_runtime()
            continue

        result = _extract_json(message.get("content"))
        action = str(result.get("action") or "").strip()
        if action == "no_memory":
            return {"action": "no_memory", "objects": []}
        if action == "write_plan" and isinstance(result.get("objects"), list):
            if metrics["retrieval_success_count"] < 1:
                prompt.append(_history_message(message))
                prompt.append({"role": "user", "content": "Before write_plan, complete at least one successful Structured Retrieval call."})
                continue
            return {"action": "write_plan", "objects": [item for item in result["objects"] if isinstance(item, dict)]}
        raise RuntimeError("terminal_result_invalid")
    raise RuntimeError("model_error")


def _latest_evidence(messages, root=None):
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("provenance_unavailable")
    latest_user_index = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), None)
    if latest_user_index is None:
        raise RuntimeError("provenance_unavailable")
    latest_user = dict(messages[latest_user_index])
    previous_assistant_index = (
        latest_user_index - 1
        if latest_user_index > 0 and messages[latest_user_index - 1].get("role") == "assistant"
        else None
    )
    previous_assistant = dict(messages[previous_assistant_index]) if previous_assistant_index is not None else None
    stored = event_stream.load_messages(root)
    if len(stored) < len(messages) or stored[: len(messages)] != messages:
        raise RuntimeError("provenance_unavailable")
    provenance_slots = [latest_user_index]
    if previous_assistant_index is not None:
        provenance_slots.append(previous_assistant_index)
    return {
        "projection": {
            "latest_user_message": latest_user,
            **({"immediately_previous_assistant_message": previous_assistant} if previous_assistant is not None else {}),
        },
        "slots": provenance_slots,
    }


def _normalized_plan(decision, provenance_slots, roots):
    if decision["action"] == "no_memory":
        return None
    item = {
        "source_kind": "memorize",
        "pending_slot_ranges": json.dumps([{"start": slot, "end": slot} for slot in provenance_slots]),
    }
    return formation._normalized_write_plan(item, decision, roots)


def _run_memorize_impl(
    instruction,
    messages=None,
    context_snapshot=None,
    runtime_state=None,
    check_cancel=None,
    *,
    observer_state=None,
    memorize_id=None,
    started_at=None,
):
    metrics = _metrics()
    started = time.perf_counter()
    result = None
    reason_code = "memorize_error"
    if not isinstance(instruction, str) or not instruction.strip():
        result = {"status": "rejected", "reason_code": "invalid_request"}
        reason_code = "invalid_request"
        return result
    try:
        evidence = _latest_evidence(messages or event_stream.load_messages())
        bootstrap = meta_memory.bootstrap_system_tags()
        if bootstrap.get("status") == "failed":
            result = {"status": "failed", "reason_code": "bootstrap_failed"}
            reason_code = "bootstrap_failed"
            return result
        roots = meta_memory.system_owned_tags()
        if len(roots) != len(meta_memory.SYSTEM_TAG_DEFINITIONS):
            result = {"status": "failed", "reason_code": "bootstrap_failed"}
            reason_code = "bootstrap_failed"
            return result
        metrics["context_snapshot_used"] = context_snapshot is not None
        metrics["provenance_slot_count"] = len(evidence["slots"])
        projection = _projection(instruction, context_snapshot, evidence["projection"], roots)
        decision = _decision(
            projection,
            runtime_state or {},
            metrics,
            check_cancel=check_cancel,
        )
        try:
            plan = _normalized_plan(decision, evidence["slots"], roots)
        except RuntimeError:
            result = {"status": "rejected", "reason_code": "invalid_plan"}
            reason_code = "invalid_plan"
            metrics["validation_status"] = "failed"
            metrics["commit_status"] = "not_run"
            return result
        if plan is None:
            result = {"status": "no_memory", "reason_code": "not_durable"}
            reason_code = "not_durable"
            metrics["validation_status"] = "not_applicable"
            metrics["commit_status"] = "not_applicable"
        else:
            metrics["validation_status"] = "passed"
            digest = hashlib.sha256(
                json.dumps({"instruction": instruction, "slots": evidence["slots"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            commit = ltm_store.commit_write_plan(plan, f"memorize:{digest}")
            metrics["commit_status"] = commit.get("status")
            if commit.get("status") not in {"committed", "already_committed"}:
                result = {"status": "rejected", "reason_code": "invalid_plan"}
                reason_code = "invalid_plan"
                return result
            result = {"status": "committed", "reason_code": "stored"}
            reason_code = "stored"
        return result
    except MemorizeError as error:
        reason = error.code
        mapping = {
            "provenance_unavailable": ("failed", "provenance_unavailable"),
            "tool_limit": ("failed", "tool_limit"),
            "model_error": ("failed", "model_error"),
            "terminal_result_invalid": ("failed", "invalid_result"),
            "timeout": ("failed", "timeout"),
            "cancelled": ("failed", "cancelled"),
        }
        status, reason_code = mapping.get(reason, ("failed", "memorize_error"))
        result = {"status": status, "reason_code": reason_code}
        return result
    except RuntimeError as error:
        reason = str(error)
        mapping = {
            "provenance_unavailable": ("failed", "provenance_unavailable"),
            "tool_limit": ("failed", "tool_limit"),
            "model_error": ("failed", "model_error"),
            "terminal_result_invalid": ("failed", "invalid_result"),
        }
        status, reason_code = mapping.get(reason, ("failed", "memorize_error"))
        result = {"status": status, "reason_code": reason_code}
        return result
    except Exception:
        result = {"status": "failed", "reason_code": "memorize_error"}
        reason_code = "memorize_error"
        return result
    finally:
        metrics["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        _record(
            runtime_state,
            result or {"status": "failed", "reason_code": reason_code},
            metrics,
            reason_code,
            observer_state=observer_state,
            memorize_id=memorize_id,
            started_at=started_at,
            count_request=observer_state is None,
        )


from .storage_gate import memory_task


@memory_task
def run_memorize(instruction, messages=None, context_snapshot=None, runtime_state=None, check_cancel=None):
    if isinstance(runtime_state, dict) and callable(runtime_state.get("wake_callback")):
        memorize_id = "memorize_task_" + uuid.uuid4().hex
        started_at = time.time()
        message_snapshot = copy.deepcopy(messages if isinstance(messages, list) else event_stream.load_messages())
        context_snapshot_copy = copy.deepcopy(context_snapshot)
        wake_callback = runtime_state["wake_callback"]
        _begin_async_observer(runtime_state, memorize_id, started_at)

        def worker():
            emit_ui_event(runtime_state, "memorize_started", path="background", memorize_id=memorize_id)
            task_state = dict(runtime_state)
            task_state.pop("wake_callback", None)
            task_state.pop("_memorize_trajectory_context", None)
            caller = task_state.get("trajectory_context")
            context = None
            result = {"status": "failed", "reason_code": "memorize_error"}
            try:
                context = start_trajectory(
                    "memorize",
                    caller=caller,
                    metadata={"memorize_mode": "memorize", "memorize_id": memorize_id},
                )
                task_state["_memorize_trajectory_context"] = context
                result = _run_memorize_impl(
                    instruction,
                    message_snapshot,
                    context_snapshot_copy,
                    task_state,
                    None,
                    observer_state=runtime_state,
                    memorize_id=memorize_id,
                    started_at=started_at,
                )
                observer = _observer(runtime_state) or {}
                if (observer.get("last_result") or {}).get("status") == "running":
                    _record(
                        task_state,
                        result,
                        _metrics(),
                        result.get("reason_code") or "memorize_error",
                        observer_state=runtime_state,
                        memorize_id=memorize_id,
                        started_at=started_at,
                        count_request=False,
                    )
                if context is not None:
                    record_event(
                        context,
                        "memorize.completed",
                        status="failed" if result.get("status") in {"failed", "rejected"} else "completed",
                        summary=result,
                        artifacts={"result": result},
                    )
                    if result.get("status") in {"failed", "rejected"}:
                        fail_trajectory(
                            context,
                            RuntimeError(result.get("reason_code") or "Memorize failed."),
                            metadata={"memorize_mode": "memorize", "memorize_id": memorize_id},
                        )
                    else:
                        complete_trajectory(
                            context,
                            metadata={"memorize_mode": "memorize", "memorize_id": memorize_id},
                        )
            except Exception:
                _record(
                    task_state,
                    result,
                    {"model_round_count": 0, "retrieval_call_count": 0, "retrieval_success_count": 0,
                     "retrieval_error_count": 0, "retrieval_partial_count": 0, "validation_status": "not_run",
                     "commit_status": "not_run", "context_snapshot_used": context_snapshot_copy is not None,
                     "provenance_slot_count": 0, "elapsed_ms": 0},
                    result["reason_code"],
                    observer_state=runtime_state,
                    memorize_id=memorize_id,
                    started_at=started_at,
                    count_request=False,
                )
                try:
                    if context is not None:
                        fail_trajectory(
                            context,
                            RuntimeError(result["reason_code"]),
                            metadata={"memorize_mode": "memorize", "memorize_id": memorize_id},
                        )
                except Exception:
                    pass
            finally:
                emit_ui_event(
                    runtime_state,
                    "memorize_finished",
                    path="background",
                    memorize_id=memorize_id,
                    status=str(result.get("status") or "failed"),
                    error=str(result.get("reason_code") or "") if result.get("status") in {"failed", "rejected"} else "",
                )
                task_state.pop("_memorize_trajectory_context", None)
                try:
                    wake_callback(
                        "memorize",
                        "completion",
                        {
                            "memorize_id": memorize_id,
                            "status": result.get("status"),
                            "reason_code": result.get("reason_code") or "memorize_error",
                        },
                        processing_class="tool_result_like",
                        wake_id="wake_" + memorize_id,
                    )
                except Exception:
                    pass

        threading.Thread(target=worker, name="memorize-worker", daemon=True).start()
        return {"status": "running", "memorize_id": memorize_id}

    started_at = time.time()
    caller = (runtime_state or {}).get("trajectory_context")
    context = start_trajectory("memorize", caller=caller, metadata={"memorize_mode": "memorize"})
    if isinstance(runtime_state, dict):
        runtime_state["_memorize_trajectory_context"] = context
    try:
        result = _run_memorize_impl(
            instruction,
            messages,
            context_snapshot,
            runtime_state,
            check_cancel,
            started_at=started_at,
        )
        record_event(context, "memorize.completed", status="failed" if result.get("status") in {"failed", "rejected"} else "completed", summary=result, artifacts={"result": result})
        if result.get("status") in {"failed", "rejected"}:
            fail_trajectory(context, RuntimeError(result.get("reason_code") or "Memorize failed."), metadata={"memorize_mode": "memorize"})
        else:
            complete_trajectory(context, metadata={"memorize_mode": "memorize"})
        return result
    except Exception as error:
        fail_trajectory(context, error, metadata={"memorize_mode": "memorize"})
        raise
    finally:
        if isinstance(runtime_state, dict):
            runtime_state.pop("_memorize_trajectory_context", None)
