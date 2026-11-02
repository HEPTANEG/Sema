"""Regressions for the real-conversation search and truncated-output failure."""

import copy
import json
from dataclasses import replace

import pytest
from persistence import trajectory

from memory_engine import event_stream
from memory_engine.ltm import candidate_generation, ltm_retrieval, ltm_store
from memory_engine.ltm import maintenance_observation, memory_retrieval as retrieval
from memory_engine.ltm import retrieval_observation as observation


OBJECT_ID = "mem_" + "a" * 32


def test_tool_history_omits_planning_without_mutating_provider_response():
    calls = [{"id": "read", "type": "function", "function": {"name": "get_object", "arguments": "{}"}}]
    response = {"content": "I have enough evidence. Let me read again.", "reasoning_content": "provider-required reasoning", "tool_calls": calls}
    original = copy.deepcopy(response)
    history = retrieval._assistant_history(response, tool_calls=calls)
    assert history == {"role": "assistant", "content": "", "reasoning_content": response["reasoning_content"], "tool_calls": calls}
    assert response == original
    assert retrieval._assistant_history({"content": "checkpoint JSON"})["content"] == "checkpoint JSON"


def test_tool_planning_is_not_replayed_but_raw_trajectory_and_continuation_survive(isolated_workspace, monkeypatch):
    monkeypatch.setattr(ltm_store, "get_object", lambda *args, **kwargs: {"object_id": OBJECT_ID})
    planning = "I have verified everything. I will return a checkpoint, but read again."
    calls = [{"id": "read", "type": "function", "function": {"name": "get_object", "arguments": json.dumps({"object_id": OBJECT_ID, "projection": "payload"})}}]
    terminal = json.dumps({"kind": "checkpoint", "message": "Verified", "evidence": [{"object_id": OBJECT_ID, "title": "Source", "body": "Exact source"}]})
    outputs = [{"role": "assistant", "content": planning, "tool_calls": calls},
               {"role": "assistant", "content": terminal},
               {"role": "assistant", "content": '{"kind":"need_clue","message":"Which date?"}'}]
    requests = []
    def provider(messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        message = outputs.pop(0)
        response = {"choices": [{"message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}]}
        observer = kwargs["exchange_observer"]
        payload = {"model": "fake", "messages": messages, "tools": kwargs["tools"]}
        observer.on_request(json.dumps(payload).encode(), payload, {})
        observer.on_response(json.dumps(response).encode(), message, response["choices"][0]["finish_reason"], {})
        return response
    monkeypatch.setattr(retrieval, "call_chat_completion", provider)
    monkeypatch.setattr(maintenance_observation, "get_object", lambda args: {"status": "ok", "items": [{"object_id": OBJECT_ID, "title": "Source", "payload": {"body": "Exact source"}}]})
    monkeypatch.setattr(ltm_store, "record_successful_uses", lambda *args: {"status": "committed"})
    state = {}
    first = retrieval.run_retrieve_memory({"query": "find source", "message": "Start here"}, runtime_state=state)
    assert first["kind"] == "checkpoint"
    second = retrieval.run_retrieve_memory({"continuation_id": first["continuation_id"], "message": "Check a different date"}, runtime_state=state)
    assert second["kind"] == "need_clue"
    for history in requests[1:]:
        assert planning not in json.dumps(history)
        action = next(m for m in history if m.get("tool_calls"))
        assert action["content"] == "" and action["tool_calls"] == calls
        result = next(m for m in history if m["role"] == "tool")
        assert result["tool_call_id"] == "read"
        assert json.loads(result["content"])["items"][0]["payload"]["body"] == "Exact source"
    assert any(m.get("content") == terminal for m in requests[-1])
    assert "Check a different date" in requests[-1][-1]["content"]
    event = None
    for trace in trajectory.list_trajectories(process_type="memory_retriever"):
        candidate_events = [e for e in trajectory.get_trajectory(trace["trajectory_id"])["events"] if e["event_type"] == "model.responded"]
        for candidate in candidate_events:
            refs = dict(ref.split(":", 1) for ref in candidate["artifact_refs"].split(","))
            raw_candidate = json.loads(trajectory.load_artifact(refs["provider_response"])["content"])
            if raw_candidate["choices"][0]["message"].get("content") == planning:
                event, raw = candidate, raw_candidate
                break
        if event is not None:
            break
    assert raw["choices"][0]["message"]["content"] == planning


@pytest.mark.parametrize("fail_embedding", [False, True])
def test_both_dispatches_real_services_and_preserves_partial_results(monkeypatch, fail_embedding):
    calls = []
    def semantic(queries, **kwargs):
        calls.append(("embedding", queries))
        if fail_embedding:
            raise RuntimeError("embedding offline")
        return [{"status": "ok", "items": [{"object_id": OBJECT_ID, "similarity": .9}]}]
    def lexical(args, **kwargs):
        calls.append(("lexical", args["text"]))
        return {"status": "ok", "items": [{"object_id": OBJECT_ID, "title": "criteria"}]}
    monkeypatch.setattr(ltm_retrieval, "semantic_search_many", semantic)
    monkeypatch.setattr(ltm_retrieval, "search_objects", lexical)
    result = candidate_generation.build_candidate_pool([{"query": "industry criteria", "service": "both"}])
    assert sorted(calls) == [("embedding", ["industry criteria"]), ("lexical", "industry criteria")]
    assert result["status"] == ("partial" if fail_embedding else "ok")
    assert len(result["items"]) == 1
    assert {e["source"] for e in result["query_evidence"]} == ({"lexical"} if fail_embedding else {"lexical", "embedding"})


def test_unknown_service_is_not_successful_empty_pool():
    with pytest.raises(ValueError, match="services"):
        candidate_generation.build_candidate_pool([{"query": "criteria", "service": "typo"}])


@pytest.mark.parametrize("name,args", [
    ("get_links", {"object_id": OBJECT_ID, "direction": "outgoing"}),
    ("read_event_slots", {"object_id": OBJECT_ID, "slot_start": 100, "slot_end": 102}),
    ("read_event_slots", {}),
    ("read_event_slots", {"start_slot_seq": True}),
    ("read_event_slots", {"start_slot_seq": -1}),
    ("get_object", {"projection": "body"}),
    ("get_object", {"limit": 0}),
    ("traverse_objects", {"start_object_ids": [OBJECT_ID], "max_depth": 4}),
])
def test_invalid_observation_arguments_never_reach_backend(monkeypatch, name, args):
    monkeypatch.setattr(maintenance_observation, name, lambda *_a, **_k: pytest.fail("backend must not run"))
    with pytest.raises(ValueError):
        observation.run(name, args)


def test_schema_and_backend_agree_on_explicit_payload_and_slot_range(isolated_workspace, monkeypatch):
    for content in ["unrelated", "industry criteria: demand, growth, timing"]:
        event_stream.append_message({"role": "user", "content": content}, root=isolated_workspace)
    plan = {"objects": [{"object_id": OBJECT_ID, "object_type": "assertion", "title": "Industry criteria", "summary": None,
                         "payload": {"schema_version": 1, "body": "demand, growth, timing"}}],
            "links": [{"source": {"object_id": OBJECT_ID}, "relation_type": "sourced_from", "target": {"event_slots": {"start": 1, "end": 1}}}]}
    assert ltm_store.commit_write_plan(plan, "retrieval-contract-test", isolated_workspace)["status"] == "committed"
    core = observation.run("get_object", {"object_id": OBJECT_ID}, root=isolated_workspace)
    assert core["actual_projection"] == "core" and "payload" not in core["items"][0]
    payload = observation.run("get_object", {"object_id": OBJECT_ID, "projection": "payload"}, root=isolated_workspace)
    assert payload["items"][0]["payload"]["body"] == "demand, growth, timing"
    fts = observation.run("get_object", {"query": "demand", "projection": "payload"}, root=isolated_workspace)
    assert [item["object_id"] for item in fts["items"]] == [OBJECT_ID]
    links = observation.run("get_links", {"source_object_ids": [OBJECT_ID], "relation_types": ["sourced_from"]}, root=isolated_workspace)
    assert len(links["items"]) == 1 and links["items"][0]["slot_seq_start"] == 1
    assert observation.run("get_links", {"target_object_ids": [OBJECT_ID]}, root=isolated_workspace)["items"] == []
    monkeypatch.setattr(ltm_store, "get_maintenance_runtime_state", lambda *_a: pytest.fail("Retrieval must not read coverage cursor"))
    slots = observation.run("read_event_slots", {"start_slot_seq": 1, "limit": 1, "include_adjacent_context": False}, root=isolated_workspace)
    assert [s["slot_seq"] for s in slots["target_slots"]] == [1]
    assert "coverage_eligible_slot_seqs" not in slots
    for schema in observation.tool_schemas():
        assert schema["function"]["parameters"]["additionalProperties"] is False


def test_profile_enforces_projection_and_depth_limits(monkeypatch):
    captured = []
    monkeypatch.setattr(maintenance_observation, "traverse_objects", lambda args: captured.append(args) or {"status": "ok", "items": []})
    profile = replace(observation.PROFILE, max_depth=1, max_payload_objects=2)
    observation.run("traverse_objects", {"start_object_ids": [OBJECT_ID], "projection": "payload", "max_nodes": 50}, profile=profile)
    assert captured[0]["max_nodes"] == 2 and captured[0]["max_depth"] == 1


def test_slot_budget_omits_whole_records_and_reports_next_range(monkeypatch):
    slots = [{"slot_seq": i, "content": "word " * 150} for i in range(5)]
    original = {"status": "ok", "target_slots": slots, "context_slots": [], "coverage_eligible_slot_seqs": list(range(5))}
    monkeypatch.setattr(maintenance_observation, "read_event_slots", lambda args: original)
    profile = replace(observation.PROFILE, max_result_tokens=650)
    result = observation.run("read_event_slots", {"start_slot_seq": 0}, profile=profile)
    assert result["status"] == "partial"
    assert 0 < len(result["target_slots"]) < 5
    assert result["target_slots"] == slots[:len(result["target_slots"])]
    assert result["next_start_slot_seq"] == len(result["target_slots"])
    assert observation._tokens(result) <= profile.max_result_tokens
    assert len(original["target_slots"]) == 5


def test_oversized_source_is_not_recorded_as_observed(monkeypatch):
    monkeypatch.setattr(maintenance_observation, "get_object", lambda args: {"status": "ok", "items": [{"object_id": OBJECT_ID, "payload": {"body": "word " * 14000}}]})
    from memory_engine.ltm.object_references import ObjectReferenceMap

    session = {"object_references": ObjectReferenceMap()}
    result = retrieval._run_internal_tool("get_object", {"object_id": OBJECT_ID, "projection": "payload"}, session)
    assert result["status"] == "error"
    assert OBJECT_ID not in session.get("observed_object_ids", set())


def test_retrieval_model_contract_uses_session_refs_without_leaking_object_ids():
    from memory_engine.ltm import memory_retrieval as retrieval
    from memory_engine.ltm.object_references import ObjectReferenceMap

    session = {"object_references": ObjectReferenceMap()}
    pool = {
        "pool_id": "cp_test",
        "status": "ok",
        "items": [{"object_id": OBJECT_ID, "object_type": "assertion", "title": "Source"}],
        "errors": [],
    }
    projected = retrieval._pool_projection(session, pool, "title_summary")
    assert projected["items"] == [{"ref": 1, "object_type": "assertion", "title": "Source"}]
    assert OBJECT_ID not in str(projected)

    args = retrieval._resolve_model_args("get_object", {"object_ref": 1, "projection": "payload"}, session)
    assert args["object_id"] == OBJECT_ID
    assert "object_ref" not in args

    result = {"status": "ok", "items": [{"object_id": OBJECT_ID, "title": "Source"}]}
    model_result = retrieval._modelize_result(result, session)
    assert model_result["items"] == [{"ref": 1, "title": "Source"}]
    assert OBJECT_ID not in str(model_result)


def test_retrieval_observation_schemas_use_integer_refs():
    from memory_engine.ltm import memory_retrieval as retrieval

    schemas = {schema["function"]["name"]: schema["function"]["parameters"] for schema in retrieval.RETRIEVAL_TOOL_SCHEMAS}
    get_object = schemas["get_object"]["properties"]
    get_links = schemas["get_links"]["properties"]
    assert "object_id" not in get_object and "object_ids" not in get_object
    assert get_object["object_ref"]["type"] == "integer"
    assert get_object["object_refs"]["items"]["type"] == "integer"
    assert get_links["source_object_refs"]["items"]["type"] == "integer"


def test_retrieval_terminal_evidence_refs_resolve_to_real_ids():
    from memory_engine.ltm import memory_retrieval as retrieval
    from memory_engine.ltm.object_references import ObjectReferenceMap

    session = {"object_references": ObjectReferenceMap(), "observed_object_ids": {OBJECT_ID}}
    session["object_references"].register(OBJECT_ID)
    result = retrieval._validate_terminal({
        "kind": "checkpoint",
        "message": "Verified",
        "evidence": [{"ref": 1, "title": "Source", "body": "Exact"}],
    }, session)
    assert result["evidence"][0]["object_id"] == OBJECT_ID
    assert "ref" not in result["evidence"][0]


def _provider(monkeypatch, outputs):
    calls = []
    def call(messages, **kwargs):
        calls.append((copy.deepcopy(messages), kwargs))
        message, reason = outputs.pop(0)
        return {"choices": [{"message": message, "finish_reason": reason}]}
    monkeypatch.setattr(retrieval, "call_chat_completion", call)
    return calls


def test_null_structured_data_delivers_source_without_format_correction(isolated_workspace, monkeypatch):
    source = {"object_id": OBJECT_ID, "object_type": "assertion", "title": "Source",
              "payload": {"schema_version": 1, "body": "Exact source", "structured_data": None}}
    assert ltm_store.commit_write_plan({"objects": [source], "links": []}, "nullable-evidence", isolated_workspace)["status"] == "committed"
    evidence = {"object_id": OBJECT_ID, "title": "Source", "body": "Exact source", "structured_data": None}
    calls = _provider(monkeypatch, [
        ({"tool_calls": [{"id": "read", "function": {"name": "get_object", "arguments": json.dumps({"object_id": OBJECT_ID, "projection": "payload"})}}]}, "tool_calls"),
        ({"content": json.dumps({"kind": "checkpoint", "message": "Verified", "evidence": [evidence]})}, "stop"),
    ])
    state = {}
    result = retrieval.run_retrieve_memory({"query": "source"}, runtime_state=state)
    assert result["kind"] == "checkpoint" and result["evidence"] == [evidence]
    assert len(calls) == 2
    observed = next(json.loads(m["content"]) for m in state[retrieval.SESSION_KEY]["history"] if m["role"] == "tool")
    assert observed["items"][0]["payload"]["structured_data"] is None
    assert state[retrieval.SESSION_KEY]["successful_use_object_ids"] == {OBJECT_ID}
    trace = trajectory.list_trajectories(process_type="memory_retriever")[0]
    events = trajectory.get_trajectory(trace["trajectory_id"])["events"]
    assert not any(e["event_type"] == "retriever.format_correction" for e in events)


@pytest.mark.parametrize("fields", [{}, {"structured_data": None}, {"structured_data": {}}, {"structured_data": {"layers": ["reasoning", "interaction"]}}])
def test_evidence_preserves_optional_structured_data(fields):
    evidence = {"object_id": OBJECT_ID, "title": "Source", **fields}
    assert retrieval._validate_evidence([evidence], {"observed_object_ids": {OBJECT_ID}}) == [evidence]


@pytest.mark.parametrize("value", [[], "", 0, False])
def test_evidence_still_rejects_non_object_structured_data(value):
    with pytest.raises(retrieval.RetrievalRuntimeError, match="object or null"):
        retrieval._validate_evidence([{"object_id": OBJECT_ID, "title": "Source", "structured_data": value}], {"observed_object_ids": {OBJECT_ID}})


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_single_format_correction_disables_tools_and_excludes_failed_prose(isolated_workspace, monkeypatch, reason):
    calls = _provider(monkeypatch, [({"content": "BROKEN_PROSE search plans"}, reason),
                                  ({"content": '{"kind":"checkpoint","message":"Verified observations only"}'}, "stop")])
    state = {}
    result = retrieval.run_retrieve_memory({"query": "criteria"}, runtime_state=state)
    assert result["kind"] == "checkpoint" and len(calls) == 2
    assert calls[0][1]["tools"] and calls[1][1]["tools"] is None
    assert "BROKEN_PROSE" not in json.dumps(state[retrieval.SESSION_KEY]["history"])
    assert calls[1][1]["timeout_seconds"] <= calls[0][1]["timeout_seconds"]
    trace = trajectory.list_trajectories(process_type="memory_retriever")[0]
    events = trajectory.get_trajectory(trace["trajectory_id"])["events"]
    corrections = [e for e in events if e["event_type"] == "retriever.format_correction"]
    assert len(corrections) == 1


@pytest.mark.parametrize("second,reason,code", [
    ({"content": "still not JSON"}, "stop", "invalid_result"),
    ({"content": '{"kind":"checkpoint","message":"looks complete"}'}, "length", "output_truncated"),
    ({"tool_calls": [{"id": "bad", "function": {"name": "get_object", "arguments": "{}"}}]}, "stop", "invalid_result"),
])
def test_failed_correction_returns_resumable_checkpoint_without_more_search(isolated_workspace, monkeypatch, second, reason, code):
    responses = [({"content": "bad"}, "stop"), (second, reason)]
    calls = _provider(monkeypatch, responses)
    monkeypatch.setattr(retrieval, "_run_internal_tool", lambda *_a: pytest.fail("format correction cannot search"))
    state = {}
    result = retrieval.run_retrieve_memory({"query": "criteria"}, runtime_state=state)
    assert result["kind"] == "technical" and result["error"]["code"] == code
    assert result["continuation_id"] and len(calls) == 2
    assert not any(m.get("tool_calls") for m in state[retrieval.SESSION_KEY]["history"])
    responses.append(({"content": '{"kind":"checkpoint","message":"Recovered"}'}, "stop"))
    resumed = retrieval.run_retrieve_memory({"continuation_id": result["continuation_id"], "message": "Continue from existing observations"}, runtime_state=state)
    assert resumed["kind"] == "checkpoint" and len(calls) == 3
    assert calls[-1][1]["tools"]


def test_correction_uses_original_deadline(isolated_workspace, monkeypatch):
    clock = {"now": 0}
    calls = []
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])
    def provider(messages, timeout, **kwargs):
        calls.append(timeout)
        clock["now"] += 70
        return {"content": "bad"}
    monkeypatch.setattr(retrieval, "_call_agent", provider)
    result = retrieval.run_retrieve_memory({"query": "criteria"}, runtime_state={})
    assert result["error"]["code"] == "timeout"
    assert calls == [120, 50]


@pytest.mark.parametrize("interrupt", ["timeout", "cancelled"])
def test_no_correction_after_runtime_interrupt(isolated_workspace, monkeypatch, interrupt):
    clock, calls = {"now": 0}, []
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock["now"])
    def provider(*args, **kwargs):
        calls.append(True)
        clock["now"] = 121 if interrupt == "timeout" else 1
        return {"content": "broken", "finish_reason": "length"}
    def cancel():
        if interrupt == "cancelled" and calls:
            raise RuntimeError("cancelled")
    monkeypatch.setattr(retrieval, "_call_agent", provider)
    result = retrieval.run_retrieve_memory({"query": "criteria"}, runtime_state={}, check_cancel=cancel)
    assert result["error"]["code"] == interrupt and len(calls) == 1


def test_correction_cannot_promote_unobserved_candidates_to_evidence(isolated_workspace, monkeypatch):
    evidence = [{"object_id": OBJECT_ID, "title": "Candidate, not observed"}]
    _provider(monkeypatch, [({"content": "broken"}, "stop"),
                           ({"content": json.dumps({"kind": "checkpoint", "message": "answer", "evidence": evidence})}, "stop")])
    state = {}
    result = retrieval.run_retrieve_memory({"query": "criteria"}, runtime_state=state)
    assert result["error"]["code"] == "invalid_result"
    assert not state[retrieval.SESSION_KEY]["evidence_object_ids"]
    assert not state[retrieval.SESSION_KEY]["successful_use_object_ids"]


def test_search_observe_payload_and_corrected_delivery_end_to_end(isolated_workspace, monkeypatch):
    plan = {"objects": [{"object_id": OBJECT_ID, "object_type": "assertion", "title": "Industry criteria", "summary": None,
                         "payload": {"schema_version": 1, "body": "demand, growth, timing"}}], "links": []}
    assert ltm_store.commit_write_plan(plan, "retrieval-delivery-test", isolated_workspace)["status"] == "committed"

    monkeypatch.setattr(ltm_retrieval, "semantic_search_many", lambda queries, **kwargs: [{"status": "ok", "items": []} for _ in queries])
    def tool(name, args):
        return {"tool_calls": [{"id": name, "function": {"name": name, "arguments": json.dumps(args)}}]}
    evidence = {"object_id": OBJECT_ID, "title": "Industry criteria", "body": "demand, growth, timing"}
    calls = _provider(monkeypatch, [
        (tool("search_candidates", {"query": "demand", "service": "both"}), "tool_calls"),
        (tool("get_object", {"object_id": OBJECT_ID, "projection": "payload"}), "tool_calls"),
        ({"content": "truncated plans"}, "length"),
        ({"content": json.dumps({"kind": "checkpoint", "message": "Verified source", "evidence": [evidence]})}, "stop"),
    ])
    state = {}
    result = retrieval.run_retrieve_memory({"query": "industry criteria"}, runtime_state=state)
    assert result["kind"] == "checkpoint" and result["evidence"] == [evidence]
    history = state[retrieval.SESSION_KEY]["history"]
    observations = [json.loads(m["content"]) for m in history if m["role"] == "tool"]
    assert observations[0]["candidate_count"] == 1
    assert observations[1]["items"][0]["payload"]["body"] == evidence["body"]
    assert len(calls) == 4 and calls[-1][1]["tools"] is None
    assert state[retrieval.SESSION_KEY]["successful_use_object_ids"] == {OBJECT_ID}
