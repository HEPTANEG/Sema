import os

from memory_engine import event_stream
from memory_engine.ltm import formation, ltm_store, maintenance_pressure


def _append(root, count=3):
    for index in range(count):
        event_stream.append_message({"role": "user", "content": f"message {index}"}, root=root)


def test_maintenance_state_starts_before_slot_zero_and_advances_continuously(isolated_workspace, monkeypatch):
    _append(isolated_workspace)
    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 2)
    state = ltm_store.get_maintenance_runtime_state(isolated_workspace)
    assert state["maintenance_coverage_cursor"] == -1
    assert state["latest_event_stream_slot_seq"] == 2
    assert state["maintenance_pressure"] == 3 / 200
    run = ltm_store.start_or_resume_maintenance_run(isolated_workspace)
    assert run["status"] == "started"
    assert ltm_store.advance_maintenance_coverage(run["run_id"], 1, isolated_workspace)["status"] == "advanced"
    assert ltm_store.get_maintenance_runtime_state(isolated_workspace)["maintenance_coverage_cursor"] == 1
    try:
        ltm_store.advance_maintenance_coverage(run["run_id"], 3, isolated_workspace)
    except ValueError:
        pass
    else:
        raise AssertionError("coverage cannot exceed the latest slot")


def test_failed_run_resumes_with_same_id_and_completion_keeps_last_fifty_reports(isolated_workspace):
    _append(isolated_workspace, 1)
    run = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    ltm_store.record_maintenance_failure(run, 0, "tool failure", isolated_workspace)
    resumed = ltm_store.start_or_resume_maintenance_run(isolated_workspace)
    assert resumed["run_id"] == run and resumed["status"] == "resumed"
    ltm_store.advance_maintenance_coverage(run, 0, isolated_workspace)
    ltm_store.complete_maintenance_run(run, {"processed_slot_count": 1}, isolated_workspace)
    assert ltm_store.get_maintenance_runtime_state(isolated_workspace)["run_status"] == "idle"
    connection = ltm_store._connect(isolated_workspace, read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM maintenance_reports").fetchone()[0] == 1
    finally:
        connection.close()


def test_dead_running_owner_starts_new_run_without_reusing_run_id(isolated_workspace):
    original = ltm_store.start_or_resume_maintenance_run(isolated_workspace)
    connection = ltm_store._connect(isolated_workspace)
    try:
        connection.execute(
            "UPDATE maintenance_runtime_state SET run_owner_pid=?, next_plan_index=3, last_committed_plan_index=2 WHERE state_id=1",
            (os.getpid() + 1000000,),
        )
        connection.commit()
    finally:
        connection.close()

    restarted = ltm_store.start_or_resume_maintenance_run(isolated_workspace)

    assert restarted["status"] == "started_after_interruption"
    assert restarted["run_id"] != original["run_id"]
    assert restarted["interrupted_run_id"] == original["run_id"]
    state = ltm_store.get_maintenance_runtime_state(isolated_workspace)
    assert state["run_owner_pid"] == os.getpid()
    assert state["next_plan_index"] == 3
    assert state["last_committed_plan_index"] == 2


def test_maintenance_state_schema_migration_adds_runner_columns(isolated_workspace):
    connection = ltm_store._connect(isolated_workspace)
    try:
        ltm_store._create_core_schema(connection)
        connection.execute("DROP TABLE maintenance_runtime_state")
        connection.execute(
            """CREATE TABLE maintenance_runtime_state (
                state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                maintenance_coverage_cursor INTEGER NOT NULL DEFAULT -1,
                active_run_id TEXT,
                run_status TEXT NOT NULL DEFAULT 'idle',
                run_started_at TEXT,
                last_checkpoint_slot_seq INTEGER,
                next_plan_index INTEGER NOT NULL DEFAULT 0,
                last_committed_plan_index INTEGER,
                last_failure_slot_seq INTEGER,
                last_failure_reason TEXT,
                last_failure_at TEXT,
                updated_at TEXT NOT NULL,
                CHECK (run_status IN ('idle', 'running', 'failed'))
            )"""
        )
        connection.execute(
            """INSERT INTO maintenance_runtime_state(
                state_id, maintenance_coverage_cursor, run_status, next_plan_index, updated_at)
               VALUES (1, -1, 'idle', 0, 'old')"""
        )
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()

    migrated = ltm_store.ensure_ltm_store(isolated_workspace)
    try:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(maintenance_runtime_state)")}
        report_columns = {row["name"] for row in migrated.execute("PRAGMA table_info(maintenance_reports)")}
        assert "run_start_cursor" in columns
        assert "run_owner_pid" in columns
        assert "run_heartbeat_at" in columns
        assert "plan_commit_count" in report_columns
        assert "semantic_digest_status" in report_columns
        assert "semantic_digest_id" in report_columns
        assert "semantic_digest_token_count" in report_columns
        assert "maintainable_frontier" in report_columns
        assert "coverage_target_slot_seq" in report_columns
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == ltm_store.LTM_DB_SCHEMA_VERSION
    finally:
        migrated.close()

    run = ltm_store.start_or_resume_maintenance_run(isolated_workspace)
    assert run["status"] == "started"
    assert run["state"]["run_start_cursor"] == -1


def test_maintenance_pressure_uses_two_hundred_slot_threshold(isolated_workspace, monkeypatch):
    _append(isolated_workspace, 200)
    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 199)

    state = ltm_store.get_maintenance_runtime_state(isolated_workspace)

    assert state["unmaintained_slot_count"] == 200
    assert state["maintenance_pressure"] == 1.0


def test_maintenance_pressure_levels():
    assert maintenance_pressure.maintenance_pressure_level(0.49) == "none"
    assert maintenance_pressure.maintenance_pressure_level(0.5) == "mild"
    assert maintenance_pressure.maintenance_pressure_level(0.75) == "strong"
    assert maintenance_pressure.maintenance_pressure_level(0.9) == "urgent"
    assert maintenance_pressure.maintenance_pressure_level(1.0) == "blocked"


def test_pressure_snapshot_uses_formation_frontier_not_latest_slot(isolated_workspace, monkeypatch):
    _append(isolated_workspace, 10)
    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 4)

    snapshot = maintenance_pressure.maintenance_pressure_snapshot(isolated_workspace)

    assert snapshot["latest_event_stream_slot_seq"] == 9
    assert snapshot["maintainable_frontier"] == 4
    assert snapshot["unmaintained_slot_count"] == 5
    assert snapshot["pressure"] == 0.025


def test_pressure_snapshot_recomputes_frontier_instead_of_using_stale_runtime_value(monkeypatch):
    monkeypatch.setattr(ltm_store, "get_maintenance_runtime_state", lambda _root=None: {
        "maintainable_frontier": 1,
        "maintenance_coverage_cursor": 4,
        "latest_event_stream_slot_seq": 99,
    })
    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 17)

    snapshot = maintenance_pressure.maintenance_pressure_snapshot()

    assert snapshot["maintainable_frontier"] == 17
    assert snapshot["unmaintained_slot_count"] == 13


def test_formation_buffer_frontier_is_the_newest_buffer_slot(isolated_workspace, monkeypatch):
    _append(isolated_workspace, 1)
    monkeypatch.setattr(
        formation,
        "_buffer_slots",
        lambda _messages, _runtime_state=None, root=None: ({2, 4, 6}, 6000),
    )

    assert formation.maintainable_slot_frontier(root=isolated_workspace) == 6
