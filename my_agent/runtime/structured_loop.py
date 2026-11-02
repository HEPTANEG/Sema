import subprocess
import time
from contextlib import nullcontext

from runtime.context_snapshot import capture_context_snapshot
from runtime.dependency_validation import validate_dependencies


STRUCTURED_LOOP_REQUIRED_DEPENDENCIES = (
    "MAX_RETRIES_PER_FAILURE_POINT",
    "append_message_and_persist",
    "assistant_history_message",
    "build_structured_tool_result_message",
    "call_model_with_structured_retries",
    "check_cancel_requested",
    "expire_image_context",
    "format_tool_error",
    "finish_trace_span",
    "mark_assistant_turn_finished",
    "pop_last_edit_stats",
    "record_tool_step",
    "render_markdown",
    "render_tool_progress",
    "run_requested_tool",
    "save_last_session",
    "schedule_final_answer_background_tasks",
    "should_show_tool_status",
    "structured_retry_limit_message",
    "start_trace_span",
    "summarize_args",
    "summarize_tool_result",
    "tool_call_to_request",
    "tool_failure_key",
    "tool_status",
    "trace_event",
    "trace_span_event",
    "ui_event",
)


def run_structured_loop(messages, permissions=None, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        STRUCTURED_LOOP_REQUIRED_DEPENDENCIES,
        "structured_loop.run_structured_loop",
    )
    failure_counts = {}
    while True:
        dependencies["check_cancel_requested"](runtime_state)
        assistant_message = dependencies["call_model_with_structured_retries"](messages, runtime_state)
        provider_context_messages = assistant_message.pop("_provider_context_messages", None)
        content = assistant_message.get("content") or ""
        tool_calls = assistant_message.get("tool_calls") or []
        message_id = assistant_message.get("message_id")

        if not tool_calls:
            dependencies["append_message_and_persist"](
                messages,
                dependencies["assistant_history_message"](assistant_message),
            )
            dependencies["ui_event"](
                runtime_state,
                "final_answer",
                path="structured",
                content=content,
                content_chars=len(content),
                message_id=message_id,
            )
            dependencies["render_markdown"](content)
            expired_images = dependencies["expire_image_context"](messages)
            if expired_images:
                dependencies["trace_event"](
                    runtime_state,
                    "image_context_expired",
                    status="ok",
                    path="structured",
                    count=expired_images,
                )
            dependencies["save_last_session"](messages)
            dependencies["mark_assistant_turn_finished"](runtime_state)
            dependencies["schedule_final_answer_background_tasks"](messages, runtime_state)
            dependencies["trace_event"](runtime_state, "final", status="ok", path="structured", content_chars=len(content))
            return

        for tool_call in tool_calls:
            if not tool_call.get("id"):
                raise RuntimeError("Structured tool call missing tool_call id.")

        current_context_retrieval_ids = set()
        memorize_ids = set()
        for tool_call in tool_calls:
            try:
                request = dependencies["tool_call_to_request"](tool_call)
            except RuntimeError:
                continue
            args = request.get("args") if isinstance(request.get("args"), dict) else {}
            if request.get("tool") == "retrieve_memory" and args.get("include_context") is True:
                current_context_retrieval_ids.add(tool_call["id"])
            if request.get("tool") == "memorize":
                memorize_ids.add(tool_call["id"])
        context_snapshot = None
        if (current_context_retrieval_ids or memorize_ids) and provider_context_messages is not None:
            context_snapshot = capture_context_snapshot(provider_context_messages)

        dependencies["append_message_and_persist"](
            messages,
            dependencies["assistant_history_message"](assistant_message, tool_calls=tool_calls),
        )
        if content and not ((runtime_state or {}).get("streaming_enabled") and message_id):
            dependencies["ui_event"](
                runtime_state,
                "assistant_content",
                path="structured",
                content=content,
                content_chars=len(content),
                message_id=message_id,
            )
            dependencies["render_markdown"](content)
        elif content:
            dependencies["render_markdown"](content)
        for tool_call in tool_calls:
            dependencies["check_cancel_requested"](runtime_state)
            tool_call_id = tool_call.get("id")
            try:
                tool_request = dependencies["tool_call_to_request"](tool_call)
                tool = tool_request["tool"]
                args = tool_request["args"]
                conversion_error = None
            except RuntimeError as error:
                function = tool_call.get("function") or {}
                tool = function.get("name") or "unknown_tool"
                args = "(invalid arguments)"
                tool_request = {"tool": tool, "args": args}
                conversion_error = error

            tool_started_at = time.perf_counter()
            tool_status_value = "ok"
            tool_error = None
            item_id = str(tool_call_id or f"tool-{tool}-{time.time_ns()}")
            dependencies["ui_event"](
                runtime_state,
                "tool_started",
                path="structured",
                tool=tool,
                args=args,
                item_id=item_id,
            )
            tool_trace_span = dependencies["start_trace_span"](
                runtime_state,
                "tool_call",
                tool,
                metadata={"tool": tool, "item_id": item_id},
            )
            dependencies["trace_span_event"](
                tool_trace_span,
                "tool.started",
                status="running",
                tool=tool,
                args_summary=dependencies["summarize_args"](tool, args),
                artifacts={"raw_tool_arguments": (tool_call.get("function") or {}).get("arguments", ""), "tool_arguments": args},
            )

            if conversion_error is not None:
                tool_status_value = "error"
                tool_error = str(conversion_error)
                tool_result = dependencies["format_tool_error"](tool_request, conversion_error)
            else:
                try:
                    status = (
                        dependencies["tool_status"](tool, args)
                        if dependencies["should_show_tool_status"](tool)
                        else nullcontext()
                    )
                    with status:
                        tool_result = dependencies["run_requested_tool"](
                            tool_request,
                            messages=messages,
                            permissions=permissions,
                            runtime_state=runtime_state,
                            context_snapshot=(
                                context_snapshot
                                if tool_call_id in current_context_retrieval_ids or tool_call_id in memorize_ids
                                else None
                            ),
                        )
                except (RuntimeError, subprocess.TimeoutExpired) as error:
                    tool_status_value = "error"
                    tool_error = str(error)
                    key = dependencies["tool_failure_key"](tool_request, error)
                    failure_counts[key] = failure_counts.get(key, 0) + 1
                    if failure_counts[key] > dependencies["MAX_RETRIES_PER_FAILURE_POINT"] + 1:
                        tool_result = dependencies["structured_retry_limit_message"](tool, error)
                        dependencies["trace_event"](
                            runtime_state,
                            "retry_limit",
                            status="error",
                            path="structured",
                            tool=tool,
                            args_summary=dependencies["summarize_args"](tool, args),
                            error=str(error),
                        )
                    else:
                        tool_result = dependencies["format_tool_error"](tool_request, error)

            dependencies["render_tool_progress"](
                tool,
                args,
                tool_result,
                verbose=bool((runtime_state or {}).get("verbose")),
            )
            dependencies["record_tool_step"](runtime_state, tool, args, tool_result)
            edit_stats = dependencies["pop_last_edit_stats"](runtime_state)
            dependencies["ui_event"](
                runtime_state,
                "tool_finished",
                path="structured",
                tool=tool,
                args=args,
                status=tool_status_value,
                item_id=item_id,
                result=tool_result,
                result_chars=len(tool_result),
                error=tool_error or "",
                edit_stats=edit_stats or {},
            )
            trace_summary = dependencies["summarize_tool_result"](tool, tool_result)
            if tool_error:
                trace_summary["error"] = tool_error
            dependencies["trace_span_event"](
                tool_trace_span,
                "tool.failed" if tool_error else "tool.completed",
                status=tool_status_value,
                path="structured",
                tool=tool,
                args_summary=dependencies["summarize_args"](tool, args),
                duration_ms=round((time.perf_counter() - tool_started_at) * 1000),
                artifacts={"tool_arguments": args, "tool_result": tool_result},
                **trace_summary,
            )
            dependencies["finish_trace_span"](
                tool_trace_span,
                RuntimeError(tool_error) if tool_error else None,
                metadata={"tool": tool, "item_id": item_id},
            )
            dependencies["append_message_and_persist"](
                messages,
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": dependencies["build_structured_tool_result_message"](tool, tool_result),
                },
            )
            dependencies["check_cancel_requested"](runtime_state)
