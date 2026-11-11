import json
from http.client import IncompleteRead

import pytest

from memory_engine import event_stream
from memory_engine.ltm import formation, ltm_store, maintenance_digest_cache, maintenance_runner
from persistence import trajectory


def _call(name, args, call_id="call"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _response(calls, content="", finish_reason="tool_calls"):
    return {"choices": [{"message": {"content": content, "tool_calls": calls}, "finish_reason": finish_reason}]}


def _stage_responses():
    responses = []
    for index, stage in enumerate(maintenance_runner.MAINTENANCE_STAGES[:-1]):
        responses.extend([
            _response([_call("get_object", {"limit": 1}, f"observe-{index}")]),
            _response([_call("complete_maintenance_stage", {"stage": stage, "status": "completed", "stage_summary": f"Facts relevant to {stage}."}, f"stage-{index}")]),
        ])
    responses.append(_response([_call("finish_maintenance", {}, "finish")]))
    return responses


def test_empty_tool_calls_are_omitted_from_provider_history(monkeypatch):
    messages = [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "user", "content": "continue"},
    ]
    captured = {}

    def model(outbound_messages, **_kwargs):
        captured["messages"] = outbound_messages
        return _response([], content="still working", finish_reason="stop")

    monkeypatch.setattr(maintenance_runner, "call_chat_completion", model)

    maintenance_runner._call_maintainer_model(messages, {}, "maintenance_run_test")

    assert "tool_calls" not in captured["messages"][0]
    assert all(message.get("tool_calls") for message in captured["messages"] if "tool_calls" in message)


def test_submit_plan_records_runtime_commit_phases(isolated_workspace):
    run = ltm_store.start_or_resume_maintenance_run(isolated_workspace)
    context = trajectory.start_trajectory("ltm_maintainer", run["run_id"], root=isolated_workspace)
    token = maintenance_runner._MAINTENANCE_TRAJECTORY_CONTEXT.set(context)
    retrieval_token = maintenance_runner._RETRIEVAL_STATE.set({})
    plan = {
        "atomic_operations": [{
            "operation_type": "create_object",
            "new_object": {
                "temp_id": "diagnostic-object",
                "object_type": "assertion",
                "title": "Diagnostic object",
                "payload": {"schema_version": 1, "body": "diagnostic"},
            },
        }],
    }
    try:
        result = maintenance_runner._execute_tool(
            "submit_maintenance_plan", plan, run["run_id"], maintenance_runner._empty_counters(), isolated_workspace,
            stage="recent_review",
        )
    finally:
        maintenance_runner._RETRIEVAL_STATE.reset(retrieval_token)
        maintenance_runner._MAINTENANCE_TRAJECTORY_CONTEXT.reset(token)

    assert result["status"] == "committed"
    events = trajectory.get_trajectory(context["trajectory_id"], root=isolated_workspace)["events"]
    phases = [json.loads(event["summary_json"])["phase"] for event in events if event["event_type"] == "maintainer.plan_phase"]
    assert phases == [
        "compile", "compile", "close_retriever", "close_retriever",
        "commit_maintenance_plan", "ensure_ltm_store", "ensure_ltm_store",
        "stage_vector_embedding", "stage_vector_embedding", "sqlite_commit", "sqlite_commit",
        "commit_maintenance_plan",
    ]


def test_rejected_tool_call_turn_is_followed_by_tool_results(isolated_workspace, monkeypatch):
    captured = []
    invalid = _response([
        _call("get_object", {"limit": 1}, "bad-observe"),
        _call("finish_maintenance", {}, "bad-finish"),
    ])
    responses = [invalid, invalid, invalid]

    def model(messages, **_kwargs):
        captured.append(json.loads(json.dumps(messages)))
        return responses.pop(0)

    monkeypatch.setattr(maintenance_runner, "call_chat_completion", model)
    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "failed"
    second_request = captured[1]
    assistant_index = max(index for index, item in enumerate(second_request) if item.get("role") == "assistant")
    assert second_request[assistant_index + 1]["role"] == "tool"
    assert second_request[assistant_index + 1]["tool_call_id"] == "bad-observe"
    assert second_request[assistant_index + 2]["role"] == "tool"
    assert second_request[assistant_index + 2]["tool_call_id"] == "bad-finish"


