import json

import pytest

from runtime import agent
from model import provider as model_provider
from tools import tool_schemas


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


class FakeSseResponse:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)


def _messages():
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


def _sse_chunk(content):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n").encode("utf-8")


def test_stream_parser_ignores_repeated_full_content_chunks():
    deltas = []
    response = FakeSseResponse([
        _sse_chunk("你说得对，我完全同意这个方向。"),
        _sse_chunk("你说得对，我完全同意这个方向。"),
        b"data: [DONE]\n",
    ])

    result = model_provider.parse_chat_completion_stream(response, on_delta=deltas.append)

    assert result["content"] == "你说得对，我完全同意这个方向。"
    assert deltas == ["你说得对，我完全同意这个方向。"]


def test_stream_parser_handles_cumulative_content_snapshots():
    deltas = []
    response = FakeSseResponse([
        _sse_chunk("Hello"),
        _sse_chunk("Hello world"),
        b"data: [DONE]\n",
    ])

    result = model_provider.parse_chat_completion_stream(response, on_delta=deltas.append)

    assert result["content"] == "Hello world"
    assert deltas == ["Hello", " world"]


def test_stream_parser_deduplicates_short_cjk_overlap():
    deltas = []
    response = FakeSseResponse([
        _sse_chunk("听起来挺惬意的！"),
        _sse_chunk("惬意的！村里的夏天"),
        b"data: [DONE]\n",
    ])

    result = model_provider.parse_chat_completion_stream(response, on_delta=deltas.append)

    assert result["content"] == "听起来挺惬意的！村里的夏天"
    assert deltas == ["听起来挺惬意的！", "村里的夏天"]


def test_stream_parser_ignores_repeated_short_cjk_chunk():
    deltas = []
    response = FakeSseResponse([
        _sse_chunk("你说得对"),
        _sse_chunk("你说得对"),
        _sse_chunk("，这个方向很好。"),
        b"data: [DONE]\n",
    ])

    result = model_provider.parse_chat_completion_stream(response, on_delta=deltas.append)

    assert result["content"] == "你说得对，这个方向很好。"
    assert deltas == ["你说得对", "，这个方向很好。"]


def test_build_model_tools_payload_includes_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    payload = agent.build_model_tools_payload(_messages())

    assert payload["stream"] is False
    assert payload["tools"] == tool_schemas.TOOL_SCHEMAS
    assert [schema["function"]["name"] for schema in payload["tools"]] == tool_schemas.tool_schema_names()


def test_build_model_tools_payload_refreshes_persisted_system_prompt_without_mutating_history(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    messages = [
        {"role": "system", "content": "old persisted system prompt"},
        {"role": "user", "content": "hello"},
    ]

    payload = agent.build_model_tools_payload(messages)

    assert payload["messages"][0] == agent.create_initial_messages()[0]
    assert {"role": "user", "content": "hello"} in payload["messages"]
    assert messages[0]["content"] == "old persisted system prompt"


def test_build_model_tools_payload_rejects_image_history_for_non_vision_provider(monkeypatch, tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "user",
            "content": [
                {
                    "type": "sema_image",
                    "path": str(image),
                    "filename": "one.png",
                    "mime_type": "image/png",
                }
            ],
        },
    ]

    with pytest.raises(RuntimeError, match="does not support image input"):
        agent.build_model_tools_payload(messages)


def test_parse_model_tool_message_reads_plain_content():
    result = agent.parse_model_tool_message(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello.",
                    }
                }
            ]
        }
    )

    assert result == {
        "content": "Hello.",
        "tool_calls": [],
    }


def test_parse_model_tool_message_reads_tool_calls():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"app.py"}',
            },
        }
    ]

    result = agent.parse_model_tool_message(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls,
                    }
                }
            ]
        }
    )

    assert result["content"] == ""
    assert result["tool_calls"] == tool_calls


