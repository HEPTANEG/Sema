import json
import base64
import time
from http.client import IncompleteRead

import pytest

from model import provider as model_provider


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")

    def close(self):
        pass


class FakeStreamingResponse:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def __iter__(self):
        return iter(self.lines)

    def close(self):
        pass


class RecordingExchangeObserver:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _record(self, name, *args):
        if self.fail:
            raise RuntimeError("trajectory failed")
        self.calls.append((name, args))

    def on_request(self, *args):
        self._record("request", *args)

    def on_response(self, *args):
        self._record("response", *args)

    def on_error(self, *args):
        self._record("error", *args)

    def on_stream_chunk(self, *args):
        self._record("stream", *args)


class _PoolResponse:
    def __init__(self, body=b"{}", will_close=False):
        self.body = body
        self.will_close = will_close
        self.status = 200
        self.reason = "OK"
        self.headers = {}
        self.closed = False

    def read(self, _amount=-1):
        body, self.body = self.body, b""
        return body

    def close(self):
        self.closed = True


def test_pooled_response_enforces_total_timeout():
    class Lease:
        def release(self, _reusable):
            pass

    class SlowResponse:
        will_close = False

        def read(self, _amount=-1):
            time.sleep(0.03)
            return b"x"

        def close(self):
            pass

    response = model_provider._PooledResponse(Lease(), SlowResponse(), timeout=0.05)
    with pytest.raises(TimeoutError, match="total timeout"):
        response.read()
    response.close()


class _PoolConnection:
    instances = []

    def __init__(self, host, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = []
        self.responses = []
        self.closed = False
        type(self).instances.append(self)

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))

    def getresponse(self):
        return self.responses.pop(0) if self.responses else _PoolResponse(b"{}")

    def close(self):
        self.closed = True


def _pool_request(url="https://example.test/v1/chat/completions"):
    from urllib.request import Request

    return Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")


def test_provider_connection_pool_reuses_completed_response(monkeypatch):
    _PoolConnection.instances.clear()
    pool = model_provider._ProviderConnectionPool(max_idle=8, idle_seconds=60)
    monkeypatch.setattr(pool, "_create_connection", lambda _origin, _timeout: _PoolConnection("example.test"))

    first = pool.open(_pool_request(), timeout=5)
    assert first.read() == b"{}"
    first.close()
    second = pool.open(_pool_request(), timeout=5)
    second.close()

    assert len(_PoolConnection.instances) == 1
    assert len(_PoolConnection.instances[0].requests) == 2
    pool.close_all()


def test_provider_connection_pool_does_not_share_active_connection(monkeypatch):
    _PoolConnection.instances.clear()
    pool = model_provider._ProviderConnectionPool(max_idle=8, idle_seconds=60)
    monkeypatch.setattr(pool, "_create_connection", lambda _origin, _timeout: _PoolConnection("example.test"))

    first = pool.open(_pool_request(), timeout=5)
    second = pool.open(_pool_request(), timeout=5)
    first.close()
    second.close()

    assert len(_PoolConnection.instances) == 2
    pool.close_all()


def test_provider_connection_pool_discards_idle_connections_after_timeout(monkeypatch):
    _PoolConnection.instances.clear()
    pool = model_provider._ProviderConnectionPool(max_idle=8, idle_seconds=0)
    monkeypatch.setattr(pool, "_create_connection", lambda _origin, _timeout: _PoolConnection("example.test"))

    first = pool.open(_pool_request(), timeout=5)
    first.close()
    time.sleep(0.001)
    second = pool.open(_pool_request(), timeout=5)
    second.close()

    assert len(_PoolConnection.instances) == 2
    assert _PoolConnection.instances[0].closed is True
    pool.close_all()


def test_provider_connection_pool_does_not_reuse_connection_close_response(monkeypatch):
    _PoolConnection.instances.clear()
    pool = model_provider._ProviderConnectionPool(max_idle=8, idle_seconds=60)
    monkeypatch.setattr(pool, "_create_connection", lambda _origin, _timeout: _PoolConnection("example.test"))

    first = pool.open(_pool_request(), timeout=5)
    first._response.will_close = True
    first.close()
    second = pool.open(_pool_request(), timeout=5)
    second.close()

    assert len(_PoolConnection.instances) == 2
    assert _PoolConnection.instances[0].closed is True
    pool.close_all()


