import json

from persistence import trajectory


def _message(role, content):
    return {"role": role, "content": content}


def _projection():
    return {
        "messages": [_message("system", "system"), _message("user", "latest")],
        "budget": 10000,
        "selected_tokens": 2,
        "selected_blocks": [
            {"kind": "interaction", "messages": [_message("user", "latest")], "start_index": 1, "end_index": 1}
        ],
        "omitted_blocks": [],
        "wm_ingress_watermark": -1,
    }


def _pressure():
    return {
        "budgets": {"stm": 10000, "wm": 10000},
        "scores": {},
        "signals": {},
    }


def _wm_projection():
    return {
        "content": "<working-memory>old context</working-memory>",
        "budget": 10000,
        "selected_records": [],
        "selected_tokens": 2,
        "candidate_count": 0,
        "skipped_count": 0,
    }


def test_main_agent_context_timing_records_ordered_metadata_spans(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from runtime import setup_capabilities

    parent = trajectory.start_trajectory("main_agent")
    runtime_state = {
        "trajectory_context": parent,
        "_main_agent_context_timing_enabled": True,
        "_memory_push_enabled": False,
    }
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: {"action": "no_change", "detector_error": ""},
    )
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda *_args, **_kwargs: _projection())
    monkeypatch.setattr(context_builder.wm_ingress, "effective_wm_ingress_watermark", lambda *_args, **_kwargs: -1)
    monkeypatch.setattr(context_builder.wm_ingress, "stage_wm_ingress_from_projection", lambda *_args, **_kwargs: -1)
    monkeypatch.setattr(context_builder, "_build_working_memory_projection", lambda *_args, **_kwargs: _wm_projection())
    monkeypatch.setattr(setup_capabilities, "observe_window", lambda *_args, **_kwargs: None)

    context_builder.build_event_stream_context(
        [_message("system", "system"), _message("user", "latest")],
        runtime_state=runtime_state,
    )

    spans = trajectory.get_trajectory(parent["trajectory_id"])["spans"]
    names = [span["name"] for span in spans]
    assert names[0] == "Main Agent context build"
    assert names[1:] == [
        "focus_detector",
        "context_pressure",
        "stm_projection",
        "wm_ingress",
        "wm_projection",
        "observe_window",
        "ltm_result_processing",
        "context_assembly",
        "context_token_count",
    ]
    assert all(span["status"] == "completed" for span in spans)
    assert all(
        json.loads(span["metadata_json"]).get("temporary_diagnostic") == "main_agent_context_timing_v1"
        for span in spans
    )
    assert "latest" not in str(spans)


def test_background_or_unmarked_context_does_not_create_main_agent_timing_spans(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from runtime import setup_capabilities

    parent = trajectory.start_trajectory("ltm_maintainer")
    runtime_state = {"trajectory_context": parent, "_memory_push_enabled": False}
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: {"action": "no_change", "detector_error": ""},
    )
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda *_args, **_kwargs: _projection())
    monkeypatch.setattr(context_builder.wm_ingress, "effective_wm_ingress_watermark", lambda *_args, **_kwargs: -1)
    monkeypatch.setattr(context_builder.wm_ingress, "stage_wm_ingress_from_projection", lambda *_args, **_kwargs: -1)
    monkeypatch.setattr(context_builder, "_build_working_memory_projection", lambda *_args, **_kwargs: _wm_projection())
    monkeypatch.setattr(setup_capabilities, "observe_window", lambda *_args, **_kwargs: None)

    context_builder.build_event_stream_context([_message("user", "latest")], runtime_state=runtime_state)

    assert trajectory.get_trajectory(parent["trajectory_id"])["spans"] == []


def test_provider_preparation_is_recorded_without_changing_model_result(isolated_workspace, monkeypatch):
    from runtime import agent

    parent = trajectory.start_trajectory("main_agent")
    runtime_state = {
        "trajectory_context": parent,
        "_main_agent_context_timing_enabled": True,
    }
    monkeypatch.setattr(agent, "messages_for_model", lambda *_args, **_kwargs: [_message("user", "prepared")])
    monkeypatch.setattr(agent, "tool_schemas_for_model", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        agent,
        "call_chat_completion",
        lambda *_args, **_kwargs: {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )

    result = agent.call_model_with_tools([], runtime_state=runtime_state)

    assert result["content"] == "ok"
    spans = trajectory.get_trajectory(parent["trajectory_id"])["spans"]
    names = [span["name"] for span in spans]
    assert names[:4] == [
        "system_prompt_refresh",
        "runtime_directives",
        "Main Agent provider preparation",
        "tool_schema_build",
    ]
    assert names[4:] == ["tool calling"]
    assert all(span["status"] == "completed" for span in spans[:4])