def test_staged_runner_completes_in_order_and_injects_only_previous_stage(isolated_workspace, monkeypatch):
    captured = []
    responses = _stage_responses()

    def model(messages, **_kwargs):
        captured.append(json.loads(json.dumps(messages)))
        return responses.pop(0)

    monkeypatch.setattr(maintenance_runner, "call_chat_completion", model)
    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed", result
    assert result["final_check_status"] == "passed"
    assert [stage for stage in maintenance_runner.MAINTENANCE_STAGES[:-1] if f"[{stage}]" in result["maintenance_summary"]] == list(maintenance_runner.MAINTENANCE_STAGES[:-1])
    assert json.loads(captured[2][-2]["content"])["maintenance_run"]["previous_stage_summary"] == "Facts relevant to recent_review."
    assert "global_duplicate_review" not in captured[0][0]["content"]


def test_no_new_slots_still_runs_all_semantic_stages(isolated_workspace, monkeypatch):
    seen = []
    responses = _stage_responses()
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda messages, **_kwargs: (seen.append(messages), responses.pop(0))[1])

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"
    assert len(seen) == 9
    assert all(f"[{stage}]" in result["maintenance_summary"] for stage in maintenance_runner.MAINTENANCE_STAGES[:-1])


def test_stage_report_requires_relevant_evidence(isolated_workspace, monkeypatch):
    responses = [_response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "No change."}, "stage")]) for _ in range(3)]
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "failed"
    assert "requires relevant observation" in result["failure_reason"]


@pytest.mark.parametrize(
    ("summary_text", "overflow_submissions", "expected_rewrites", "warning_repetitions"),
    [
        ("word " * 1100, 3, 2, 1),
        ("word " * 2100, 5, 4, 3),
    ],
)
def test_oversized_stage_summary_is_retried_then_truncated(
    isolated_workspace,
    monkeypatch,
    summary_text,
    overflow_submissions,
    expected_rewrites,
    warning_repetitions,
):
    responses = [_response([_call("get_object", {"limit": 1})])]
    responses.extend(
        _response([_call("complete_maintenance_stage", {
            "stage": "recent_review",
            "status": "completed",
            "stage_summary": summary_text,
        })])
        for _ in range(overflow_submissions)
    )
    for stage in maintenance_runner.MAINTENANCE_STAGES[1:-1]:
        responses.extend([
            _response([_call("get_object", {"limit": 1})]),
            _response([_call("complete_maintenance_stage", {
                "stage": stage,
                "status": "no_applicable_work",
                "stage_summary": "No additional high-confidence change.",
            })]),
        ])
    responses.append(_response([_call("finish_maintenance", {})]))
    events = []
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(
        {"ui_event_sink": events.append}, root=isolated_workspace
    )

    assert result["status"] == "completed", result
    accepted = maintenance_digest_cache.load_stage_summary("recent_review", isolated_workspace)
    assert accepted is not None
    assert maintenance_runner.count_text_tokens(accepted) <= maintenance_runner.STAGE_SUMMARY_RUNTIME_MAX_TOKENS
    assert "truncated by Runtime" in accepted
    notices = [
        event["content"] for event in events
        if event["type"] == "ltm_maintenance_runtime_notice"
        and "stage_summary has" in event.get("content", "")
    ]
    assert len(notices) == expected_rewrites
    assert all(notice.count("stage_summary has") == warning_repetitions for notice in notices)
    assert all("under 500 tokens" in notice for notice in notices)
    assert all("under 1000" not in notice and "at most 1000" not in notice for notice in notices)


def test_stage_summary_hard_limit_is_stated_in_prompt():
    prompt = maintenance_runner._stage_prompt("recent_review")

    assert "stage_summary MUST NOT exceed" in prompt
    assert "500 tokens" in prompt
    assert "normal output contract" in prompt


