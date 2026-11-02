import sqlite3

import pytest

from test_ltm_core_store import _id, _plan, _seed_stream


def _deleted(isolated_workspace, monkeypatch):
    from memory_engine.ltm import ltm_store as store
    _seed_stream(isolated_workspace)
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 0)
    store.commit_write_plan(_plan(_id("a")), "first")
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 20000)
    store.finish_forgetting_batch("old")
    return store


def test_warning_default_validation_and_project_isolation(isolated_workspace, tmp_path):
    from memory_engine.ltm import storage_management as storage
    assert storage.status()["warning_mib"] is None
    for invalid in (-1, 0, True, "3", float("nan")):
        with pytest.raises(ValueError):
            storage.update_warning(invalid)
    result = storage.update_warning(.0001)
    assert result["over_warning"]
    assert storage.status(tmp_path / "other")["warning_mib"] is None
    assert storage.update_warning(None)["warning_mib"] is None


def test_purge_preview_is_stable_busy_and_preserves_live_graph(isolated_workspace, monkeypatch):
    from memory_engine.ltm import storage_management as storage, storage_gate
    store = _deleted(isolated_workspace, monkeypatch)
    preview = storage.preview()
    store.commit_write_plan(_plan(_id("b")), "second")
    monkeypatch.setattr(store, "_latest_event_slot_seq", lambda root=None: 40000)
    store.finish_forgetting_batch("later")
    store.commit_write_plan(_plan(_id("c")), "survivor")
    survivor = store.get_object(_id("c"), True)
    with pytest.raises(ValueError):
        storage.purge(preview["preview_token"])
    with storage_gate.activity():
        assert storage.purge(preview["preview_token"], confirmed=True)["status"] == "busy"
    assert storage.purge(preview["preview_token"], confirmed=True)["removed_count"] == 1
    from memory_engine.ltm import formation_backfill
    assert 1 in formation_backfill._known_provenance_slots()
    assert store.get_object(_id("c"), True) == survivor
    with sqlite3.connect(store.ltm_db_path()) as db:
        assert db.execute("SELECT COUNT(*) FROM memory_objects WHERE object_id=?", (_id("b"),)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_objects WHERE object_id=?", (_id("a"),)).fetchone()[0] == 0
    assert storage.purge(preview["preview_token"], confirmed=True)["removed_count"] == 0


def test_compaction_failure_does_not_rollback_records_and_can_retry(isolated_workspace, monkeypatch):
    from memory_engine.ltm import storage_management as storage
    _deleted(isolated_workspace, monkeypatch)
    preview = storage.preview()
    vacuum = storage._vacuum
    monkeypatch.setattr(storage, "_vacuum", lambda connection: (_ for _ in ()).throw(RuntimeError("test busy reader")))
    result = storage.purge(preview["preview_token"], confirmed=True)
    assert result["status"] == "compaction_failed" and result["records_purged"]
    assert storage.status()["deleted_object_count"] == 0
    monkeypatch.setattr(storage, "_vacuum", vacuum)
    assert storage.purge(preview["preview_token"], confirmed=True)["status"] == "purged"


def test_storage_bridge_contract(isolated_workspace):
    import io
    from bridge.ui_bridge_server import BridgeServer
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    assert server.ltm_storage_status({})["warning_mib"] is None
    assert server.ltm_storage_warning({"warning_mib": 128})["warning_mib"] == 128
    preview = server.ltm_storage_preview({})
    with pytest.raises(ValueError):
        server.ltm_storage_purge({"preview_token": preview["preview_token"]})
    assert server.ltm_storage_purge({"preview_token": preview["preview_token"], "confirmed": True})["removed_count"] == 0


def test_cross_process_activity_blocks_cleanup(isolated_workspace):
    import subprocess
    import sys
    from memory_engine.ltm import storage_gate
    import pathlib
    module_root = str(pathlib.Path(storage_gate.__file__).resolve().parents[2])
    script = "import sys; sys.path.insert(0, sys.argv[1]); from memory_engine.ltm.storage_gate import activity; "
    script += "\nwith activity(sys.argv[2]):\n print('ready', flush=True)\n input()\n"
    process = subprocess.Popen([sys.executable, "-c", script, module_root, str(isolated_workspace)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="busy"):
            with storage_gate.exclusive(isolated_workspace):
                pytest.fail("must not acquire exclusive access")
    finally:
        process.communicate("\n", timeout=10)
    with storage_gate.exclusive(isolated_workspace):
        pass
