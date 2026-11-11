import json
import sqlite3

import pytest


def _id(suffix):
    return "mem_" + (suffix * 32)[:32]


def _plan(object_id=None, body="small", summary=None, object_type="assertion", target_start=1, target_end=1):
    object_id = object_id or _id("a")
    return {
        "objects": [
            {
                "object_id": object_id,
                "object_type": object_type,
                "title": "A durable memory",
                "summary": summary,
                "payload": {
                    "schema_version": 1,
                    "body": body,
                    "structured_data": None,
                },
            }
        ],
        "links": [
            {
                "source": {"object_id": object_id},
                "relation_type": "sourced_from",
                "target": {"event_slots": {"start": target_start, "end": target_end}},
            }
        ],
    }


def _seed_stream(isolated_workspace, count=3):
    from memory_engine import event_stream

    messages = [{"role": "user", "content": f"message {index}"} for index in range(count)]
    event_stream.save_messages(messages)
    return event_stream


def test_ltm_store_is_separate_and_initializes_schema(isolated_workspace):
    from memory_engine.ltm import ltm_store

    connection = ltm_store.ensure_ltm_store()
    connection.close()
    assert ltm_store.ltm_db_path().parts[-2:] == ("ltm", "ltm.db")
    assert ltm_store.ltm_db_path().exists()
    with sqlite3.connect(ltm_store.ltm_db_path()) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memory_objects", "memory_payloads", "memory_links", "ltm_operations"} <= names


