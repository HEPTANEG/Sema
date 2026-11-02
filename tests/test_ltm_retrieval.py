import json
import sqlite3


def _id(number):
    return f"mem_{number:032x}"


def _seed_stream(count=20):
    from memory_engine import event_stream

    event_stream.save_messages(
        [{"role": "user", "content": f"retrieval evidence {index}"} for index in range(count)]
    )


def _object(number, title, body, object_type="assertion", summary=None, structured_data=None):
    return {
        "object_id": _id(number),
        "object_type": object_type,
        "title": title,
        "summary": summary,
        "payload": {
            "schema_version": 1,
            "body": body,
            "structured_data": structured_data,
        },
    }


def _derived(object_id, slot):
    return {
        "source": {"object_id": object_id},
        "relation_type": "sourced_from",
        "target": {"event_slots": {"start": slot, "end": slot}},
    }


def _commit_objects(objects, key="retrieval-seed", extra_links=None):
    from memory_engine.ltm import ltm_store

    links = []
    for index, obj in enumerate(objects):
        if obj["object_type"] != "tag":
            links.append(_derived(obj["object_id"], index))
    links.extend(extra_links or [])
    result = ltm_store.commit_write_plan({"objects": objects, "links": links}, key)
    assert result["status"] == "committed", result
    return result


def _profile():
    from memory_engine.ltm.ltm_retrieval import FORMATION_EXISTING_MEMORY_PROFILE

    return FORMATION_EXISTING_MEMORY_PROFILE


def _set_strength(object_id, strength):
    from memory_engine.ltm import ltm_store

    with sqlite3.connect(ltm_store.ltm_db_path()) as connection:
        connection.execute(
            "UPDATE memory_object_derived SET strength = ? WHERE object_id = ?",
            (strength, object_id),
        )


