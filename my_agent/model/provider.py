import atexit
import io
import json
import http.client
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path



















SEMA_HOME_ENV = "SEMA_HOME"
LEGACY_HOME_ENV = "EMAX_HOME"
CONFIG_FILENAME = "config.json"
CONFIG_LOCK = threading.RLock()
DEFAULT_TIMEOUT_SECONDS = 120
PROVIDER_CONNECTION_MAX_IDLE = 8
PROVIDER_CONNECTION_IDLE_SECONDS = 60.0
DEFAULT_NOTIFICATION_CONFIG = {
    "enabled": True,
}


class _PooledConnectionLease:
    def __init__(self, pool, key, connection):
        self._pool = pool
        self.key = key
        self.connection = connection
        self._released = False

    def release(self, reusable):
        if self._released:
            return
        self._released = True
        self._pool.release(self, reusable)


class _PooledResponse:
    def __init__(self, lease, response):
        self._lease = lease
        self._response = response
        self._fully_consumed = False
        self._closed = False

    def read(self, amount=-1):
        data = self._response.read(amount)
        if amount is None or amount < 0 or not data:
            self._fully_consumed = True
        return data

    def __iter__(self):
        try:
            for line in self._response:
                yield line
            self._fully_consumed = True
        finally:
            if self._closed:
                self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            reusable = self._fully_consumed and not bool(getattr(self._response, "will_close", False))
            self._lease.release(reusable)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self._response, name)


class _ProviderConnectionPool:
    """Small HTTP/1.1 keep-alive pool for OpenAI-compatible providers."""

    def __init__(self, max_idle=PROVIDER_CONNECTION_MAX_IDLE, idle_seconds=PROVIDER_CONNECTION_IDLE_SECONDS):
        self.max_idle = max_idle
        self.idle_seconds = idle_seconds
        self._lock = threading.RLock()
        self._idle = defaultdict(list)

    @staticmethod
    def _origin(url):
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise urllib.error.URLError(f"Unsupported provider URL: {url}")
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, parsed.hostname, port

    @staticmethod
    def _create_connection(origin, timeout):
        scheme, host, port = origin
        connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        return connection_type(host, port=port, timeout=timeout)

    @staticmethod
    def _request_target(url):
        parsed = urllib.parse.urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        return target

    def _discard_expired_locked(self, origin, now):
        retained = []
        for timestamp, connection in self._idle.pop(origin, []):
            if now - timestamp <= self.idle_seconds:
                retained.append((timestamp, connection))
            else:
                connection.close()
        if retained:
            self._idle[origin] = retained

    def acquire(self, request, timeout):
        origin = self._origin(request.full_url)
        now = time.monotonic()
        with self._lock:
            self._discard_expired_locked(origin, now)
            if self._idle[origin]:
                _, connection = self._idle[origin].pop()
            else:
                connection = self._create_connection(origin, timeout)
        return _PooledConnectionLease(self, origin, connection)

    def release(self, lease, reusable):
        connection = lease.connection
        if not reusable:
            connection.close()
            return
        with self._lock:
            idle = self._idle[lease.key]
            if len(idle) >= self.max_idle:
                connection.close()
            else:
                idle.append((time.monotonic(), connection))

    def open(self, request, timeout):
        lease = self.acquire(request, timeout)
        try:
            headers = dict(request.header_items())
            lease.connection.request(
                request.get_method(),
                self._request_target(request.full_url),
                body=request.data,
                headers=headers,
            )
            response = lease.connection.getresponse()
        except Exception:
            lease.release(False)
            raise

        if response.status >= 400:
            reusable = False
            try:
                body = response.read()
                headers = response.headers
                reason = response.reason
                reusable = not bool(getattr(response, "will_close", False))
            finally:
                response.close()
                lease.release(reusable)
            raise urllib.error.HTTPError(
                request.full_url,
                response.status,
                reason,
                headers,
                io.BytesIO(body),
            )
        return _PooledResponse(lease, response)

    def close_all(self):
        with self._lock:
            idle = [connection for entries in self._idle.values() for _, connection in entries]
            self._idle.clear()
        for connection in idle:
            connection.close()


_PROVIDER_CONNECTION_POOL = _ProviderConnectionPool()
_ORIGINAL_URLOPEN = urllib.request.urlopen


def _open_provider_response(request, timeout):

    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        return urllib.request.urlopen(request, timeout=timeout)
    return _PROVIDER_CONNECTION_POOL.open(request, timeout)


