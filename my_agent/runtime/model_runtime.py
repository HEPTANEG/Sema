import time
import uuid
import re

from memory_engine.context_budget_override import effective_budget_ranges
from memory_engine.dynamic_context_budget import STM_MAX_TOKENS, STM_MIN_TOKENS, WM_MAX_TOKENS, WM_MIN_TOKENS
from persistence.trajectory import complete_span, fail_span, provider_exchange_observer, start_span
from runtime.dependency_validation import validate_dependencies


STM_CONTEXT_BUILD_ERROR_MARKER = "STM latest interaction block exceeds budget"
CONTEXT_BUILDER_ERROR_MARKER = "Context Builder WM slot exceeds budget"


MODEL_PAYLOAD_REQUIRED_DEPENDENCIES = (
    "build_chat_payload",
    "messages_for_model",
    "messages_with_runtime_directives",
    "trace_retrieval_plan",
)
MODEL_CALL_REQUIRED_DEPENDENCIES = MODEL_PAYLOAD_REQUIRED_DEPENDENCIES + (
    "MODEL_TIMEOUT_SECONDS",
    "ToolRunStopped",
    "call_chat_completion",
    "call_chat_completion_stream",
    "check_cancel_requested",
    "parse_chat_completion_message",
    "ui_event",
)
MODEL_RETRY_REQUIRED_DEPENDENCIES = (
    "MAX_MODEL_RETRIES_PER_FAILURE_POINT",
    "call_model_with_tools",
    "check_cancel_requested",
    "model_status",
    "trace_event",
    "ui_event",
)


def model_timeout_message(dependencies):
    validate_dependencies(dependencies, ("MODEL_TIMEOUT_SECONDS", "model_label"), "model_runtime.model_timeout_message")
    return (
        f"{dependencies['model_label']()} request timed out after {dependencies['MODEL_TIMEOUT_SECONDS']} seconds. "
        "The model API did not return in time. Please retry, or reduce context if this keeps happening."
    )


def build_model_tools_payload(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        MODEL_PAYLOAD_REQUIRED_DEPENDENCIES,
        "model_runtime.build_model_tools_payload",
    )
    if "tool_schemas_for_model" not in dependencies:
        validate_dependencies(dependencies, ("TOOL_SCHEMAS",), "model_runtime.build_model_tools_payload")
    retrieval_plan = None
    dependencies["trace_retrieval_plan"](runtime_state, retrieval_plan)
    outbound_messages = _messages_for_model_with_preflight_recovery(messages, retrieval_plan, runtime_state, dependencies)
    tools = dependencies.get("tool_schemas_for_model", lambda _runtime_state=None: dependencies["TOOL_SCHEMAS"])(runtime_state)
    return dependencies["build_chat_payload"](outbound_messages, tools=tools)


