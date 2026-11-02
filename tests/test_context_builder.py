import json


def _message(role, content):
    return {"role": role, "content": content}


def _messages_with_old_blocks():
    return [
        _message("system", "system prompt"),
        _message("user", "old user raw"),
        _message("assistant", "old assistant raw"),
        _message("user", "middle user raw"),
        _message("assistant", "middle assistant raw"),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]


def _fake_stm_projection():
    return {
        "messages": [
            _message("system", "system prompt"),
            _message("user", "latest user"),
            _message("assistant", "latest assistant"),
        ],
        "budget": 10000,
        "selected_tokens": 10,
        "selected_blocks": [
            {
                "kind": "interaction",
                "messages": [_message("user", "latest user"), _message("assistant", "latest assistant")],
                "start_index": 5,
                "end_index": 6,
                "message_count": 2,
                "token_count": 10,
            }
        ],
        "omitted_blocks": [
            {"kind": "interaction", "start_index": 1, "end_index": 2, "message_count": 2, "token_count": 10},
            {"kind": "interaction", "start_index": 3, "end_index": 4, "message_count": 2, "token_count": 10},
        ],
        "latest_block_tokens": 10,
        "total_blocks": 3,
    }


def _fake_pressure(stm=10000, wm=6000):
    return {
        "budget_source": "dynamic",
        "scores": {"I": 0.1, "S": 0.2, "R": 0.0, "P_context": 0.1},
        "budgets": {
            "stm_min": 10000,
            "stm_max": 50000,
            "stm": stm,
            "wm_min": 10000,
            "wm_max": 50000,
            "wm": wm,
        },
        "signals": {},
    }


def test_context_builder_orders_system_wm_then_stm(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())

    context = context_builder.build_event_stream_context(messages)

    assert context["messages"][0] == _message("system", "system prompt")
    assert context["messages"][1]["role"] == "user"
    assert context["messages"][1]["content"].startswith("<working-memory>")
    assert context["messages"][2:] == [_message("user", "latest user"), _message("assistant", "latest assistant")]
    assert "LTM" not in context["messages"][1]["content"]


def test_context_builder_emits_focus_shift_after_detector_finishes(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    session.ensure_project()
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: {
            "triggered": True,
            "detector_recorded": True,
            "detector_error": "",
            "action": "activated_inactive",
            "target_segment_id": "seg-focus",
            "active_title": "Retriever 状态接入",
            "reason": "new topic",
            "reason_code": "target_inactive",
        },
    )
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())

    events = []
    trajectory_events = []
    monkeypatch.setattr(
        context_builder,
        "record_event",
        lambda _context, event_type, status=None, summary=None, **_kwargs: trajectory_events.append(
            {"event_type": event_type, "status": status, "summary": summary}
        ),
    )
    context_builder.build_event_stream_context(
        _messages_with_old_blocks(),
        runtime_state={"ui_event_sink": events.append},
    )

    assert [event["type"] for event in events[:3]] == [
        "focus_detector_started",
        "focus_detector_finished",
        "focus_shifted",
    ]
    assert events[2]["title"] == "Retriever 状态接入"
    assert events[1]["reason_code"] == "target_inactive"
    assert trajectory_events == [
        {
            "event_type": "focus_detector.completed",
            "status": "ok",
            "summary": {
                "action": "activated_inactive",
                "target_segment_id": "seg-focus",
                "active_title": "Retriever 状态接入",
                "reason": "new topic",
                "reason_code": "target_inactive",
            },
        }
    ]


def test_tool_result_like_context_skips_focus_and_memory_push(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    runtime_state = {
        "_memory_push_enabled": True,
        "turn_processing_class": "tool_result_like",
    }
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("focus detector must be skipped")),
    )
    monkeypatch.setattr(
        context_builder,
        "_memory_push_for_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("memory push must be skipped")),
    )

    context = context_builder.build_event_stream_context(messages, runtime_state=runtime_state)

    assert context["memory_push"] == {"status": "not_run", "injected": False, "metrics": {}}
    assert runtime_state.get("_memory_push_attempted") is None
    assert any(message.get("content", "").startswith("<working-memory>") for message in context["messages"])


def test_preflight_context_skips_focus_and_memory_push_but_builds_memory_context(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    runtime_state = {"_context_build_phase": "preflight", "_memory_push_enabled": True}
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("focus detector must be skipped in preflight")),
    )
    monkeypatch.setattr(
        context_builder,
        "_memory_push_for_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("memory push must be skipped in preflight")),
    )

    context = context_builder.build_event_stream_context(messages, runtime_state=runtime_state)

    assert context["memory_push"] == {"status": "not_run", "injected": False, "metrics": {}}
    assert any(message.get("content", "").startswith("<working-memory>") for message in context["messages"])


