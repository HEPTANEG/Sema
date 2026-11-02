import json


def _candidate(object_id, title="Title", summary=None):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "title": title,
        "summary": summary,
        "retrieval_evidence": {"channels": ["general_semantic"], "sources": ["embedding"], "scores": {}},
    }


def _memory_object(object_id, title="Title", payload=None, created_at="2026-01-01T00:00:00+00:00"):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "status": "active",
        "title": title,
        "summary": "derived summary",
        "created_at": created_at,
        "updated_at": created_at,
        "payload": payload,
    }


def test_rank_2_preparation_is_stm_only_and_bounded():
    from memory_engine.ltm.memory_push_ranking import (
        RANK_2_QUERY_MAX_TOKENS,
        build_rank_2_query,
        prepare_rank_2_candidates,
    )
    from memory_engine.token_counter import count_text_tokens

    query = build_rank_2_query([
        {"role": "system", "content": "never include this"},
        {"role": "user", "content": "latest context"},
    ])
    assert "never include this" not in query
    assert "latest context" in query

    prepared = prepare_rank_2_candidates([_candidate("mem_a", "A", "summary")])
    assert prepared == [{"object_id": "mem_a", "text": "A\nsummary"}]

    long_messages = [{"role": "user", "content": "latest " + ("中文内容 " * 2000)}]
    bounded_query = build_rank_2_query(long_messages)
    assert count_text_tokens(bounded_query) <= RANK_2_QUERY_MAX_TOKENS
    assert "latest" in bounded_query


def test_rank_3_result_validation_is_strict():
    from memory_engine.ltm.memory_push_ranking import _validate_rank_3_result
    from memory_engine.ltm.object_references import ObjectReferenceMap

    references = ObjectReferenceMap()
    references.register("mem_a")
    assert _validate_rank_3_result('{"selected_refs":[1]}', references) == ["mem_a"]
    for raw in (
        '{"selected_refs":[2]}',
        '{"selected_refs":[1,1]}',
        '{"selected_refs":[],"extra":1}',
        "```json\n{\"selected_refs\": []}\n```",
    ):
        try:
            _validate_rank_3_result(raw, references)
        except RuntimeError:
            continue
        raise AssertionError(f"invalid Rank 3 result was accepted: {raw}")


def test_rank_3_truncates_excess_selection_after_response():
    from memory_engine.ltm.memory_push_ranking import _validate_rank_3_result
    from memory_engine.ltm.object_references import ObjectReferenceMap

    references = ObjectReferenceMap()
    for index in range(45):
        references.register(f"mem_{index}")
    selected = list(range(1, 46))

    assert _validate_rank_3_result(
        json.dumps({"selected_refs": selected}),
        references,
    ) == [f"mem_{index}" for index in range(40)]


def test_render_memory_push_uses_payload_or_title_only():
    from memory_engine.ltm.memory_push_ranking import render_memory_push

    content = render_memory_push([
        _memory_object("mem_a", title="Should not appear", payload={"schema_version": 1, "body": "original body", "structured_data": None}),
        _memory_object("mem_b", title="Title only", payload=None),
    ])
    assert "original body" in content
    assert "Should not appear" not in content
    assert "Title only" in content
    assert "derived summary" not in content


def test_validate_push_result_rehydrates_selected_objects_in_one_batch(monkeypatch):
    from memory_engine.ltm import memory_push_ranking as ranking

    calls = []
    objects = {
        "mem_a": {
            **_memory_object("mem_a", "A", payload={"schema_version": 1, "body": "A body", "structured_data": None}),
            "derived": {"strength": 1.0},
        },
        "mem_b": {
            **_memory_object("mem_b", "B", payload=None),
            "derived": {"strength": 0.09},
        },
    }

    def get_objects(object_ids, **_kwargs):
        calls.append(list(object_ids))
        return {object_id: objects[object_id] for object_id in object_ids if object_id in objects}

    monkeypatch.setattr(ranking.ltm_store, "get_objects", get_objects)
    monkeypatch.setattr(
        ranking.ltm_store,
        "get_object",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("single-object read")),
    )

    result = ranking.validate_push_result({"used_object_ids": ["mem_a", "mem_b"]})

    assert calls == [["mem_a", "mem_b"]]
    assert result["used_object_ids"] == ["mem_a"]
    assert "A body" in result["content"]


