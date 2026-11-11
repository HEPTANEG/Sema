import concurrent.futures
import json
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from memory_engine import observer
from memory_engine.ltm import embedding_adapter, ltm_store, meta_memory, vector_index


def _vector(index):
    values = np.zeros(vector_index.DIMENSION, dtype=np.float32)
    values[index] = 1.0
    return values


def _metadata():
    return vector_index._expected_metadata()


def test_staging_worker_failure_preserves_original_index(isolated_workspace, monkeypatch):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_old", _vector(0), _metadata())
    before_index = adapter.index_path.read_bytes()
    before_metadata = adapter.metadata_path.read_bytes()
    monkeypatch.setattr(vector_index.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=3221225477, stderr="access violation", stdout=""))
    with pytest.raises(vector_index.VectorIndexUnavailable, match="access violation"):
        adapter.stage_upserts([{"object_id": "mem_new", "vector": _vector(1)}])
    assert adapter.index_path.read_bytes() == before_index
    assert adapter.metadata_path.read_bytes() == before_metadata
    assert adapter.search(_vector(0), 1)[0]["object_id"] == "mem_old"


def test_staging_worker_commits_only_after_install(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_old", _vector(0), _metadata())
    staged = adapter.stage_upserts([{"object_id": "mem_new", "vector": _vector(1)}])
    assert adapter.object_ids() == ["mem_old"]
    adapter.install_staged(staged)
    assert adapter.object_ids() == ["mem_new", "mem_old"]


def test_staging_worker_timeout_preserves_original_index(isolated_workspace, monkeypatch):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_old", _vector(0), _metadata())
    original = adapter.index_path.read_bytes()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(vector_index.subprocess, "run", timeout)
    with pytest.raises(vector_index.VectorIndexUnavailable, match="timed out"):
        adapter.stage_upserts([{"object_id": "mem_new", "vector": _vector(1)}])
    assert adapter.index_path.read_bytes() == original


def test_bulk_removal_recovers_between_file_replacements(isolated_workspace, monkeypatch):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_a", _vector(0), _metadata())
    adapter.upsert("mem_b", _vector(1), _metadata())
    replace = vector_index.os.replace
    def fail_metadata(source, target):
        if str(target) == str(adapter.metadata_path):
            raise OSError("interrupted metadata replacement")
        return replace(source, target)
    monkeypatch.setattr(vector_index.os, "replace", fail_metadata)
    with pytest.raises(vector_index.VectorIndexUnavailable):
        adapter.remove_many(["mem_a"])
    monkeypatch.setattr(vector_index.os, "replace", replace)
    reopened = vector_index.VectorIndexAdapter()
    reopened.remove_many(["mem_a"])
    assert [item["object_id"] for item in reopened.search(_vector(1), 10)] == ["mem_b"]
    reopened.remove_many(["mem_a"])
    assert reopened.status()["object_count"] == 1


def test_index_upsert_search_persist_and_remove(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()
    assert adapter.status()["status"] == "not_initialized"

    adapter.upsert("mem_a", _vector(0), _metadata())
    adapter.upsert("mem_b", _vector(1), _metadata())
    results = adapter.search(_vector(0), 2)
    assert [item["object_id"] for item in results] == ["mem_a", "mem_b"]
    assert results[0]["similarity"] > results[1]["similarity"]

    reopened = vector_index.VectorIndexAdapter()
    assert reopened.status()["status"] == "ready"
    assert reopened.status()["object_count"] == 2
    assert reopened.search(_vector(0), 2)[0]["object_id"] == "mem_a"

    assert reopened.remove("mem_a")["removed"] is True
    assert [item["object_id"] for item in reopened.search(_vector(0), 2)] == ["mem_b"]


def test_search_ties_are_stable_and_empty_index_is_safe(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()
    assert adapter.search(_vector(0), 3) == []
    adapter.upsert("mem_b", _vector(1), _metadata())
    adapter.upsert("mem_a", _vector(1), _metadata())
    assert [item["object_id"] for item in adapter.search(_vector(1), 2)] == ["mem_a", "mem_b"]


@pytest.mark.parametrize(
    "operation",
    [
        lambda adapter: adapter.search(_vector(0), 0),
        lambda adapter: adapter.search([1.0], 1),
        lambda adapter: adapter.upsert("mem_a", _vector(0) * 2, _metadata()),
        lambda adapter: adapter.upsert("mem_a", _vector(0), {**_metadata(), "dimension": 512}),
    ],
)
def test_index_rejects_invalid_requests(isolated_workspace, operation):
    with pytest.raises(vector_index.VectorIndexError):
        operation(vector_index.VectorIndexAdapter())


def test_rebuild_compacts_labels_and_keeps_only_supplied_items(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_old", _vector(0), _metadata())
    result = adapter.rebuild(
        [
            {"object_id": "mem_z", "vector": _vector(2)},
            {"object_id": "mem_a", "vector": _vector(3)},
        ]
    )
    assert result == {"status": "ready", "object_count": 2}
    metadata = json.loads(adapter.metadata_path.read_text(encoding="utf-8"))
    assert metadata["object_to_label"] == {"mem_a": 0, "mem_z": 1}
    assert adapter.search(_vector(0), 3) == [
        {"object_id": "mem_a", "similarity": 0.0},
        {"object_id": "mem_z", "similarity": 0.0},
    ]


def test_failed_rebuild_does_not_replace_previous_index(isolated_workspace, monkeypatch):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_old", _vector(0), _metadata())
    old_path = adapter.index_path.read_bytes()

    def fail(*_args, **_kwargs):
        raise vector_index.VectorIndexUnavailable("simulated rebuild failure")

    monkeypatch.setattr(adapter, "_persist_locked", fail)
    with pytest.raises(vector_index.VectorIndexUnavailable):
        adapter.rebuild([{"object_id": "mem_new", "vector": _vector(1)}])
    assert adapter.index_path.read_bytes() == old_path
    reopened = vector_index.VectorIndexAdapter()
    assert reopened.search(_vector(0), 1)[0]["object_id"] == "mem_old"


def test_corrupt_index_is_reported_unavailable(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()
    adapter.upsert("mem_corrupt", _vector(0), _metadata())
    adapter.metadata_path.write_text("not-json", encoding="utf-8")

    reopened = vector_index.VectorIndexAdapter()
    assert reopened.status()["status"] == "unavailable"
    with pytest.raises(vector_index.VectorIndexUnavailable):
        reopened.search(_vector(0), 1)


def test_missing_usearch_is_reported_explicitly(isolated_workspace, monkeypatch):
    monkeypatch.setattr(
        vector_index,
        "_load_usearch",
        lambda: (_ for _ in ()).throw(vector_index.VectorIndexUnavailable("usearch missing")),
    )
    with pytest.raises(vector_index.VectorIndexUnavailable, match="usearch missing"):
        vector_index.VectorIndexAdapter().upsert("mem_missing", _vector(0), _metadata())


def test_process_local_concurrent_upserts_are_serialized(isolated_workspace):
    adapter = vector_index.VectorIndexAdapter()

    def add(index):
        return adapter.upsert(f"mem_{index:02d}", _vector(index), _metadata())["status"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result == "ready" for result in pool.map(add, range(8)))
    assert adapter.status()["object_count"] == 8


@pytest.mark.real_ltm_vector_sync
def test_store_commit_stages_vector_and_rejects_embedding_failure(isolated_workspace, monkeypatch):
    from memory_engine.ltm.embedding_adapter import EmbeddingBatch

    event_stream = __import__("memory_engine.event_stream", fromlist=["event_stream"])
    event_stream.save_messages([{"role": "user", "content": "source"}])
    meta_memory.bootstrap_system_tags()
    root_tag = next(tag for tag in meta_memory.system_owned_tags() if tag["tag_key"] == "user_model")
    vector_index.close_vector_indexes()
    monkeypatch.setattr(
        vector_index,
        "embed_documents",
        lambda texts: EmbeddingBatch(vector_index.MODEL_NAME, vector_index.DIMENSION, (_vector(0),)),
    )
    plan = {
        "objects": [
            {
                "object_id": "mem_1234567890abcdef1234567890abcdef",
                "object_type": "assertion",
                "title": "Sync object",
                "summary": None,
                "payload": {"schema_version": 1, "body": "Body", "structured_data": None},
            }
        ],
        "links": [
            {
                "source": {"object_id": "mem_1234567890abcdef1234567890abcdef"},
                "relation_type": "sourced_from",
                "target": {"event_slots": {"start": 0, "end": 0}},
            },
            {
                "source": {"object_id": "mem_1234567890abcdef1234567890abcdef"},
                "relation_type": "tagged_with",
                "target": {"object_id": root_tag["object_id"]},
            }
        ],
    }
    committed = ltm_store.commit_write_plan(plan, "vector-sync")
    assert committed["status"] == "committed"
    assert (
        vector_index.get_vector_index().search(_vector(0), 1)[0]["object_id"]
        == "mem_1234567890abcdef1234567890abcdef"
    )
    def fail(_texts):
        raise embedding_adapter.EmbeddingServiceError("embedding unavailable")

    monkeypatch.setattr(vector_index, "embed_documents", fail)
    failed_id = "mem_abcdefabcdefabcdefabcdefabcdefab"
    failed_plan = {
        "objects": [{**plan["objects"][0], "object_id": failed_id}],
        "links": [
            {**plan["links"][0], "source": {"object_id": failed_id}},
            {**plan["links"][1], "source": {"object_id": failed_id}},
        ],
    }
    failed = ltm_store.commit_write_plan(failed_plan, "vector-failure")
    assert failed["status"] == "rejected"
    assert failed["errors"][0]["code"] == "vector_sync"
    assert ltm_store.get_object("mem_1234567890abcdef1234567890abcdef") is not None


def test_store_recovers_prepared_vector_journal(isolated_workspace, monkeypatch):
    calls = []

    monkeypatch.setattr(
        vector_index,
        "install_staged_vector",
        lambda staged, root=None: calls.append((staged, root)),
    )
    connection = ltm_store.ensure_ltm_store()
    connection.execute(
        """INSERT INTO ltm_vector_journal(
               journal_id, state, staged_index_path, staged_metadata_path, created_at, updated_at)
           VALUES ('journal_test', 'prepared', 'index.stage', 'metadata.stage', 'now', 'now')"""
    )
    connection.commit()
    connection.close()

    recovered = ltm_store.ensure_ltm_store()
    state = recovered.execute(
        "SELECT state FROM ltm_vector_journal WHERE journal_id = 'journal_test'"
    ).fetchone()[0]
    recovered.close()

    assert calls == [({"index_path": "index.stage", "metadata_path": "metadata.stage"}, None)]
    assert state == "completed"


def test_observer_exposes_vector_index_metadata_without_vectors(isolated_workspace):
    snapshot = observer.memory_engine_snapshot(messages=[])
    vector_snapshot = snapshot["ltm"]["vector_index"]
    assert vector_snapshot["status"] == "not_initialized"
    assert "vectors" not in json.dumps(snapshot, ensure_ascii=False)
    assert "Payload" not in json.dumps(snapshot, ensure_ascii=False)