def test_default_initial_prompt_keeps_500_token_target(isolated_workspace):
    messages = maintenance_runner._initial_messages(
        {"run_id": "run-1", "status": "running"}, isolated_workspace, "recent_review"
    )

    assert "stage_summary MUST NOT exceed 500 tokens" in messages[0]["content"]
    assert "stage_summary MUST NOT exceed 1000 tokens" not in messages[0]["content"]


def test_default_runtime_limit_is_1000_tokens():
    assert maintenance_runner.STAGE_SUMMARY_RUNTIME_MAX_TOKENS == 1000


def test_default_runtime_tolerates_summary_above_prompt_target(isolated_workspace, monkeypatch):
    summary_text = "word " * 600
    responses = [
        _response([_call("get_object", {"limit": 1}, "observe")]),
        _response([_call("complete_maintenance_stage", {
            "stage": "recent_review",
            "status": "completed",
            "stage_summary": summary_text,
        }, "stage")]),
    ]
    for stage in maintenance_runner.MAINTENANCE_STAGES[1:-1]:
        responses.extend([
            _response([_call("get_object", {"limit": 1}, f"observe-{stage}")]),
            _response([_call("complete_maintenance_stage", {
                "stage": stage,
                "status": "no_applicable_work",
                "stage_summary": "No additional high-confidence change.",
            }, f"stage-{stage}")]),
        ])
    responses.append(_response([_call("finish_maintenance", {}, "finish")]))
    events = []
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(
        {"ui_event_sink": events.append}, root=isolated_workspace
    )

    assert result["status"] == "completed", result
    accepted = maintenance_digest_cache.load_stage_summary("recent_review", isolated_workspace)
    assert maintenance_runner.count_text_tokens(accepted) == 600
    assert "truncated by Runtime" not in accepted
    assert not [
        event for event in events
        if event["type"] == "ltm_maintenance_runtime_notice"
        and "stage_summary has" in event.get("content", "")
    ]


def test_stage_order_is_runtime_enforced(isolated_workspace, monkeypatch):
    responses = [_response([_call("complete_maintenance_stage", {"stage": "abstraction", "status": "completed", "stage_summary": "Wrong stage."})]) for _ in range(3)]
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "failed"
    assert "current stage" in result["failure_reason"]


def test_text_only_and_truncated_responses_never_complete(isolated_workspace, monkeypatch):
    for reason in (None, "length", "content_filter", "unknown"):
        responses = [_response([], content="still working", finish_reason=reason) for _ in range(3)]
        monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))
        result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)
        assert result["status"] == "failed"
        assert result["final_check_status"] == "not_run"


def test_finish_checks_coverage_before_health(isolated_workspace, monkeypatch):
    event_stream.append_message({"role": "user", "content": "review me"}, root=isolated_workspace)
    monkeypatch.setattr(formation, "maintainable_slot_frontier", lambda **_kwargs: 0)
    responses = _stage_responses()

    responses[-1:] = [
        _response([_call("finish_maintenance", {}, "early")]),
        _response([_call("mark_maintenance_coverage", {"end_slot_seq": 0}, "coverage")]),
        _response([_call("finish_maintenance", {}, "finish")]),
    ]
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"
    assert result["state"]["maintenance_coverage_cursor"] == 0


def test_maintainer_rejects_event_source_beyond_formation_frontier(isolated_workspace, monkeypatch):
    plan = {
        "atomic_operations": [
            {"operation_type": "create_object", "new_object": {
                "temp_id": "new", "object_type": "assertion", "title": "New fact",
                "payload": {"schema_version": 1, "body": "new fact"},
            }},
            {"operation_type": "create_link", "link": {
                "source": {"temp_id": "new"}, "relation_type": "sourced_from",
                "target": {"event_slots": {"start": 3, "end": 4}},
            }},
        ]
    }
    committed = []
    monkeypatch.setattr(ltm_store, "commit_maintenance_plan", lambda *args, **kwargs: committed.append(True))

    result = maintenance_runner._execute_tool(
        "submit_maintenance_plan", plan, "maintenance_run_test", {}, isolated_workspace,
        coverage_target=2, maintainable_frontier=2,
    )

    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "maintenance_source_not_eligible"
    assert result["errors"][0]["path"] == "links[0].target.event_slots"
    assert committed == []