def test_parse_model_tool_message_preserves_reasoning_content():
    result = agent.parse_model_tool_message(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "I should inspect Memory Engine status.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "show_memory_state",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert result["reasoning_content"] == "I should inspect Memory Engine status."


def test_call_model_with_tools_posts_non_streaming_payload(monkeypatch, tmp_path):
    captured = {}
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "replace_block",
                                "arguments": '{"path":"app.py","old":"old","new":"new"}',
                            },
                        }
                    ],
                }
            }
        ]
    }

    def fake_urlopen(request, timeout):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        return FakeResponse(response_data)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    result = agent.call_model_with_tools(_messages())

    assert captured["timeout"] == agent.MODEL_TIMEOUT_SECONDS
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["tools"] == tool_schemas.TOOL_SCHEMAS
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert result["tool_calls"][0]["function"]["name"] == "replace_block"


def test_call_model_with_tools_uses_agent_monkeypatched_non_stream_provider(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent, "MODEL_TIMEOUT_SECONDS", 17)

    def fake_call(
        messages,
        tools=None,
        timeout_seconds=None,
        purpose="model",
        exchange_observer=None,
        exchange_metadata=None,
    ):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["timeout_seconds"] = timeout_seconds
        captured["purpose"] = purpose
        captured["exchange_observer"] = exchange_observer
        captured["exchange_metadata"] = exchange_metadata
        return {"choices": [{"message": {"role": "assistant", "content": "patched", "tool_calls": []}}]}

    monkeypatch.setattr(agent, "call_chat_completion", fake_call)

    result = agent.call_model_with_tools(_messages())

    assert captured["tools"] == tool_schemas.TOOL_SCHEMAS
    assert captured["timeout_seconds"] == 17
    assert captured["purpose"] == "tool calling"
    assert captured["exchange_observer"] is None
    assert captured["exchange_metadata"]["purpose"] == "tool calling"
    assert captured["exchange_metadata"]["streaming"] is False
    assert result["content"] == "patched"


def test_call_model_with_tools_uses_agent_monkeypatched_stream_provider(monkeypatch):
    captured = {}
    events = []
    monkeypatch.setattr(agent, "MODEL_TIMEOUT_SECONDS", 23)

    def fake_stream(
        messages,
        tools=None,
        timeout_seconds=None,
        purpose="model",
        on_delta=None,
        check_cancel=None,
        exchange_observer=None,
        exchange_metadata=None,
    ):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["timeout_seconds"] = timeout_seconds
        captured["purpose"] = purpose
        captured["exchange_observer"] = exchange_observer
        captured["exchange_metadata"] = exchange_metadata
        on_delta("hello")
        check_cancel()
        return {"role": "assistant", "content": "streamed", "tool_calls": []}

    monkeypatch.setattr(agent, "call_chat_completion_stream", fake_stream)

    result = agent.call_model_with_tools(
        _messages(),
        runtime_state={"streaming_enabled": True, "ui_event_sink": events.append},
    )

    assert captured["tools"] == tool_schemas.TOOL_SCHEMAS
    assert captured["timeout_seconds"] == 23
    assert captured["purpose"] == "tool calling"
    assert captured["exchange_observer"] is None
    assert captured["exchange_metadata"]["purpose"] == "tool calling"
    assert captured["exchange_metadata"]["streaming"] is True
    assert result["content"] == "streamed"
    assert result["message_id"]
    assert any(event["type"] == "assistant_delta" and event["delta"] == "hello" for event in events)


def test_call_model_with_tools_result_can_convert_to_internal_request(monkeypatch, tmp_path):
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "replace_block",
                                "arguments": '{"path":"app.py","old":"old","new":"new"}',
                            },
                        }
                    ],
                }
            }
        ]
    }

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.setattr(model_provider.urllib.request, "urlopen", lambda _request, timeout: FakeResponse(response_data))

    result = agent.call_model_with_tools(_messages())
    request = tool_schemas.tool_call_to_request(result["tool_calls"][0])

    assert request["tool"] == "replace_block"
    assert request["args"] == "app.py"
    assert request["old_block"] == "old"
    assert request["new_block"] == "new"


def test_parse_model_tool_message_rejects_missing_message():
    with pytest.raises(RuntimeError, match="missing assistant message"):
        agent.parse_model_tool_message({"choices": []})


def test_call_model_with_tools_wraps_timeout(monkeypatch, tmp_path):
    def timeout_urlopen(_request, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.setattr(model_provider.urllib.request, "urlopen", timeout_urlopen)

    with pytest.raises(RuntimeError, match="request timed out"):
        agent.call_model_with_tools(_messages())