def test_close_provider_connections_closes_idle_connections(monkeypatch):
    _PoolConnection.instances.clear()
    pool = model_provider._ProviderConnectionPool(max_idle=8, idle_seconds=60)
    monkeypatch.setattr(pool, "_create_connection", lambda _origin, _timeout: _PoolConnection("example.test"))
    monkeypatch.setattr(model_provider, "_PROVIDER_CONNECTION_POOL", pool)

    response = pool.open(_pool_request(), timeout=5)
    response.close()
    model_provider.close_provider_connections()

    assert _PoolConnection.instances[0].closed is True


def test_default_model_config_is_deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.delenv("SEMA_PROVIDER", raising=False)

    config = model_provider.load_model_config()

    assert config["provider"] == "deepseek"
    assert config["protocol"] == "openai_compatible"
    assert config["model"] == "deepseek-chat"
    assert config["api_key_env"] == "DEEPSEEK_API_KEY"


def test_custom_model_config_uses_explicit_protocol_without_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom-news",
            "protocol": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "news-model",
            "api_key_env": "NEWS_API_KEY",
        }
    )

    config = model_provider.load_model_config()

    assert config["provider"] == "custom-news"
    assert config["protocol"] == "openai_compatible"
    assert config["model"] == "news-model"


def test_saved_model_takes_precedence_over_environment_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setenv("SEMA_MODEL", "environment-model")
    model_provider.save_global_model_config({"provider": "deepseek", "model": "saved-model"})

    config = model_provider.load_model_config()

    assert config["model"] == "saved-model"


def test_environment_model_is_used_when_no_model_is_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    config = model_provider.load_model_config()

    assert config["model"] == "deepseek-chat"


def test_save_global_model_config_sets_kimi_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    path = model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi"]))
    config = model_provider.load_model_config()

    assert path == tmp_path / "config.json"
    assert config["provider"] == "kimi"
    assert config["base_url"] == "https://api.moonshot.cn/v1"
    assert config["supports_vision"] is True
    assert config["api_key_env"] == "KIMI_API_KEY"


def test_kimi_intl_profile_uses_international_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi_intl"]))
    config = model_provider.load_model_config()

    assert config["provider"] == "kimi_intl"
    assert config["base_url"] == "https://api.moonshot.ai/v1"
    assert config["supports_vision"] is True


def test_ollama_profile_uses_native_defaults_without_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["ollama"]))
    config = model_provider.load_model_config()

    assert config["provider"] == "ollama"
    assert config["protocol"] == "ollama_native"
    assert config["base_url"] == "http://127.0.0.1:11434"
    assert config["model"] == "qwen3.5:9b"
    assert model_provider.has_api_key(config) is True


def test_ollama_native_call_normalizes_text_and_tool_calls(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["ollama"]))

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "model": "qwen3.5:9b",
                "done": True,
                "done_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning": "Inspecting the directory.",
                    "tool_calls": [
                        {"function": {"name": "list_files", "arguments": {"path": "."}}}
                    ],
                },
            }
        )

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)
    result = model_provider.call_chat_completion(
        [{"role": "user", "content": "List files."}],
        tools=[{"type": "function", "function": {"name": "list_files"}}],
        temperature=0.2,
        timeout_seconds=17,
    )

    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert "Authorization" not in captured["headers"]
    assert captured["payload"]["think"] is False
    assert captured["payload"]["options"] == {"temperature": 0.2}
    assert result["choices"][0]["message"]["reasoning_content"] == "Inspecting the directory."
    assert result["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "ollama-call-0",
            "type": "function",
            "function": {"name": "list_files", "arguments": '{"path":"."}'},
        }
    ]


def test_ollama_native_streaming_is_explicitly_unsupported(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["ollama"]))

    with pytest.raises(RuntimeError, match="does not support streaming"):
        model_provider.call_chat_completion_stream([], timeout_seconds=1)


def test_ollama_native_payload_converts_tool_history_arguments_to_objects(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["ollama"]))

    payload = model_provider.build_chat_payload(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"path":"."}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "file.txt"},
        ]
    )

    assert payload["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "list_files", "arguments": {"path": "."}}}
            ],
        },
        {"role": "tool", "content": "file.txt"},
    ]


