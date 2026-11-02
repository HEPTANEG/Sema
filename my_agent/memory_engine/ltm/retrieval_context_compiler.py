"""LLM query compilation for Memory Push retrieval channels."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import json
import time

from model.provider import call_chat_completion_stream, parse_chat_completion_message
from shared.prompt_loader import load_prompt
from persistence.trajectory import complete_span, fail_span, provider_exchange_observer, record_event, start_span


CHANNEL_ORDER = ("general_semantic", "latest_semantic", "entity")
MAX_QUERIES_PER_CHANNEL = 20
DEFAULT_TIMEOUT_SECONDS = 15


class CompilerError(RuntimeError):
    """Raised when a channel query compiler cannot produce a valid result."""


def _response_content(response):
    if isinstance(response, dict) and isinstance(response.get("content"), str):
        return response["content"]
    return parse_chat_completion_message(response).get("content") or ""


def _copy_messages(messages, name):
    if not isinstance(messages, (list, tuple)):
        raise ValueError(f"{name} must be a message sequence.")
    copied = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError(f"{name} must contain message objects.")
        copied.append(dict(message))
    return copied


def build_channel_contexts(stm_messages, wm_messages):
    """Build the fixed STM-only input projection for each retrieval channel."""
    raw_stm = _copy_messages(stm_messages, "stm_messages")
    _copy_messages(wm_messages, "wm_messages")
    stm = [message for message in raw_stm if message.get("role") != "system"]
    latest_count = (len(stm) + 3) // 4
    latest = stm[-latest_count:] if latest_count else []
    return {
        "general_semantic": [dict(message) for message in stm],
        "latest_semantic": [dict(message) for message in latest],
        "entity": [dict(message) for message in stm],
    }


def _parse_channel_result(raw_content, expected_channel):
    try:
        parsed = json.loads(str(raw_content or "").strip())
    except (TypeError, json.JSONDecodeError) as error:
        raise CompilerError("channel query compiler returned invalid JSON") from error
    if not isinstance(parsed, dict) or set(parsed) != {"channel", "queries"}:
        raise CompilerError("channel query compiler returned an invalid result shape")
    if parsed.get("channel") != expected_channel:
        raise CompilerError("channel query compiler returned the wrong channel name")
    queries = parsed.get("queries")
    if not isinstance(queries, list):
        raise CompilerError("queries must be an array")
    normalized = []
    seen = set()
    for query in queries:
        if not isinstance(query, str) or not query.strip():
            raise CompilerError("queries must contain non-empty strings")
        query = query.strip()
        if query not in seen:
            seen.add(query)
            normalized.append(query)
    return {"channel": expected_channel, "queries": normalized[:MAX_QUERIES_PER_CHANNEL]}


def _compile_channel_queries(channel, context, timeout_seconds, trajectory_context=None, diagnostic_profiler=None):
    channel_span = start_span(trajectory_context, "query_compiler", f"{channel} query compiler")
    prompt_name = f"memory_push_{channel}"
    messages = [
        {"role": "system", "content": load_prompt(prompt_name)},
        {
            "role": "user",
            "content": json.dumps({"context": context}, ensure_ascii=False),
        },
    ]
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
        observer = provider_exchange_observer(channel_span, purpose=f"Memory Push {channel} query compiler", agent_type="memory_push")
        response = call_chat_completion_stream(
            messages,
            tools=None,
            timeout_seconds=max(1, int(timeout_seconds)),
            purpose=f"Memory Push {channel} query compiler",
            on_delta=on_delta,
            on_headers=on_headers,
            exchange_observer=observer,
        )
        provider_timing["complete_ms"] = (time.perf_counter() - provider_started) * 1000
        if provider_timing["first_content_ms"] is not None:
            provider_timing["output_ms"] = provider_timing["complete_ms"] - provider_timing["first_content_ms"]
        validation_started = time.perf_counter()
        result = _parse_channel_result(_response_content(response), channel)
        provider_timing["validation_ms"] = (time.perf_counter() - validation_started) * 1000
        record_event(channel_span, "memory_push.query_compilation.completed", status="completed", summary={"channel": channel, "query_count": len(result["queries"])})
        complete_span(channel_span)
        return result
    except Exception as error:
        record_event(channel_span, "memory_push.query_compilation.failed", status="failed", summary={"channel": channel, "error": str(error)})
        fail_span(channel_span, error)
        raise
    finally:
        if provider_timing["complete_ms"] is None:
            provider_timing["complete_ms"] = (time.perf_counter() - provider_started) * 1000
        if diagnostic_profiler is not None:
            diagnostic_profiler.record_provider_timing("query_compilation", channel, provider_timing)


def compile_retrieval_queries(channel_contexts, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, channels=None, trajectory_context=None, _diagnostic_profiler=None):
    """Run all three query compilers independently within one deadline."""
    if not isinstance(channel_contexts, dict):
        raise ValueError("channel_contexts must be an object.")
    unknown = set(channel_contexts) - set(CHANNEL_ORDER)
    missing = set(CHANNEL_ORDER) - set(channel_contexts)
    if unknown or missing:
        raise ValueError("channel_contexts must contain exactly the three retrieval channels.")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")

    selected_channels = tuple(channels or CHANNEL_ORDER)
    selected_channels = tuple(channel for channel in CHANNEL_ORDER if channel in selected_channels)
    deadline = time.monotonic() + float(timeout_seconds)
    executor = ThreadPoolExecutor(max_workers=max(1, len(selected_channels)))
    futures = {
        channel: executor.submit(
            _compile_channel_queries,
            channel,
            _copy_messages(channel_contexts[channel], f"{channel}_context"),
            max(1, deadline - time.monotonic()),
            trajectory_context,
            _diagnostic_profiler,
        )
        for channel in selected_channels
    }
    results = []
    errors = []
    try:
        for channel in selected_channels:
            future = futures[channel]
            try:
                result = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except (FutureTimeoutError, Exception) as error:
                errors.append({"channel": channel, "error": str(error) or "channel failed"})
                result = {"channel": channel, "queries": []}
            results.append(result)
    finally:
        for future in futures.values():
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return {"channels": results, "errors": errors}