def test_run_memory_push_skips_unneeded_stages_and_orders_provenance(monkeypatch):
    import memory_engine.ltm.memory_push as memory_push
    import memory_engine.ltm.memory_push_rank_1 as memory_push_rank_1
    import memory_engine.ltm.memory_push_ranking as ranking
    import memory_engine.ltm.retrieval_context_compiler as compiler
    from persistence import trajectory

    monkeypatch.setattr(compiler, "compile_retrieval_queries", lambda _contexts, **_kwargs: {"channels": [], "errors": []})
    monkeypatch.setattr(compiler, "build_channel_contexts", lambda stm, wm: {"stm": stm, "wm": wm})
    monkeypatch.setattr(memory_push, "build_candidate_pool", lambda *_args, **_kwargs: {"status": "ok", "items": [_candidate("mem_b"), _candidate("mem_a")], "query_evidence": [], "errors": []})
    monkeypatch.setattr(memory_push_rank_1, "rank_1_candidate_pool", lambda pool, **_kwargs: {"status": "ok", "items": pool["items"], "query_evidence": [], "errors": []})
    monkeypatch.setattr(ranking, "rank_2_candidates", lambda *_args, **_kwargs: {"status": "ok", "items": [{"object_id": "mem_b", "relevance_score": 0.8}, {"object_id": "mem_a", "relevance_score": 0.9}], "errors": []})
    monkeypatch.setattr(ranking, "_full_objects", lambda *_args, **_kwargs: {
        "mem_a": _memory_object("mem_a", "A", payload={"schema_version": 1, "body": "A body", "structured_data": None}, created_at="2026-01-02T00:00:00+00:00"),
        "mem_b": _memory_object("mem_b", "B", payload=None, created_at="2026-01-01T00:00:00+00:00"),
    })
    monkeypatch.setattr(ranking, "_rank_3_json_response", lambda *_args, **_kwargs: json.dumps({"selected_refs": [1, 2]}))
    monkeypatch.setattr(ranking.ltm_store, "get_links", lambda object_id, *_args, **_kwargs: [{"target_kind": "event_slot", "slot_seq_start": 20, "slot_seq_end": 20}] if object_id == "mem_a" else [])

    events = []
    parent = trajectory.start_trajectory("main_agent")
    result = ranking.run_memory_push(
        [{"role": "user", "content": "current"}],
        [],
        runtime_state={"ui_event_sink": events.append, "trajectory_context": parent},
    )

    assert result["status"] == "ok"
    data = trajectory.get_trajectory(parent["trajectory_id"])
    assert any(span["span_type"] == "memory_push" and span["status"] == "completed" for span in data["spans"])
    assert not trajectory.list_trajectories(process_type="memory_push")
    assert result["metrics"]["rank_1_count"] == 0
    assert result["metrics"]["rank_2_input_count"] == 0
    assert result["metrics"]["rank_2_output_count"] == 0
    assert result["metrics"]["rank_3_selected_count"] == 2
    assert result["content"].index("A body") < result["content"].index("B")
    assert [(event["type"], event["status"]) for event in events] == [
        ("memory_push_started", "running"),
        ("memory_push_finished", "ok"),
    ]


