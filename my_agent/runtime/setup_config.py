"""User-level first-run configuration; no model imports or downloads on read."""

import hashlib
import importlib.util
import json
import os
import sqlite3
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from model import provider

FIELDS = {
    "embedding": {"model_path": "SEMA_EMBEDDING_MODEL_PATH"},
    "reranker": {"model_path": "SEMA_RERANKER_MODEL_PATH", "python_path": "SEMA_RERANKER_PYTHON"},
    "search": {"api_key": "TAVILY_API_KEY"},
    "remote": {"bot_token": "SEMA_TELEGRAM_BOT_TOKEN", "user_id": "SEMA_TELEGRAM_USER_ID", "chat_id": "SEMA_TELEGRAM_CHAT_ID"},
}
SECRETS = {"api_key", "bot_token"}
TEST_LOCK = threading.Lock()
_checking = None


def defaults(component):
    if component == "embedding":
        return {"model_path": str(Path.home() / ".cache/sema/models/bge-m3")}
    if component == "reranker":
        snapshots = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3-Reranker-0.6B/snapshots"
        found = sorted(snapshots.glob("*/config.json"))
        root = Path(__file__).resolve().parents[2]
        python = root / ".venv-reranker/Scripts/python.exe"
        legacy = root / "my_agent/.venv-reranker/Scripts/python.exe"
        if not python.is_file() and legacy.is_file():
            python = legacy
        return {"model_path": str(found[-1].parent) if found else "",
                "python_path": str(python)}
    return {key: "" for key in FIELDS[component]}


def effective(component):
    if component not in FIELDS:
        raise ValueError("Unknown setup component")
    saved = provider._read_config_file(provider.global_config_path()).get(component, {})
    if not isinstance(saved, dict):
        raise ValueError(f"Invalid {component} configuration")
    base = defaults(component)
    values, sources = {}, {}
    for key, env in FIELDS[component].items():
        value = os.getenv(env)
        sources[key] = f"environment: {env}" if value else "saved" if saved.get(key) else "default"
        values[key] = str(value or saved.get(key) or base.get(key) or "")
    return values, sources


def value(component, key):
    return effective(component)[0][key]


def public_model():
    config = dict(provider.load_model_config())
    config["api_key_configured"] = provider.has_api_key(config)
    try:
        config["api_key_source"] = provider.api_key_for_config(config)[1]
    except RuntimeError:
        config["api_key_source"] = "missing"
    overrides = {key: env for key, env in {"provider": "SEMA_PROVIDER", "model": "SEMA_MODEL", "base_url": "SEMA_BASE_URL", "api_key_env": "SEMA_API_KEY_ENV"}.items() if os.getenv(env)}
    config["environment_overrides"] = overrides
    config.pop("api_key", None)
    return config


def save(component, updates):
    if component not in FIELDS or not isinstance(updates, dict):
        raise ValueError("Unknown setup component or invalid updates")
    allowed = set(FIELDS[component]) | {f"clear_{key}" for key in SECRETS & set(FIELDS[component])}
    if set(updates) - allowed:
        raise ValueError("Unknown setup field")
    if any(not isinstance(val, bool) for key, val in updates.items() if key.startswith("clear_")):
        raise ValueError("Clear flags must be booleans")
    clean = {}
    for key in FIELDS[component]:
        if updates.get(f"clear_{key}") is True:
            clean[key] = ""
        elif key in updates:
            if not isinstance(updates[key], str):
                raise ValueError(f"{key} must be text")
            text = updates[key].strip()
            if key in SECRETS and not text:
                continue
            if key in {"user_id", "chat_id"} and text:
                if not text.lstrip("-").isdigit() or int(text) == 0 or (key == "user_id" and int(text) < 0):
                    raise ValueError(f"Invalid {key}")
            clean[key] = str(Path(text).expanduser().resolve()) if key.endswith("_path") and text else text
    provider.update_global_section(component, clean)