def test_call_chat_completion_uses_configured_openai_compatible_provider(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    data = model_provider.call_chat_completion(
        [{"role": "user", "content": "hello"}],
        timeout_seconds=12,
        purpose="test",
    )

    assert data["choices"][0]["message"]["content"] == "ok"
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer saved-key"
    assert captured["payload"]["model"] == "custom-model"
    assert captured["payload"]["stream"] is False
    assert captured["timeout"] == 12


def test_call_chat_completion_request_snapshot_keeps_unicode_readable(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(request, timeout):
        captured["raw"] = request.data.decode("utf-8")
        captured["payload"] = json.loads(captured["raw"])
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    model_provider.call_chat_completion([{"role": "user", "content": "测试"}])

    assert "测试" in captured["raw"]
    assert "\\u6d4b\\u8bd5" not in captured["raw"]
    assert captured["payload"]["messages"][0]["content"] == "测试"


def test_call_chat_completion_observes_exchange_before_request(monkeypatch, tmp_path):
    captured = {}
    observer = RecordingExchangeObserver()
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(request, timeout):
        assert observer.calls[0][0] == "request"
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    model_provider.call_chat_completion(
        [{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        exchange_observer=observer,
        exchange_metadata={"trajectory_id": "trajectory-1"},
    )

    _raw, payload, metadata = observer.calls[0][1]
    assert payload == captured["payload"]
    assert payload["tools"][0]["function"]["name"] == "read_file"
    assert metadata == {"trajectory_id": "trajectory-1"}
    assert observer.calls[-1][0] == "response"
    raw_response, parsed_response, finish_reason, _metadata = observer.calls[-1][1]
    assert json.loads(raw_response.decode("utf-8"))["choices"][0]["message"]["content"] == "ok"
    assert parsed_response["content"] == "ok"
    assert finish_reason == ""


def test_call_chat_completion_exchange_observer_failure_does_not_block_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )
    monkeypatch.setattr(
        model_provider.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
    )

    data = model_provider.call_chat_completion(
        [{"role": "user", "content": "hello"}],
        exchange_observer=RecordingExchangeObserver(fail=True),
    )

    assert data["choices"][0]["message"]["content"] == "ok"


def test_call_chat_completion_observes_exchange_when_request_fails(monkeypatch, tmp_path):
    import urllib.error

    observer = RecordingExchangeObserver()
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(_request, timeout):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="Could not reach"):
        model_provider.call_chat_completion(
            [{"role": "user", "content": "hello"}],
            exchange_observer=observer,
        )

    assert observer.calls[0][1][1]["messages"][0]["content"] == "hello"
    assert observer.calls[-1][0] == "error"


def test_call_chat_completion_wraps_incomplete_response(monkeypatch, tmp_path):
    observer = RecordingExchangeObserver()
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    class InterruptedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise IncompleteRead(b"partial", 10)

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", lambda _request, timeout: InterruptedResponse())

    with pytest.raises(RuntimeError, match="Could not complete custom_openai:custom-model model API request: incomplete response") as raised:
        model_provider.call_chat_completion(
            [{"role": "user", "content": "hello"}],
            exchange_observer=observer,
        )

    assert isinstance(raised.value.__cause__, IncompleteRead)
    assert observer.calls[-1][0] == "error"


def test_build_chat_payload_supports_stream_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    payload = model_provider.build_chat_payload([{"role": "user", "content": "hello"}], stream=True)

    assert payload["stream"] is True


def test_build_chat_payload_omits_thinking_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    payload = model_provider.build_chat_payload([{"role": "user", "content": "hello"}])

    assert "think" not in payload


def test_build_chat_payload_can_disable_model_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config({"provider": "custom_openai", "model": "qwen3.5:9b", "thinking": False})

    payload = model_provider.build_chat_payload([{"role": "user", "content": "hello"}])

    assert payload["think"] is False


def test_build_chat_payload_normalizes_kimi_k26_temperature(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi"]))

    payload = model_provider.build_chat_payload([{"role": "user", "content": "hello"}], temperature=0)

    assert payload["model"] == "kimi-k2.6"
    assert payload["temperature"] == 1


def test_build_chat_payload_preserves_non_kimi_temperature(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))

    payload = model_provider.build_chat_payload([{"role": "user", "content": "hello"}], temperature=0)

    assert payload["model"] == "deepseek-chat"
    assert payload["temperature"] == 0


def test_parse_chat_completion_stream_accumulates_content_and_tool_calls():
    deltas = []
    response = FakeStreamingResponse(
        [
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"lo","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_","arguments":"{\\"path\\""}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"file","arguments":":\\"a.py\\"}"}}]}}]}\n',
            b"data: [DONE]\n",
        ]
    )

    message = model_provider.parse_chat_completion_stream(response, on_delta=deltas.append)

    assert deltas == ["Hel", "lo"]
    assert message["content"] == "Hello"
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        }
    ]


