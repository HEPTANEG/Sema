import json


def _candidate(object_id="mem_a"):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "title": "Visible title must not be logged",
        "summary": "Visible summary must not be logged",
        "retrieval_evidence": {"channels": ["general_semantic"], "sources": ["embedding"], "scores": {}},
    }


def _memory_object(object_id="mem_a"):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "status": "active",
        "title": "Visible title must not be logged",
        "summary": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "payload": {
            "schema_version": 1,
            "body": "Visible payload must not be logged",
            "structured_data": None,
        },
    }


def _patch_fake_pipeline(monkeypatch, *, compiler=None):
    import memory_engine.ltm.memory_push as memory_push
    import memory_engine.ltm.memory_push_ranking as ranking
    import memory_engine.ltm.memory_push_rank_1 as rank_1
    import memory_engine.ltm.retrieval_context_compiler as context_compiler

    monkeypatch.setattr(
        context_compiler,
        "compile_retrieval_queries",
        compiler or (lambda _contexts, **_kwargs: {"channels": [{"channel": "general_semantic", "queries": ["secret query"]}]}),
    )

    def fake_pool(_compiled, _diagnostic_profiler=None, **_kwargs):
        if not _compiled.get("channels"):
            return {"status": "ok", "items": [], "query_evidence": [], "errors": []}
        if _diagnostic_profiler is not None:
            _diagnostic_profiler.increment("semantic_query_count")
            _diagnostic_profiler.increment("embedding_request_count")
        return {"status": "ok", "items": [_candidate()], "query_evidence": [], "errors": []}

    def fake_rank(pool, **_kwargs):
        return {"status": "ok", "items": pool["items"], "query_evidence": [], "errors": []}

    def fake_rank_2(_stm, items, **_kwargs):
        if _kwargs.get("_diagnostic_profiler") is not None:
            _kwargs["_diagnostic_profiler"].increment("reranker_request_count")
        return {"status": "ok", "items": [{"object_id": items[0]["object_id"]}], "errors": []}

    monkeypatch.setattr(context_compiler, "build_channel_contexts", lambda stm, wm: {"stm": stm, "wm": wm})
    monkeypatch.setattr(memory_push, "build_candidate_pool", fake_pool)
    monkeypatch.setattr(rank_1, "rank_1_candidate_pool", fake_rank)
    monkeypatch.setattr(ranking, "rank_2_candidates", fake_rank_2)
    monkeypatch.setattr(ranking, "_full_objects", lambda *_args, **_kwargs: {"mem_a": _memory_object()})
    monkeypatch.setattr(ranking, "_rank_3_json_response", lambda *_args, **_kwargs: '{"selected_refs":[1]}')
    monkeypatch.setattr(ranking.embedding_adapter, "close_default_adapter", lambda: None)
    monkeypatch.setattr(ranking.reranker_adapter, "close_default_adapter", lambda: None)
    monkeypatch.setattr(ranking.ltm_store, "get_links", lambda *_args, **_kwargs: [])


def test_real_run_memory_push_writes_turn_records_and_process_summary(monkeypatch, tmp_path):
    _patch_fake_pipeline(monkeypatch)

    from memory_engine.ltm.memory_push_ranking import run_memory_push

    root = tmp_path / "project"
    first = run_memory_push(
        [{"role": "user", "content": "private user message"}],
        [],
        root=root,
        runtime_state={},
    )
    second = run_memory_push([], [], root=root, runtime_state={})

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    records_path = root / ".sema" / "logs" / "memory-push-latency.jsonl"
    summary_path = root / ".sema" / "logs" / "memory-push-latency-summary.json"
    records = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines()]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert len(records) == 2
    assert [record["turn_index"] for record in records] == [1, 2]
    assert records[0]["counts"]["compiler_query_count"] == 1
    assert records[0]["counts"]["candidate_pool_count"] == 1
    assert records[0]["counts"]["rank_3_selected_count"] == 1
    assert summary["turn_count"] == 2
    assert summary["stages"]["total_ms"]["count"] == 2
    assert summary["stages"]["total_ms"]["average_ms"] >= 0

    serialized = records_path.read_text(encoding="utf-8")
    assert "secret query" not in serialized
    assert "private user message" not in serialized
    assert "Visible payload" not in serialized
    assert "Visible title" not in serialized


def test_empty_compilation_records_an_empty_turn(monkeypatch, tmp_path):
    _patch_fake_pipeline(monkeypatch, compiler=lambda _contexts, **_kwargs: {"channels": [], "errors": []})

    from memory_engine.ltm.memory_push_ranking import run_memory_push

    root = tmp_path / "project"
    result = run_memory_push([], [], root=root, runtime_state={})
    record = json.loads((root / ".sema" / "logs" / "memory-push-latency.jsonl").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert "reason: no_compiled_queries" in result["content"]
    assert result["diagnostic_only"] is True
    assert record["status"] == "empty"
    assert record["stop_reason"] == "no_compiled_queries"


def test_failed_memory_push_still_records_error(monkeypatch, tmp_path):
    _patch_fake_pipeline(monkeypatch, compiler=lambda _contexts, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")))

    from memory_engine.ltm.memory_push_ranking import run_memory_push

    root = tmp_path / "project"
    result = run_memory_push([], [], root=root, runtime_state={})
    record = json.loads((root / ".sema" / "logs" / "memory-push-latency.jsonl").read_text(encoding="utf-8"))

    assert result["status"] == "error"
    assert record["status"] == "error"
    assert record["stop_reason"] == "error"
    assert record["error_type"] == "RuntimeError"


def test_diagnostic_write_failures_do_not_change_memory_push(monkeypatch, tmp_path):
    _patch_fake_pipeline(monkeypatch)

    import memory_engine.ltm.memory_push_latency_diagnostics as diagnostics
    from memory_engine.ltm.memory_push_ranking import run_memory_push

    monkeypatch.setattr(diagnostics, "_write_record", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(diagnostics, "_update_summary", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    result = run_memory_push([], [], root=tmp_path / "project", runtime_state={})

    assert result["status"] == "ok"
    assert result["content"]


def test_provider_stream_timing_is_metadata_only(monkeypatch, tmp_path):
    import memory_engine.ltm.memory_push_latency_diagnostics as diagnostics

    profiler = diagnostics.begin_turn(tmp_path / "project")
    profiler.record_provider_timing(
        "rank_3",
        "provider",
        {
            "transport": "stream",
            "headers_ms": 12.5,
            "first_content_ms": 345.6,
            "complete_ms": 456.7,
            "content_delta_count": 4,
        },
    )
    diagnostics.end_turn(profiler, {"status": "ok", "candidate_pool_count": 1})

    record = json.loads(
        (tmp_path / "project" / ".sema" / "logs" / "memory-push-latency.jsonl")
        .read_text(encoding="utf-8")
    )
    assert record["provider_timings"]["rank_3"]["provider"] == {
        "transport": "stream",
        "headers_ms": 12.5,
        "first_content_ms": 345.6,
        "complete_ms": 456.7,
        "content_delta_count": 4,
    }