def test_select_projections_and_keyset_pagination(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval

    _seed_stream()
    _commit_objects(
        [
            _object(1, "Concise answers", "The user prefers concise technical answers."),
            _object(2, "Event Stream architecture", "The project uses Event Stream memory."),
        ]
    )

    first = ltm_retrieval.select_objects(
        {"object_types": ["assertion"], "projection": "title", "limit": 1}, profile=_profile()
    )
    assert first["status"] == "partial"
    assert set(first["items"][0]) == {"object_id", "object_type", "title", "strength"}
    assert first["next_cursor"]

    second = ltm_retrieval.select_objects(
        {
            "object_types": ["assertion"],
            "projection": "title",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
        profile=_profile(),
    )
    assert second["status"] == "ok"
    assert second["items"][0]["object_id"] != first["items"][0]["object_id"]

    payload = ltm_retrieval.select_objects(
        {"object_ids": [_id(1)], "projection": "payload"}, profile=_profile()
    )
    assert set(payload["items"][0]) == {
        "object_id", "object_type", "title", "summary", "payload", "strength"
    }
    assert payload["items"][0]["payload"]["body"].startswith("The user")


def test_all_retrieval_projections_include_current_strength(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval, ltm_store

    _seed_stream()
    _commit_objects([_object(1, "Projected memory", "projection-token")])
    before = ltm_retrieval.select_objects(
        {"object_ids": [_id(1)], "projection": "core"}, profile=_profile()
    )
    before_strength = before["items"][0]["strength"]

    recorded = ltm_store.record_successful_uses([_id(1)], "projection-successful-use")
    assert recorded["status"] == "committed"
    for projection in ("title", "title_summary", "core", "payload", "full"):
        result = ltm_retrieval.select_objects(
            {"object_ids": [_id(1)], "projection": projection}, profile=_profile()
        )
        assert result["items"][0]["strength"] > before_strength


def test_zero_strength_is_excluded_from_all_retrieval_paths(isolated_workspace, monkeypatch):
    from memory_engine.ltm import embedding_adapter, ltm_retrieval, vector_index

    _seed_stream()
    _commit_objects(
        [_object(1, "Sleeping memory", "sleeping-token"), _object(2, "Awake memory", "awake-token")],
        extra_links=[
            {"source": {"object_id": _id(2)}, "relation_type": "supports", "target": {"object_id": _id(1)}},
        ],
    )
    _set_strength(_id(1), 0.0)

    selected = ltm_retrieval.select_objects({}, profile=_profile())
    assert [item["object_id"] for item in selected["items"]] == [_id(2)]
    searched = ltm_retrieval.search_objects({"text": "sleeping-token"}, profile=_profile())
    assert searched["items"] == []
    traversed = ltm_retrieval.traverse_objects(
        {"start_object_ids": [_id(2)], "relation_types": ["supports"]}, profile=_profile()
    )
    assert traversed["items"] == []

    class FakeIndex:
        requested_limit = None

        def __init__(self, root=None):
            pass

        def search(self, _vector, limit):
            type(self).requested_limit = limit
            return [
                {"object_id": _id(1), "similarity": 0.99},
                {"object_id": _id(2), "similarity": 0.88},
            ]

    monkeypatch.setattr(embedding_adapter, "embed_queries", lambda _texts: type("Batch", (), {"vectors": ((1.0,),)})())
    monkeypatch.setattr(vector_index, "VectorIndexAdapter", FakeIndex)
    semantic = ltm_retrieval.semantic_search("query", profile=ltm_retrieval.MEMORY_PUSH_PROFILE)
    assert [item["object_id"] for item in semantic["items"]] == [_id(2)]
    assert semantic["items"][0]["strength"] > 0.5
    assert FakeIndex.requested_limit == ltm_retrieval.MEMORY_PUSH_PROFILE.max_objects * 5


def test_profile_rejects_legacy_status_and_query_does_not_initialize_store(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval, ltm_store

    missing = ltm_retrieval.select_objects({}, profile=_profile())
    assert missing["status"] == "error"
    assert missing["errors"][0]["code"] == "not_initialized"
    assert not ltm_store.ltm_db_path().exists()

    _seed_stream()
    _commit_objects([_object(1, "Active memory", "active")])
    denied = ltm_retrieval.select_objects({"status": "forgotten"}, profile=_profile())
    assert denied["status"] == "error"
    assert denied["errors"][0]["code"] == "unknown_parameter"
    arbitrary = ltm_retrieval.select_objects({"sql": "SELECT *"}, profile=_profile())
    assert arbitrary["status"] == "error"
    assert arbitrary["errors"][0]["code"] == "unknown_parameter"


def test_contains_full_text_chinese_and_short_query_fallback(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval

    _seed_stream()
    _commit_objects(
        [
            _object(1, "用户回复偏好", "用户喜欢简洁的技术回答。"),
            _object(2, "Architecture notes", "The project uses an event stream architecture."),
        ]
    )

    body_contains = ltm_retrieval.search_objects(
        {"text": "简洁的技术", "mode": "contains", "fields": ["payload_body"]},
        profile=_profile(),
    )
    assert [item["object_id"] for item in body_contains["items"]] == [_id(1)]

    chinese_fts = ltm_retrieval.search_objects(
        {"text": "简洁的技术", "mode": "full_text"}, profile=_profile()
    )
    assert [item["object_id"] for item in chinese_fts["items"]] == [_id(1)]

    english_fts = ltm_retrieval.search_objects(
        {"text": "stream architecture", "mode": "full_text"}, profile=_profile()
    )
    assert [item["object_id"] for item in english_fts["items"]] == [_id(2)]

    short_fallback = ltm_retrieval.search_objects(
        {"text": "偏好", "mode": "full_text", "fields": ["title"]}, profile=_profile()
    )
    assert [item["object_id"] for item in short_fallback["items"]] == [_id(1)]


def test_search_literal_escaping_and_pagination(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval

    _seed_stream()
    _commit_objects(
        [
            _object(1, 'Quoted "memory" one', 'Literal "memory" phrase.'),
            _object(2, 'Quoted "memory" two', 'Another literal "memory" phrase.'),
        ]
    )
    first = ltm_retrieval.search_objects(
        {"text": '"memory"', "mode": "full_text", "limit": 1}, profile=_profile()
    )
    assert first["status"] == "partial"
    second = ltm_retrieval.search_objects(
        {"text": '"memory"', "mode": "full_text", "limit": 1, "cursor": first["next_cursor"]},
        profile=_profile(),
    )
    assert second["status"] == "ok"
    assert second["items"][0]["object_id"] != first["items"][0]["object_id"]


def test_schema_migration_backfills_and_write_sync_index(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval, ltm_store

    _seed_stream()
    _commit_objects([_object(1, "Backfilled memory", "backfill-search-token")])
    with sqlite3.connect(ltm_store.ltm_db_path()) as connection:
        connection.execute(f"DROP TABLE {ltm_store.LTM_FTS_TABLE}")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    connection = ltm_store.ensure_ltm_store()
    connection.close()
    found = ltm_retrieval.search_objects(
        {"text": "backfill-search-token", "mode": "full_text"}, profile=_profile()
    )
    assert [item["object_id"] for item in found["items"]] == [_id(1)]

    _commit_objects([_object(2, "New indexed memory", "new-index-token")], key="new-index")
    indexed = ltm_retrieval.search_objects(
        {"text": "new-index-token", "mode": "full_text"}, profile=_profile()
    )
    assert [item["object_id"] for item in indexed["items"]] == [_id(2)]



def test_meta_memory_bootstrap_syncs_system_tags_into_index(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval, meta_memory

    result = meta_memory.bootstrap_system_tags()
    assert result["status"] == "bootstrapped"
    found = ltm_retrieval.search_objects(
        {"text": "User Model", "mode": "full_text", "fields": ["title"]}, profile=_profile()
    )
    assert len(found["items"]) == 1
    assert found["items"][0]["object_type"] == "tag"


def test_traverse_is_bounded_deduplicated_and_returns_minimal_paths(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval

    _seed_stream()
    objects = [
        _object(1, "Assertion A", "A"),
        _object(2, "Assertion B", "B"),
        _object(3, "Assertion C", "C"),
    ]
    links = [
        {"source": {"object_id": _id(1)}, "relation_type": "supports", "target": {"object_id": _id(2)}},
        {"source": {"object_id": _id(2)}, "relation_type": "supports", "target": {"object_id": _id(3)}},
        {"source": {"object_id": _id(3)}, "relation_type": "supports", "target": {"object_id": _id(1)}},
    ]
    _commit_objects(objects, extra_links=links)

    result = ltm_retrieval.traverse_objects(
        {
            "start_object_ids": [_id(1)],
            "direction": "outgoing",
            "relation_types": ["supports"],
            "max_depth": 2,
        },
        profile=_profile(),
    )
    assert [item["object_id"] for item in result["items"]] == [_id(2), _id(3)]
    assert len(result["items"][0]["path"]) == 1
    assert len(result["items"][1]["path"]) == 2

    bounded = ltm_retrieval.traverse_objects(
        {
            "start_object_ids": [_id(1)],
            "direction": "outgoing",
            "relation_types": ["supports"],
            "max_depth": 2,
            "limit": 1,
        },
        profile=_profile(),
    )
    assert bounded["status"] == "partial"
    assert bounded["next_cursor"] is None
    assert len(bounded["items"]) == 1


def test_retrieval_results_do_not_mutate_canonical_store(isolated_workspace):
    from memory_engine.ltm import ltm_retrieval, ltm_store

    _seed_stream()
    _commit_objects([_object(1, "Read only", "read-only-token")])
    before = ltm_store.ltm_store_snapshot()
    ltm_retrieval.select_objects({}, profile=_profile())
    ltm_retrieval.search_objects({"text": "read-only-token"}, profile=_profile())
    ltm_retrieval.traverse_objects({"start_object_ids": [_id(1)]}, profile=_profile())
    after = ltm_store.ltm_store_snapshot()
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)
