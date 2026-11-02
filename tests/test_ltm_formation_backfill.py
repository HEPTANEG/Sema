import json
import sqlite3

def _messages(count=8):
    messages = []
    for index in range(count):
        messages.append({"role": "user" if index % 2 == 0 else "assistant", "content": f"history {index}"})
    return messages


def test_historical_backfill_excludes_current_buffer_and_processes_one_batch(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill

    messages = _messages()
    event_stream.save_messages(messages)
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({6, 7}, 3))
    runtime_state = {"eval_formation_decisions": [{"action": "no_memory"}]}

    result = formation_backfill.run_formation_backfill(messages, runtime_state)

    assert result["triggered"] is True
    assert result["processed_count"] == 1
    assert result["historical_backfill_end_slot"] == 5
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        rows = connection.execute(
            "SELECT pending_slot_ranges, status FROM formation_work_items ORDER BY created_at"
        ).fetchall()
    assert rows
    assert all(6 not in range(item["start"], item["end"] + 1) for row in rows for item in json.loads(row[0]))
    assert all(7 not in range(item["start"], item["end"] + 1) for row in rows for item in json.loads(row[0]))


def test_historical_backfill_keeps_fixed_end_boundary(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill

    messages = _messages(6)
    event_stream.save_messages(messages)
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({4, 5}, 100))
    first = formation_backfill.run_formation_backfill(
        messages, {"eval_formation_decisions": [{"action": "no_memory"}]}
    )
    assert first["historical_backfill_end_slot"] == 3

    messages.extend(_messages(2))
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({6, 7}, 100))
    second = formation_backfill.run_formation_backfill(
        messages, {"eval_formation_decisions": [{"action": "no_memory"}]}
    )
    assert second["historical_backfill_end_slot"] == 3


def test_historical_backfill_status_is_metadata_only(isolated_workspace):
    from memory_engine.ltm import formation_backfill

    status = formation_backfill.formation_backfill_status()

    assert set(status) == {
        "status",
        "cursor_slot_seq",
        "historical_backfill_end_slot",
        "last_error",
        "updated_at",
    }
    assert status["status"] == "not_initialized"


def test_empty_history_does_not_bootstrap_ltm(isolated_workspace):
    from memory_engine.ltm import formation_backfill, ltm_store

    result = formation_backfill.run_formation_backfill([], {})

    assert result["triggered"] is False
    assert not ltm_store.ltm_db_path().exists()


def test_backfill_only_discovers_slots_older_than_buffer(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill

    messages = _messages()
    event_stream.save_messages(messages)
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({3, 4}, 100))
    result = formation_backfill.run_formation_backfill(
        messages, {"eval_formation_decisions": [{"action": "no_memory"}]}
    )
    assert result["historical_backfill_end_slot"] == 2
    with event_stream.ensure_event_stream() as connection:
        rows = formation._work_item_rows(connection)
    assert rows
    assert all(max(formation._slots_from_ranges(formation._row_ranges(row))) < 3 for row in rows)


def test_empty_buffer_defers_without_freezing_boundary(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill

    messages = _messages()
    event_stream.save_messages(messages)
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: (set(), 100))
    result = formation_backfill.run_formation_backfill(messages, {})
    assert result["status"] == "deferred"
    with event_stream.ensure_event_stream() as connection:
        assert formation_backfill._state(connection, formation_backfill.STATE_END) is None
        assert formation._work_item_rows(connection) == []
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({3, 4}, 100))
    result = formation_backfill.run_formation_backfill(
        messages, {"eval_formation_decisions": [{"action": "no_memory"}]}
    )
    assert result["historical_backfill_end_slot"] == 2


def test_legacy_queue_respects_live_boundary(isolated_workspace):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill

    with event_stream.ensure_event_stream() as connection:
        formation._ensure_tables(connection)
        for start, end in [(0, 1), (2, 5), (6, 7)]:
            formation._create_work_item(connection, "unassigned", None,
                                        [{"start": start, "end": end}], 2)
        rows = formation_backfill._eligible_rows(connection, 7, {3, 4})
        assert len(rows) == 1
        assert formation._row_ranges(rows[0]) == [{"start": 0, "end": 1}]
        assert formation_backfill._eligible_rows(connection, 7, set()) == []
    assert formation_backfill._historical_slots(set(range(8)), {3, 4}, 7) == {0, 1, 2}


def test_online_queue_does_not_execute_too_new_legacy_task(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    messages = _messages()
    event_stream.save_messages(messages)
    with event_stream.ensure_event_stream() as connection:
        formation._ensure_tables(connection)
        formation._create_work_item(connection, "unassigned", None,
                                    [{"start": 6, "end": 7}], 2)
        connection.commit()
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({3, 4}, 100))
    monkeypatch.setattr(formation, "_refresh_work_items", lambda *args: {"buffer_slot_count": 2, "created_count": 0})
    monkeypatch.setattr(formation, "_process_item", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsafe task executed")))
    result = formation.run_formation_maintenance(messages, runtime_state={})
    assert result["processed_count"] == 0