def call_model_with_tools(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        MODEL_CALL_REQUIRED_DEPENDENCIES,
        "model_runtime.call_model_with_tools",
    )
    if "tool_schemas_for_model" not in dependencies:
        validate_dependencies(dependencies, ("TOOL_SCHEMAS",), "model_runtime.call_model_with_tools")
    streaming_enabled = (runtime_state or {}).get("streaming_enabled")
    message_id = (runtime_state or {}).get("assistant_message_id") or f"assistant-{uuid.uuid4().hex}"
    if runtime_state is not None and message_id:
        runtime_state["assistant_message_id"] = message_id
    retrieval_plan = None
    dependencies["trace_retrieval_plan"](runtime_state, retrieval_plan)
    outbound_messages = _messages_for_model_with_preflight_recovery(messages, retrieval_plan, runtime_state, dependencies)
    preparation_parent = (runtime_state or {}).get("trajectory_context")
    preparation_span = _start_preparation_span(preparation_parent, runtime_state)
    try:
        with _preparation_stage(preparation_span, "tool_schema_build"):
            tools = dependencies.get("tool_schemas_for_model", lambda _runtime_state=None: dependencies["TOOL_SCHEMAS"])(runtime_state)
    except Exception as error:
        _finish_preparation_span(preparation_span, error)
        raise
    else:
        _finish_preparation_span(preparation_span)
    trajectory_parent = (runtime_state or {}).get("trajectory_context")
    if streaming_enabled:
        dependencies["ui_event"](runtime_state, "assistant_stage", path="structured", message_id=message_id, label="Finalizing context...")
        streamed_any_delta = False

        def emit_delta(delta):
            nonlocal streamed_any_delta
            streamed_any_delta = True
            dependencies["ui_event"](runtime_state, "assistant_delta", path="structured", message_id=message_id, delta=delta)

        try:
            dependencies["ui_event"](runtime_state, "assistant_stage", path="structured", message_id=message_id, label="Waiting for the model...")
            observer = provider_exchange_observer(
                trajectory_parent,
                purpose="tool calling",
                metadata=_provider_exchange_metadata(runtime_state, message_id, streaming=True),
                agent_type="main_agent",
            )
            assistant_message = dependencies["call_chat_completion_stream"](
                outbound_messages,
                tools=tools,
                timeout_seconds=dependencies["MODEL_TIMEOUT_SECONDS"],
                purpose="tool calling",
                on_delta=emit_delta,
                check_cancel=lambda: dependencies["check_cancel_requested"](runtime_state),
                exchange_observer=observer,
                exchange_metadata=_provider_exchange_metadata(runtime_state, message_id, streaming=True),
            )
            assistant_message["message_id"] = message_id
            assistant_message["_provider_context_messages"] = outbound_messages
            return assistant_message
        except dependencies["ToolRunStopped"]:
            raise
        except RuntimeError:
            if streamed_any_delta:
                raise


    try:
        observer = provider_exchange_observer(
            trajectory_parent,
            purpose="tool calling",
            metadata=_provider_exchange_metadata(runtime_state, message_id, streaming=False),
            agent_type="main_agent",
        )
        data = dependencies["call_chat_completion"](
            outbound_messages,
            tools=tools,
            timeout_seconds=dependencies["MODEL_TIMEOUT_SECONDS"],
            purpose="tool calling",
            exchange_observer=observer,
            exchange_metadata=_provider_exchange_metadata(runtime_state, message_id, streaming=False),
        )
    except TimeoutError as error:
        raise RuntimeError(model_timeout_message(dependencies)) from error

    assistant_message = parse_model_tool_message(data, dependency_source=dependencies)
    assistant_message["_provider_context_messages"] = outbound_messages
    if streaming_enabled:
        assistant_message["message_id"] = message_id
    return assistant_message


def _provider_exchange_metadata(runtime_state, message_id, streaming=False):
    return {
        "purpose": "tool calling",
        "streaming": bool(streaming),
        "trajectory_id": ((runtime_state or {}).get("trajectory_context") or {}).get("trajectory_id") or "",
        "assistant_message_id": message_id or "",
        "retry": int((runtime_state or {}).get("_model_retry_index") or 0),
    }


def _start_preparation_span(parent, runtime_state):
    if not isinstance(runtime_state, dict) or not runtime_state.get("_main_agent_context_timing_enabled"):
        return None
    if not (parent or {}).get("enabled"):
        return None
    return start_span(
        parent,
        "provider_preparation",
        "Main Agent provider preparation",
        metadata={"temporary_diagnostic": "main_agent_context_timing_v1"},
    )


class _PreparationStage:
    def __init__(self, parent, name):
        self.parent = parent
        self.name = name
        self.span = None

    def __enter__(self):
        if (self.parent or {}).get("enabled"):
            self.span = start_span(
                self.parent,
                "provider_preparation_stage",
                self.name,
                metadata={"temporary_diagnostic": "main_agent_context_timing_v1"},
            )
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        if self.span is not None:
            metadata = {"temporary_diagnostic": "main_agent_context_timing_v1"}
            if exc_value is None:
                complete_span(self.span, metadata=metadata)
            else:
                fail_span(self.span, exc_value, metadata=metadata)
        return False


def _preparation_stage(parent, name):
    return _PreparationStage(parent, name)


def _finish_preparation_span(span, error=None):
    if span is None:
        return
    metadata = {"temporary_diagnostic": "main_agent_context_timing_v1"}
    if error is None:
        complete_span(span, metadata=metadata)
    else:
        fail_span(span, error, metadata=metadata)


def parse_model_tool_message(data, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        ("parse_chat_completion_message",),
        "model_runtime.parse_model_tool_message",
    )
    return dependencies["parse_chat_completion_message"](data)


