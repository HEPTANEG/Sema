import json


def _model_response(content):
    return {"choices": [{"message": {"content": content}}]}


def test_build_channel_contexts_uses_stm_slots_and_latest_quarter():
    from memory_engine.ltm.retrieval_context_compiler import build_channel_contexts

    stm = [
        {"role": "system", "content": "projection instructions"},
        *[{"role": "user", "content": f"slot-{index}"} for index in range(1, 9)],
    ]
    wm = [{"role": "assistant", "content": "working"}]
    contexts = build_channel_contexts(stm, wm)

    expected_stm = stm[1:]
    assert contexts["general_semantic"] == expected_stm
    assert contexts["entity"] == expected_stm
    assert contexts["latest_semantic"] == expected_stm[-2:]
    assert contexts["general_semantic"] is not contexts["latest_semantic"]


def test_latest_semantic_uses_slot_count_not_token_count():
    from memory_engine.ltm.retrieval_context_compiler import build_channel_contexts

    stm = [
        {"role": "user", "content": "short"},
        {"role": "tool", "content": "very large tool result " * 1000},
        {"role": "user", "content": "middle"},
        {"role": "user", "content": "newest"},
        {"role": "assistant", "content": "latest answer"},
    ]

    contexts = build_channel_contexts(stm, [])

    assert [message["content"] for message in contexts["latest_semantic"]] == ["newest", "latest answer"]


def test_compiler_validates_and_deduplicates_channel_results(monkeypatch):
    import memory_engine.ltm.retrieval_context_compiler as compiler

    responses = {
        "memory_push_general_semantic": '{"channel":"general_semantic","queries":["a","a","b"]}',
        "memory_push_latest_semantic": '{"channel":"latest_semantic","queries":["latest"]}',
        "memory_push_entity": '{"channel":"entity","queries":["Agent","代理"]}',
    }
    monkeypatch.setattr(compiler, "load_prompt", lambda name: name)
    monkeypatch.setattr(
        compiler,
        "call_chat_completion_stream",
        lambda messages, **kwargs: _model_response(responses[messages[0]["content"]]),
    )

    contexts = compiler.build_channel_contexts([], [])
    result = compiler.compile_retrieval_queries(contexts)

    assert result["errors"] == []
    assert result["channels"] == [
        {"channel": "general_semantic", "queries": ["a", "b"]},
        {"channel": "latest_semantic", "queries": ["latest"]},
        {"channel": "entity", "queries": ["Agent", "代理"]},
    ]


def test_compiler_isolates_invalid_channel(monkeypatch):
    import memory_engine.ltm.retrieval_context_compiler as compiler

    monkeypatch.setattr(compiler, "load_prompt", lambda name: name)

    def fake_call(messages, **kwargs):
        name = messages[0]["content"]
        if name == "memory_push_latest_semantic":
            raise RuntimeError("provider unavailable")
        channel = name.removeprefix("memory_push_")
        return _model_response(json.dumps({"channel": channel, "queries": [channel]}))

    monkeypatch.setattr(compiler, "call_chat_completion_stream", fake_call)
    result = compiler.compile_retrieval_queries(compiler.build_channel_contexts([], []))

    assert result["channels"][0]["queries"] == ["general_semantic"]
    assert result["channels"][1]["queries"] == []
    assert result["channels"][2]["queries"] == ["entity"]
    assert result["errors"][0]["channel"] == "latest_semantic"


def test_compiler_limits_queries_to_twenty(monkeypatch):
    import memory_engine.ltm.retrieval_context_compiler as compiler

    monkeypatch.setattr(compiler, "load_prompt", lambda name: name)
    queries = [f"q-{index}" for index in range(25)]
    monkeypatch.setattr(
        compiler,
        "call_chat_completion_stream",
        lambda messages, **kwargs: _model_response(
            json.dumps({"channel": messages[0]["content"].removeprefix("memory_push_"), "queries": queries})
        ),
    )

    result = compiler.compile_retrieval_queries(compiler.build_channel_contexts([], []))
    assert all(len(item["queries"]) == 20 for item in result["channels"])


def test_memory_push_prompts_are_registered_and_distinct():
    from shared.prompt_loader import load_prompt

    general = load_prompt("memory_push_general_semantic")
    latest = load_prompt("memory_push_latest_semantic")
    entity = load_prompt("memory_push_entity")

    assert "general_semantic" in general
    assert "latest_semantic" in latest
    assert "entity" in entity
    assert "directly extract" in entity
    assert "Grounded Interpretation" in general
    assert "no more than 20 queries" in general
    assert "no more than 20 queries" in latest
    assert "Do not enumerate every entity" in entity