def test_parse_chat_completion_stream_normalizes_ollama_reasoning_delta():
    response = FakeStreamingResponse(
        [
            b'data: {"choices":[{"delta":{"reasoning":"Thinking"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"OK"}}]}\n',
            b"data: [DONE]\n",
        ]
    )

    message = model_provider.parse_chat_completion_stream(response)

    assert message["content"] == "OK"
    assert message["reasoning_content"] == "Thinking"


def test_parse_chat_completion_message_normalizes_ollama_reasoning():
    message = model_provider.parse_chat_completion_message(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning": "Need to inspect the directory first.",
                        "tool_calls": [],
                    }
                }
            ]
        }
    )

    assert message["reasoning_content"] == "Need to inspect the directory first."


def test_parse_chat_completion_message_preserves_finish_reason():
    message = model_provider.parse_chat_completion_message(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    )

    assert message["finish_reason"] == "length"


def test_parse_chat_completion_stream_merges_missing_index_tool_argument_chunks():
    response = FakeStreamingResponse(
        [
            b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"write_file","arguments":"{\\"path\\":\\"a.md\\","}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"\\"content\\":\\"hello\\"}"}}]}}]}\n',
            b"data: [DONE]\n",
        ]
    )

    message = model_provider.parse_chat_completion_stream(response)

    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"a.md","content":"hello"}'},
        }
    ]


def test_parse_chat_completion_stream_prunes_duplicate_partial_tool_call_artifact():
    response = FakeStreamingResponse(
        [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"bad","type":"function","function":{"name":"write_file","arguments":"{\\"path\\":\\"a.md\\",\\"content\\":\\"unterminated"}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"good","type":"function","function":{"name":"write_file","arguments":"{\\"path\\":\\"a.md\\",\\"content\\":\\"complete\\"}"}}]}}]}\n',
            b"data: [DONE]\n",
        ]
    )

    message = model_provider.parse_chat_completion_stream(response)

    assert message["tool_calls"] == [
        {
            "id": "good",
            "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"a.md","content":"complete"}'},
        }
    ]


def test_parse_chat_completion_stream_rejects_malformed_json():
    response = FakeStreamingResponse([b"data: {bad json}\n"])

    try:
        model_provider.parse_chat_completion_stream(response)
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected malformed streaming response error.")

    assert "Malformed streaming model response" in message


def test_call_chat_completion_stream_posts_streaming_payload(monkeypatch, tmp_path):
    captured = {}
    boundaries = []
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return FakeStreamingResponse([b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b"data: [DONE]\n"])

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    message = model_provider.call_chat_completion_stream(
        [{"role": "user", "content": "hello"}],
        timeout_seconds=9,
        on_headers=lambda: boundaries.append("headers"),
    )

    assert message["content"] == "ok"
    assert captured["payload"]["stream"] is True
    assert captured["headers"]["Accept"] == "text/event-stream"
    assert captured["timeout"] == 9
    assert boundaries == ["headers"]


def test_call_chat_completion_stream_observes_full_exchange(monkeypatch, tmp_path):
    observer = RecordingExchangeObserver()
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )
    monkeypatch.setattr(
        model_provider.urllib.request,
        "urlopen",
        lambda _request, timeout: FakeStreamingResponse([b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b"data: [DONE]\n"]),
    )

    model_provider.call_chat_completion_stream(
        [{"role": "user", "content": "hello"}],
        exchange_observer=observer,
        exchange_metadata={"streaming": True},
    )

    assert observer.calls[0][0] == "request"
    assert observer.calls[0][1][1]["stream"] is True
    assert observer.calls[0][1][2] == {"streaming": True}
    assert b"".join(call[1][0] for call in observer.calls if call[0] == "stream") == b'data: {"choices":[{"delta":{"content":"ok"}}]}\ndata: [DONE]\n'
    assert observer.calls[-1][0] == "response"