def test_run_memory_push_routes_rank_1_and_rank_2_by_pool_size(monkeypatch):
    import memory_engine.ltm.memory_push as memory_push
    import memory_engine.ltm.memory_push_rank_1 as memory_push_rank_1
    import memory_engine.ltm.memory_push_ranking as ranking
    import memory_engine.ltm.retrieval_context_compiler as compiler

    monkeypatch.setattr(compiler, "compile_retrieval_queries", lambda *_args, **_kwargs: {"channels": [], "errors": []})
    monkeypatch.setattr(compiler, "build_channel_contexts", lambda *_args: {})
    calls = {"rank_1": 0, "rank_2": 0}

    def build_pool(*_args, **_kwargs):
        size = build_pool.size
        return {"status": "ok", "items": [_candidate(f"mem_{index}") for index in range(size)], "query_evidence": [], "errors": []}

    def rank(pool, **_kwargs):
        calls["rank_1"] += 1
        return {"status": "ok", "items": pool["items"][:400], "query_evidence": [], "errors": []}

    def rank_2(*_args, **kwargs):
        calls["rank_2"] += 1
        candidates = kwargs.get("candidates") or _args[1]
        return {"status": "ok", "items": [{"object_id": item["object_id"]} for item in candidates[:80]], "errors": []}

    monkeypatch.setattr(memory_push, "build_candidate_pool", build_pool)
    monkeypatch.setattr(memory_push_rank_1, "rank_1_candidate_pool", rank)
    monkeypatch.setattr(ranking, "rank_2_candidates", rank_2)
    monkeypatch.setattr(
        ranking,
        "_full_objects",
        lambda items, **_kwargs: {
            item["object_id"]: _memory_object(item["object_id"], item["object_id"])
            for item in items
        },
    )
    monkeypatch.setattr(ranking, "_rank_3_json_response", lambda *_args, **_kwargs: '{"selected_refs": []}')

    for size in (2, 80, 81, 400, 401):
        build_pool.size = size
        result = ranking.run_memory_push([], [], runtime_state={})
        assert result["status"] == "ok"
        if size <= 80:
            assert result["metrics"]["rank_1_count"] == 0
            assert result["metrics"]["rank_2_input_count"] == 0
        elif size <= 400:
            assert result["metrics"]["rank_1_count"] == 0
            assert result["metrics"]["rank_2_input_count"] == size
        else:
            assert result["metrics"]["rank_1_count"] == 400
            assert result["metrics"]["rank_2_input_count"] == 400
    assert calls == {"rank_1": 1, "rank_2": 3}


def test_incremental_rechecks_old_candidate_not_in_selected_objects(monkeypatch):
    import memory_engine.ltm.memory_push as memory_push
    import memory_engine.ltm.memory_push_ranking as ranking
    import memory_engine.ltm.retrieval_context_compiler as compiler

    baseline = {
        "status": "ok",
        "content": "<long-term-memory>old</long-term-memory>",
        "used_object_ids": ["mem_old"],
        "selected_objects": [_memory_object("mem_old", "Old")],
        "candidate_pool": {
            "status": "ok",
            "items": [_candidate("mem_old"), _candidate("mem_f1", "F1")],
            "errors": [],
        },
    }
    monkeypatch.setattr(compiler, "build_channel_contexts", lambda *_args: {})
    monkeypatch.setattr(compiler, "compile_retrieval_queries", lambda *_args, **_kwargs: {"channels": [], "errors": []})
    monkeypatch.setattr(
        memory_push,
        "build_candidate_pool",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "items": [_candidate("mem_f1", "F1")],
            "query_evidence": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        ranking,
        "_full_objects",
        lambda items, **_kwargs: {"mem_f1": _memory_object("mem_f1", "F1")},
    )
    rank_3_inputs = []
    monkeypatch.setattr(
        ranking,
        "_rank_3_json_response",
        lambda _runtime, messages: rank_3_inputs.append(messages) or '{"selected_refs":[1]}',
    )
    result = ranking.run_memory_push([], [], runtime_state={}, mode="incremental", baseline=baseline)

    assert result["status"] == "ok"
    assert [item["object_id"] for item in result["selected_objects"]] == ["mem_f1", "mem_old"]
    assert [item["object_id"] for item in result["candidate_pool"]["items"]] == ["mem_old", "mem_f1"]
    assert rank_3_inputs
    assert '"object_id": "mem_f1"' not in rank_3_inputs[0][1]["content"]
    assert '"ref": 1' in rank_3_inputs[0][1]["content"]