def test_context_builder_injects_ltm_maintenance_pressure_notice_at_thresholds(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())
    monkeypatch.setattr(
        context_builder.maintenance_pressure,
        "maintenance_pressure_snapshot",
        lambda *_args, **_kwargs: {
            "level": "mild",
            "blocked": False,
            "pressure": 0.5,
            "unmaintained_slot_count": 50,
            "maintenance_coverage_cursor": -1,
            "latest_event_stream_slot_seq": 49,
        },
    )

    mild = context_builder.build_event_stream_context(messages)

    assert mild["messages"][1]["role"] == "system"
    assert "pressure: 0.50" in mild["messages"][1]["content"]
    assert mild["messages"][1]["content"].count(context_builder.maintenance_pressure.MILD_SLEEP_SIGNAL) == 1
    assert "because many recent experiences and memories" in mild["messages"][1]["content"]

    monkeypatch.setattr(
        context_builder.maintenance_pressure,
        "maintenance_pressure_snapshot",
        lambda *_args, **_kwargs: {
            "level": "strong",
            "blocked": False,
            "pressure": 0.75,
            "unmaintained_slot_count": 75,
            "maintenance_coverage_cursor": -1,
            "latest_event_stream_slot_seq": 74,
        },
    )
    strong = context_builder.build_event_stream_context(messages)
    assert "pressure: 0.75" in strong["messages"][1]["content"]
    assert strong["messages"][1]["content"].count(context_builder.maintenance_pressure.STRONG_SLEEP_SIGNAL) == 3
    assert "A LARGE BACKLOG" in strong["messages"][1]["content"]

    monkeypatch.setattr(
        context_builder.maintenance_pressure,
        "maintenance_pressure_snapshot",
        lambda *_args, **_kwargs: {
            "level": "urgent",
            "blocked": False,
            "pressure": 0.9,
            "unmaintained_slot_count": 90,
            "maintenance_coverage_cursor": -1,
            "latest_event_stream_slot_seq": 89,
        },
    )
    urgent = context_builder.build_event_stream_context(messages)
    assert "pressure: 0.90" in urgent["messages"][1]["content"]
    assert urgent["messages"][1]["content"].count(context_builder.maintenance_pressure.URGENT_SLEEP_SIGNAL) == 10
    assert "SIGNIFICANTLY INTERFERING WITH YOUR ABILITY TO USE MEMORY RELIABLY" in urgent["messages"][1]["content"]


def test_context_builder_does_not_inject_ltm_pressure_below_threshold(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())
    monkeypatch.setattr(
        context_builder.maintenance_pressure,
        "maintenance_pressure_snapshot",
        lambda *_args, **_kwargs: {
            "level": "none",
            "blocked": False,
            "pressure": 0.49,
            "unmaintained_slot_count": 49,
            "maintenance_coverage_cursor": -1,
            "latest_event_stream_slot_seq": 48,
        },
    )

    context = context_builder.build_event_stream_context(messages)

    assert all("<ltm-maintenance-pressure>" not in message["content"] for message in context["messages"])


def test_context_builder_stages_wm_ingress_watermark(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    messages = _messages_with_old_blocks()
    runtime_state = {}
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())

    context_builder.build_event_stream_context(messages, runtime_state=runtime_state)

    status = wm_ingress.wm_ingress_watermark_status(runtime_state)
    assert status["persisted_watermark"] == -1
    assert status["staged_watermark"] == 4
    assert status["effective_watermark"] == 4


def test_wm_ingress_watermark_commit_persists_staged_value(isolated_workspace):
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    runtime_state = {}
    session.ensure_project()
    session.save_last_session([_message("system", "system")])

    wm_ingress.stage_wm_ingress_watermark(runtime_state, 8)
    result = wm_ingress.commit_staged_wm_ingress_watermark(runtime_state)
    status = wm_ingress.wm_ingress_watermark_status({})

    assert result["updated"] is True
    assert status["persisted_watermark"] == 8
    assert status["effective_watermark"] == 8


def test_post_answer_message_can_create_and_persist_new_wm_ingress(isolated_workspace):
    import memory_engine.stm_projection as stm_projection
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    before_answer = [
        _message("system", "system prompt"),
        _message("user", "old evidence " * 40),
        _message("assistant", "old response " * 40),
        _message("user", "latest question"),
    ]
    initial = stm_projection.build_stm_projection(before_answer, budget=100000)
    budget = initial["selected_tokens"]
    assert not stm_projection.build_stm_projection(before_answer, budget=budget)["omitted_blocks"]

    after_answer = [*before_answer, _message("assistant", "final response " * 30)]
    post_answer = stm_projection.build_stm_projection(after_answer, budget=budget)
    assert post_answer["omitted_blocks"]
    assert post_answer["latest_block_tokens"] <= budget

    session.ensure_project()
    session.save_last_session(after_answer)
    runtime_state = {"context_pressure": {"budgets": {"stm": budget}}}

    staged = wm_ingress.stage_wm_ingress_from_messages(after_answer, runtime_state)
    committed = wm_ingress.commit_staged_wm_ingress_watermark(runtime_state)

    assert staged["omitted_block_count"] == 1
    assert staged["staged_watermark"] == 2
    assert committed["persisted_watermark"] == 2
    assert wm_ingress.wm_ingress_watermark_status({})["persisted_watermark"] == 2


