import copy
import json

import pytest

from memory_engine.ltm.formation_progress import FormationRetrievalProgress


def result(*items, status="ok", **metadata):
    return {"status": status, "items": list(items), **metadata}


def test_material_growth_subsets_metadata_and_relations():
    tracker = FormationRetrievalProgress(40)
    obj = {"object_id": "a", "title": "A", "summary": None}
    first, _ = tracker.observe_round([result(obj)], 1, 1)
    assert first["new_objects"] == first["new_fields"] == 1
    richer = {**obj, "payload": {"body": "Body", "structured_data": {"x": 0, "y": False}}}
    second, _ = tracker.observe_round([result(richer)], 2, 2)
    assert second["new_fields"] == 3
    for candidate in (obj, {**richer, "strength": .9, "updated_at": "now", "_raw_score": 10}):
        summary, _ = tracker.observe_round([result(candidate, next_cursor="different")], 3, 3)
        assert summary["new_fields"] == summary["new_objects"] == 0
    edge = {"link_id": "l", "relation_type": "supports", "source_object_id": "a", "target_object_id": "b"}
    summary, _ = tracker.observe_round([result({**obj, "path": [edge]})], 4, 4)
    assert summary["new_relations"] == 1
    summary, _ = tracker.observe_round([result({**obj, "path": [{**edge, "direction": "incoming"}]})], 5, 5)
    assert summary["new_relations"] == 0
    assert summary["consecutive_no_new_material_rounds"] == 1


def test_alternating_results_warn_once_and_new_material_rearms():
    tracker = FormationRetrievalProgress(40)
    a, b = {"object_id": "a", "title": "A"}, {"object_id": "b", "title": "B"}
    tracker.observe_round([result(a, b)], 1, 1)
    notices = []
    for i, items in enumerate(((b, a), (a,), (b,), (a,), (b,)), 2):
        _, emitted = tracker.observe_round([result(*items)], i, i)
        notices.extend(emitted)
    assert [n[0]["kind"] for n in notices] == ["no_new_material"]
    tracker.observe_round([result({"object_id": "c"})], 7, 7)
    for i in range(8, 11):
        _, emitted = tracker.observe_round([result()], i, i)
    assert emitted[0][0]["kind"] == "no_new_material"


def test_errors_are_counted_and_partial_material_resets():
    tracker = FormationRetrievalProgress(40)
    for i in range(1, 4):
        summary, notices = tracker.observe_round([result(status="error"), result()], i * 2, i)
    assert summary["no_new_material_error_call_count"] == 3
    assert "3 failed tool calls" in notices[0][1]
    summary, _ = tracker.observe_round([result({"object_id": "a"}, status="partial"), result(status="error")], 8, 4)
    assert summary["consecutive_no_new_material_rounds"] == 0
    assert summary["error_call_count"] == 1
    assert summary["no_new_material_error_call_count"] == 0


@pytest.mark.parametrize("limit,thresholds", [(40, [20, 30, 35]), (20, [10, 15, 18]), (3, [2, 3]), (1, [1])])
def test_budget_thresholds_and_small_limits(limit, thresholds):
    tracker = FormationRetrievalProgress(limit)
    emitted_at = []
    for used in range(1, limit + 1):
        _, notices = tracker.observe_round([result({"object_id": str(used)})], used, used)
        for metadata, message in notices:
            assert metadata["kind"] == "retrieval_budget"
            assert f"{limit - used} remain" in message
            emitted_at.append(used)
    assert emitted_at == thresholds


def test_crossed_thresholds_only_emit_highest():
    tracker = FormationRetrievalProgress(40)
    _, notices = tracker.observe_round([result({"object_id": "a"})], 36, 1)
    assert len(notices) == 1
    assert notices[0][0]["threshold"] == 35
    assert "36 of 40" in notices[0][1]
    _, notices = tracker.observe_round([result({"object_id": "b"})], 37, 2)
    assert notices == []