def _messages_for_model_with_preflight_recovery(messages, retrieval_plan, runtime_state, dependencies):
    model_messages = _run_preparation_stage(
        runtime_state,
        "system_prompt_refresh",
        lambda: _messages_with_current_system_prompt(messages, dependencies),
    )

    def build_once():
        directed_messages = _run_preparation_stage(
            runtime_state,
            "runtime_directives",
            lambda: dependencies["messages_with_runtime_directives"](model_messages, runtime_state),
        )
        return dependencies["messages_for_model"](
            directed_messages,
            retrieval_plan=retrieval_plan,
            runtime_state=runtime_state,
        )

    try:
        return build_once()
    except RuntimeError as error:
        if not is_context_build_error(error):
            raise
        failure = apply_context_build_failure_pressure(error, runtime_state)
        try:
            if not failure.get("needs_preflight_recovery"):
                return build_once()
            if not dependencies.get("attempt_preflight_working_memory_recovery"):
                raise
            result = dependencies["attempt_preflight_working_memory_recovery"](error, messages, runtime_state)
            if not result.get("triggered"):
                raise
            return build_once()
        finally:
            clear_context_build_failure_pressure(runtime_state)


def _messages_with_current_system_prompt(messages, dependencies):
    """Refresh the transient provider prompt without rewriting append-only history."""
    create_initial_messages = dependencies.get("create_initial_messages")
    if not create_initial_messages:
        return messages

    initial_messages = create_initial_messages()
    if not isinstance(initial_messages, list) or not initial_messages:
        return messages
    current_system = initial_messages[0]
    if not isinstance(current_system, dict) or current_system.get("role") != "system":
        return messages

    existing = list(messages or [])
    if existing and existing[0].get("role") == "system":
        return [dict(current_system), *existing[1:]]
    return [dict(current_system), *existing]


def _run_preparation_stage(runtime_state, name, callback):
    """Time preparation that occurs around, but not inside, Context Builder."""
    parent = (runtime_state or {}).get("trajectory_context")
    span = _start_preparation_stage(parent, runtime_state, name)
    try:
        result = callback()
    except Exception as error:
        _finish_preparation_stage(span, error)
        raise
    else:
        _finish_preparation_stage(span)
        return result


def _start_preparation_stage(parent, runtime_state, name):
    if not isinstance(runtime_state, dict) or not runtime_state.get("_main_agent_context_timing_enabled"):
        return None
    if not (parent or {}).get("enabled"):
        return None
    return start_span(
        parent,
        "provider_preparation_stage",
        name,
        metadata={"temporary_diagnostic": "main_agent_context_timing_v1"},
    )


def _finish_preparation_stage(span, error=None):
    if span is None:
        return
    metadata = {"temporary_diagnostic": "main_agent_context_timing_v1"}
    if error is None:
        complete_span(span, metadata=metadata)
    else:
        fail_span(span, error, metadata=metadata)


def is_recoverable_model_error(error, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        ("RECOVERABLE_MODEL_ERROR_MARKERS",),
        "model_runtime.is_recoverable_model_error",
    )
    if is_context_build_error(error):
        return True
    error_text = str(error)
    return any(marker in error_text for marker in dependencies["RECOVERABLE_MODEL_ERROR_MARKERS"])


def is_context_limit_model_error(error, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        ("CONTEXT_LIMIT_ERROR_MARKERS",),
        "model_runtime.is_context_limit_model_error",
    )
    if is_context_build_error(error):
        return True
    error_text = str(error)
    return any(marker in error_text for marker in dependencies["CONTEXT_LIMIT_ERROR_MARKERS"])


def is_context_build_error(error):
    error_text = str(error)
    return (
        STM_CONTEXT_BUILD_ERROR_MARKER in error_text
        or CONTEXT_BUILDER_ERROR_MARKER in error_text
    )


def context_build_failure_details(error):
    error_text = str(error)
    if STM_CONTEXT_BUILD_ERROR_MARKER in error_text:
        match = re.search(r"latest_block_tokens=(\d+), budget=(\d+)", error_text)
        if not match:
            return {}
        required_tokens = int(match.group(1))
        failed_budget = int(match.group(2))
        max_budget = _effective_max_budget("stm")
        return {
            "target": "stm",
            "required_tokens": required_tokens,
            "failed_budget": failed_budget,
            "max_budget": max_budget,
            "needs_preflight_recovery": required_tokens > max_budget,
        }
    if CONTEXT_BUILDER_ERROR_MARKER in error_text:
        match = re.search(r"tokens=(\d+), budget=(\d+)", error_text)
        if not match:
            return {}
        required_tokens = int(match.group(1))
        failed_budget = int(match.group(2))
        max_budget = _effective_max_budget("wm")
        return {
            "target": "wm",
            "required_tokens": required_tokens,
            "failed_budget": failed_budget,
            "max_budget": max_budget,
            "needs_preflight_recovery": required_tokens > max_budget,
        }
    return {}