def test_incremental_candidate_pool_union_deduplicates_and_prefers_new_records():
    from memory_engine.ltm.memory_push_ranking import _merge_candidate_pool_items

    merged = _merge_candidate_pool_items(
        [_candidate("mem_old", "old"), _candidate("mem_shared", "stale")],
        [_candidate("mem_shared", "fresh"), _candidate("mem_new", "new"), _candidate("mem_new", "newer")],
    )

    assert [item["object_id"] for item in merged] == ["mem_old", "mem_shared", "mem_new"]
    assert [item["title"] for item in merged] == ["old", "fresh", "newer"]


def test_memory_push_stage_ui_failures_do_not_change_fail_open_behavior(monkeypatch):
    import memory_engine.ltm.memory_push_ranking as ranking

    monkeypatch.setattr(ranking.retrieval_context_compiler, "build_channel_contexts", lambda *_args: {})
    runtime_state = {"ui_event_sink": lambda _event: (_ for _ in ()).throw(RuntimeError("ui unavailable"))}

    result = ranking.run_memory_push([], [], runtime_state=runtime_state)

    assert result["status"] == "error"
    assert "Memory Push failed" in result["content"]
    assert runtime_state["ui_event_errors"]


def test_memory_push_failure_placeholder_exposes_stage_and_stable_reason(monkeypatch):
    import memory_engine.ltm.memory_push_ranking as ranking

    monkeypatch.setattr(ranking.retrieval_context_compiler, "build_channel_contexts", lambda *_args: {})
    result = ranking.run_memory_push([], [], runtime_state={})

    assert result["status"] == "error"
    assert "stage: query_compilation" in result["content"]
    assert "reason: query_compilation_failed" in result["content"]
    assert "error_type" not in result["content"]


def test_memory_push_empty_candidate_placeholder_is_not_injected(monkeypatch):
    import memory_engine.ltm.memory_push_ranking as ranking

    monkeypatch.setattr(ranking.retrieval_context_compiler, "build_channel_contexts", lambda *_args: {})
    monkeypatch.setattr(
        ranking.retrieval_context_compiler,
        "compile_retrieval_queries",
        lambda *_args, **_kwargs: {"channels": [{"channel": "entity", "queries": ["term"]}], "errors": []},
    )
    monkeypatch.setattr(ranking.memory_push, "build_candidate_pool", lambda *_args, **_kwargs: {"status": "ok", "items": [], "errors": []})

    result = ranking.run_memory_push([], [], runtime_state={})

    assert result["status"] == "ok"
    assert result["metrics"]["reason_code"] == "empty_candidate_pool"
    assert "No relevant long-term memory" in result["content"]
    assert result["diagnostic_only"] is True


def test_run_memory_push_failure_is_fail_open_with_placeholder(monkeypatch):
    import memory_engine.ltm.memory_push_ranking as ranking

    monkeypatch.setattr(ranking.retrieval_context_compiler, "build_channel_contexts", lambda *_args: {})
    monkeypatch.setattr(ranking, "memory_push_rank_1", None)
    result = ranking.run_memory_push([], [], runtime_state={})
    assert result["status"] == "error"
    assert "Memory Push failed" in result["content"]


def test_memory_push_runs_once_per_turn(monkeypatch):
    import memory_engine.context_builder as context_builder

    calls = []
    monkeypatch.setattr(
        context_builder.memory_push_ranking,
        "run_memory_push",
        lambda *_args, **_kwargs: calls.append(True) or {"status": "ok", "content": "<long-term-memory>memory</long-term-memory>", "metrics": {}},
    )
    runtime_state = {"_memory_push_enabled": True}
    first = context_builder._memory_push_for_turn([], [], runtime_state)
    second = context_builder._memory_push_for_turn([], [], runtime_state)
    assert first == second
    assert len(calls) == 1