def test_call_chat_completion_stream_observes_interrupted_partial_stream(monkeypatch, tmp_path):
    observer = RecordingExchangeObserver()
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {"provider": "custom_openai", "base_url": "https://example.test/v1", "model": "custom-model", "api_key": "saved-key"}
    )

    class InterruptedResponse(FakeStreamingResponse):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n'
            raise IncompleteRead(b"partial")

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", lambda _request, timeout: InterruptedResponse([]))

    with pytest.raises(RuntimeError, match="Could not complete custom_openai:custom-model model streaming API request: incomplete response") as raised:
        model_provider.call_chat_completion_stream(
            [{"role": "user", "content": "hello"}],
            exchange_observer=observer,
        )

    assert isinstance(raised.value.__cause__, IncompleteRead)
    assert any(call[0] == "stream" for call in observer.calls)
    assert observer.calls[-1][0] == "error"


def test_kimi_auth_error_mentions_region_hint(monkeypatch, tmp_path):
    import urllib.error

    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            **model_provider.PROVIDER_PROFILES["kimi"],
            "base_url": "https://api.moonshot.ai/v1",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(_request, timeout):
        raise urllib.error.HTTPError(
            "https://api.moonshot.ai/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            FakeResponse({"error": {"message": "Invalid Authentication"}}),
        )

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    try:
        model_provider.call_chat_completion([{"role": "user", "content": "hello"}])
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected Kimi auth error.")

    assert "Moonshot/Kimi 401" in message
    assert "https://api.moonshot.cn/v1" in message
    assert "API key source: config" in message


def test_remote_disconnected_is_wrapped_as_runtime_error(monkeypatch, tmp_path):
    import http.client

    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    model_provider.save_global_model_config(
        {
            "provider": "custom_openai",
            "base_url": "https://example.test/v1",
            "model": "custom-model",
            "api_key": "saved-key",
        }
    )

    def fake_urlopen(_request, timeout):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(model_provider.urllib.request, "urlopen", fake_urlopen)

    try:
        model_provider.call_chat_completion([{"role": "user", "content": "hello"}], purpose="vision")
    except RuntimeError as error:
        message = str(error)
    else:
        raise AssertionError("Expected RemoteDisconnected to be wrapped.")

    assert "Could not complete custom_openai:custom-model vision API request" in message
    assert "Remote end closed connection" in message


def test_build_chat_payload_converts_local_image_reference(monkeypatch, tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"abc")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi"]))

    payload = model_provider.build_chat_payload(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this."},
                    {
                        "type": "sema_image",
                        "path": str(image),
                        "filename": "one.png",
                        "mime_type": "image/png",
                    },
                ],
            }
        ]
    )

    image_part = payload["messages"][0]["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"] == f"data:image/png;base64,{base64.b64encode(b'abc').decode('ascii')}"


def test_build_chat_payload_rejects_images_for_non_vision_provider(monkeypatch, tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"abc")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))

    try:
        model_provider.build_chat_payload(
            [
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
                }
            ]
        )
    except RuntimeError as error:
        assert "does not support image input" in str(error)
    else:
        raise AssertionError("Expected non-vision provider to reject image input.")


def test_normalize_messages_replaces_lone_surrogates():
    messages = [
        {"role": "user", "content": "bad \ud800 text"},
        {"role": "user", "content": [{"type": "text", "text": "also \udfff bad"}]},
    ]

    normalized = model_provider.normalize_messages_for_provider(
        messages,
        {
            "provider": "deepseek",
            "supports_vision": False,
        },
    )

    assert "\ud800" not in normalized[0]["content"]
    assert "\udfff" not in normalized[1]["content"][0]["text"]
    assert "?" in normalized[0]["content"]