def test_coverage_cannot_advance_beyond_fixed_target(isolated_workspace):
    run = ltm_store.start_or_resume_maintenance_run(isolated_workspace)

    result = maintenance_runner._execute_tool(
        "mark_maintenance_coverage", {"end_slot_seq": 3}, run["run_id"], {}, isolated_workspace,
        coverage_target=2, maintainable_frontier=2,
    )

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "coverage_exceeds_maintainable_frontier"
    assert ltm_store.get_maintenance_runtime_state(isolated_workspace)["maintenance_coverage_cursor"] == -1


def test_plan_commit_counts_are_preserved_in_staged_completion(isolated_workspace, monkeypatch):
    monkeypatch.setattr(maintenance_runner.maintenance_observation, "get_overall_health", lambda *_args, **_kwargs: {"status": "ok"})
    plan = {"atomic_operations": [{"operation_type": "create_object", "new_object": {"temp_id": "new", "object_type": "assertion", "title": "Runner Object", "payload": {"schema_version": 1, "body": "runner"}}}]}
    responses = [
        _response([_call("get_object", {"limit": 1})]),
        _response([_call("submit_maintenance_plan", plan)]),
        _response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "A new durable project fact was recorded."})]),
    ]
    for stage in maintenance_runner.MAINTENANCE_STAGES[1:-1]:
        responses.extend([_response([_call("get_object", {"limit": 1})]), _response([_call("complete_maintenance_stage", {"stage": stage, "status": "no_applicable_work", "stage_summary": "No additional high-confidence change."})])])
    responses.append(_response([_call("finish_maintenance", {})]))
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"
    assert result["plan_commit_count"] == 1
    assert result["created_object_count"] == 1


def test_final_check_failure_is_retried_inside_completion_stage(isolated_workspace, monkeypatch):
    health = iter([{"status": "ok"}] * 9 + [{"status": "ok", "derived": {"missing": 1}}, {"status": "ok"}])
    responses = _stage_responses()[:-1] + [_response([_call("finish_maintenance", {}, "first")]), _response([_call("finish_maintenance", {}, "second")])]
    monkeypatch.setattr(maintenance_runner.maintenance_observation, "get_overall_health", lambda *_args, **_kwargs: next(health))
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"
    assert result["final_check_status"] == "passed"


def test_repeated_model_outputs_warn_without_failing_maintenance(isolated_workspace, monkeypatch):
    events, records = [], []
    original = maintenance_runner.record_event
    result_index = 0

    def observation(*_args):
        nonlocal result_index
        result_index += 1
        return {"status": "ok", "items": [{"result_index": result_index}]}

    monkeypatch.setattr(maintenance_runner.maintenance_observation, "run_maintenance_observation_tool", observation)
    monkeypatch.setattr(maintenance_runner, "record_event", lambda context, event_type, **kwargs: (records.append((event_type, kwargs)), original(context, event_type, **kwargs))[1])
    responses = [_response([_call("get_object", {"query": "same"}, f"call-{i}")]) for i in range(8)]
    responses.extend([
        _response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "Repeated observations were reviewed."}, "stage")]),
        _response([_call("finish_maintenance", {}, "finish")]),
    ])
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(
        {"ui_event_sink": events.append},
        root=isolated_workspace,
        stages=("recent_review", "completion"),
    )

    assert result["status"] == "completed", result
    assert sum(event["type"] == "ltm_maintenance_repetition_warning" for event in events) == 3
    diagnostics = [kwargs["summary"] for event, kwargs in records if event == "maintainer.observation_repetition_warning"]
    assert [item["threshold"] for item in diagnostics] == [3, 5, 8]
    assert all(item["pattern_type"] in {"exact", "near_duplicate"} for item in diagnostics)
    assert all(item["tool_span_ids"] for item in diagnostics)
    assert not any(event == "maintainer.observation_repetition_stopped" for event, _ in records)


