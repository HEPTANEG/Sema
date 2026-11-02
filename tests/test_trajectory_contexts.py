import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from persistence import trajectory as t
from persistence import trajectory_contexts as cache


RAW = b'{ "model": "fake", "messages": [{"role":"user","content":"hello"}], "tools": [] }'


def test_window_independence_duplicate_requests_and_expiration():
    main = t.start_trajectory("main_agent")
    old = cache.save_request(main, RAW)
    push = t.start_span(main, "memory_push", "push")
    for _ in range(51):
        assert cache.save_request(push, RAW)
    assert len(cache.list_requests("memory_push")) == 50
    assert cache.get_request(old["request_id"])["state"] == "available"
    for _ in range(50):
        cache.save_request(main, RAW)
    expired = cache.get_request(old["request_id"])
    assert expired["state"] == "expired"
    assert not Path(expired["path"]).exists()
    rows = cache.list_requests()
    assert len(rows) == 50
    assert len({r["request_id"] for r in rows}) == 50
    assert len({r["sha256"] for r in rows}) == 1
    assert Path(rows[0]["path"]).read_bytes() == RAW
    Path(rows[0]["path"]).unlink()
    assert cache.get_request(rows[0]["request_id"])["state"] == "missing"


def test_root_pruning_keeps_recent_requests_and_no_gzip_request():
    first = None
    for _ in range(21):
        root = t.start_trajectory("main_agent")
        observer = t.provider_exchange_observer(root, agent_type="main_agent")
        observer.on_request(RAW, json.loads(RAW))
        if first is None:
            first = root
        t.complete_trajectory(root)
    assert not t.get_trajectory(first["trajectory_id"])
    assert len(cache.list_requests()) == 21
    assert all(Path(row["path"]).exists() for row in cache.list_requests())
    with t._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM trajectory_artifact_refs WHERE role='provider_request'").fetchone()[0] == 0


def test_parallel_requests_are_distinct_and_capped():
    root = t.start_trajectory("formation", metadata={"formation_mode":"historical"})
    with ThreadPoolExecutor(max_workers=6) as executor:
        rows = list(executor.map(lambda _: cache.save_request(root, RAW), range(60)))
    assert all(rows)
    assert len({r["request_seq"] for r in rows}) == 60
    assert len(cache.list_requests("formation")) == 50
    assert json.loads(rows[0]["metadata_json"])["formation_mode"] == "historical"


@pytest.mark.parametrize("agent", cache.AGENT_TYPES)
def test_each_agent_has_its_own_fifty_request_window(agent):
    context = t.start_trajectory(agent)
    first = cache.save_request(context, RAW)
    for _ in range(50):
        assert cache.save_request(context, RAW)
    assert len(cache.list_requests(agent)) == 50
    assert cache.get_request(first["request_id"])["state"] == "expired"
    assert t.get_trajectory(context["trajectory_id"])["trajectory"]["status"] == "running"