def fingerprint(component):
    values = provider.load_model_config() if component == "llm" else effective(component)[0]
    if component == "llm":
        values = dict(values)
        try:
            values["effective_key"] = provider.api_key_for_config(values)[0]
        except RuntimeError:
            pass
    metadata = []
    for key, text in values.items():
        if key.endswith("_path") and text:
            path = Path(text)
            try:
                candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
                for candidate in candidates:
                    if candidate.is_file():
                        stat = candidate.stat()
                        metadata.append((str(candidate), stat.st_size, stat.st_mtime_ns))
            except OSError:
                metadata.append((str(path), "unreadable"))
    raw = json.dumps([values, metadata], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def infrastructure():
    checks = {}
    for module in ("tiktoken", "usearch", "sentence_transformers", "torch"):
        checks[module] = "Ready" if importlib.util.find_spec(module) else "Missing"
    try:
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE VIRTUAL TABLE setup_fts USING fts5(content)")
        checks["SQLite / FTS5"] = "Ready"
    except sqlite3.Error:
        checks["SQLite / FTS5"] = "Missing"
    return checks


def status(active_fingerprints=None):
    records = provider._read_config_file(provider.global_config_path()).get("setup_tests", {})
    components = {}
    for component in ("llm", *FIELDS):
        values, sources = ({}, {}) if component == "llm" else effective(component)
        if component == "llm":
            config = provider.load_model_config()
            missing = not config.get("base_url") or not config.get("model") or not provider.has_api_key(config)
        else:
            missing = any(not text or (key.endswith("_path") and not Path(text).exists()) for key, text in values.items())
        stamp = fingerprint(component)
        tested = records.get(component, {})
        state = "Missing" if missing else tested.get("status", "Not tested") if tested.get("fingerprint") == stamp else "Not tested"
        if active_fingerprints and component in active_fingerprints and active_fingerprints[component] != stamp:
            state = "Restart required"
        if _checking == component:
            state = "Checking"
        missing_fields = [key.replace("_", " ") for key, text in values.items() if not text or (key.endswith("_path") and not Path(text).exists())]
        detail = tested.get("detail", "") if tested.get("fingerprint") == stamp else ""
        if missing:
            detail = ", ".join(missing_fields) if component != "llm" else "LLM endpoint, model, or API key"
        components[component] = {
            "fields": [{"key": key, "label": key.replace("_", " ").capitalize(), "secret": key in SECRETS} for key in FIELDS.get(component, {})],
            "status": state, "values": {key: text for key, text in values.items() if key not in SECRETS},
            "sources": sources, "secrets": {key: bool(text) for key, text in values.items() if key in SECRETS},
            "detail": detail,
            "model": {"embedding": "BAAI/bge-m3", "reranker": "Qwen/Qwen3-Reranker-0.6B"}.get(component),
            "dimension": 1024 if component == "embedding" else None,
            "device": tested.get("device", "") if tested.get("fingerprint") == stamp else "",
            "required": component in {"llm", "embedding", "reranker"},
        }
    return {"components": components, "infrastructure": infrastructure()}


def test(component):
    global _checking
    if component not in {"llm", *FIELDS}:
        raise ValueError("Unknown setup component")
    if not TEST_LOCK.acquire(blocking=False):
        raise RuntimeError("A setup test is already running")
    try:
        _checking = component
        stamp = fingerprint(component)
        try:
            metadata = _test(component) or {}
            result = {"fingerprint": stamp, "status": "Ready", "detail": "Test passed.", "device": metadata.get("device", "")}
        except Exception as error:

            detail = "Test failed. Check credentials and network access." if component in {"llm", "search", "remote"} else str(error)
            result = {"fingerprint": stamp, "status": "Failed", "detail": detail}
        provider.update_global_section("setup_tests", {component: result})
        return result
    finally:
        _checking = None
        TEST_LOCK.release()


def _test(component):
    if component == "llm":
        from memory_engine.token_counter import count_text_tokens
        count_text_tokens("Sema setup")
        result = provider.call_chat_completion([{"role": "user", "content": "Reply OK."}], purpose="setup connection test")
        if not result.get("choices"):
            raise RuntimeError("No response")
        return {}
    config, _sources = effective(component)
    if component == "embedding":
        from memory_engine.ltm.embedding_adapter import EmbeddingAdapter
        adapter = EmbeddingAdapter(model_path=config["model_path"])
        try:
            adapter.embed_queries(["Sema setup test"])
            return adapter._request_json("GET", "/health")
        finally:
            adapter.close()
        return
    if component == "reranker":

        from memory_engine.ltm.reranker_adapter import RerankerAdapter
        adapter = RerankerAdapter(python_path=config["python_path"], model_path=config["model_path"])
        try:
            adapter.rerank("Sema setup", [{"object_id": "setup-test", "text": "Sema setup"}])
            return adapter._request_json("GET", "/health")
        finally:
            adapter.close()
        return
    if component == "remote":
        if not config["user_id"].isdigit() or not config["chat_id"].lstrip("-").isdigit():
            raise ValueError("Invalid Telegram identifiers")
        base = f"https://api.telegram.org/bot{config['bot_token']}"
        for method, body in (("getMe", {}), ("getChat", {"chat_id": config["chat_id"]})):
            request = Request(base + "/" + method, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=30) as response:
                if not json.load(response).get("ok"):
                    raise RuntimeError("Telegram check failed")
        return
    request = Request("https://api.tavily.com/search", data=json.dumps({"api_key": config["api_key"], "query": "Sema setup", "max_results": 1}).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        json.load(response)
