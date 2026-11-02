import math
import sqlite3

import pytest

from test_ltm_core_store import _id, _plan, _seed_stream


@pytest.mark.parametrize("baseline", [0.9, 0.7, 0.5, 0.3, 0.1])
def test_half_life_and_thresholds(baseline):
    from memory_engine.ltm import ltm_store as store
    assert store.decayed_strength(baseline, 2000, 0) == pytest.approx(baseline / 2)
    gap = 2000 * math.log2(baseline / .01)
    assert store.decayed_strength(baseline, gap, 0) == pytest.approx(.01)
    assert store.decayed_strength(baseline, 999999, 0, protected=True) == baseline
    assert store.decayed_strength(baseline, 0, 5) == baseline


def test_refresh_and_effective_use_are_not_compounded(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store
    _seed_stream(isolated_workspace)
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 10)
    oid = _id("b")
    assert store.commit_write_plan(_plan(oid), "seed")["status"] == "committed"
    initial = store.get_object(oid)["derived"]
    assert initial["decay_origin_slot_seq"] == 10
    assert initial["last_effective_use_slot_seq"] is None
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 2010)
    store.finish_forgetting_batch("one")
    assert store.get_object(oid)["derived"]["strength"] == pytest.approx(initial["strength"] / 2)
    store.finish_forgetting_batch("two")
    assert store.get_object(oid)["derived"]["strength"] == pytest.approx(initial["strength"] / 2)
    store.record_successful_uses([oid], "use")
    used = store.get_object(oid)["derived"]
    assert used["last_effective_use_slot_seq"] == 2010
    assert used["successful_use_count"] == 1
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 4000)
    store.record_successful_uses([oid], "use")
    assert store.get_object(oid)["derived"] == used


def test_tombstone_reads_retention_and_run_idempotency(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store, maintenance_observation, ltm_retrieval
    _seed_stream(isolated_workspace)
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 0)
    oid = _id("c")
    store.commit_write_plan(_plan(oid), "seed")
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 20000)
    result = store.finish_forgetting_batch("run")
    assert result["deleted_object_ids"] == [oid]
    assert store.get_object(oid, True) is None
    assert store.get_links(oid, "outgoing") == []
    assert maintenance_observation.get_object({"object_ids": [oid]})["items"] == []
    assert ltm_retrieval.select_objects({"object_ids": [oid]}, profile=ltm_retrieval.FORMATION_EXISTING_MEMORY_PROFILE)["items"] == []
    assert store.record_successful_uses([oid], "deleted-use")["status"] == "rejected"
    with sqlite3.connect(store.ltm_db_path()) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_objects WHERE deleted_at IS NOT NULL").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_payloads").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_objects_fts").fetchone()[0] == 0

    new_id = _id("d")
    store.commit_write_plan(_plan(new_id), "new")
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 40000)
    assert store.finish_forgetting_batch("run") == result
    assert store.get_object(new_id) is not None
    assert store.finish_forgetting_batch("next")["deleted_object_ids"] == [new_id]


def test_neighbor_recompute_is_not_cascading_and_roots_are_protected(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store, meta_memory
    _seed_stream(isolated_workspace)
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 0)
    meta_memory.bootstrap_system_tags()
    a, b = _id("a"), _id("b")
    store.commit_write_plan(_plan(a), "a")
    plan = _plan(b)
    plan["links"] = [{"source": {"object_id": a}, "relation_type": "supports", "target": {"object_id": b}}]
    store.commit_write_plan(plan, "b")
    with sqlite3.connect(store.ltm_db_path()) as db:
        db.execute("UPDATE memory_object_derived SET decay_origin_slot_seq=8600 WHERE object_id=?", (b,))
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 20000)
    assert store.finish_forgetting_batch("batch")["deleted_object_ids"] == [a]
    assert store.get_object(b)["derived"]["strength"] < .01
    assert store.get_links(b, "incoming") == []
    assert store.finish_forgetting_batch("batch")["deleted_object_ids"] == [a]
    assert store.finish_forgetting_batch("next-batch")["deleted_object_ids"] == [b]
    assert len(meta_memory.system_owned_tags()) == 4
    assert all(store.get_object(item["object_id"])["derived"]["strength"] >= .5 for item in meta_memory.system_owned_tags())


