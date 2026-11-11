import gzip
import json
import threading

from persistence import trajectory


def test_trajectory_stores_hierarchy_and_complete_artifacts(isolated_workspace):
    context = trajectory.start_trajectory("main_agent")
    span = trajectory.start_span(context, "tool_call", "read_file")
    large = "x" * 20_000
    event = trajectory.record_event(
        span,
        "tool.completed",
        status="completed",
        summary={"tool": "read_file"},
        artifacts={"tool_arguments": {"path": "a.txt"}, "tool_result": large},
    )
    trajectory.complete_span(span)
    trajectory.complete_trajectory(context)

    data = trajectory.get_trajectory(context["trajectory_id"])
    assert data["trajectory"]["status"] == "completed"
    assert data["spans"][0]["parent_span_id"] is None
    result_id = next(item["artifact_id"] for item in event["artifacts"] if item["role"] == "tool_result")
    loaded = trajectory.load_artifact(result_id)
    assert loaded["content"].decode("utf-8") == large
    assert loaded["raw_bytes"] == 20_000


def test_external_id_resumes_the_same_trajectory(isolated_workspace):
    first = trajectory.start_trajectory("ltm_maintainer", "run-1")
    trajectory.fail_trajectory(first, RuntimeError("offline"))
    resumed = trajectory.resume_trajectory("ltm_maintainer", "run-1")
    assert resumed["trajectory_id"] == first["trajectory_id"]
    assert trajectory.get_trajectory(first["trajectory_id"])["trajectory"]["status"] == "running"


def test_external_id_can_be_marked_failed_before_new_owner_starts(isolated_workspace):
    first = trajectory.start_trajectory("ltm_maintainer", "run-stale")

    assert trajectory.fail_trajectory_by_external_id(
        "ltm_maintainer",
        "run-stale",
        RuntimeError("owner exited"),
        metadata={"reason": "stale owner"},
    )

    stored = trajectory.get_trajectory(first["trajectory_id"])
    assert stored["trajectory"]["status"] == "failed"
    assert "owner exited" in stored["trajectory"]["error_message"]


def test_content_addressed_artifacts_are_reused(isolated_workspace):
    context = trajectory.start_trajectory("main_agent")
    first = trajectory.record_event(context, "one", artifacts={"body": "same"})
    second = trajectory.record_event(context, "two", artifacts={"body": "same"})

    assert first["artifacts"][0]["artifact_id"] == second["artifacts"][0]["artifact_id"]
    with trajectory._connect_raw() as connection:
        assert connection.execute("SELECT COUNT(*) FROM trajectory_artifacts").fetchone()[0] == 1


