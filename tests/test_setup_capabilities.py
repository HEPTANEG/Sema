import io
import json

import pytest

from runtime import setup_capabilities as caps
from runtime import setup_config


def test_explicit_skip_tracks_window_without_deleting_history(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation
    from persistence.session import ensure_project
    ensure_project()
    with event_stream.ensure_event_stream() as connection:
        before = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    monkeypatch.setattr(caps, "_managed", True)
    monkeypatch.setattr(caps, "_enabled", False)
    monkeypatch.setattr(caps, "_accepted", True)
    windows = iter([({1, 2}, 100), (set(), 100), ({2, 3}, 100), ({3, 4}, 100)])
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: next(windows))
    caps.observe_window([])
    caps.observe_window([])
    assert caps.excluded_slots() == set()
    caps.observe_window([])
    assert caps.excluded_slots() == {1}
    monkeypatch.setattr(caps, "_enabled", True)
    caps.observe_window([])
    assert caps.excluded_slots() == {1, 2}
    with event_stream.ensure_event_stream() as connection:
        assert connection.execute("SELECT COUNT(*) FROM setup_ltm_window").fetchone()[0] == 0
        after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert all(row[0] in after for row in before)


def test_no_expiry_without_explicit_limited_mode(isolated_workspace, monkeypatch):
    monkeypatch.setattr(caps, "_managed", True)
    monkeypatch.setattr(caps, "_enabled", False)
    monkeypatch.setattr(caps, "_accepted", False)
    from memory_engine.ltm import formation
    monkeypatch.setattr(formation, "_buffer_slots", lambda *_: pytest.fail("must not project"))
    caps.observe_window([])
    assert not caps.excluded_slots()


def test_disabled_tools_and_background_do_not_run(isolated_workspace, monkeypatch):
    monkeypatch.setattr(caps, "_enabled", False)
    from runtime.agent import tool_schemas_for_model
    from tools.tool_executor import execute_registered_tool
    from memory_engine.ltm.formation import run_formation_maintenance
    from memory_engine.ltm.formation_backfill import run_formation_backfill
    from memory_engine.context_builder import _memory_push_for_turn
    assert not caps.LTM_TOOLS & {item['function']['name'] for item in tool_schemas_for_model()}
    for tool in caps.LTM_TOOLS:
        with pytest.raises(RuntimeError, match="unavailable"):
            execute_registered_tool({"tool": tool}, None)
    assert run_formation_maintenance()["status"] == "unavailable"
    assert run_formation_backfill()["status"] == "unavailable"
    assert _memory_push_for_turn([], [], {"_memory_push_enabled": True})["content"] is None


def test_restart_refuses_busy_backend(isolated_workspace):
    from bridge.backend_server import BackendServer
    from bridge.ui_bridge_server import JsonRpcError
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.turn_lock.acquire()
    with pytest.raises(JsonRpcError, match="busy"):
        server.dispatch("backend.restart", {})
    server.turn_lock.release()
    assert server.dispatch("backend.restart", {}) == {"restarting": True}
    with pytest.raises(JsonRpcError, match="restarting"):
        server.dispatch("message.send", {"prompt": "hello"})


def test_llm_gate_requires_test_then_explicit_continue(isolated_workspace, monkeypatch):
    monkeypatch.setattr(caps, "_managed", True)
    monkeypatch.setattr(caps, "_enabled", False)
    monkeypatch.setattr(setup_config, "status", lambda *args: {"components": {"llm": {"status": "Ready"}}})
    with pytest.raises(RuntimeError, match="explicitly"):
        caps.require_chat()
    caps.accept_limited()
    caps.require_chat()