def test_context_builder_injects_ltm_between_wm_and_stm(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder


    monkeypatch.setattr(context_builder.memory_push_ranking, "validate_push_result", lambda result: result)

    monkeypatch.setattr(context_builder.focus, "run_focus_detector_before_context", lambda *_args: None)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"stm": 1000, "wm": 1000}, "scores": {}})
    monkeypatch.setattr(context_builder.event_stream, "load_active_trunk_records", lambda: [])
    monkeypatch.setattr(context_builder.wm_ingress, "stage_wm_ingress_from_projection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        context_builder,
        "build_stm_projection",
        lambda *_args, **_kwargs: {
            "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "latest"}],
            "selected_blocks": [],
            "budget": 1000,
            "selected_tokens": 1,
            "wm_ingress_watermark": -1,
        },
    )
    calls = []
    successful_uses = []

    def fake_memory_push(*_args, **kwargs):
        calls.append(kwargs.get("mode"))
        return {
            "status": "ok",
            "content": "<long-term-memory>remembered</long-term-memory>",
            "used_object_ids": ["mem_a", "mem_a", "mem_b"],
            "metrics": {},
        }

    monkeypatch.setattr(
        context_builder.memory_push_ranking,
        "run_memory_push",
        fake_memory_push,
    )
    monkeypatch.setattr(
        context_builder.ltm_store,
        "record_successful_uses",
        lambda object_ids, idempotency_key: successful_uses.append((object_ids, idempotency_key)) or {"status": "committed"},
    )
    runtime_state = {"_memory_push_enabled": True, "trace_id": "push-trace"}

    first = context_builder.build_event_stream_context([], runtime_state=runtime_state)
    second = context_builder.build_event_stream_context([], runtime_state=runtime_state)

    assert [message["content"] for message in first["messages"]] == [
        "system",
        "<long-term-memory>remembered</long-term-memory>",
        "latest",
    ]
    assert second["messages"] == first["messages"]
    assert [mode for mode in calls if mode != "full_background"] == ["full"]
    assert successful_uses == [(["mem_a", "mem_b"], "successful_use:memory_push:push-trace")]
    assert first["memory_push"]["metrics"]["successful_use_status"] == "committed"


def test_reranker_service_request_validation():
    from memory_engine.ltm.reranker_service import RerankerRequestError, _validate_request

    query, candidates = _validate_request({"query": "context", "candidates": [{"object_id": "mem_a", "text": "memory"}]})
    assert query == "context"
    assert candidates[0]["object_id"] == "mem_a"
    for payload in ({"query": "", "candidates": []}, {"query": "q", "candidates": [{"object_id": "mem_a"}]}):
        try:
            _validate_request(payload)
        except RerankerRequestError:
            continue
        raise AssertionError("invalid reranker request was accepted")


def test_reranker_service_scores_candidates_in_gpu_batches():
    from memory_engine.ltm import reranker_service

    class FakeSigmoid:
        pass

    class FakeNN:
        Sigmoid = FakeSigmoid

    class FakeCuda:
        OutOfMemoryError = RuntimeError

        @staticmethod
        def empty_cache():
            raise AssertionError("CUDA cache should not be cleared without OOM")

    class FakeTorch:
        nn = FakeNN()
        cuda = FakeCuda()

    class FakeModel:
        def __init__(self):
            self.batch_sizes = []

        def predict(self, batch, **kwargs):
            self.batch_sizes.append(kwargs["batch_size"])
            assert kwargs["batch_size"] == len(batch)
            return [float(pair[1].split()[-1]) for pair in batch]

    model = FakeModel()
    candidates = [{"object_id": f"mem_{index}", "text": f"memory {index}"} for index in range(10)]

    scores = reranker_service._score(model, FakeTorch, "cuda", "context", candidates)

    assert model.batch_sizes == [8, 2]
    assert scores == [float(index) for index in range(10)]