def close_provider_connections():
    _PROVIDER_CONNECTION_POOL.close_all()


atexit.register(close_provider_connections)

PROVIDER_PROFILES = {
    "deepseek": {
        "provider": "deepseek",
        "protocol": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key_env_fallbacks": [],
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": False,
    },
    "kimi": {
        "provider": "kimi",
        "protocol": "openai_compatible",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "api_key_env": "KIMI_API_KEY",
        "api_key_env_fallbacks": ["MOONSHOT_API_KEY"],
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": False,
    },
    "kimi_intl": {
        "provider": "kimi_intl",
        "protocol": "openai_compatible",
        "base_url": "https://api.moonshot.ai/v1",
        "model": "kimi-k2.6",
        "api_key_env": "KIMI_API_KEY",
        "api_key_env_fallbacks": ["MOONSHOT_API_KEY"],
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": False,
    },
    "custom_openai": {
        "provider": "custom_openai",
        "protocol": "openai_compatible",
        "base_url": "",
        "model": "",
        "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
        "api_key_env_fallbacks": [],
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": False,
    },
    "ollama": {
        "provider": "ollama",
        "protocol": "ollama_native",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen3.5:9b",
        "api_key_env": "",
        "api_key_env_fallbacks": [],
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": False,
        "thinking": False,
    },
}



def sema_home():
    configured_home = os.getenv(SEMA_HOME_ENV) or os.getenv(LEGACY_HOME_ENV)
    if configured_home:
        return Path(configured_home).expanduser()

    sema_dir = Path.home() / ".sema"
    legacy_dir = Path.home() / ".emax"
    if not sema_dir.exists() and legacy_dir.exists():
        return legacy_dir
    return sema_dir.expanduser()



def global_config_path():
    return sema_home() / CONFIG_FILENAME


