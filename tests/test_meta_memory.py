import sqlite3


def _tag_plan(object_id):
    return {
        "objects": [
            {
                "object_id": object_id,
                "object_type": "tag",
                "title": "Attempted overwrite",
            }
        ],
        "links": [],
    }


def test_bootstrap_creates_four_project_local_system_tags(isolated_workspace):
    from memory_engine.ltm import ltm_store, meta_memory

    result = meta_memory.bootstrap_system_tags()

    assert result["status"] == "bootstrapped"
    assert result["created_count"] == 4
    assert set(result["tag_ids"]) == {"sema_self", "user_model", "safety", "others"}
    assert all(value.startswith("mem_") and len(value) == 36 for value in result["tag_ids"].values())

    with sqlite3.connect(ltm_store.ltm_db_path()) as connection:
        objects = connection.execute(
            "SELECT object_id, object_type, title, summary FROM memory_objects ORDER BY title"
        ).fetchall()
        registry = connection.execute(
            "SELECT tag_key, object_id, owner, protected_kind FROM system_owned_objects ORDER BY tag_key"
        ).fetchall()
        payload_count = connection.execute("SELECT COUNT(*) FROM memory_payloads").fetchone()[0]
        link_count = connection.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]

    assert [(row[1], row[3]) for row in objects] == [
        ("tag", None),
        ("tag", None),
        ("tag", None),
        ("tag", None),
    ]
    assert [row[0] for row in registry] == ["others", "safety", "sema_self", "user_model"]
    assert all(row[2:] == ("meta_memory_agent", "system_tag") for row in registry)
    assert payload_count == 0
    assert link_count == 0


def test_bootstrap_is_idempotent_and_ids_are_stable(isolated_workspace):
    from memory_engine.ltm import meta_memory

    first = meta_memory.bootstrap_system_tags()
    second = meta_memory.bootstrap_system_tags()

    assert first["status"] == "bootstrapped"
    assert second["status"] == "already_bootstrapped"
    assert second["created_count"] == 0
    assert second["tag_ids"] == first["tag_ids"]


def test_bootstrap_conflict_is_atomic(isolated_workspace):
    from memory_engine.ltm import ltm_store, meta_memory

    connection = ltm_store.ensure_ltm_store()
    conflict_id = meta_memory.system_tag_object_id("safety")
    connection.execute(
        "INSERT INTO memory_objects(object_id, object_type, title, created_at, updated_at) VALUES (?, 'assertion', 'Conflict', 'now', 'now')",
        (conflict_id,),
    )
    connection.commit()
    connection.close()

    result = meta_memory.bootstrap_system_tags()

    assert result["status"] == "failed"
    with sqlite3.connect(ltm_store.ltm_db_path()) as check:
        assert check.execute("SELECT COUNT(*) FROM system_owned_objects").fetchone()[0] == 0
        assert check.execute("SELECT COUNT(*) FROM memory_objects").fetchone()[0] == 1


def test_existing_ltm_objects_survive_bootstrap(isolated_workspace):
    from memory_engine.ltm import ltm_store, meta_memory

    connection = ltm_store.ensure_ltm_store()
    object_id = "mem_" + "a" * 32
    connection.execute(
        "INSERT INTO memory_objects(object_id, object_type, title, created_at, updated_at) VALUES (?, 'tag', 'Ordinary Tag', 'now', 'now')",
        (object_id,),
    )
    connection.commit()
    connection.close()

    result = meta_memory.bootstrap_system_tags()

    assert result["status"] == "bootstrapped"
    assert ltm_store.get_object(object_id)["title"] == "Ordinary Tag"


def test_system_owned_tag_cannot_be_overwritten_or_updated(isolated_workspace):
    from memory_engine.ltm import ltm_store, meta_memory

    bootstrap = meta_memory.bootstrap_system_tags()
    object_id = bootstrap["tag_ids"]["safety"]

    write = ltm_store.commit_write_plan(_tag_plan(object_id), "overwrite-system-tag")
    update = ltm_store.update_memory_object_title(object_id, "Changed", "update-system-tag")

    assert write["status"] == "rejected"
    assert write["errors"][0]["code"] == "protected_system_object"
    assert update["status"] == "rejected"
    assert update["errors"][0]["code"] == "protected_system_object"


def test_observer_exposes_bootstrap_metadata_without_payload(isolated_workspace):
    import json

    from memory_engine import observer
    from memory_engine.ltm import meta_memory

    meta_memory.bootstrap_system_tags()
    snapshot = observer.memory_engine_snapshot(messages=[])
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["ltm"]["bootstrap_complete"] is True
    assert snapshot["ltm"]["system_owned_tag_count"] == 4
    assert {item["tag_key"] for item in snapshot["ltm"]["system_owned_tags"]} == {
        "sema_self",
        "user_model",
        "safety",
        "others",
    }
    assert '"body":' not in serialized


def test_formation_bootstrap_failure_blocks_work_processing(isolated_workspace, monkeypatch):
    from memory_engine.ltm import formation, meta_memory

    called = {"refresh": False}

    def fail_bootstrap():
        return {"status": "failed", "error": "bootstrap conflict"}

    def refresh(*_args, **_kwargs):
        called["refresh"] = True
        raise AssertionError("Formation discovery must not run after Bootstrap failure")

    monkeypatch.setattr(meta_memory, "bootstrap_system_tags", fail_bootstrap)
    monkeypatch.setattr(formation, "_refresh_work_items", refresh)

    result = formation.run_formation_maintenance(messages=[])

    assert result["reason"] == "meta_memory_bootstrap_failed"
    assert result["processed_count"] == 0
    assert called["refresh"] is False


def test_formation_loads_system_root_tags_once_per_maintenance(isolated_workspace, monkeypatch):
    from memory_engine.ltm import formation, meta_memory

    calls = {"count": 0}
    original = meta_memory.system_owned_tags

    def load_once():
        calls["count"] += 1
        return original()

    monkeypatch.setattr(meta_memory, "system_owned_tags", load_once)
    monkeypatch.setattr(
        formation,
        "_refresh_work_items",
        lambda *_args, **_kwargs: {"buffer_slot_count": 0, "created_count": 0},
    )

    result = formation.run_formation_maintenance(messages=[])

    assert result["reason"] == "no_formation_work_items"
    assert result["retrieval_call_count"] == 0
    assert calls["count"] == 1


def test_incomplete_system_root_tag_list_blocks_formation(isolated_workspace, monkeypatch):
    from memory_engine.ltm import formation, meta_memory

    meta_memory.bootstrap_system_tags()
    incomplete = meta_memory.system_owned_tags()[:-1]
    called = {"refresh": False}

    def refresh(*_args, **_kwargs):
        called["refresh"] = True
        raise AssertionError("Formation discovery must not run with incomplete system roots")

    monkeypatch.setattr(meta_memory, "system_owned_tags", lambda: incomplete)
    monkeypatch.setattr(formation, "_refresh_work_items", refresh)

    result = formation.run_formation_maintenance(messages=[])

    assert result["reason"] == "formation_maintenance_failed"
    assert "integrity check failed" in result["error"]
    assert result["processed_count"] == 0
    assert called["refresh"] is False