def test_semantic_search_hydrates_active_objects_and_excludes_roots(monkeypatch):
    import memory_engine.ltm.ltm_retrieval as retrieval

    class FakeIndex:
        def __init__(self, root=None):
            pass

        def search(self, vector, limit):
            return [
                {"object_id": "mem_root", "similarity": 0.99},
                {"object_id": "mem_a", "similarity": 0.88},
            ]

    monkeypatch.setattr(retrieval, "_active_root_tag_ids", lambda root=None: {"mem_root"})
    monkeypatch.setattr(retrieval, "_semantic_objects_by_ids", lambda *args, **kwargs: [
        {"object_id": "mem_a", "object_type": "assertion", "status": "active", "title": "A", "summary": "S"},
    ])
    import memory_engine.ltm.embedding_adapter as embedding_adapter
    import memory_engine.ltm.vector_index as vector_index
    monkeypatch.setattr(embedding_adapter, "embed_queries", lambda texts: type("Batch", (), {"vectors": ((1.0,),)})())
    monkeypatch.setattr(vector_index, "VectorIndexAdapter", FakeIndex)

    result = retrieval.semantic_search("query", profile=retrieval.MEMORY_PUSH_PROFILE)

    assert result["status"] == "ok"
    assert result["items"] == [{
        "object_id": "mem_a",
        "object_type": "assertion",
        "status": "active",
        "title": "A",
        "summary": "S",
        "similarity": 0.88,
        "retrieval_source": "embedding",
    }]


def test_semantic_search_many_batches_embeddings_and_preserves_query_order(monkeypatch):
    import memory_engine.ltm.ltm_retrieval as retrieval

    embed_calls = []
    object_read_calls = []

    class Batch:
        vectors = ((1.0,), (2.0,))

    class FakeIndex:
        def __init__(self, root=None):
            pass

        def search(self, vector, limit):
            object_id = "mem_one" if vector[0] == 1.0 else "mem_two"
            return [{"object_id": object_id, "similarity": vector[0] / 2.0}]

    def fake_objects_by_ids_many(groups, *args, **kwargs):
        object_read_calls.append([list(group) for group in groups])
        return [
            [
                {
                    "object_id": object_id,
                    "object_type": "assertion",
                    "status": "active",
                    "title": object_id,
                    "summary": None,
                }
                for object_id in group
            ]
            for group in groups
        ]

    monkeypatch.setattr(retrieval, "_semantic_objects_by_ids_many", fake_objects_by_ids_many)
    monkeypatch.setattr(retrieval, "_active_root_tag_ids", lambda root=None: set())
    import memory_engine.ltm.embedding_adapter as embedding_adapter
    import memory_engine.ltm.vector_index as vector_index
    monkeypatch.setattr(embedding_adapter, "embed_queries", lambda texts: embed_calls.append(list(texts)) or Batch())
    monkeypatch.setattr(vector_index, "VectorIndexAdapter", FakeIndex)

    results = retrieval.semantic_search_many(["one", "two"], profile=retrieval.MEMORY_PUSH_PROFILE)

    assert embed_calls == [["one", "two"]]
    assert object_read_calls == [[["mem_one"], ["mem_two"]]]
    assert [result["items"][0]["object_id"] for result in results] == ["mem_one", "mem_two"]
    assert [result["items"][0]["similarity"] for result in results] == [0.5, 1.0]


def test_candidate_pool_integrates_sources_and_preserves_raw_scores(monkeypatch):
    import memory_engine.ltm.memory_push as push

    def fake_semantic_many(queries, *, profile, root=None):
        return [{
            "status": "ok",
            "items": [{
                "object_id": "mem_a",
                "object_type": "assertion",
                "status": "active",
                "title": "A",
                "summary": "S",
                "similarity": 0.8 if query == "one" else 0.7,
                "strength": 0.5,
            }],
            "errors": [],
        } for query in queries]

    def fake_search(params, *, profile, root=None, _include_raw_score=False):
        assert _include_raw_score is True
        return {
            "status": "ok",
            "items": [{
                "object_id": "mem_a",
                "object_type": "assertion",
                "status": "active",
                "title": "A",
                "summary": "S",
                "_raw_score": -2.5,
                "strength": 0.5,
            }],
            "errors": [],
        }

    monkeypatch.setattr(push.ltm_retrieval, "semantic_search_many", fake_semantic_many)
    monkeypatch.setattr(push.ltm_retrieval, "search_objects", fake_search)
    result = push.build_candidate_pool({
        "channels": [
            {"channel": "general_semantic", "queries": ["one"]},
            {"channel": "entity", "queries": ["entity"]},
            {"channel": "latest_semantic", "queries": []},
        ]
    })

    assert result["status"] == "ok"
    assert len(result["items"]) == 1
    assert result["items"][0]["retrieval_evidence"] == {
        "channels": ["general_semantic", "entity"],
        "sources": ["embedding", "lexical"],
        "scores": {"embedding": [0.8, 0.7], "lexical": [-2.5]},
    }


def test_candidate_pool_returns_empty_when_all_queries_fail(monkeypatch):
    import memory_engine.ltm.memory_push as push

    monkeypatch.setattr(
        push.ltm_retrieval,
        "semantic_search_many",
        lambda queries, **kwargs: [
            {"status": "error", "items": [], "errors": [{"code": "unavailable"}]}
            for _query in queries
        ],
    )
    result = push.build_candidate_pool({
        "channels": [{"channel": "general_semantic", "queries": ["query"]}]
    })

    assert result["status"] == "error"
    assert result["items"] == []
    assert result["errors"]