def test_reranker_service_retries_same_batch_after_cuda_oom():
    from memory_engine.ltm import reranker_service

    class FakeOOM(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOOM
        empty_cache_calls = 0

        @classmethod
        def empty_cache(cls):
            cls.empty_cache_calls += 1

    class FakeNN:
        Sigmoid = lambda self: object()

    class FakeTorch:
        cuda = FakeCuda
        nn = FakeNN()

    class FakeModel:
        def __init__(self):
            self.batch_sizes = []

        def predict(self, batch, **kwargs):
            self.batch_sizes.append(len(batch))
            if len(batch) > 2:
                raise FakeOOM("CUDA out of memory")
            return [0.5] * len(batch)

    model = FakeModel()
    candidates = [{"object_id": f"mem_{index}", "text": "memory"} for index in range(5)]

    scores = reranker_service._score(model, FakeTorch, "cuda", "context", candidates)

    assert model.batch_sizes == [5, 2, 2, 1]
    assert scores == [0.5] * 5
    assert FakeCuda.empty_cache_calls == 1


def test_reranker_service_fails_when_single_cuda_example_still_ooms():
    from memory_engine.ltm import reranker_service

    class FakeOOM(RuntimeError):
        pass

    class FakeCuda:
        OutOfMemoryError = FakeOOM
        empty_cache_calls = 0

        @classmethod
        def empty_cache(cls):
            cls.empty_cache_calls += 1

    class FakeNN:
        Sigmoid = lambda self: object()

    class FakeTorch:
        cuda = FakeCuda
        nn = FakeNN()

    class FakeModel:
        def predict(self, batch, **kwargs):
            raise FakeOOM("CUDA out of memory")

    candidates = [{"object_id": "mem_a", "text": "memory"}]
    try:
        reranker_service._score(FakeModel(), FakeTorch, "cuda", "context", candidates)
    except FakeOOM:
        pass
    else:
        raise AssertionError("single-example CUDA OOM should fail the request")


def test_reranker_service_does_not_use_cuda_backoff_on_cpu():
    from memory_engine.ltm import reranker_service

    class ForbiddenCuda:
        def __getattr__(self, name):
            raise AssertionError(f"CPU scoring accessed CUDA.{name}")

    class FakeNN:
        Sigmoid = lambda self: object()

    class FakeTorch:
        cuda = ForbiddenCuda()
        nn = FakeNN()

    class FakeModel:
        def __init__(self):
            self.batch_sizes = []

        def predict(self, batch, **kwargs):
            self.batch_sizes.append(kwargs["batch_size"])
            return [0.25] * len(batch)

    model = FakeModel()
    candidates = [{"object_id": f"mem_{index}", "text": "memory"} for index in range(3)]

    assert reranker_service._score(model, FakeTorch, "cpu", "context", candidates) == [0.25] * 3
    assert model.batch_sizes == [1, 1, 1]


def test_reranker_service_does_not_swallow_non_oom_model_errors():
    from memory_engine.ltm import reranker_service

    class FakeCuda:
        OutOfMemoryError = RuntimeError

        @staticmethod
        def empty_cache():
            raise AssertionError("non-OOM errors must not trigger CUDA cleanup")

    class FakeNN:
        Sigmoid = lambda self: object()

    class FakeTorch:
        cuda = FakeCuda
        nn = FakeNN()

    class FakeModel:
        def predict(self, batch, **kwargs):
            raise ValueError("tokenizer failed")

    try:
        reranker_service._score(
            FakeModel(),
            FakeTorch,
            "cuda",
            "context",
            [{"object_id": "mem_a", "text": "memory"}],
        )
    except ValueError as error:
        assert str(error) == "tokenizer failed"
    else:
        raise AssertionError("non-OOM model errors must propagate")