def test_context_builder_renders_raw_and_sum_timeline(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.event_stream as event_stream
    from persistence import session

    messages = _messages_with_old_blocks()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    event_stream.create_sum_event(
        2,
        {
            "source_role": "assistant",
            "summary": "summarized old assistant",
            "reason": "test",
            "original_token_count": 1000,
            "summary_token_count": 10,
            "created_by": "test",
        },
    )
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())

    wm = context_builder.build_event_stream_context(messages)["messages"][1]["content"]

    assert "[1] user: old user raw" in wm
    assert "[2] sema summary: summarized old assistant" in wm
    assert "[3] user: middle user raw" in wm
    assert "[4] sema: middle assistant raw" in wm
    assert "event_id" not in wm
    assert "original_event_id" not in wm
    assert "created_at" not in wm
    assert "content_hash" not in wm
    assert "artifact" not in wm


def test_context_builder_keeps_latest_wm_suffix_under_budget(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = [
        _message("system", "system prompt"),
        _message("user", "old " * 70),
        _message("assistant", "middle " * 20),
        _message("user", "latest user"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure(wm=80))
    monkeypatch.setattr(
        context_builder,
        "build_stm_projection",
        lambda _messages, budget=10000, **_kwargs: {
            "messages": [_message("system", "system prompt"), _message("user", "latest user")],
            "budget": 10000,
            "selected_tokens": 10,
            "selected_blocks": [{"kind": "interaction", "messages": [_message("user", "latest user")], "start_index": 3, "end_index": 3, "message_count": 1, "token_count": 10}],
            "omitted_blocks": [{"kind": "interaction", "start_index": 1, "end_index": 2, "message_count": 2, "token_count": 5000}],
            "latest_block_tokens": 10,
            "total_blocks": 2,
        },
    )

    context = context_builder.build_event_stream_context(messages)
    wm = context["messages"][1]["content"]

    assert "old old old" not in wm
    assert "[2] sema:" in wm
    assert context["working_memory"]["skipped_count"] == 1


def test_context_builder_rejects_single_wm_slot_over_budget(isolated_workspace, monkeypatch):
    import pytest
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = [
        _message("system", "system prompt"),
        _message("user", "测试" * 12000),
        _message("user", "latest user"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure(wm=6000))
    monkeypatch.setattr(
        context_builder,
        "build_stm_projection",
        lambda _messages, budget=10000, **_kwargs: {
            "messages": [_message("system", "system prompt"), _message("user", "latest user")],
            "budget": 10000,
            "selected_tokens": 10,
            "selected_blocks": [{"kind": "interaction", "messages": [_message("user", "latest user")], "start_index": 2, "end_index": 2, "message_count": 1, "token_count": 10}],
            "omitted_blocks": [{"kind": "interaction", "start_index": 1, "end_index": 1, "message_count": 1, "token_count": 12000}],
            "latest_block_tokens": 10,
            "total_blocks": 2,
        },
    )

    with pytest.raises(RuntimeError, match="Context Builder WM slot exceeds budget"):
        context_builder.build_event_stream_context(messages)


def test_context_status_is_metadata_only(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    from persistence import session

    messages = _messages_with_old_blocks()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _fake_pressure())
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _fake_stm_projection())

    status = context_builder.context_status(messages)
    rendered = context_builder.format_context_status(status)

    assert "working_memory" in rendered
    assert "old user raw" not in rendered
    assert "latest assistant" not in rendered
    assert status["wm_selected_count"] == 4
    assert status["budget_source"] == "dynamic"
    assert status["pressure_scores"]["S"] == 0.2


def test_context_status_uses_effective_watermark_without_mutating_runtime_or_budget_state(isolated_workspace, monkeypatch):
    import copy
    import sqlite3
    import memory_engine.context_builder as context_builder
    import memory_engine.event_stream as event_stream
    import memory_engine.state as memory_state
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    messages = [
        _message("system", "system prompt"),
        _message("user", "locked old user " * 20),
        _message("assistant", "locked old assistant " * 20),
        _message("user", "eligible user " * 20),
        _message("assistant", "eligible assistant " * 20),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    runtime_state = {
        "context_pressure": _fake_pressure(stm=10000, wm=10000),
        "wm_ingress_watermark_slot_seq": 2,
    }
    before_runtime = copy.deepcopy(runtime_state)
    before_watermark = wm_ingress.wm_ingress_watermark_status(runtime_state)

    status = context_builder.context_status(messages, runtime_state=runtime_state)
    state = memory_state.build_memory_engine_state(messages=messages, runtime_state=runtime_state)

    assert status["wm_ingress_watermark"] == 2
    assert status["watermark_locked_omitted_block_count"] == 1
    assert status["stm_selected_block_count"] == 2
    assert state["wm_ingress"]["runtime_watermark"] == 2
    assert state["wm_ingress"]["effective_watermark"] == 2
    assert state["stm"]["selected_block_count"] == 2
    assert runtime_state == before_runtime
    assert wm_ingress.wm_ingress_watermark_status(runtime_state) == before_watermark
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('context_budget_state', 'working_memory_state', 'working_memory_slot_state')"
            ).fetchall()
        }
    assert tables == set()