def test_resume_restarts_stages_with_same_run_id(isolated_workspace, monkeypatch):
    responses = [_response([], content="not a completion") for _ in range(3)]
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))
    failed = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)
    responses = _stage_responses()
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    resumed = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert failed["status"] == "failed"
    assert resumed["status"] == "completed"
    assert resumed["run_id"] == failed["run_id"]


def test_recoverable_model_errors_include_incomplete_read(isolated_workspace, monkeypatch):
    responses = iter([IncompleteRead(b"x" * 11062), *_stage_responses()])

    def model(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(maintenance_runner, "call_chat_completion", model)
    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"


def test_stage_schema_replaces_model_digest_writer():
    names = {item["function"]["name"] for item in maintenance_runner.MAINTAINER_TOOL_SCHEMAS}
    assert "complete_maintenance_stage" in names
    assert "write_maintenance_semantic_digest" not in names


def _title_plan(first_title, second_title):
    return {
        "atomic_operations": [
            {"operation_type": "create_object", "new_object": {
                "temp_id": "too_long", "object_type": "assertion", "title": first_title,
                "payload": {"schema_version": 1, "body": "first"},
            }},
            {"operation_type": "create_object", "new_object": {
                "temp_id": "independent", "object_type": "assertion", "title": second_title,
                "payload": {"schema_version": 1, "body": "second"},
            }},
        ]
    }


def _summary_plan(summary, body="x" * 2500, independent_title="Independent object"):
    return {
        "atomic_operations": [
            {"operation_type": "create_object", "new_object": {
                "temp_id": "summary_target", "object_type": "assertion", "title": "Summary target",
                "summary": summary,
                "payload": {"schema_version": 1, "body": body},
            }},
            {"operation_type": "create_object", "new_object": {
                "temp_id": "independent", "object_type": "assertion", "title": independent_title,
                "payload": {"schema_version": 1, "body": "Independent body"},
            }},
        ]
    }


def test_summary_rejection_reports_payload_size_and_repair_commits(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _summary_plan(None), run_id,
        maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    error = first["errors"][0]
    assert first["status"] == "rejected"
    assert error["code"] == "missing_summary"
    assert error["actual_payload_bytes"] > error["summary_threshold_bytes"]
    assert error["max_payload_bytes"] == ltm_store.SPLIT_THRESHOLD_BYTES
    assert first["repair_targets"] == [{"target": {"temp_id": "summary_target"}, "field": "summary"}]

    repaired = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{
            "target": {"temp_id": "summary_target"},
            "field": "summary",
            "value": "A concise summary.",
        }]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert repaired["status"] == "committed"
    assert not state["drafts"]
    assert repaired["_plan_diagnostics"]["payload_bytes_total"] > 0


def test_summary_repair_failure_partially_commits_independent_group(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _summary_plan(None), run_id,
        maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )
    second = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{
            "target": {"temp_id": "summary_target"},
            "field": "summary",
            "value": None,
        }]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert second["status"] == "partial_committed"
    assert second["committed_groups"]
    assert second["skipped_groups"]
    assert [item["title"] for item in ltm_store.list_objects("assertion", root=isolated_workspace)] == ["Independent object"]