def _read_config_file(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object.")
    return data


def _write_config_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def update_global_section(section, updates):
    with CONFIG_LOCK:
        path = global_config_path()
        data = _read_config_file(path)
        current = data.get(section, {})
        if not isinstance(current, dict):
            current = {}
        data[section] = {**current, **updates}
        _write_config_file(path, data)
        return path


def _profile(name):
    profile = PROVIDER_PROFILES.get(name)
    if not profile:
        raise RuntimeError(f"Unknown model provider: {name}")
    return dict(profile)


def _configured_model_section():
    data = _read_config_file(global_config_path())
    model_section = data.get("model", {})
    if not isinstance(model_section, dict):
        raise RuntimeError(f"{global_config_path()} field 'model' must be an object.")
    return model_section


def load_model_config():
    configured = _configured_model_section()
    provider_name = os.getenv("SEMA_PROVIDER") or configured.get("provider") or "deepseek"
    profile = PROVIDER_PROFILES.get(provider_name)
    if profile:
        config = dict(profile)
    else:


        config = {
            "provider": str(provider_name),
            "protocol": "openai_compatible",
            "base_url": "",
            "model": "",
            "api_key_env": "",
            "api_key_env_fallbacks": [],
            "supports_tools": True,
            "supports_vision": False,
            "supports_streaming": False,
        }
    config.update({key: value for key, value in configured.items() if value not in (None, "")})
    if not config.get("protocol"):
        config["protocol"] = (profile or {}).get("protocol", "openai_compatible")


    if config["provider"] == "deepseek":
        config["model"] = os.getenv("DEEPSEEK_MODEL", config["model"])
    config["model"] = os.getenv("SEMA_MODEL", config["model"])
    config["base_url"] = os.getenv("SEMA_BASE_URL", config["base_url"])
    config["api_key_env"] = os.getenv("SEMA_API_KEY_ENV", config["api_key_env"])
    return config


def save_global_model_config(config):
    return update_global_section("model", config)


def load_notification_config():
    data = _read_config_file(global_config_path())
    current = data.get("notifications", {})
    if not isinstance(current, dict):
        raise RuntimeError(f"{global_config_path()} field 'notifications' must be an object.")
    config = dict(DEFAULT_NOTIFICATION_CONFIG)
    config.update({key: value for key, value in current.items() if value is not None})
    config["enabled"] = bool(config.get("enabled", True))
    return config


def save_global_notification_config(config):
    return update_global_section("notifications", config)


def model_label(config=None):
    config = config or load_model_config()
    provider = config.get("provider") or "unknown"
    model = config.get("model") or "(missing model)"
    return f"{provider}:{model}"


def api_key_for_config(config):
    if config.get("protocol") == "ollama_native":
        for env_name in [config.get("api_key_env")] + list(config.get("api_key_env_fallbacks") or []):
            if env_name and os.getenv(env_name):
                return os.getenv(env_name), env_name
        if config.get("api_key"):
            return str(config["api_key"]), "config"
        return None, "none"
    env_names = [config.get("api_key_env")] + list(config.get("api_key_env_fallbacks") or [])
    for env_name in env_names:
        if env_name and os.getenv(env_name):
            return os.getenv(env_name), env_name
    if config.get("api_key"):
        return str(config["api_key"]), "config"
    expected = config.get("api_key_env") or "API_KEY"
    raise RuntimeError(f"Missing {expected} environment variable.")


def has_api_key(config=None):
    try:
        api_key_for_config(config or load_model_config())
    except RuntimeError:
        return False
    return True


def _chat_completions_url(base_url):
    base_url = str(base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Model provider base_url is not configured.")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _ollama_chat_url(base_url):
    base_url = str(base_url or "").rstrip("/")
    if not base_url:
        raise RuntimeError("Model provider base_url is not configured.")
    if base_url.endswith("/api/chat"):
        return base_url
    if base_url.endswith("/api"):
        return f"{base_url}/chat"
    return f"{base_url}/api/chat"


def _format_http_error(config, status_code, message, key_source):
    detail = message
    if status_code == 401 and str(config.get("base_url", "")).startswith("https://api.moonshot."):

        other_base_url = (
            "https://api.moonshot.cn/v1"
            if "api.moonshot.ai" in str(config.get("base_url"))
            else "https://api.moonshot.ai/v1"
        )
        detail += (
            "\n\nAuthentication hint: this is a Moonshot/Kimi 401. "
            "Check that the API key belongs to the same region as base_url. "
            f"Current base_url is {config.get('base_url')}; try {other_base_url} if your key comes from the other Moonshot/Kimi console. "
            f"API key source: {key_source}."
        )
    return detail


def _normalize_content_part(part, config):
    if not isinstance(part, dict):
        return _strip_lone_surrogates(part)
    if part.get("type") != "sema_image":
        return _strip_lone_surrogates(dict(part))
    if not config.get("supports_vision"):
        raise RuntimeError(
            f"Current provider {model_label(config)} does not support image input. "
            "Use /config -> Use built-in profile -> kimi."
        )


    from integrations.image_context import image_part_to_data_url

    return {
        "type": "image_url",
        "image_url": {
            "url": image_part_to_data_url(part),
        },
    }


def normalize_messages_for_provider(messages, config=None):
    config = config or load_model_config()
    normalized = []
    for message in messages:
        copied = _strip_lone_surrogates(dict(message))
        content = copied.get("content")
        if isinstance(content, list):
            copied["content"] = [_normalize_content_part(part, config) for part in content]
        elif isinstance(content, str):
            copied["content"] = _strip_lone_surrogates(content)
        normalized.append(copied)
    return normalized


def normalize_messages_for_ollama(messages, config=None):
    normalized = normalize_messages_for_provider(messages, config)
    for message in normalized:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            native_tool_calls = []
            for index, tool_call in enumerate(message["tool_calls"]):
                function = tool_call.get("function") or {}
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            f"Cannot send malformed Ollama tool-call arguments for call {index}."
                        ) from error
                if not isinstance(arguments, dict):
                    raise RuntimeError(f"Ollama tool-call arguments for call {index} must be a JSON object.")
                native_tool_calls.append(
                    {
                        "function": {
                            "name": function.get("name") or "",
                            "arguments": arguments,
                        }
                    }
                )
            message["tool_calls"] = native_tool_calls
        if message.get("role") == "tool":


            message.pop("tool_call_id", None)
    return normalized


def _strip_lone_surrogates(value):
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_strip_lone_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {
            _strip_lone_surrogates(key): _strip_lone_surrogates(item)
            for key, item in value.items()
        }
    return value


def normalize_temperature_for_provider(config, temperature):
    if temperature is None:
        return None
    provider = config.get("provider")
    model = str(config.get("model") or "")
    if provider in {"kimi", "kimi_intl"} and model.startswith("kimi-k2.6"):
        return 1
    return temperature


def build_chat_payload(messages, tools=None, temperature=None, stream=False):
    config = load_model_config()
    if not config.get("model"):
        raise RuntimeError("Model provider model is not configured.")

    message_normalizer = normalize_messages_for_ollama if config.get("protocol") == "ollama_native" else normalize_messages_for_provider
    payload = {
        "model": config["model"],
        "messages": message_normalizer(messages, config),
        "stream": bool(stream),
    }


    if "thinking" in config and config["thinking"] is not None:
        payload["think"] = bool(config["thinking"])
    if tools is not None:
        payload["tools"] = tools
    temperature = normalize_temperature_for_provider(config, temperature)
    if temperature is not None:
        if config.get("protocol") == "ollama_native":
            payload["options"] = {"temperature": temperature}
        else:
            payload["temperature"] = temperature
    return payload


def call_chat_completion(
    messages,
    tools=None,
    temperature=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    purpose="model",
    *,
    exchange_observer=None,
    exchange_metadata=None,
):
    config = load_model_config()
    if config.get("protocol") == "ollama_native":
        return _call_ollama_chat_completion(
            config,
            messages,
            tools=tools,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            exchange_observer=exchange_observer,
            exchange_metadata=exchange_metadata,
        )
    api_key, key_source = api_key_for_config(config)
    payload = build_chat_payload(messages, tools=tools, temperature=temperature)
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _chat_completions_url(config.get("base_url")),
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    _notify_exchange_observer(exchange_observer, "on_request", request_body, payload, exchange_metadata)

    try:
        with _open_provider_response(request, timeout_seconds) as response:
            raw_body = response.read()
            data = json.loads(raw_body.decode("utf-8"))
            _notify_exchange_observer(
                exchange_observer,
                "on_response",
                raw_body,
                _parsed_response_for_observer(data),
                _finish_reason(data),
                exchange_metadata,
            )
            return data
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        message = raw_body.decode("utf-8", errors="replace")
        detail = _format_http_error(config, error.code, message, key_source)
        wrapped = RuntimeError(f"{model_label(config)} {purpose} API error {error.code}: {detail}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, raw_body, exchange_metadata)
        raise wrapped from error
    except urllib.error.URLError as error:
        wrapped = RuntimeError(f"Could not reach {model_label(config)} {purpose} API: {error.reason}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except TimeoutError as error:
        wrapped = RuntimeError(f"{model_label(config)} {purpose} API request timed out: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except http.client.IncompleteRead as error:


        partial = error.partial or None
        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} API request: incomplete response: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, partial, exchange_metadata)
        raise wrapped from error
    except (http.client.RemoteDisconnected, ConnectionError) as error:

        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} API request: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        wrapped = RuntimeError(f"Malformed {model_label(config)} {purpose} API response: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, locals().get("raw_body"), exchange_metadata)
        raise wrapped from error
    except Exception as error:
        _notify_exchange_observer(exchange_observer, "on_error", error, locals().get("raw_body"), exchange_metadata)
        raise


def call_chat_completion_stream(
    messages,
    tools=None,
    temperature=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    purpose="model",
    on_delta=None,
    check_cancel=None,
    on_headers=None,
    *,
    exchange_observer=None,
    exchange_metadata=None,
):
    config = load_model_config()
    if config.get("protocol") == "ollama_native":
        raise RuntimeError("Ollama Native provider does not support streaming in V1.")
    api_key, key_source = api_key_for_config(config)
    payload = build_chat_payload(messages, tools=tools, temperature=temperature, stream=True)
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _chat_completions_url(config.get("base_url")),
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    _notify_exchange_observer(exchange_observer, "on_request", request_body, payload, exchange_metadata)

    try:
        with _open_provider_response(request, timeout_seconds) as response:
            if on_headers is not None:
                on_headers()
            message = parse_chat_completion_stream(
                response,
                on_delta=on_delta,
                check_cancel=check_cancel,
                on_raw_chunk=(exchange_observer.on_stream_chunk if exchange_observer is not None else None),
            )
            _notify_exchange_observer(
                exchange_observer,
                "on_response",
                None,
                message,
                message.get("finish_reason"),
                exchange_metadata,
            )
            return message
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        message = raw_body.decode("utf-8", errors="replace")
        detail = _format_http_error(config, error.code, message, key_source)
        wrapped = RuntimeError(f"{model_label(config)} {purpose} streaming API error {error.code}: {detail}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, raw_body, exchange_metadata)
        raise wrapped from error
    except urllib.error.URLError as error:
        wrapped = RuntimeError(f"Could not reach {model_label(config)} {purpose} streaming API: {error.reason}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except TimeoutError as error:
        wrapped = RuntimeError(f"{model_label(config)} {purpose} streaming API request timed out: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except http.client.IncompleteRead as error:
        partial = error.partial or None
        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} streaming API request: incomplete response: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, partial, exchange_metadata)
        raise wrapped from error
    except (http.client.RemoteDisconnected, ConnectionError) as error:
        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} streaming API request: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except RuntimeError as error:
        _notify_exchange_observer(exchange_observer, "on_error", error, None, exchange_metadata)
        raise
    except Exception as error:
        _notify_exchange_observer(exchange_observer, "on_error", error, None, exchange_metadata)
        raise


def _notify_exchange_observer(observer, method, *args):
    if observer is None:
        return
    try:
        getattr(observer, method)(*args)
    except Exception:
        pass


def _finish_reason(data):
    try:
        return data["choices"][0].get("finish_reason") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _parsed_response_for_observer(data):
    try:
        return parse_chat_completion_message(data)
    except Exception:
        return data


def _normalize_ollama_tool_calls(tool_calls):
    normalized = []
    for index, tool_call in enumerate(tool_calls or []):
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        if not isinstance(function, dict) or not function.get("name"):
            continue
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        normalized.append(
            {
                "id": tool_call.get("id") or f"ollama-call-{index}",
                "type": "function",
                "function": {
                    "name": str(function["name"]),
                    "arguments": arguments,
                },
            }
        )
    return normalized


def _normalize_ollama_response(data):
    if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
        raise RuntimeError("Ollama response missing assistant message.")
    source = data["message"]
    message = {
        "role": source.get("role") or "assistant",
        "content": source.get("content") or "",
        "tool_calls": _normalize_ollama_tool_calls(source.get("tool_calls")),
    }
    reasoning = source.get("reasoning_content")
    if reasoning is None:
        reasoning = source.get("reasoning")
    if reasoning is None:
        reasoning = source.get("thinking")
    if reasoning is not None:
        message["reasoning_content"] = reasoning or ""
    finish_reason = data.get("done_reason") or ("tool_calls" if message["tool_calls"] else "stop")
    return {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "done": bool(data.get("done", True)),
    }


def _call_ollama_chat_completion(
    config,
    messages,
    *,
    tools=None,
    temperature=None,
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    purpose="model",
    exchange_observer=None,
    exchange_metadata=None,
):
    api_key, key_source = api_key_for_config(config)
    payload = build_chat_payload(messages, tools=tools, temperature=temperature, stream=False)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request_body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _ollama_chat_url(config.get("base_url")),
        data=request_body,
        headers=headers,
        method="POST",
    )
    _notify_exchange_observer(exchange_observer, "on_request", request_body, payload, exchange_metadata)

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw_body = response.read()
            data = _normalize_ollama_response(json.loads(raw_body.decode("utf-8")))
            _notify_exchange_observer(
                exchange_observer,
                "on_response",
                raw_body,
                _parsed_response_for_observer(data),
                _finish_reason(data),
                exchange_metadata,
            )
            return data
    except urllib.error.HTTPError as error:
        raw_body = error.read()
        message = raw_body.decode("utf-8", errors="replace")
        detail = _format_http_error(config, error.code, message, key_source)
        wrapped = RuntimeError(f"{model_label(config)} {purpose} API error {error.code}: {detail}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, raw_body, exchange_metadata)
        raise wrapped from error
    except urllib.error.URLError as error:
        wrapped = RuntimeError(f"Could not reach {model_label(config)} {purpose} API: {error.reason}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except TimeoutError as error:
        wrapped = RuntimeError(f"{model_label(config)} {purpose} API request timed out: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except http.client.IncompleteRead as error:
        partial = error.partial or None
        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} API request: incomplete response: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, partial, exchange_metadata)
        raise wrapped from error
    except (http.client.RemoteDisconnected, ConnectionError) as error:
        wrapped = RuntimeError(f"Could not complete {model_label(config)} {purpose} API request: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, None, exchange_metadata)
        raise wrapped from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        wrapped = RuntimeError(f"Malformed {model_label(config)} {purpose} API response: {error}")
        _notify_exchange_observer(exchange_observer, "on_error", wrapped, locals().get("raw_body"), exchange_metadata)
        raise wrapped from error
    except Exception as error:
        _notify_exchange_observer(exchange_observer, "on_error", error, locals().get("raw_body"), exchange_metadata)
        raise


def parse_chat_completion_stream(response, on_delta=None, check_cancel=None, on_raw_chunk=None):
    message = {"content": "", "tool_calls": []}
    reasoning_parts = []
    saw_done = False
    finish_reason = ""
    for data in iter_sse_data(response, on_raw_chunk=on_raw_chunk):
        if check_cancel:
            check_cancel()
        if data == "[DONE]":
            saw_done = True
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Malformed streaming model response: {error}") from error
        for choice in payload.get("choices") or []:
            if choice.get("finish_reason") is not None:
                finish_reason = choice.get("finish_reason") or ""
            delta = choice.get("delta") or {}
            content_delta = delta.get("content")
            if content_delta:
                append_delta = normalize_stream_content_delta(message["content"], content_delta)
                if append_delta:
                    message["content"] += append_delta
                    if on_delta:
                        on_delta(append_delta)


            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
            for tool_call_delta in delta.get("tool_calls") or []:
                merge_tool_call_delta(message["tool_calls"], tool_call_delta)
    if check_cancel:
        check_cancel()
    if not saw_done:
        raise RuntimeError("Streaming model response ended before [DONE].")
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    message["tool_calls"] = prune_stream_tool_call_artifacts(message["tool_calls"])
    message["finish_reason"] = finish_reason or ("tool_calls" if message["tool_calls"] else "stop")
    return message


def normalize_stream_content_delta(existing, incoming):
    existing = existing or ""
    incoming = incoming or ""
    if not incoming:
        return ""
    if not existing:
        return incoming
    if incoming == existing:
        return ""
    if incoming.startswith(existing):
        return incoming[len(existing):]
    threshold = stream_overlap_threshold(incoming)
    if len(incoming.strip()) >= threshold and existing.endswith(incoming):
        return ""
    overlap = max_stream_overlap(existing, incoming)
    if overlap >= threshold:
        return incoming[overlap:]
    return incoming


def stream_overlap_threshold(text):
    return 4 if contains_cjk(text) else 8


def contains_cjk(text):
    return any("\u4e00" <= char <= "\u9fff" for char in text or "")


def max_stream_overlap(existing, incoming):
    limit = min(len(existing), len(incoming))
    for size in range(limit, 0, -1):
        if existing.endswith(incoming[:size]):
            return size
    return 0


def iter_sse_data(response, on_raw_chunk=None):
    for raw_line in response:
        if on_raw_chunk:
            try:
                on_raw_chunk(raw_line if isinstance(raw_line, bytes) else str(raw_line).encode("utf-8"))
            except Exception:
                pass
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[len("data:") :].strip()


def merge_tool_call_delta(tool_calls, delta):
    try:
        fallback_index = len(tool_calls) - 1 if tool_calls else 0
        index = int(delta.get("index", fallback_index))
    except (TypeError, ValueError):
        index = len(tool_calls) - 1 if tool_calls else 0
    while len(tool_calls) <= index:
        tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    tool_call = tool_calls[index]
    if delta.get("id") and not tool_call.get("id"):
        tool_call["id"] = delta["id"]
    if delta.get("type"):
        tool_call["type"] = delta["type"]
    function_delta = delta.get("function") or {}
    function = tool_call.setdefault("function", {"name": "", "arguments": ""})
    if function_delta.get("name"):
        function["name"] = function.get("name", "") + function_delta["name"]
    if function_delta.get("arguments"):
        function["arguments"] = function.get("arguments", "") + function_delta["arguments"]


def prune_stream_tool_call_artifacts(tool_calls):
    if len(tool_calls) <= 1:
        return tool_calls

    valid_names = {
        (tool_call.get("function") or {}).get("name")
        for tool_call in tool_calls
        if stream_tool_call_has_valid_arguments(tool_call)
    }
    if not valid_names:
        return tool_calls

    pruned = []
    for tool_call in tool_calls:
        name = (tool_call.get("function") or {}).get("name")
        if name in valid_names and not stream_tool_call_has_valid_arguments(tool_call):
            continue
        pruned.append(tool_call)
    return pruned


def stream_tool_call_has_valid_arguments(tool_call):
    function = tool_call.get("function") or {}
    if not tool_call.get("id") or not function.get("name"):
        return False
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(arguments, dict)


def parse_chat_completion_message(data):
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Model response missing assistant message.") from error

    parsed = {
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }
    finish_reason = (data.get("choices") or [{}])[0].get("finish_reason")
    if finish_reason is not None:
        parsed["finish_reason"] = finish_reason or ""
    reasoning_content = message.get("reasoning_content")
    if reasoning_content is None:


        reasoning_content = message.get("reasoning")
    if reasoning_content is not None:

        parsed["reasoning_content"] = reasoning_content or ""
    return parsed
