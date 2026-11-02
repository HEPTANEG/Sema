import math


def _candidate(object_id, strength=None):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "title": object_id,
        "summary": None,
        "strength": strength,
        "retrieval_evidence": {"channels": ["general_semantic"], "sources": ["embedding"], "scores": {}},
    }


def _evidence(object_id, query_id, source, source_rank, channel="general_semantic"):
    return {
        "object_id": object_id,
        "query_id": query_id,
        "channel": channel,
        "source": source,
        "source_rank": source_rank,
        "raw_score": 0.5,
    }


def test_candidate_integration_retains_query_level_evidence(monkeypatch):
    import memory_engine.ltm.memory_push as push

    def fake_semantic_many(queries, *, profile, root=None):
        return [{
            "status": "ok",
            "items": [
                {"object_id": "mem_a", "object_type": "assertion", "title": "A", "summary": None, "similarity": 0.8},
                {"object_id": "mem_b", "object_type": "assertion", "title": "B", "summary": None, "similarity": 0.7},
            ],
            "errors": [],
        } for _query in queries]

    def fake_search(params, *, profile, root=None, _include_raw_score=False):
        return {
            "status": "ok",
            "items": [
                {"object_id": "mem_b", "object_type": "assertion", "title": "B", "summary": None, "_raw_score": -1.0},
            ],
            "errors": [],
        }

    monkeypatch.setattr(push.ltm_retrieval, "semantic_search_many", fake_semantic_many)
    monkeypatch.setattr(push.ltm_retrieval, "search_objects", fake_search)

    result = push.build_candidate_pool({
        "channels": [
            {"channel": "general_semantic", "queries": ["same"]},
            {"channel": "entity", "queries": ["same"]},
        ]
    })

    evidence = result["query_evidence"]
    assert len(evidence) == 5
    assert {item["query_id"] for item in evidence} == {"q_0001"}
    assert {(item["channel"], item["source"]) for item in evidence} == {
        ("general_semantic", "embedding"),
        ("entity", "embedding"),
        ("entity", "lexical"),
    }
    assert all(item["source_rank"] in {1, 2} for item in evidence)
    assert all("query_evidence" not in item for item in result["items"])


def test_rank_1_uses_best_source_rank_and_auxiliary_support_order():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    result = rank_1_candidate_pool({
        "status": "ok",
        "items": [_candidate("mem_a"), _candidate("mem_b"), _candidate("mem_c")],
        "query_evidence": [
            _evidence("mem_a", "q1", "embedding", 1),
            _evidence("mem_a", "q2", "embedding", 5),
            _evidence("mem_b", "q1", "embedding", 1),
            _evidence("mem_b", "q1", "lexical", 1),
            _evidence("mem_c", "q3", "embedding", 2),
        ],
        "errors": [],
    })

    assert [item["object_id"] for item in result["items"]] == ["mem_b", "mem_a", "mem_c"]
    assert result["items"][0]["rank_1_score"] == 2 / 61
    assert result["items"][1]["rank_1_score"] == 1 / 61 + 0
    assert [item["rank_1_position"] for item in result["items"]] == [1, 2, 3]


def test_rank_1_keeps_best_rank_but_uses_support_as_tiebreaker():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    result = rank_1_candidate_pool({
        "status": "ok",
        "items": [_candidate("mem_a"), _candidate("mem_b"), _candidate("mem_c")],
        "query_evidence": [
            _evidence("mem_a", "q1", "embedding", 1),
            _evidence("mem_b", "q1", "embedding", 1),
            _evidence("mem_b", "q2", "embedding", 7, channel="entity"),
            _evidence("mem_c", "q1", "embedding", 1),
            _evidence("mem_c", "q1", "lexical", 9),
        ],
        "errors": [],
    })

    assert [item["object_id"] for item in result["items"]] == ["mem_c", "mem_b", "mem_a"]


def test_rank_1_ignores_malformed_evidence_and_excludes_uncovered_candidates():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    result = rank_1_candidate_pool({
        "status": "ok",
        "items": [_candidate("mem_a"), _candidate("mem_b")],
        "query_evidence": [
            _evidence("mem_a", "q1", "embedding", 1),
            {"object_id": "mem_a", "query_id": "q2", "source": "embedding", "source_rank": 0},
            {"object_id": "mem_b", "query_id": "q3", "source": "unknown", "source_rank": 1},
        ],
        "errors": [],
    })

    assert result["status"] == "partial"
    assert [item["object_id"] for item in result["items"]] == ["mem_a"]
    assert {error["code"] for error in result["errors"]} >= {
        "invalid_query_evidence",
        "candidate_without_evidence",
    }


def test_rank_1_caps_output_at_four_hundred_and_stably_sorts_ties():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    items = [_candidate(f"mem_{index:04d}") for index in range(401, -1, -1)]
    evidence = [_evidence(item["object_id"], "q1", "embedding", 1) for item in items]
    result = rank_1_candidate_pool({"status": "ok", "items": items, "query_evidence": evidence, "errors": []})

    assert len(result["items"]) == 400
    assert result["items"][0]["object_id"] == "mem_0000"
    assert result["items"][-1]["object_id"] == "mem_0399"
    assert result["items"][-1]["rank_1_position"] == 400


def test_rank_1_does_not_need_raw_scores_or_modify_input():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    candidate_pool = {
        "status": "ok",
        "items": [_candidate("mem_a")],
        "query_evidence": [{
            "object_id": "mem_a",
            "query_id": "q1",
            "channel": "general_semantic",
            "source": "embedding",
            "source_rank": 3,
        }],
        "errors": [],
    }
    result = rank_1_candidate_pool(candidate_pool)

    assert result["status"] == "ok"
    assert math.isclose(result["items"][0]["rank_1_score"], 1 / 63)
    assert "rank_1_score" not in candidate_pool["items"][0]


def test_rank_1_applies_strength_weights_and_excludes_sleeping_candidates():
    from memory_engine.ltm.memory_push_rank_1 import rank_1_candidate_pool

    object_ids = ["mem_zero", "mem_low", "mem_mid", "mem_high"]
    result = rank_1_candidate_pool({
        "status": "ok",
        "items": [
            _candidate("mem_zero", 0.0),
            _candidate("mem_low", 0.39),
            _candidate("mem_mid", 0.7),
            _candidate("mem_high", 0.71),
        ],
        "query_evidence": [_evidence(object_id, "q1", "embedding", 1) for object_id in object_ids],
        "errors": [],
    })

    assert [item["object_id"] for item in result["items"]] == ["mem_high", "mem_mid", "mem_low"]
    assert [item["strength_weight"] for item in result["items"]] == [1.25, 1.0, 0.5]
    assert math.isclose(result["items"][0]["rank_1_score"], 1.25 / 61)