def test_write_read_and_payload_is_lazy(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    plan = _plan()
    result = ltm_store.commit_write_plan(plan, "write-1")
    assert result["status"] == "committed"
    object_id = result["object_ids"][0]
    shallow = ltm_store.get_object(object_id)
    assert "payload" not in shallow
    full = ltm_store.get_object(object_id, include_payload=True)
    assert full["payload"]["body"] == "small"
    assert len(ltm_store.get_links(object_id, "outgoing")) == 1
    assert len(ltm_store.get_links(object_id, "incoming")) == 0


def test_get_objects_reads_multiple_objects_in_input_order(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace, count=2)
    first_id = _id("a")
    second_id = _id("b")
    assert ltm_store.commit_write_plan(_plan(first_id, body="first"), "batch-1")["status"] == "committed"
    assert ltm_store.commit_write_plan(_plan(second_id, body="second"), "batch-2")["status"] == "committed"

    objects = ltm_store.get_objects([second_id, first_id, second_id], include_payload=True)

    assert list(objects) == [second_id, first_id]
    assert objects[first_id]["payload"]["body"] == "first"
    assert objects[second_id]["payload"]["body"] == "second"
    assert objects[first_id]["object_id"] == first_id


def test_ltm_store_tests_skip_real_vector_staging_by_default(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store, vector_index

    _seed_stream(isolated_workspace)
    monkeypatch.setattr(
        vector_index,
        "stage_object_upserts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("real vector staging was called")),
    )

    result = ltm_store.commit_write_plan(_plan(_id("b")), "default-vector-mock")

    assert result["status"] == "committed", result


@pytest.mark.real_ltm_vector_sync
def test_ltm_store_can_opt_into_real_vector_staging_path(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store, vector_index

    _seed_stream(isolated_workspace)
    calls = []
    monkeypatch.setattr(
        vector_index,
        "stage_object_upserts",
        lambda objects, root=None, timeout_seconds=None: calls.append((objects, root, timeout_seconds)) or None,
    )

    result = ltm_store.commit_write_plan(_plan(_id("c")), "real-vector-staging-path", isolated_workspace)

    assert result["status"] == "committed", result
    assert calls and calls[0][0][0]["object_id"] == _id("c")


def test_payload_thresholds_are_byte_based(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    small = ltm_store.validate_write_plan(_plan(body="x" * 2048))
    assert small["valid"]
    missing = ltm_store.validate_write_plan(_plan(body="x" * 2049))
    assert not missing["valid"]
    missing_error = next(error for error in missing["errors"] if error["code"] == "missing_summary")
    assert missing_error["actual_payload_bytes"] == 2049
    assert missing_error["summary_threshold_bytes"] == ltm_store.SUMMARY_THRESHOLD_BYTES
    assert missing_error["max_payload_bytes"] == ltm_store.SPLIT_THRESHOLD_BYTES
    medium = ltm_store.validate_write_plan(_plan(body="x" * 2049, summary="compressed"))
    assert medium["valid"]
    huge = ltm_store.validate_write_plan(_plan(body="x" * 8193, summary="compressed"))
    assert not huge["valid"]
    huge_error = next(error for error in huge["errors"] if error["code"] == "payload_too_large")
    assert huge_error["actual_payload_bytes"] == 8193
    assert any(error["code"] == "payload_too_large" for error in huge["errors"])


@pytest.mark.parametrize("structured_data,expected_code", [
    ({"unknown": True}, "episode_schema"),
    ({"time_scope": None}, "time_scope"),
    ({"time_scope": {}}, "empty_time_scope"),
    ({"time_scope": {"start": "2026", "unexpected": "x"}}, "time_scope_fields"),
    ({"time_scope": {"start": "2026", "precision": "bad"}}, "time_scope_precision"),
])
def test_episode_structured_data_rejects_invalid_shapes(isolated_workspace, structured_data, expected_code):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    plan = _plan(object_type="episode")
    plan["objects"][0]["payload"]["structured_data"] = structured_data
    result = ltm_store.validate_write_plan(plan, root=isolated_workspace)
    assert not result["valid"]
    assert expected_code in {error["code"] for error in result["errors"]}


def test_episode_structured_data_accepts_null_empty_and_time_scope(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    for index, structured_data in enumerate((None, {}, {"time_scope": {"start": "2026", "precision": "approximate"}})):
        plan = _plan(_id(chr(ord("a") + index)), object_type="episode")
        plan["objects"][0]["payload"]["structured_data"] = structured_data
        result = ltm_store.validate_write_plan(plan, root=isolated_workspace)
        assert result["valid"], result


def test_provenance_validation_is_read_only_and_atomic(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    before = event_stream.event_stream_status()
    invalid = _plan(target_start=1, target_end=99)
    result = ltm_store.commit_write_plan(invalid, "invalid-1")
    assert result["status"] == "rejected"
    assert any(error["code"] == "slot_not_found" for error in result["errors"])
    assert ltm_store.ltm_store_snapshot()["object_count"] == 0
    assert event_stream.event_stream_status()["slot_count"] == before["slot_count"]


def test_provenance_accepts_event_stream_slot_zero(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace, count=1)
    result = ltm_store.commit_write_plan(_plan(target_start=0, target_end=0), "slot-zero")
    assert result["status"] == "committed"


def test_sourced_from_rejects_memory_object_targets(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    source_id, target_id = _id("a"), _id("b")
    assert ltm_store.commit_write_plan(_plan(source_id, target_start=0, target_end=0), "sourced-source", isolated_workspace)["status"] == "committed"
    assert ltm_store.commit_write_plan(_plan(target_id, target_start=0, target_end=0), "sourced-target", isolated_workspace)["status"] == "committed"
    result = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": source_id}, "relation_type": "sourced_from",
            "target": {"object_id": target_id},
        }]},
        "sourced-object-target", isolated_workspace,
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "relation_contract"
    assert result["errors"][0]["details"]["expected"]["target"] == ["event_slot"]
    assert result["errors"][0]["details"]["actual"]["target"] == "assertion"


def test_idempotency_survives_reopen_and_conflict_is_rejected(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    plan = _plan()
    first = ltm_store.commit_write_plan(plan, "same-key")
    second = ltm_store.commit_write_plan(plan, "same-key")
    assert second["status"] == "already_committed"
    assert second["object_ids"] == first["object_ids"]
    conflict = ltm_store.commit_write_plan(_plan(_id("b")), "same-key")
    assert conflict["status"] == "rejected"
    assert conflict["errors"][0]["code"] == "idempotency_conflict"


def test_derived_strength_tracks_use_and_link_effects(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    source_id, target_id = _id("c"), _id("d")
    first = ltm_store.commit_write_plan(_plan(source_id), "derived-source")
    second = ltm_store.commit_write_plan(_plan(target_id, target_start=2, target_end=2), "derived-target")
    baseline = ltm_store.get_object(target_id)["derived"]
    supports = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": source_id}, "relation_type": "supports",
            "target": {"object_id": target_id},
        }]},
        "support-target",
    )
    assert supports["status"] == "committed"
    gained = ltm_store.get_object(target_id)["derived"]
    assert gained["gain_link_count"] == baseline["gain_link_count"] + 1
    assert gained["strength"] > baseline["strength"]
    used = ltm_store.record_successful_uses([target_id, target_id], "use-target")
    assert used["status"] == "committed"
    assert ltm_store.get_object(target_id)["derived"]["successful_use_count"] == 1
    supersedes = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": source_id}, "relation_type": "supersedes",
            "target": {"object_id": target_id},
        }]},
        "supersede-target",
    )
    assert supersedes["status"] == "committed"
    assert ltm_store.get_object(target_id)["derived"]["loss_link_count"] == 1