def test_unexpected_summary_is_normalized_and_committed(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _summary_plan("Unnecessary summary", body="short"), run_id,
        maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert first["status"] == "committed"
    assert first["_plan_diagnostics"]["auto_normalized_summaries"][0]["actual_payload_bytes"] <= 2048
    assert not state["drafts"]
    obj = ltm_store.get_object(first["temp_id_map"]["summary_target"], root=isolated_workspace)
    assert obj["summary"] is None


def test_mixed_summary_and_title_errors_are_not_automatically_changed(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    plan = _summary_plan("Unnecessary summary", body="short", independent_title="long title " * 60)
    result = maintenance_runner._execute_tool(
        "submit_maintenance_plan", plan, run_id, maintenance_runner._empty_counters(),
        isolated_workspace, plan_repair_state=state,
    )
    assert result["status"] == "rejected"
    assert {error["code"] for error in result["errors"]} >= {"unexpected_summary", "title_length"}
    assert "auto_normalized_summaries" not in result["_plan_diagnostics"]
    assert ltm_store.list_objects("assertion", root=isolated_workspace) == []


def test_title_rejection_creates_local_repair_draft_and_repair_commits(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    plan = _title_plan("too long " * 60, "Independent object")

    rejected = maintenance_runner._execute_tool(
        "submit_maintenance_plan", plan, run_id, maintenance_runner._empty_counters(), isolated_workspace,
        plan_repair_state=state,
    )

    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "title_length"
    assert rejected["errors"][0]["actual_tokens"] > 50
    assert rejected["errors"][0]["max_tokens"] == 50
    assert rejected["repair_targets"] == [{"target": {"temp_id": "too_long"}, "field": "title"}]

    repaired = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": rejected["draft_id"], "repairs": [{"target": {"temp_id": "too_long"}, "field": "title", "value": "Concise object"}]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace,
        plan_repair_state=state,
    )

    assert repaired["status"] == "committed"
    assert len(ltm_store.list_objects("assertion", root=isolated_workspace)) == 2
    assert not state["drafts"]


def test_second_title_failure_partially_commits_independent_plan_units(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    plan = _title_plan("too long " * 60, "Independent object")

    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", plan, run_id, maintenance_runner._empty_counters(), isolated_workspace,
        plan_repair_state=state,
    )
    second = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{"target": {"temp_id": "too_long"}, "field": "title", "value": "still too long " * 60}]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace,
        plan_repair_state=state,
    )

    assert second["status"] == "partial_committed"
    assert second["committed_groups"]
    assert second["skipped_groups"]
    objects = ltm_store.list_objects("assertion", root=isolated_workspace)
    assert [item["title"] for item in objects] == ["Independent object"]
    assert second["draft_id"]


def test_title_repair_tool_cannot_modify_unmarked_targets(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _title_plan("too long " * 60, "Independent object"),
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    result = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{"target": {"temp_id": "independent"}, "field": "title", "value": "Changed"}]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "repair_target_not_allowed"


def _episode_plan(structured_data, independent_title="Independent assertion"):
    return {
        "atomic_operations": [
            {"operation_type": "create_object", "new_object": {
                "temp_id": "episode_1", "object_type": "episode", "title": "Bounded episode",
                "payload": {"schema_version": 1, "body": "Episode body", "structured_data": structured_data},
            }},
            {"operation_type": "create_object", "new_object": {
                "temp_id": "independent", "object_type": "assertion", "title": independent_title,
                "payload": {"schema_version": 1, "body": "Independent body"},
            }},
        ]
    }


def test_episode_structured_data_repair_uses_generic_repair_tool(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _episode_plan({"time_scope": {}}), run_id,
        maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert first["status"] == "rejected"
    assert first["errors"][0]["code"] == "empty_time_scope"
    assert first["repair_targets"] == [{"target": {"temp_id": "episode_1"}, "field": "payload.structured_data"}]

    repaired = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{
            "target": {"temp_id": "episode_1"},
            "field": "payload.structured_data",
            "value": {"time_scope": {"start": "2026-11-01", "precision": "approximate"}},
        }]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert repaired["status"] == "committed"
    assert len(ltm_store.list_objects("episode", root=isolated_workspace)) == 1


def test_episode_structured_data_failure_partially_commits_independent_group(isolated_workspace):
    state = {"drafts": {}}
    run_id = ltm_store.start_or_resume_maintenance_run(isolated_workspace)["run_id"]
    first = maintenance_runner._execute_tool(
        "submit_maintenance_plan", _episode_plan({"time_scope": {}}), run_id,
        maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )
    second = maintenance_runner._execute_tool(
        "repair_maintenance_plan",
        {"draft_id": first["draft_id"], "repairs": [{
            "target": {"temp_id": "episode_1"},
            "field": "payload.structured_data",
            "value": {"time_scope": {}},
        }]},
        run_id, maintenance_runner._empty_counters(), isolated_workspace, plan_repair_state=state,
    )

    assert second["status"] == "partial_committed"
    assert [item["title"] for item in ltm_store.list_objects("assertion", root=isolated_workspace)] == ["Independent assertion"]
    assert second["draft_id"]