def test_mixed_formation_item_only_completes_allowed_ranges(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation
    from memory_engine.focus import lifecycle
    event_stream.save_messages([{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}])
    with event_stream.ensure_event_stream() as connection:
        formation._ensure_tables(connection)
        connection.execute("CREATE TABLE setup_ltm_skips (slot_seq INTEGER PRIMARY KEY, reason TEXT NOT NULL)")
        connection.execute("INSERT INTO setup_ltm_skips VALUES (0, 'unconfigured_ltm')")
        mixed, _ = formation._create_work_item(connection, "unassigned", None, [{"start": 0, "end": 1}], 0)
        skipped, _ = formation._create_work_item(connection, "unassigned", None, [{"start": 0, "end": 0}], 0)
        connection.commit()
        assert {row['work_item_id'] for row in formation._work_item_rows(connection, statuses=('pending',))} == {mixed}
    seen = []
    monkeypatch.setattr(formation, "_projection_records", lambda ranges: seen.append(ranges) or [{"slot_seq": 1}])
    monkeypatch.setattr(formation, "_formation_decision", lambda *args, **kwargs: {"action": "no_memory"})
    result = formation._process_item(mixed, [])
    assert result['status'] == 'completed'
    assert seen == [[{"start": 1, "end": 1}]]
    assert formation._process_item(skipped, [])['status'] == 'skipped'
    status = formation.formation_status()
    assert status['explicitly_skipped_count'] == 1
    assert status['completed_count'] == 1
    assert status['pending_count'] == 0
    with event_stream.ensure_event_stream() as connection:
        assert json.loads(formation._load_work_item(connection, mixed)['pending_slot_ranges']) == [{"start": 1, "end": 1}]
        assert formation._load_work_item(connection, skipped)['status'] == 'pending'
        coverage, _ = lifecycle._formation_work_item_slots(connection)
        assert coverage == {0, 1}


def test_restart_refuses_running_background_command(isolated_workspace, monkeypatch):
    from bridge.backend_server import BackendServer
    from bridge.ui_bridge_server import JsonRpcError
    from tools import background_commands
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    monkeypatch.setattr(background_commands, 'has_running_commands', lambda: True)
    with pytest.raises(JsonRpcError, match='busy'):
        server.dispatch('backend.restart', {})


def test_backend_initializes_without_loading_missing_models(isolated_workspace, monkeypatch):
    from bridge.backend_server import BackendServer
    from runtime import agent
    components = {name: {'status': 'Missing'} for name in ('llm', 'embedding', 'reranker')}
    monkeypatch.setattr(setup_config, 'status', lambda *args: {'components': components, 'infrastructure': {}})
    monkeypatch.setattr(agent, 'run_formation_backfill', lambda *args, **kwargs: pytest.fail('must not start LTM'))
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    try:
        result = server.initialize({})
        assert result['setup']['ltm_enabled'] is False
        assert not result['setup']['limited_mode_accepted']
        assert server.config_get({})['config']
    finally:
        server.wake_scheduler.stop()


def test_projection_failure_does_not_expire_pending_window(isolated_workspace, monkeypatch):
    from memory_engine.ltm import formation
    monkeypatch.setattr(caps, '_managed', True)
    monkeypatch.setattr(caps, '_enabled', False)
    monkeypatch.setattr(caps, '_accepted', True)
    monkeypatch.setattr(formation, '_buffer_slots', lambda *_: ({1, 2}, 100))
    caps.observe_window([])
    def fail(*_):
        raise RuntimeError('projection unavailable')
    monkeypatch.setattr(formation, '_buffer_slots', fail)
    caps.observe_window([])
    assert not caps.excluded_slots()


def test_pause_tracks_jumps_and_defers_empty_restore(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, formation_backfill
    event_stream.save_messages([{'role': 'user', 'content': 'Before pause'}])
    monkeypatch.setattr(caps, '_managed', True)
    monkeypatch.setattr(caps, '_enabled', False)
    monkeypatch.setattr(setup_config, 'status', lambda *args: {'components': {'llm': {'status': 'Ready'}}})
    caps.accept_limited()
    for i in range(1, 6):
        event_stream.append_message({'role': 'user', 'content': str(i)})

    monkeypatch.setattr(caps, '_accepted', False)
    monkeypatch.setattr(caps, '_enabled', True)
    monkeypatch.setattr(formation, '_buffer_slots', lambda *_: (set(), 100))
    caps.observe_window([])
    assert caps.restoration_pending()
    assert not caps.excluded_slots()
    assert formation.run_formation_maintenance()['status'] == 'deferred'
    assert formation_backfill.run_formation_backfill()['status'] == 'deferred'
    monkeypatch.setattr(formation, '_buffer_slots', lambda *_: ({5}, 100))
    caps.observe_window([])
    assert caps.excluded_slots() == {1, 2, 3, 4}
    assert not caps.restoration_pending()
    assert len(event_stream.load_messages()) == 6


def test_wake_remains_busy_between_dequeue_and_delivery():
    from runtime.wake import WakeQueue, wake_event
    queue = WakeQueue()
    event = wake_event('retriever', 'result', {})
    queue.put(event)
    assert queue.get(timeout=0) is event
    assert queue.snapshot() == []
    assert queue.has_work()
    queue.requeue(event)
    assert queue.has_work()
    assert queue.get(timeout=0) is event
    queue.mark_delivered(event)
    assert not queue.has_work()