def test_derived_and_supersedes_graphs_reject_cycles_but_ignore_inactive_links(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    first_id, second_id = _id("e"), _id("f")
    ltm_store.commit_write_plan(_plan(first_id), "cycle-first")
    ltm_store.commit_write_plan(_plan(second_id, target_start=2, target_end=2), "cycle-second")
    first_edge = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": first_id}, "relation_type": "supersedes",
            "target": {"object_id": second_id},
        }]}, "cycle-forward",
    )
    rejected = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": second_id}, "relation_type": "supersedes",
            "target": {"object_id": first_id},
        }]}, "cycle-backward",
    )
    assert first_edge["status"] == "committed"
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "relation_cycle"
    ltm_store.deactivate_memory_link(first_edge["link_ids"][0], "deactivate-cycle")
    accepted = ltm_store.commit_write_plan(
        {"objects": [], "links": [{
            "source": {"object_id": second_id}, "relation_type": "supersedes",
            "target": {"object_id": first_id},
        }]}, "cycle-after-deactivate",
    )
    assert accepted["status"] == "committed"


def test_legacy_active_schema_migrates_and_forgotten_object_refuses_migration(isolated_workspace):
    from memory_engine.ltm import ltm_store

    connection = ltm_store.ensure_ltm_store()
    connection.execute(
        "INSERT INTO memory_objects(object_id, object_type, title, summary, created_at, updated_at) VALUES (?, 'tag', 'Legacy', NULL, 'now', 'now')",
        (_id("9"),),
    )
    connection.execute("ALTER TABLE memory_objects ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    connection.execute("ALTER TABLE memory_links ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    migrated = ltm_store.ensure_ltm_store()
    assert "status" not in {row["name"] for row in migrated.execute("PRAGMA table_info(memory_objects)")}
    assert "link_state" in {row["name"] for row in migrated.execute("PRAGMA table_info(memory_links)")}
    migrated.close()

    connection = ltm_store.ensure_ltm_store()
    connection.execute("ALTER TABLE memory_objects ADD COLUMN status TEXT NOT NULL DEFAULT 'forgotten'")
    connection.commit()
    connection.close()
    try:
        ltm_store.ensure_ltm_store()
    except RuntimeError as error:
        assert "forgotten MemoryObjects" in str(error)
    else:
        raise AssertionError("legacy forgotten object migration must fail")


def test_legacy_derived_relations_migrate_to_canonical_semantics(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import ltm_store

    event_stream.save_messages([{"role": "user", "content": "legacy relation evidence"}], root=isolated_workspace)
    source_id = _id("d")
    tag_id = _id("e")
    assertion_id = _id("f")
    source_write = ltm_store.commit_write_plan(_plan(source_id, target_start=0, target_end=0), "legacy-source", isolated_workspace)
    assert source_write["status"] == "committed", source_write
    assert ltm_store.commit_write_plan(
        {"objects": [{"object_id": tag_id, "object_type": "tag", "title": "Legacy topic", "summary": None,
                       "payload": {"schema_version": 1, "body": "topic"}}], "links": []},
        "legacy-tag", isolated_workspace,
    )["status"] == "committed"
    assert ltm_store.commit_write_plan(_plan(assertion_id, target_start=0, target_end=0), "legacy-target", isolated_workspace)["status"] == "committed"

    connection = sqlite3.connect(ltm_store.ltm_db_path(isolated_workspace))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE memory_links RENAME TO memory_links_legacy_manual")
    connection.execute(
        """CREATE TABLE memory_links (
            link_id TEXT PRIMARY KEY, source_object_id TEXT NOT NULL, target_kind TEXT NOT NULL,
            target_object_id TEXT, slot_seq_start INTEGER, slot_seq_end INTEGER,
            relation_type TEXT NOT NULL, link_state TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(source_object_id) REFERENCES memory_objects(object_id),
            FOREIGN KEY(target_object_id) REFERENCES memory_objects(object_id),
            CHECK (target_kind IN ('memory_object', 'event_slot')),
            CHECK (relation_type IN ('derived_from', 'tagged_with', 'supports', 'contradicts', 'supersedes')),
            CHECK (link_state IN ('active', 'inactive'))
        )"""
    )
    connection.execute(
        """INSERT INTO memory_links(link_id, source_object_id, target_kind, target_object_id,
           slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at)
           VALUES (?, ?, 'memory_object', ?, NULL, NULL, 'derived_from', 'active', 'legacy', 'legacy')""",
        ("lnk_legacy_tag", source_id, tag_id),
    )
    connection.execute(
        """INSERT INTO memory_links(link_id, source_object_id, target_kind, target_object_id,
           slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at)
           VALUES (?, ?, 'event_slot', NULL, 0, 0, 'derived_from', 'active', 'legacy', 'legacy')""",
        ("lnk_legacy_source", source_id),
    )
    connection.execute(
        """INSERT INTO memory_links(link_id, source_object_id, target_kind, target_object_id,
           slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at)
           VALUES (?, ?, 'event_slot', NULL, 0, 0, 'derived_from', 'active', 'legacy', 'legacy')""",
        ("lnk_legacy_target_source", assertion_id),
    )
    connection.execute(
        """INSERT INTO memory_links(link_id, source_object_id, target_kind, target_object_id,
           slot_seq_start, slot_seq_end, relation_type, link_state, created_at, updated_at)
           VALUES (?, ?, 'memory_object', ?, NULL, NULL, 'derived_from', 'active', 'legacy', 'legacy')""",
        ("lnk_legacy_support", source_id, assertion_id),
    )
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()

    migrated = ltm_store.ensure_ltm_store(isolated_workspace)
    migrated.close()
    links = ltm_store.get_links(source_id, "outgoing", root=isolated_workspace)
    assert {link["relation_type"] for link in links} >= {"sourced_from", "tagged_with", "supports"}
    assert not any(link["relation_type"] == "derived_from" for link in links)
    assert ltm_store.get_object(assertion_id, root=isolated_workspace)["derived"]["gain_link_count"] == 2


def test_link_deactivation_is_one_way_and_idempotent(isolated_workspace):
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    result = ltm_store.commit_write_plan(_plan(), "forget-source")
    object_id = result["object_ids"][0]
    link_id = result["link_ids"][0]
    inactive = ltm_store.deactivate_memory_link(link_id, "deactivate-1")
    assert inactive["status"] == "inactive"
    assert ltm_store.get_object(object_id) is not None
    assert ltm_store.get_links(object_id, "outgoing") == []
    repeated = ltm_store.deactivate_memory_link(link_id, "deactivate-1")
    assert repeated["status"] == "already_inactive"


def test_observer_includes_ltm_metadata_without_payload(isolated_workspace):
    from memory_engine import observer
    from memory_engine.ltm import ltm_store

    _seed_stream(isolated_workspace)
    body = "x" * 2049
    result = ltm_store.commit_write_plan(_plan(body=body, summary="compressed"), "observe-1")
    snapshot = observer.memory_engine_snapshot(messages=[])
    assert snapshot["ltm"]["status"] == "ok"
    assert snapshot["ltm"]["object_count"] == 1
    assert snapshot["ltm"]["payload_count"] == 1
    assert snapshot["ltm"]["db_schema_version"] == ltm_store.LTM_DB_SCHEMA_VERSION
    assert snapshot["ltm"]["retrieval_index_initialized"] is True
    assert snapshot["ltm"]["retrieval_index_count"] == 1
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert body not in serialized


def test_observer_counts_objects_without_system_root_links(isolated_workspace):
    from memory_engine.ltm import ltm_store, meta_memory

    _seed_stream(isolated_workspace)
    bootstrap = meta_memory.bootstrap_system_tags()
    assert ltm_store.ltm_store_snapshot()["unanchored_object_count"] == 0

    committed = ltm_store.commit_write_plan(_plan(), "unanchored-object")
    object_id = committed["object_ids"][0]
    assert ltm_store.ltm_store_snapshot()["unanchored_object_count"] == 1

    linked = ltm_store.commit_write_plan(
        {
            "objects": [],
            "links": [
                {
                    "source": {"object_id": object_id},
                    "relation_type": "tagged_with",
                    "target": {"object_id": bootstrap["tag_ids"]["others"]},
                }
            ],
        },
        "anchor-object",
    )
    assert linked["status"] == "committed"
    assert ltm_store.ltm_store_snapshot()["unanchored_object_count"] == 0

    tag_link = next(
        link
        for link in ltm_store.get_links(object_id, "outgoing")
        if link["relation_type"] == "tagged_with"
    )
    ltm_store.deactivate_memory_link(tag_link["link_id"], "deactivate-anchor")
    assert ltm_store.ltm_store_snapshot()["unanchored_object_count"] == 1