def _effective_max_budget(target):
    try:
        ranges = effective_budget_ranges(
            {
                "stm_min": STM_MIN_TOKENS,
                "stm_max": STM_MAX_TOKENS,
                "wm_min": WM_MIN_TOKENS,
                "wm_max": WM_MAX_TOKENS,
            }
        )
    except Exception:
        ranges = {"stm_max": STM_MAX_TOKENS, "wm_max": WM_MAX_TOKENS}
    return int(ranges.get(f"{target}_max") or (STM_MAX_TOKENS if target == "stm" else WM_MAX_TOKENS))


def apply_context_build_failure_pressure(error, runtime_state=None):
    details = context_build_failure_details(error)
    if runtime_state is not None and details:
        runtime_state["context_build_failure"] = {
            "target": details["target"],
            "required_tokens": details["required_tokens"],
            "failed_budget": details["failed_budget"],
            "max_budget": details["max_budget"],
        }
    return details


def clear_context_build_failure_pressure(runtime_state=None):
    if runtime_state is not None:
        runtime_state.pop("context_build_failure", None)


def context_build_error_message(error):
    if CONTEXT_BUILDER_ERROR_MARKER in str(error):
        return (
            f"Context build failed: {error}\n\n"
            "Sema did not send this overlarge working-memory slot to the model. "
            "Context Builder V1 keeps complete WM slots only; it does not truncate slots. "
            "Continue with a shorter message, or let Working Memory summarize the oversized older slot."
        )
    return (
        f"Context build failed: {error}\n\n"
        "Sema did not send this overlarge interaction block to the model. "
        "STM V1 keeps complete interaction blocks only; it does not truncate messages or split a block. "
        "Send a shorter input, or continue with a new shorter message so the oversized block can be omitted from the next provider context."
    )


def recoverable_model_error_message(error):
    if is_context_build_error(error):
        return context_build_error_message(error)
    return (
        f"Model request failed: {error}\n\n"
        "Sema kept the current conversation and tool results. You can ask me to continue, "
        "or retry after the provider recovers."
    )


def call_model_with_structured_retries(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        MODEL_RETRY_REQUIRED_DEPENDENCIES,
        "model_runtime.call_model_with_structured_retries",
    )
    last_error = None
    for retry_index in range(dependencies["MAX_MODEL_RETRIES_PER_FAILURE_POINT"] + 1):
        dependencies["check_cancel_requested"](runtime_state)
        if isinstance(runtime_state, dict):
            runtime_state["_model_retry_index"] = retry_index
        model_started_at = time.perf_counter()
        dependencies["ui_event"](runtime_state, "model_thinking", path="structured")
        try:
            with dependencies["model_status"]():
                assistant_message = dependencies["call_model_with_tools"](messages, runtime_state=runtime_state)
        except RuntimeError as error:
            if not is_recoverable_model_error(error, dependency_source=dependencies):
                raise
            last_error = error
            dependencies["trace_event"](
                runtime_state,
                "main_agent.model_round.failed",
                status="error",
                path="structured",
                duration_ms=round((time.perf_counter() - model_started_at) * 1000),
                retry=retry_index,
                error=str(error),
            )
            dependencies["ui_event"](runtime_state, "model_error", path="structured", retry=retry_index, error=str(error))
            if is_context_limit_model_error(error, dependency_source=dependencies):
                return {
                    "content": recoverable_model_error_message(error),
                    "tool_calls": [],
                    "model_error": str(error),
                }
            if retry_index < dependencies["MAX_MODEL_RETRIES_PER_FAILURE_POINT"]:
                continue
            return {
                "content": recoverable_model_error_message(error),
                "tool_calls": [],
                "model_error": str(error),
            }

        dependencies["ui_event"](
            runtime_state,
            "model_finished",
            path="structured",
            tool_calls=len(assistant_message.get("tool_calls") or []),
            content_chars=len(assistant_message.get("content") or ""),
        )
        dependencies["trace_event"](
            runtime_state,
            "main_agent.model_round.completed",
            status="ok",
            path="structured",
            duration_ms=round((time.perf_counter() - model_started_at) * 1000),
            tool_calls=len(assistant_message.get("tool_calls") or []),
            content_chars=len(assistant_message.get("content") or ""),
        )
        return assistant_message
    raise RuntimeError(last_error or "Model request failed.")


def assistant_history_message(assistant_message, tool_calls=None):

    message = {
        "role": "assistant",
        "content": assistant_message.get("content") or "",
    }
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    if "reasoning_content" in assistant_message:
        message["reasoning_content"] = assistant_message.get("reasoning_content") or ""
    return message
