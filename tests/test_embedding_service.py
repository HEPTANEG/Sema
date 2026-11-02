import json
import math
import os
import threading
import urllib.error
import urllib.request
from collections import deque
from io import StringIO

import pytest


def _request(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


class _FakeModel:
    def get_sentence_embedding_dimension(self):
        return 1024

    def encode(self, texts, **_kwargs):
        rows = []
        for index, _text in enumerate(texts):
            row = [0.0] * 1024
            row[index % 1024] = 1.0
            rows.append(row)
        return rows


@pytest.fixture
def embedding_server():
    from memory_engine.ltm.embedding_service import _create_server

    server = _create_server(_FakeModel(), "cpu", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield base_url
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_health_and_batch_embed(embedding_server):
    status, health = _request(f"{embedding_server}/health")
    assert status == 200
    assert health["status"] == "ready"
    assert health["dimension"] == 1024

    status, result = _request(
        f"{embedding_server}/embed",
        method="POST",
        payload={"texts": ["first", "second"], "input_kind": "document"},
    )
    assert status == 200
    assert result["model"] == "BAAI/bge-m3"
    assert len(result["vectors"]) == 2
    assert len(result["vectors"][0]) == 1024
    assert math.isclose(math.sqrt(sum(value * value for value in result["vectors"][0])), 1.0)
    assert result["vectors"][0] != result["vectors"][1]


@pytest.mark.parametrize(
    "payload",
    [
        {"texts": [], "input_kind": "document"},
        {"texts": [1], "input_kind": "document"},
        {"texts": ["text"], "input_kind": "other"},
    ],
)
def test_embed_rejects_invalid_requests(embedding_server, payload):
    status, result = _request(f"{embedding_server}/embed", method="POST", payload=payload)
    assert status == 400
    assert result["error"]["code"] == "invalid_request"


def test_adapter_preserves_document_and_query_contract(monkeypatch):
    from memory_engine.ltm import embedding_adapter

    adapter = embedding_adapter.EmbeddingAdapter.__new__(embedding_adapter.EmbeddingAdapter)
    calls = []
    adapter._lock = threading.RLock()
    adapter._base_url = "http://local"
    adapter._process = object()
    monkeypatch.setattr(adapter, "_ensure_started", lambda: None)

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {
            "model": "BAAI/bge-m3",
            "dimension": 1024,
            "vectors": [[1.0] + [0.0] * 1023 for _ in payload["texts"]],
        }

    monkeypatch.setattr(adapter, "_request_json", fake_request)
    documents = adapter.embed_documents(["document"])
    queries = adapter.embed_queries(["query"])
    assert documents.dimension == 1024
    assert queries.vectors[0][0] == 1.0
    assert calls[0][2]["input_kind"] == "document"
    assert calls[1][2]["input_kind"] == "query"


def test_missing_model_does_not_download(monkeypatch, tmp_path):
    from memory_engine.ltm.embedding_adapter import EmbeddingServiceError, resolve_model_path

    monkeypatch.delenv("SEMA_EMBEDDING_MODEL_PATH", raising=False)
    with pytest.raises(EmbeddingServiceError, match="automatic download is disabled"):
        resolve_model_path(tmp_path / "missing-model")


def test_adapter_reports_early_service_exit_with_stderr_diagnostics(monkeypatch, tmp_path):
    from memory_engine.ltm import embedding_adapter

    class FakeProcess:
        def __init__(self):
            self.stdin = StringIO()
            self.stdout = StringIO("")
            self.stderr = StringIO("model load failed\n")

        def poll(self):
            return 1

        def wait(self, timeout=None):
            del timeout
            return 1

    adapter = embedding_adapter.EmbeddingAdapter.__new__(embedding_adapter.EmbeddingAdapter)
    adapter.model_path = tmp_path
    adapter.startup_timeout = 0.1
    adapter.request_timeout = 0.1
    adapter._process = None
    adapter._base_url = None
    adapter._lock = threading.RLock()
    adapter._stderr_tail = deque(maxlen=embedding_adapter.STARTUP_DIAGNOSTIC_LINES)
    adapter._stderr_thread = None
    monkeypatch.setattr(embedding_adapter.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess())

    with pytest.raises(
        embedding_adapter.EmbeddingServiceError,
        match=r"exited before readiness \(exit code 1\).*model load failed",
    ):
        adapter._start_once()

    adapter.close()


def test_adapter_retries_failed_cold_start_once(monkeypatch, tmp_path):
    from memory_engine.ltm import embedding_adapter

    adapter = embedding_adapter.EmbeddingAdapter.__new__(embedding_adapter.EmbeddingAdapter)
    adapter.model_path = tmp_path
    adapter.startup_timeout = 0.1
    adapter.request_timeout = 0.1
    adapter._process = None
    adapter._base_url = None
    adapter._lock = threading.RLock()
    adapter._stderr_tail = deque(maxlen=embedding_adapter.STARTUP_DIAGNOSTIC_LINES)
    adapter._stderr_thread = None
    attempts = []

    def fake_start_once():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise embedding_adapter.EmbeddingServiceError("first startup failed")
        adapter._process = type("Process", (), {"poll": lambda self: None})()
        adapter._base_url = "http://127.0.0.1:1234"

    monkeypatch.setattr(adapter, "_start_once", fake_start_once)
    monkeypatch.setattr(adapter, "close", lambda: setattr(adapter, "_process", None))

    adapter._ensure_started()

    assert attempts == [1, 2]
    assert adapter._base_url == "http://127.0.0.1:1234"


def test_embedding_service_reserves_stdout_for_readiness(monkeypatch, capsys):
    from memory_engine.ltm import embedding_service

    class FakeServer:
        server_address = ("127.0.0.1", 43210)

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    def noisy_load(_model_path):
        print("model loader chatter")
        return _FakeModel(), "cpu"

    monkeypatch.setattr(embedding_service, "_load_model", noisy_load)
    monkeypatch.setattr(embedding_service, "_create_server", lambda *_args: FakeServer())

    embedding_service.serve("unused")

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"event": "ready", "port": 43210}
    assert "model loader chatter" in captured.err


def test_embedding_service_shutdowns_when_parent_pipe_closes():
    from memory_engine.ltm.embedding_service import _shutdown_when_stream_closes

    class FakeServer:
        stopped = False

        def shutdown(self):
            self.stopped = True

    server = FakeServer()

    _shutdown_when_stream_closes(server, StringIO(""))

    assert server.stopped is True


@pytest.mark.skipif(
    os.environ.get("SEMA_EMBEDDING_SMOKE") != "1",
    reason="set SEMA_EMBEDDING_SMOKE=1 to load the local BGE-M3 model",
)
def test_local_bge_m3_smoke():
    from memory_engine.ltm.embedding_adapter import EmbeddingAdapter

    adapter = EmbeddingAdapter()
    try:
        result = adapter.embed_documents(["用户偏好简洁的技术回复"])
        assert result.model == "BAAI/bge-m3"
        assert result.dimension == 1024
        assert len(result.vectors) == 1
    finally:
        adapter.close()