def test_failed_write_and_failed_cleanup_are_nonfatal_and_retry(monkeypatch):
    root = t.start_trajectory("main_agent")
    monkeypatch.setattr(cache, "REQUEST_RETENTION", 1)
    first = cache.save_request(root, RAW)
    old_path = Path(cache.get_request(first["request_id"])["path"])
    original_unlink = Path.unlink
    with monkeypatch.context() as patch:
        def deny_old(path, *args, **kwargs):
            if path == old_path:
                raise PermissionError("open file")
            return original_unlink(path, *args, **kwargs)
        patch.setattr(Path, "unlink", deny_old)
        assert cache.save_request(root, RAW)
        assert old_path.exists()
    assert cache.save_request(root, RAW)
    assert not old_path.exists()
    with monkeypatch.context() as patch:
        patch.setattr(cache.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
        assert cache.save_request(root, RAW) is None


def test_migration_verifies_requests_handles_legacy_and_retries_corruption(monkeypatch):
    root = t.start_trajectory("main_agent")
    event = t.record_event(root, "model.requested", artifacts={"provider_request":{"content":RAW,"media_type":"application/json"}})
    bad = t.record_event(root, "model.requested", artifacts={"provider_request":{"content":RAW+b' ',"media_type":"application/json"}})
    bad_id = bad["artifacts"][0]["artifact_id"]
    bad_path = Path(t.artifact_metadata(bad_id)["path"])
    original = bad_path.read_bytes()
    bad_path.write_bytes(b"broken gzip")
    legacy = t.start_trajectory("legacy")
    wrapper = json.dumps({"metadata":{"created_at":"2020-01-01T00:00:00Z"},"payload":json.loads(RAW)}).encode()
    t.record_event(legacy, "legacy.context_snapshot", summary={"source_path":"old.json"}, artifacts={"legacy_source":{"content":wrapper,"media_type":"application/json"}})
    t.record_event(legacy, "legacy.context_snapshot", summary={"source_path":"index.json"}, artifacts={"legacy_source":{"content":b'{"snapshots":[]}',"media_type":"application/json"}})
    cache.migrate()
    rows = cache.list_requests()
    assert len(rows) == 2
    assert rows[-1]["exact_wire_bytes"] == 0
    assert Path(rows[-1]["path"]).read_bytes() == wrapper
    assert bad_path.exists()
    with t._connect() as c:
        assert c.execute("SELECT status FROM trajectory_migrations WHERE migration_key='readable_provider_requests_v2'").fetchone()[0] == "failed"
    bad_path.write_bytes(original)
    cache.migrate()
    assert len(cache.list_requests()) == 3
    assert not bad_path.exists()
    cache.migrate()
    assert len(cache.list_requests()) == 3
    data = t.get_trajectory(root["trajectory_id"])
    assert any(e.get("request_snapshot", {}).get("state") == "available" for e in data["events"])


def test_migration_does_not_displace_new_requests_and_recovers_orphan_files(monkeypatch):
    monkeypatch.setattr(cache, "REQUEST_RETENTION", 2)
    root = t.start_trajectory("main_agent")
    fresh = [cache.save_request(root, RAW) for _ in range(2)]
    event = t.record_event(root, "model.requested", artifacts={"provider_request":{"content":RAW,"media_type":"application/json"}})
    with t._connect() as connection:
        connection.execute("UPDATE trajectory_events SET ts='2000-01-01T00:00:00+00:00' WHERE event_id=?", (event["event_id"],))
    orphan = cache.context_dir() / "main_agent" / "20000101_request_orphan.json"
    orphan.write_bytes(RAW)
    cache.migrate()
    assert not orphan.exists()
    assert {r["request_id"] for r in cache.list_requests()} == {r["request_id"] for r in fresh}
    with t._connect() as connection:
        migrated = connection.execute("SELECT state FROM trajectory_requests WHERE source_key IS NOT NULL").fetchone()
        assert migrated[0] == "expired"


def test_storage_failure_does_not_fail_real_provider_path(monkeypatch, tmp_path):
    from model import provider
    import io
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    provider.save_global_model_config({"provider":"custom_openai", "model":"test", "base_url":"http://example.test", "api_key":"test"})
    monkeypatch.setattr(provider.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}'))
    root = t.start_trajectory("main_agent")
    monkeypatch.setattr(cache.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    result = provider.call_chat_completion([], exchange_observer=t.provider_exchange_observer(root))
    assert result["choices"][0]["message"]["content"] == "ok"


def test_commands_return_paths_not_bodies():
    from commands.structured_commands import handle_trajectory_command as command
    root = t.start_trajectory("main_agent")
    row = cache.save_request(root, RAW)
    for text in ["/trajectory context", "/trajectory context main_agent", "/trajectory contexts", f"/trajectory context request {row['request_id']}"]:
        result = command(text)
        assert row["request_id"] in result
        assert "hello" not in result
        assert ".json" in result
    assert "not found" in command("/trajectory context ltm_maintainer")
    with pytest.raises(ValueError):
        command("/trajectory context unknown")


def test_read_only_request_queries_do_not_use_writable_connection(monkeypatch):
    root = t.start_trajectory("main_agent")
    observer = t.provider_exchange_observer(root, agent_type="main_agent")
    observer.on_request(RAW, json.loads(RAW))
    request = cache.list_requests()[0]

    def writable_connection_must_not_be_used(*_args, **_kwargs):
        raise AssertionError("read-only request query used writable connection")

    monkeypatch.setattr(t, "_connect", writable_connection_must_not_be_used)
    assert cache.list_requests()[0]["request_id"] == request["request_id"]
    assert cache.get_request(request["request_id"])["request_id"] == request["request_id"]
    trajectory = t.get_trajectory(root["trajectory_id"])
    assert trajectory["events"]
    assert any(event.get("request_snapshot") for event in trajectory["events"])


@pytest.mark.parametrize("mode", ["nonstream", "stream", "ollama"])
def test_provider_saved_request_bytes_equal_http_data(monkeypatch, tmp_path, mode):
    from model import provider
    import io
    monkeypatch.setenv("SEMA_HOME", str(tmp_path))
    provider.save_global_model_config({"provider":"ollama" if mode == "ollama" else "custom_openai", "model":"test", "base_url":"http://example.test/v1", "api_key":"header-only-secret"})
    sent = []
    def urlopen(request, timeout):
        sent.append(request.data)
        if len(sent) == 1:
            import urllib.error
            raise urllib.error.URLError("temporary disconnect")
        if mode == "stream":
            return io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        if mode == "ollama":
            return io.BytesIO(b'{"message":{"role":"assistant","content":"ok"},"done":true}')
        return io.BytesIO(b'{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}')
    monkeypatch.setattr(provider.urllib.request, "urlopen", urlopen)
    root = t.start_trajectory("main_agent")
    for retry in range(2):
        observer = t.provider_exchange_observer(root, agent_type="main_agent", metadata={"retry":retry})
        call = provider.call_chat_completion_stream if mode == "stream" else provider.call_chat_completion
        kwargs = {"tools":[{"type":"function","function":{"name":"hello","parameters":{"type":"object"}}}], "exchange_observer":observer}
        if retry == 0:
            with pytest.raises(RuntimeError, match="Could not reach"):
                call([{"role":"user","content":"你好 🌙"}], **kwargs)
        else:
            call([{"role":"user","content":"你好 🌙"}], **kwargs)
    rows = list(reversed(cache.list_requests()))
    assert len(rows) == 2
    for row, wire in zip(rows, sent):
        assert Path(row["path"]).read_bytes() == wire
        assert hashlib.sha256(wire).hexdigest() == row["sha256"]
        assert b"header-only-secret" not in wire
    assert json.loads(rows[1]["metadata_json"])["retry"] == 1