def test_legacy_rollout_origin_and_counts_preserved(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store
    _seed_stream(isolated_workspace)
    oid = _id("e")
    store.commit_write_plan(_plan(oid), "seed")
    with sqlite3.connect(store.ltm_db_path()) as db:
        db.execute("ALTER TABLE memory_object_derived DROP COLUMN last_effective_use_slot_seq")
        db.execute("ALTER TABLE memory_object_derived DROP COLUMN decay_origin_slot_seq")
        db.execute("UPDATE memory_object_derived SET successful_use_count=9, strength_formula_version=1")
        db.execute("PRAGMA user_version=9")
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 8000)
    store.ensure_ltm_store().close()
    derived = store.get_object(oid)["derived"]
    assert derived["decay_origin_slot_seq"] == 8000
    assert derived["last_effective_use_slot_seq"] is None
    assert derived["successful_use_count"] == 9
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 9000)
    store.ensure_ltm_store().close()
    assert store.get_object(oid)["derived"] == derived


def test_failed_index_sync_retries_the_same_committed_batch(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store, vector_index
    _seed_stream(isolated_workspace)
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 0)
    store.commit_write_plan(_plan(_id("f")), "seed")
    run_id = store.start_or_resume_maintenance_run()["run_id"]
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 20000)
    adapter = vector_index.get_vector_index()
    original = adapter.remove_many
    monkeypatch.setattr(adapter, "remove_many", lambda ids: (_ for _ in ()).throw(RuntimeError("index unavailable")))
    with pytest.raises(RuntimeError, match="index unavailable"):
        store.finish_forgetting_batch("recover")
    assert store.get_object(_id("f")) is None


    store.record_maintenance_failure(run_id, 0, "index unavailable")
    assert store.get_maintenance_runtime_state()["run_status"] == "failed"
    monkeypatch.setattr(adapter, "remove_many", original)
    assert store.start_or_resume_maintenance_run()["run_id"] == run_id
    assert store.finish_forgetting_batch("recover")["deleted_count"] == 1
    with sqlite3.connect(store.ltm_db_path()) as db:
        assert db.execute("SELECT index_synced FROM ltm_forgetting_batches").fetchone()[0] == 1


def test_push_threshold_and_stale_retrieval_delivery(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store, memory_push_ranking as push, memory_retrieval
    _seed_stream(isolated_workspace)
    ids = [_id("a"), _id("b")]
    for index, oid in enumerate(ids):
        store.commit_write_plan(_plan(oid), str(index))
    with sqlite3.connect(store.ltm_db_path()) as db:
        db.execute("UPDATE memory_object_derived SET strength=.1 WHERE object_id=?", (ids[0],))
        db.execute("UPDATE memory_object_derived SET strength=.099 WHERE object_id=?", (ids[1],))
    cached = {"content": "old", "used_object_ids": ids}
    assert push.validate_push_result(cached)["used_object_ids"] == [ids[0]]
    with sqlite3.connect(store.ltm_db_path()) as db:
        db.execute("UPDATE memory_objects SET deleted_at='test' WHERE object_id=?", (ids[0],))
    assert push.validate_push_result(cached)["content"] is None
    result = memory_retrieval._revalidate_delivery({"kind": "checkpoint", "evidence": [{"object_id": ids[0]}]})
    assert result["kind"] == "technical" and result["error"]["code"] == "stale_evidence"


def test_empty_stream_creation_anchor(isolated_workspace):
    from memory_engine.ltm import ltm_store as store
    plan = _plan(_id("a"))
    plan["links"] = []
    assert store.commit_write_plan(plan, "empty")["status"] == "committed"
    assert store.get_object(_id("a"))["derived"]["decay_origin_slot_seq"] == -1
