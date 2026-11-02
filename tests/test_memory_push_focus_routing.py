def _result(content="memory", object_ids=None, candidate_ids=None, mode="full"):
    object_ids = object_ids or ["mem_old"]
    candidate_ids = candidate_ids or object_ids
    return {
        "status": "ok",
        "content": f"<long-term-memory>{content}</long-term-memory>",
        "used_object_ids": object_ids,
        "selected_objects": [{"object_id": object_id, "title": object_id} for object_id in object_ids],
        "candidate_pool": {
            "status": "ok",
            "items": [{"object_id": object_id} for object_id in candidate_ids],
            "errors": [],
        },
        "metrics": {"mode": mode},
    }


def test_focus_routes_full_incremental_and_shift(monkeypatch, isolated_workspace):
    import memory_engine.context_builder as builder

    cache = {"entry": None, "state": {"stable_turn_count": 0, "latest_revision": -1}}
    calls = []
    monkeypatch.setattr(builder.memory_push_cache, "load_latest", lambda _root=None: cache["entry"])
    monkeypatch.setattr(builder.memory_push_cache, "load_state", lambda _root=None: dict(cache["state"]))

    def save(_root, *, revision, focus_state, mode, result):
        cache["entry"] = {
            "revision": revision,
            "focus_state": focus_state,
            "mode": mode,
            **result,
        }
        return cache["entry"]

    def save_state(_root=None, **updates):
        cache["state"].update(updates)
        return dict(cache["state"])

    monkeypatch.setattr(builder.memory_push_cache, "save", save)
    monkeypatch.setattr(builder.memory_push_cache, "save_state", save_state)
    monkeypatch.setattr(
        builder.memory_push_ranking,
        "run_memory_push",
        lambda *_args, **kwargs: calls.append(kwargs) or _result(mode=kwargs["mode"]),
    )

    runtime = {"_memory_push_enabled": True}
    builder._memory_push_for_turn([], [], runtime, source_revision=1)
    assert calls[-1]["mode"] == "full"

    runtime = {"_memory_push_enabled": True}
    builder._memory_push_for_turn([], [], runtime, source_revision=2)
    assert calls[-1]["mode"] == "incremental"
    assert calls[-1]["baseline"] is not None

    runtime = {"_memory_push_enabled": True}
    builder._memory_push_for_turn([], [], runtime, focus_action="created_active", source_revision=3)
    assert calls[-1]["mode"] == "focus_shift_sync"
    assert calls[-1]["baseline"] is None


def test_cache_keeps_five_recent_entries(tmp_path):
    from memory_engine.ltm import memory_push_cache

    for revision in range(7):
        memory_push_cache.save(
            tmp_path,
            revision=revision,
            focus_state="stable",
            mode="full",
            result=_result(content=str(revision)),
        )

    latest = memory_push_cache.load_latest(tmp_path)
    assert latest["revision"] == 6
    entries = memory_push_cache._read_index(tmp_path)
    assert len(entries) == 5
    assert {entry["revision"] for entry in entries} == {2, 3, 4, 5, 6}


def test_incremental_reuses_baseline_when_query_returns_no_items(monkeypatch):
    import memory_engine.ltm.memory_push_ranking as ranking

    baseline = _result(content="old", object_ids=["mem_old"])
    monkeypatch.setattr(ranking.retrieval_context_compiler, "build_channel_contexts", lambda *_args: {})
    monkeypatch.setattr(ranking.retrieval_context_compiler, "compile_retrieval_queries", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ranking.memory_push, "build_candidate_pool", lambda *_args, **_kwargs: {"status": "ok", "items": [], "errors": []})

    result = ranking.run_memory_push([], [], runtime_state={}, mode="incremental", baseline=baseline)
    assert result["content"] == baseline["content"]
    assert result["used_object_ids"] == ["mem_old"]