def test_loop_notices_pairing_scope_and_original_limit(monkeypatch):
    from memory_engine.ltm import formation

    monkeypatch.setattr(formation, "_agent_projection", lambda *args: {"evidence": "fixed"})
    monkeypatch.setattr(formation.ltm_retrieval, "run_formation_retrieval_tool", lambda *args: result())
    events, requests = [], []
    monkeypatch.setattr(formation, "record_event", lambda context, name, **kw: events.append((context, name, kw)))
    def model(messages, _context):
        requests.append(copy.deepcopy(messages))
        index = len(requests)
        if index > 5:
            return {"content": '{"action":"no_memory"}'}
        return {"content": "different narration", "tool_calls": [{
            "id": f"call-{index}", "function": {"name": "ltm_search_objects", "arguments": json.dumps({"text": f"query-{index}"})}
        }]}
    monkeypatch.setattr(formation, "_call_formation_message", model)
    for _attempt in range(2):
        requests.clear()
        events.clear()
        decision = formation._formation_decision({"source_kind": "unassigned"}, [], [], max_tool_calls=6, work_span="work")
        assert decision["action"] == "no_memory"
        assert decision["_metrics"]["retrieval_call_count"] == 5
        notices = [m for m in requests[-1] if m["role"] == "user" and "Runtime detected" in m["content"]]
        assert len(notices) == 1
        assert "3 of 6" in notices[0]["content"]
        assert requests[3][-2]["role"] == "tool"
        assert requests[3][-1] == notices[0]
        reminders = [(ctx, kw["summary"]) for ctx, name, kw in events if name == "formation.retrieval.reminder"]
        assert all(ctx == "work" for ctx, _ in reminders)
        assert [s["kind"] for _, s in reminders].count("no_new_material") == 1
    requests.clear()
    with pytest.raises(formation.FormationLoopError, match="tool-call limit"):
        formation._formation_decision({"source_kind": "unassigned"}, [], [], max_tool_calls=2)


def test_after_reminder_write_plan_repair_keeps_progress_and_event_stream_clean(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages([{"role": "user", "content": "Durable evidence"}])
    before = event_stream.load_active_slot_range(0, 100)
    monkeypatch.setattr(formation, "_agent_projection", lambda *args: {"evidence": "Durable evidence"})
    monkeypatch.setattr(formation.ltm_retrieval, "run_formation_retrieval_tool", lambda *args: result())
    monkeypatch.setattr(formation, "_normalized_write_plan", lambda _item, output, _roots: output)
    validations = iter([
        {"valid": False, "errors": [{"code": "title_length", "path": "objects[0].title", "message": "Too long"}]},
        {"valid": True},
    ])
    monkeypatch.setattr(formation, "_write_plan_validation", lambda *args, **kw: next(validations))
    requests = []
    def model(messages, _context, *, allow_retrieval=True):
        requests.append(copy.deepcopy(messages))
        n = len(requests)
        if n in (1, 2, 3):
            return {"tool_calls": [{"id": str(n), "function": {"name": "ltm_select_objects", "arguments": "{}"}}]}
        assert allow_retrieval == (n == 4)
        return {"content": json.dumps({"action": "write_plan", "objects": [{"title": "new"}]})}
    monkeypatch.setattr(formation, "_call_formation_message", model)
    outcome = formation._formation_decision({"source_kind": "unassigned"}, [], [], max_tool_calls=10)
    assert outcome["action"] == "write_plan"
    assert outcome["_metrics"]["validation_repair_count"] == 1
    assert sum("Runtime detected" in m.get("content", "") for m in requests[-1]) == 1
    assert event_stream.load_active_slot_range(0, 100) == before


def test_field_order_empty_values_and_relation_path_order():
    tracker = FormationRetrievalProgress(40)
    a = {"link_id": "1", "relation_type": "supports", "source_object_id": "a", "target_object_id": "b"}
    b = {"link_id": "2", "relation_type": "supports", "source_object_id": "b", "target_object_id": "c"}
    obj = {"object_id": "c", "payload": {"body": "body", "structured_data": {"a": 1, "b": 2}}, "path": [a, b]}
    tracker.observe_round([result(obj)], 1, 1)
    reordered = {**obj, "summary": "", "payload": {"structured_data": {"b": 2, "a": 1}, "body": "body"}, "path": [b, a]}
    summary, _ = tracker.observe_round([result(reordered)], 2, 2)
    assert (summary["new_objects"], summary["new_fields"], summary["new_relations"]) == (0, 0, 0)