def test_recorder_failure_returns_noop_without_raising(monkeypatch, isolated_workspace):
    context = trajectory.start_trajectory("main_agent")
    monkeypatch.setattr(trajectory, "_record_event", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert trajectory.record_event(context, "ignored") is None


def test_event_sequences_are_monotonic_under_concurrency(isolated_workspace):
    context = trajectory.start_trajectory("main_agent")

    def worker(offset):
        for index in range(20):
            trajectory.record_event(context, "state.changed", summary={"value": offset + index})

    threads = [threading.Thread(target=worker, args=(value * 100,)) for value in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = trajectory.get_trajectory(context["trajectory_id"])["events"]
    sequences = [event["seq"] for event in events]
    assert sequences == list(range(1, 82))


def test_retention_keeps_latest_twenty_terminal_roots(isolated_workspace):
    ids = []
    for index in range(23):
        context = trajectory.start_trajectory("formation", metadata={"index": index})
        ids.append(context["trajectory_id"])
        trajectory.complete_trajectory(context)
    rows = trajectory.list_trajectories(limit=100, process_type="formation")
    assert len(rows) == 20
    assert not trajectory.get_trajectory(ids[0])
    assert trajectory.get_trajectory(ids[-1])


def test_retention_is_per_process_and_never_prunes_running_roots(isolated_workspace):
    running = trajectory.start_trajectory("formation", external_id="still-running")
    for process_type in ("formation", "memory_push"):
        for index in range(22):
            context = trajectory.start_trajectory(process_type, external_id=f"{process_type}-{index}")
            trajectory.complete_trajectory(context)

    assert trajectory.get_trajectory(running["trajectory_id"])["trajectory"]["status"] == "running"
    assert len([row for row in trajectory.list_trajectories(100, "formation") if row["status"] != "running"]) == 20
    assert len(trajectory.list_trajectories(100, "memory_push")) == 20


def test_read_only_queries_do_not_initialize_empty_store(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    database = trajectory.trajectory_db_path(root)

    assert trajectory.trajectory_store_status(root)["state"] == "uninitialized"
    assert trajectory.list_trajectories(root=root) == []
    assert trajectory.get_trajectory("missing", root=root) is None
    assert trajectory.load_artifact("missing", root=root) is None
    assert not database.exists()
    assert not database.parent.exists()


def test_read_only_queries_leave_legacy_sources_for_write_initialization(tmp_path):
    root = tmp_path / "legacy"
    trace = root / ".sema" / "logs" / "trace.jsonl"
    cache = root / ".sema" / "memory" / "context_cache"
    trace.parent.mkdir(parents=True)
    cache.mkdir(parents=True)
    trace.write_text('{"trace_id":"old"}\n', encoding="utf-8")
    payload = cache / "context.json"
    payload.write_text('{"messages":[]}', encoding="utf-8")

    status = trajectory.trajectory_store_status(root)
    assert status["state"] == "migration_pending"
    assert trajectory.list_trajectories(root=root) == []
    assert trace.exists()
    assert payload.exists()
    assert not trajectory.trajectory_db_path(root).exists()


def test_read_only_queries_report_unavailable_database_without_replacing_it(tmp_path):
    root = tmp_path / "broken"
    database = trajectory.trajectory_db_path(root)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    assert trajectory.trajectory_store_status(root)["state"] == "unavailable"
    assert trajectory.list_trajectories(root=root) == []
    assert database.read_bytes() == b"not a sqlite database"


def test_read_only_trajectory_queries_do_not_use_writable_connection(monkeypatch, tmp_path):
    root = tmp_path / "existing"
    context = trajectory.start_trajectory("main_agent", root=root)
    trajectory.record_event(context, "diagnostic", status="completed")

    def writable_connection_must_not_be_used(*_args, **_kwargs):
        raise AssertionError("read-only query used writable connection")

    monkeypatch.setattr(trajectory, "_connect", writable_connection_must_not_be_used)
    assert trajectory.trajectory_store_status(root)["state"] == "ready"
    assert trajectory.list_trajectories(root=root)
    assert trajectory.get_trajectory(context["trajectory_id"], root=root)
    assert trajectory.format_trajectory_errors(root=root) == "No trajectory errors."


def test_trajectory_command_reports_uninitialized_without_creating_store(isolated_workspace):
    from commands.structured_commands import handle_trajectory_command

    result = handle_trajectory_command("/trajectory")
    assert "status: uninitialized" in result
    assert not trajectory.trajectory_db_path().exists()


def test_legacy_trace_and_context_cache_are_verified_migrated_then_deleted(isolated_workspace):
    legacy_trace = isolated_workspace / ".sema" / "logs" / "trace.jsonl"
    legacy_trace.parent.mkdir(parents=True)
    legacy_trace.write_bytes(
        b'{"ts":"2026-01-01T00:00:00+00:00","trace_id":"old-1","event":"tool","status":"ok"}\n'
        b'{malformed\n'
    )
    cache = isolated_workspace / ".sema" / "memory" / "context_cache"
    cache.mkdir(parents=True)
    payload = cache / "00000001-context-old.json"
    payload.write_text(json.dumps({"metadata": {"trace_id": "old-1"}, "payload": {"messages": [{"role": "user", "content": "full"}]}}), encoding="utf-8")
    (cache / "index.json").write_text(json.dumps({"snapshots": [{"snapshot_id": "context-old", "trace_id": "old-1", "payload_path": str(payload)}]}), encoding="utf-8")

    trajectory.reset_trajectory_state_for_tests()
    trajectory.start_trajectory("main_agent", root=isolated_workspace)
    rows = trajectory.list_trajectories(limit=100, root=isolated_workspace)

    assert any(row["process_type"] == "legacy" for row in rows)
    assert not legacy_trace.exists()
    assert not payload.exists()
    assert not (cache / "index.json").exists()
    with trajectory._connect_raw(isolated_workspace) as connection:
        migration = connection.execute("SELECT status, details_json FROM trajectory_migrations").fetchone()
    assert migration["status"] == "completed"
    assert json.loads(migration["details_json"])["source_count"] == 3
    assert json.loads(migration["details_json"])["source_artifact_ref_count"] == 3
    before = len(rows)
    trajectory.reset_trajectory_state_for_tests()
    assert len(trajectory.list_trajectories(limit=100, root=isolated_workspace)) == before


def test_failed_legacy_import_leaves_sources_intact(monkeypatch, isolated_workspace):
    legacy_trace = isolated_workspace / ".sema" / "logs" / "trace.jsonl"
    legacy_trace.parent.mkdir(parents=True)
    legacy_trace.write_text('{"trace_id":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(trajectory, "_store_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    trajectory.reset_trajectory_state_for_tests()
    assert trajectory.store_artifact({}, root=isolated_workspace)["ok"] is False
    assert trajectory.list_trajectories(root=isolated_workspace) == []
    assert trajectory.trajectory_store_status(isolated_workspace)["state"] == "migration_pending"
    assert legacy_trace.exists()
    with trajectory._connect_raw(isolated_workspace) as connection:
        migration = connection.execute("SELECT status FROM trajectory_migrations").fetchone()
    assert migration["status"] == "failed"


def test_provider_exchange_observer_keeps_raw_sse_and_finish_reason(isolated_workspace):
    context = trajectory.start_trajectory("main_agent")
    observer = trajectory.provider_exchange_observer(context, purpose="tool calling")
    request = b'{"messages":[{"role":"user","content":"hello"}]}'
    observer.on_request(request, {"model": "fake", "messages": [{}], "stream": True}, {})
    observer.on_stream_chunk(b'data: {"choices":[{"delta":{"content":"A"}}]}\n')
    observer.on_stream_chunk(b"data: [DONE]\n")
    observer.on_response(None, {"content": "A", "tool_calls": [], "finish_reason": "stop"}, "stop", {})

    data = trajectory.get_trajectory(context["trajectory_id"])
    response = next(event for event in data["events"] if event["event_type"] == "model.responded")
    assert json.loads(response["summary_json"])["finish_reason"] == "stop"
    refs = dict(item.split(":", 1) for item in response["artifact_refs"].split(","))
    assert trajectory.load_artifact(refs["raw_sse"])["content"].endswith(b"data: [DONE]\n")


def test_provider_exchange_observer_reports_provider_usage(isolated_workspace):
    context = trajectory.start_trajectory("main_agent")
    observer = trajectory.provider_exchange_observer(context, purpose="qa")
    observer.on_request(b"{}", {"model": "fake", "messages": []}, {})
    observer.on_response(
        b'{"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}',
        {"content": "answer"},
        "stop",
        {},
    )

    data = trajectory.get_trajectory(context["trajectory_id"])
    response = next(event for event in data["events"] if event["event_type"] == "model.responded")
    assert json.loads(response["summary_json"])["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
    }


def test_trajectory_debug_commands_cover_roots_errors_paths_and_artifacts(isolated_workspace):
    from commands import command_registry, structured_commands

    context = trajectory.start_trajectory("main_agent")
    span = trajectory.start_span(context, "tool_call", "broken_tool")
    event = trajectory.record_event(span, "tool.failed", status="failed", artifacts={"tool_result": "failure body"})
    trajectory.fail_span(span, RuntimeError("boom"))
    trajectory.fail_trajectory(context, RuntimeError("boom"))
    artifact_id = event["artifacts"][0]["artifact_id"]

    assert context["trajectory_id"] in structured_commands.handle_trajectory_command("/trajectory")
    assert "broken_tool" in structured_commands.handle_trajectory_command("/trajectory current", {"trajectory_context": context})
    assert "tool.failed" in structured_commands.handle_trajectory_command("/trajectory errors")
    assert "trajectory.sqlite3" in structured_commands.handle_trajectory_command("/trajectory path")
    assert artifact_id in structured_commands.handle_trajectory_command(f"/trajectory artifact {artifact_id}")
    assert "/trace" not in command_registry.SLASH_COMMANDS
