import json

from memory_engine import event_stream
from memory_engine.ltm import ltm_store, maintenance_digest_cache, maintenance_runner


def _call(name, args, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _response(calls):
    return {"choices": [{"message": {"content": "", "tool_calls": calls}, "finish_reason": "tool_calls"}]}


def test_staged_maintenance_runs_all_semantic_stages_and_wakes(isolated_workspace, monkeypatch):
    responses = []
    stages = ("recent_review", "global_duplicate_review", "abstraction", "episode_review")
    for index, stage in enumerate(stages):
        observation = "read_event_slots" if stage in {"recent_review", "episode_review"} else "get_object"
        responses.append(_response([_call(observation, {"limit": 1}, f"observe-{index}")]))
        responses.append(_response([_call("complete_maintenance_stage", {
            "stage": stage,
            "status": "no_applicable_work",
            "stage_summary": f"Observed the {stage} memory material; no high-confidence change was needed.",
        }, f"stage-{index}")]))
    responses.append(_response([_call("finish_maintenance", {}, "finish")]))
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))
    wakes = []

    result = maintenance_runner.run_ltm_maintenance({"wake_callback": lambda *args, **kwargs: wakes.append((args, kwargs)) or True}, root=isolated_workspace)

    assert result["status"] == "completed", result
    assert result["final_check_status"] == "passed"
    assert [stage for stage in stages if f"[{stage}]" in result["maintenance_summary"]] == list(stages)
    assert len(wakes) == 1
    assert wakes[0][0][:2] == ("maintainer", "maintenance_completed")
    assert wakes[0][1]["processing_class"] == "tool_result_like"
    assert "maintenance_summary" in wakes[0][0][2]
    assert maintenance_digest_cache.load_latest_staged_summary(isolated_workspace) is not None


def test_maintenance_completion_uses_task_resume_wake_when_continuation_exists():
    wakes = []
    runtime_state = {
        "wake_callback": lambda *args, **kwargs: wakes.append((args, kwargs)) or True,
        "maintenance_continuation": {
            "status": "running",
            "continuation_id": "continuation-1",
            "wake_prompt": "Resume the documentation study at section 8.",
            "parent_request_id": "request-42",
        },
    }

    assert maintenance_runner._enqueue_maintenance_wake(runtime_state, "maintenance-run-1", "summary", {"status": "written"}) is True

    assert wakes[0][0][:2] == ("maintainer", "maintenance_task_resumed")
    assert wakes[0][0][2]["wake_prompt"] == "Resume the documentation study at section 8."
    assert "maintenance_summary" not in wakes[0][0][2]
    assert wakes[0][0][2]["maintenance_summary_status"] == "written"
    assert wakes[0][0][2]["parent_request_id"] == "request-42"
    assert wakes[0][1]["wake_id"] == "wake_maintenance_resume_continuation-1_maintenance-run-1"


def test_maintenance_wake_queue_rejection_is_not_reported_as_queued(monkeypatch):
    recorded = []

    def capture_event(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr(maintenance_runner, "record_event", capture_event)
    runtime_state = {
        "wake_enqueue_status_callback": lambda *_args, **_kwargs: "rejected_duplicate_session",
        "_ltm_maintenance_trajectory_context": {"trajectory_id": "trajectory-1"},
    }

    assert maintenance_runner._enqueue_maintenance_wake(runtime_state, "run-1", "summary", {"status": "written"}) is False
    event_types = [entry[0][1] for entry in recorded]
    assert "maintenance.wake_failed" in event_types
    assert "maintenance.wake_queued" not in event_types


def test_maintenance_wake_already_delivered_is_reported_as_accepted(monkeypatch):
    recorded = []
    monkeypatch.setattr(maintenance_runner, "record_event", lambda *args, **kwargs: recorded.append((args, kwargs)))
    runtime_state = {
        "wake_enqueue_status_callback": lambda *_args, **_kwargs: "already_delivered",
        "_ltm_maintenance_trajectory_context": {"trajectory_id": "trajectory-1"},
    }

    assert maintenance_runner._enqueue_maintenance_wake(runtime_state, "run-1", "summary", {"status": "written"}) is True
    assert [event[0][1] for event in recorded] == ["maintenance.wake_queued"]
    assert recorded[0][1]["summary"]["queue_status"] == "already_delivered"


def test_staged_maintenance_requires_relevant_evidence(isolated_workspace, monkeypatch):
    responses = [
        _response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "No change."}, "stage")]),
        _response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "No change."}, "stage2")]),
        _response([_call("complete_maintenance_stage", {"stage": "recent_review", "status": "completed", "stage_summary": "No change."}, "stage3")]),
    ]
    monkeypatch.setattr(maintenance_runner, "call_chat_completion", lambda *_args, **_kwargs: responses.pop(0))

    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "failed"
    assert "requires relevant observation" in result["failure_reason"]


def test_old_unstructured_summary_is_not_injected_into_staged_context(isolated_workspace, monkeypatch):
    maintenance_digest_cache.write_pending_digest("old", "old unstructured summary", root=isolated_workspace)
    maintenance_digest_cache.persist_pending_digest("old", isolated_workspace)
    captured = []
    responses = []
    for index, stage in enumerate(("recent_review", "global_duplicate_review", "abstraction", "episode_review")):
        responses.extend([
            _response([_call("get_object", {"limit": 1}, f"o{index}")]),
            _response([_call("complete_maintenance_stage", {"stage": stage, "status": "completed", "stage_summary": "Useful memory facts."}, f"s{index}")]),
        ])
    responses.append(_response([_call("finish_maintenance", {}, "finish")]))

    def model(messages, **_kwargs):
        captured.append(messages)
        return responses.pop(0)

    monkeypatch.setattr(maintenance_runner, "call_chat_completion", model)
    result = maintenance_runner.run_ltm_maintenance(root=isolated_workspace)

    assert result["status"] == "completed"
    assert all("old unstructured summary" not in json.dumps(messages, ensure_ascii=False) for messages in captured)


def test_staged_schema_does_not_expose_old_digest_writer():
    names = {item["function"]["name"] for item in maintenance_runner.MAINTAINER_TOOL_SCHEMAS}
    assert "complete_maintenance_stage" in names
    assert "write_maintenance_semantic_digest" not in names
