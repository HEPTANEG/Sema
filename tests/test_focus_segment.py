import json

import memory_engine.event_stream as event_stream
from memory_engine.focus import context as focus_context
from memory_engine.focus import detector as focus_detector
from memory_engine.focus import ingest as focus_ingest
from memory_engine.focus import maintainer as focus_maintainer
from memory_engine.focus import store as focus_store
from memory_engine.focus.common import FULL_MAINTAINER_MAX_TOOL_ROUNDS, NEW_FOCUS_PENDING_SUMMARY
from shared.prompt_loader import load_prompt


def _message(role, content):
    return {"role": role, "content": content}


def _messages():
    return [
        _message("system", "system"),
        _message("user", "old user"),
        _message("assistant", "verbose original assistant body"),
        _message("user", "middle user"),
        _message("assistant", "middle assistant"),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]


def _pressure():
    return {"budgets": {"stm": 10000}}


def _projection_with_omitted(start, end):
    return {
        "messages": [_message("system", "system"), _message("user", "latest user")],
        "budget": 10000,
        "selected_tokens": 10,
        "selected_units": [
            {
                "kind": "interaction",
                "messages": [_message("user", "latest user")],
                "start_index": 5,
                "end_index": 6,
                "message_count": 2,
                "token_count": 10,
            }
        ],
        "omitted_units": [
            {
                "kind": "interaction",
                "start_index": start,
                "end_index": end,
                "message_count": end - start + 1,
                "token_count": 1000,
            }
        ],
    }


def _projection_without_omitted():
    return {
        "messages": _messages(),
        "budget": 10000,
        "selected_tokens": 10,
        "selected_units": [
            {
                "kind": "interaction",
                "messages": _messages()[1:],
                "start_index": 1,
                "end_index": 6,
                "message_count": 6,
                "token_count": 10,
            }
        ],
        "omitted_units": [],
    }


def _detector_reply(target_kind="active", target_segment_id=""):
    if target_kind == "active":
        return "NO_SHIFT"
    decision = {
        "target_kind": target_kind,
    }
    if target_kind == "inactive":
        decision["target_segment_id"] = target_segment_id
    if target_kind == "new":
        decision["proposed_title"] = "Project Planning"
    return json.dumps(
        decision
    )


def _detector_reply_from_prompt(prompt, target_kind="active"):
    payload = json.loads(prompt[1]["content"])
    segments = payload.get("segments") or []
    target_id = ""
    if target_kind in {"active", "inactive"}:
        target = next((segment for segment in segments if segment.get("status") == target_kind), None)
        target_id = target["segment_id"] if target else ""
    return _detector_reply(target_kind, target_id)


def _segment_ingest_reply_from_prompt(prompt, confidence=0.9, action="assign", title=None, summary=None, mark_resolved=False, segment_index=0):
    payload = json.loads(prompt[1]["content"])
    segments = payload.get("segments") or []
    target_id = segments[segment_index]["segment_id"] if segments and action == "assign" else ""
    return json.dumps(
        {
            "action": action,
            "target_segment_id": target_id,
            "confidence": confidence,
            "reason": "ingest decision",
            "updates": {
                "title": title,
                "summary": summary,
                "mark_resolved": mark_resolved,
            },
        }
    )


def test_focus_detector_prompt_enforces_title_quality_without_domain_examples():
    import memory_engine.focus as focus

    prompt = load_prompt("focus_detector")

    assert "specific, concise, distinguishable" in prompt
    assert "4-20 visible characters" in prompt
    assert "2-6 English words" in prompt
    assert "reasonable semantic parent" in prompt
    assert "NO_SHIFT" in prompt
    assert '"confidence"' not in prompt
    assert '"reason"' not in prompt
    assert "new topic" in prompt
    assert "current topic" in prompt
    assert "general discussion" in prompt
    for domain_example in ["AI", "F1", "UFO", "music", "音乐", "外星人"]:
        assert domain_example not in prompt


def test_segment_ingest_prompt_treats_detector_boundary_as_a_hint():
    import memory_engine.focus as focus

    prompt = load_prompt("segment_ingest")

    assert "new_focus_start_slot" in prompt
    assert "routing hint, not a forced assignment" in prompt
    assert "independently validate semantic fit" in prompt


def test_full_maintainer_prompt_preserves_pending_detector_hints():
    import memory_engine.focus as focus

    prompt = load_prompt("full_segment_maintainer")

    assert "pending_ingest_hint_count" in prompt
    assert "Do not delete or mark resolved" in prompt
    assert "consistency_report" in prompt
    assert "bounded anomaly index" in prompt


def _enable_wm(monkeypatch, focus, start=1, end=2):
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(start, end))


def _seed_active_segment(monkeypatch, focus, messages):
    _enable_wm(monkeypatch, focus)
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)
    focus.run_focus_segment_maintenance(messages)


def _record_unassigned(connection, focus, start, end, confidence=0.4, reason="uncertain"):
    focus_store._record_assignment(
        connection,
        {"slot_range": {"start": start, "end": end}, "slot_seqs": list(range(start, end + 1))},
        {"confidence": confidence, "reason": reason},
        {"assigned": False, "target_segment_id": "", "reason": "unassigned"},
    )
    return next(
        assignment
        for assignment in focus_store._load_assignments(connection, status="unassigned")
        if assignment["slot_start"] == start and assignment["slot_end"] == end
    )


def test_focus_status_is_empty_and_does_not_bootstrap_without_event_stream(isolated_workspace):
    import memory_engine.focus as focus
    import memory_engine.event_stream as event_stream
    from persistence import session

    session.ensure_project()

    status = focus.focus_segments_status()

    assert status["active"] is False
    assert status["reason"] == "no_event_stream"
    assert status["segment_count"] == 0
    assert not event_stream.event_stream_db_path().exists()


def test_focus_segments_bootstraps_fixed_active_segment_only_when_wm_exists(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_without_omitted())

    empty_result = focus.run_focus_segment_maintenance(_messages())
    empty_status = focus.focus_segments_status()

    assert empty_result["bootstrap_created"] is False
    assert empty_status["active"] is False
    assert empty_status["reason"] == "no_focus_segments"

    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)
    result = focus.run_focus_segment_maintenance(_messages())
    status = focus.focus_segments_status()

    assert result["bootstrap_created"] is True
    assert result["assigned_count"] == 1
    assert status["counts"]["active"] == 1
    assert status["segments"][0]["title"] == "F1"
    assert status["segments"][0]["summary"] == "Primary focus for Phase 1 Focus Segment testing."
    assert status["segments"][0]["slot_ranges"] == [{"start": 1, "end": 2}]


def test_prepare_focus_can_create_empty_segment_when_wm_is_empty(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_without_omitted())
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()

    assert result["triggered"] is True
    assert result["action"] == "created_active"
    assert status["segment_count"] == 1
    assert status["segments"][0]["title"] == "Project Planning"
    assert status["segments"][0]["slot_ranges"] == []


def test_prepare_focus_can_create_first_segment_when_no_segment_exists(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()

    assert result["triggered"] is True
    assert result["action"] == "created_active"
    assert status["segment_count"] == 1
    assert status["segments"][0]["title"] == "Project Planning"
    assert status["segments"][0]["slot_ranges"] == []


def test_focus_detector_records_selected_active_segment_in_runtime_state(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    runtime_state = {}
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current work", "summary", "active")
        connection.commit()
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda prompt: _detector_reply_from_prompt(prompt, "active"))

    result = focus.run_focus_detector_before_context(messages, runtime_state)

    assert result["target_segment_id"] == segment_id
    assert runtime_state["_focus_detector_active_segment_id"] == segment_id


def test_context_build_on_fresh_project_allows_focus_detector_bootstrap(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"stm": 10000, "wm": 6000}, "scores": {}, "signals": {}, "budget_source": "dynamic"})
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_without_omitted())
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    context = context_builder.build_event_stream_context(messages)
    status = focus.focus_segments_status()

    assert context["messages"][0] == messages[0]
    assert context["messages"][1]["role"] == "user"
    assert "Project Planning: New focus awaiting working-memory ingress." in context["messages"][1]["content"]
    assert context["messages"][2:] == messages[1:]
    assert status["segment_count"] == 1
    assert status["segments"][0]["title"] == "Project Planning"
    assert status["segments"][0]["slot_ranges"] == []


def test_segment_ingest_merges_adjacent_and_appends_gap(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)

    focus.run_focus_segment_maintenance(messages)
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(3, 4))
    focus.run_focus_segment_maintenance(messages)
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(6, 6))
    focus.run_focus_segment_maintenance(messages)

    ranges = focus.focus_segments_status()["segments"][0]["slot_ranges"]

    assert ranges == [{"start": 1, "end": 4}, {"start": 6, "end": 6}]


def test_segment_ingest_can_assign_to_inactive_segment(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _seed_active_segment(monkeypatch, focus, messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        inactive_id = focus_store._create_segment(connection, "Inactive work", "Inactive summary", "inactive")
        connection.commit()
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(3, 4))
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, segment_index=1),
    )

    result = focus.run_focus_segment_maintenance(messages)
    segments = focus.focus_segments_status()["segments"]
    inactive = next(segment for segment in segments if segment["segment_id"] == inactive_id)

    assert result["assigned_count"] == 1
    assert inactive["slot_ranges"] == [{"start": 3, "end": 4}]


def test_segment_ingest_low_confidence_leaves_block_uncovered(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, confidence=0.59),
    )

    result = focus.run_focus_segment_maintenance(messages)
    status = focus.focus_segments_status()

    assert result["assigned_count"] == 0
    assert result["unassigned_count"] == 1
    assert status["segments"][0]["slot_ranges"] == []


def test_segment_ingest_payload_excludes_current_focus_signals():
    import memory_engine.focus as focus

    payload = focus_ingest._segment_ingest_payload(
        {
            "slot_range": {"start": 1, "end": 2},
            "slot_seqs": [1, 2],
            "token_count": 100,
            "records": [],
        },
        [
            {
                "segment_id": "seg_a",
                "title": "Design A",
                "summary": "Discussion about Design A.",
                "status": "active",
            }
        ],
    )

    assert "latest_detector_decision" not in payload
    assert "current_active_segment_id" not in payload
    assert payload["segments"][0]["title"] == "Design A"


def test_segment_ingest_files_by_user_intent_when_assistant_transitions_elsewhere(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = [
        _message("system", "system"),
        _message("user", "Design A is done for now."),
        _message("assistant", "Understood. Let's continue Design B next."),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        design_a_id = focus_store._create_segment(connection, "Design A", "Planning and closure for Design A.", "inactive")
        focus_store._create_segment(connection, "Design B", "Separate follow-up topic.", "active")
        connection.commit()

    def fake_ingest(prompt):
        payload = json.loads(prompt[1]["content"])
        assert "latest_detector_decision" not in payload
        assert "current_active_segment_id" not in payload
        assert "Design A is done for now." in json.dumps(payload["ingress_block"], ensure_ascii=False)
        assert "Let's continue Design B next." in json.dumps(payload["ingress_block"], ensure_ascii=False)
        return json.dumps(
            {
                "action": "assign",
                "target_segment_id": design_a_id,
                "confidence": 0.9,
                "reason": "User intent points to Design A; assistant transition is supporting context only.",
                "updates": {
                    "title": None,
                    "summary": "Design A is done for now.",
                    "mark_resolved": True,
                },
            }
        )

    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", fake_ingest)

    result = focus.run_focus_segment_maintenance(messages)
    segments = focus.focus_segments_status()["segments"]
    design_a = next(segment for segment in segments if segment["segment_id"] == design_a_id)

    assert result["assigned_count"] == 1
    assert result["resolved_count"] == 1
    assert design_a["status"] == "resolved"
    assert design_a["slot_ranges"] == [{"start": 1, "end": 2}]


def test_segment_ingest_updates_title_summary_and_marks_resolved(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(
            prompt,
            title="Resolved work",
            summary="Updated resolved summary",
            mark_resolved=True,
        ),
    )

    result = focus.run_focus_segment_maintenance(messages)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["assigned_count"] == 1
    assert result["resolved_count"] == 1
    assert segment["title"] == "Resolved work"
    assert segment["summary"] == "Updated resolved summary"
    assert segment["status"] == "resolved"


def test_segment_ingest_refreshes_an_overly_broad_title_from_ingress_topic(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = [
        _message("system", "system"),
        _message("user", "Let's discuss films, starting with directors and genres."),
        _message("assistant", "We can compare directors and genre conventions."),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Casual chat", "General conversation.", "active")
        connection.commit()
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda _prompt: json.dumps(
            {
                "action": "assign",
                "target_segment_id": segment_id,
                "confidence": 0.9,
                "reason": "The segment has converged on film discussion.",
                "updates": {"title": "Film discussion", "summary": "Discussion of films, directors, and genres.", "mark_resolved": False},
            }
        ),
    )

    result = focus.run_focus_segment_maintenance(messages)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["assigned_count"] == 1
    assert segment["title"] == "Film discussion"
    assert segment["summary"] == "Discussion of films, directors, and genres."


def test_segment_ingest_discards_invalid_title_but_keeps_assignment_and_summary(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, title="discussion", summary="A more precise summary."),
    )

    result = focus.run_focus_segment_maintenance(messages)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["assigned_count"] == 1
    assert segment["title"] == "F1"
    assert segment["summary"] == "A more precise summary."


def test_segment_ingest_requires_stm_evidence_before_marking_resolved(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    calls = []

    def fake_ingest(prompt):
        calls.append(prompt)
        mark_resolved = len(prompt) == 2
        return _segment_ingest_reply_from_prompt(prompt, mark_resolved=mark_resolved)

    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", fake_ingest)

    result = focus.run_focus_segment_maintenance(messages)
    segment = focus.focus_segments_status()["segments"][0]

    assert len(calls) == 2
    assert result["resolved_evidence_checked_count"] == 1
    assert result["resolved_count"] == 0
    assert segment["status"] == "active"
    assert "get_stm_evidence" in calls[1][3]["content"]


def test_segment_ingest_resolved_gate_uses_evidence_module(isolated_workspace, monkeypatch):
    import memory_engine.evidence as evidence
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    calls = []

    def fake_stm_evidence(messages=None, runtime_state=None, max_chars=2000):
        calls.append({"messages": messages, "runtime_state": runtime_state, "max_chars": max_chars})
        return {
            "ok": True,
            "query": {"type": "stm_evidence", "budget": 10000},
            "selected_tokens": 10,
            "selected_unit_count": 1,
            "content_preview": "recent evidence",
            "truncated": False,
            "errors": [],
        }

    monkeypatch.setattr(evidence, "get_stm_evidence", fake_stm_evidence)
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, mark_resolved=len(prompt) == 2),
    )

    focus.run_focus_segment_maintenance(messages)

    assert calls
    assert calls[0]["messages"] is messages


def test_full_maintainer_rejects_mark_resolved_without_stm_evidence(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closable work", "summary", "inactive")
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "mark_resolved", "segment_id": segment_id, "reason": "closed"}],
        )
        segment = focus_store._segment_by_id(connection, segment_id)

    assert simulation["applied_actions"] == []
    assert simulation["rejected_actions"][0]["action"] == "mark_resolved"
    assert "get_stm_evidence" in simulation["rejected_actions"][0]["error"]
    assert segment["status"] == "inactive"


def test_full_maintainer_rejects_mark_resolved_without_target_inspect(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closable work", "summary", "inactive")
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "mark_resolved", "segment_id": segment_id, "reason": "stm evidence confirms closure"}],
            allow_mark_resolved=True,
        )
        segment = focus_store._segment_by_id(connection, segment_id)

    assert simulation["applied_actions"] == []
    assert simulation["rejected_actions"][0]["action"] == "mark_resolved"
    assert "inspect_segment" in simulation["rejected_actions"][0]["error"]
    assert segment["status"] == "inactive"


def test_full_maintainer_allows_mark_resolved_after_stm_evidence_and_target_inspect(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closable work", "summary", "inactive")
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "mark_resolved", "segment_id": segment_id, "reason": "stm evidence confirms closure"}],
            allow_mark_resolved=True,
            mark_resolved_segment_ids=[segment_id],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        segment = focus_store._segment_by_id(connection, segment_id)

    assert simulation["rejected_actions"] == []
    assert segment["status"] == "resolved"


def test_full_maintainer_records_target_inspect_for_mark_resolved(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closable work", "summary", "inactive")
        connection.commit()
    replies = iter(
        [
            json.dumps({"tool": "inspect_segment", "args": {"segment_id": segment_id}}),
            json.dumps({"tool": "get_stm_evidence", "args": {}}),
            json.dumps({"repair_plan": [{"action": "mark_resolved", "segment_id": segment_id, "reason": "closed"}]}),
        ]
    )
    monkeypatch.setattr(focus_maintainer, "_call_full_maintainer_json", lambda _prompt: next(replies))

    result = focus.run_full_segment_maintenance(messages, force=True)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["updated"] is True
    assert result["applied_action_count"] == 1
    assert result["rejected_action_count"] == 0
    assert segment["status"] == "resolved"


def test_full_maintainer_does_not_authorize_resolution_after_failed_stm_evidence(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Closable work", "summary", "inactive")
        connection.commit()
    replies = iter(
        [
            json.dumps({"tool": "inspect_segment", "args": {"segment_id": segment_id}}),
            json.dumps({"tool": "get_stm_evidence", "args": {}}),
            json.dumps({"repair_plan": [{"action": "mark_resolved", "segment_id": segment_id, "reason": "closed"}]}),
        ]
    )
    monkeypatch.setattr(focus_maintainer, "_call_full_maintainer_json", lambda _prompt: next(replies))
    monkeypatch.setattr(focus_maintainer, "_stm_evidence", lambda *_args, **_kwargs: {"available": False, "errors": ["STM unavailable"]})

    result = focus.run_full_segment_maintenance(messages, force=True)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["updated"] is False
    assert result["applied_action_count"] == 0
    assert result["rejected_action_count"] == 1
    assert segment["status"] == "inactive"


def test_focus_segments_range_extension_does_not_copy_existing_ranges_to_new_active(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _seed_active_segment(monkeypatch, focus, messages)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    focus.run_focus_detector_before_context(messages)
    result = focus.run_focus_segment_maintenance(messages)
    status = focus.focus_segments_status()
    active_segments = [segment for segment in status["segments"] if segment["status"] == "active"]
    inactive_segments = [segment for segment in status["segments"] if segment["status"] == "inactive"]

    assert result["assigned_count"] == 0
    assert result["processed_block_count"] == 0
    assert active_segments[0]["title"] == "Project Planning"
    assert active_segments[0]["slot_ranges"] == []
    assert inactive_segments[0]["title"] == "F1"
    assert inactive_segments[0]["slot_ranges"] == [{"start": 1, "end": 2}]


def test_focus_detector_records_active_result_without_shifting(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    calls = []

    def detector_reply(prompt):
        calls.append(prompt)
        return _detector_reply_from_prompt(prompt, "active")

    monkeypatch.setattr(focus_detector, "_call_detector_json", detector_reply)

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        hints = focus_store._load_focus_ingest_hints(connection)

    assert result["detector_recorded"] is True
    assert status["segment_count"] == 1
    assert status["counts"]["active"] == 1
    assert status["last_detector_decision"]["target_kind"] == "active"
    assert status["last_detector_decision"]["target_segment_id"] == status["segments"][0]["segment_id"]
    assert status["last_detector_decision"]["reason_code"] == "no_shift"
    assert "confidence" not in status["last_detector_decision"]
    assert "reason" not in status["last_detector_decision"]
    assert len(calls) == 1
    assert status["last_focus_prepare_result"]["action"] == "no_change"
    assert result["active_title"] == "F1"
    assert hints == []


def test_focus_detector_retries_no_shift_without_unique_active_segment(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "First", "first", "active")
        focus_store._create_segment(connection, "Second", "second", "active")
        connection.commit()

    calls = []

    def detector_reply(prompt):
        calls.append(prompt)
        return "NO_SHIFT"

    monkeypatch.setattr(focus_detector, "_call_detector_json", detector_reply)

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()

    assert len(calls) == 2
    assert result["detector_error"]
    assert status["last_detector_decision"]["reason_code"] == "protocol_error"
    assert status["last_detector_decision"]["validation_failed"] is True


def test_focus_detector_uses_previous_turn_and_current_user_input(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    prompts = []

    def fake_detector(prompt):
        prompts.append(prompt)
        return _detector_reply_from_prompt(prompt, "active")

    monkeypatch.setattr(focus_detector, "_call_detector_json", fake_detector)

    focus.run_focus_detector_before_context(messages)
    payload = prompts[0][1]["content"]

    assert "focus_context" in payload
    assert "middle user" in payload
    assert "middle assistant" in payload
    assert "latest user" in payload
    assert "old user" not in payload
    assert "verbose original assistant body" not in payload


def test_focus_detector_activates_inactive_segment_before_context(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        inactive_id = focus_store._create_segment(connection, "Parked work", "Parked summary", "inactive")
        connection.commit()

    def inactive_reply(_prompt):
        return json.dumps(
            {
                "should_shift": True,
                "target_kind": "inactive",
                "target_segment_id": inactive_id,
                "confidence": 0.9,
                "proposed_title": "",
                "reason": "returns to parked work",
            }
        )

    monkeypatch.setattr(focus_detector, "_call_detector_json", inactive_reply)

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()
    active_titles = [segment["title"] for segment in status["segments"] if segment["status"] == "active"]

    assert result["action"] == "activated_inactive"
    assert status["last_detector_decision"]["reason_code"] == "target_inactive"
    assert "confidence" not in status["last_detector_decision"]
    assert "reason" not in status["last_detector_decision"]
    assert active_titles == ["Parked work"]


def test_focus_detector_creates_new_active_segment_before_context(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()
    active_titles = [segment["title"] for segment in status["segments"] if segment["status"] == "active"]
    inactive_titles = [segment["title"] for segment in status["segments"] if segment["status"] == "inactive"]

    assert result["action"] == "created_active"
    assert status["last_detector_decision"]["reason_code"] == "new_focus"
    assert active_titles == ["Project Planning"]
    assert "F1" in inactive_titles


def test_focus_detector_records_new_focus_start_slot_without_reason_leakage(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _seed_active_segment(monkeypatch, focus, messages)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))

    result = focus.run_focus_detector_before_context(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        hints = focus_store._load_focus_ingest_hints(connection)
        segment = focus_store._segment_by_id(connection, result["target_segment_id"])

    assert hints == [
        {
            "start_slot": 5,
            "segment_id": result["target_segment_id"],
            "created_at": hints[0]["created_at"],
        }
    ]
    assert segment["summary"] == NEW_FOCUS_PENDING_SUMMARY
    assert segment["summary"] != "same focus"


def test_segment_ingest_receives_matching_new_focus_start_slot_as_hint(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _seed_active_segment(monkeypatch, focus, messages)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("new"))
    detector_result = focus.run_focus_detector_before_context(messages)
    _enable_wm(monkeypatch, focus, 5, 6)
    payloads = []

    def fake_ingest(prompt):
        payload = json.loads(prompt[1]["content"])
        payloads.append(payload)
        hinted = next(segment for segment in payload["segments"] if "new_focus_start_slot" in segment)
        return json.dumps(
            {
                "action": "assign",
                "target_segment_id": hinted["segment_id"],
                "confidence": 0.9,
                "reason": "block fits hinted segment",
                "updates": {"title": None, "summary": None, "mark_resolved": False},
            }
        )

    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", fake_ingest)

    result = focus.run_segment_ingest_maintenance(messages)
    hinted = next(segment for segment in payloads[0]["segments"] if "new_focus_start_slot" in segment)

    assert result["assigned_count"] == 1
    assert hinted["segment_id"] == detector_result["target_segment_id"]
    assert hinted["new_focus_start_slot"] == 5
    assert hinted["summary"] == NEW_FOCUS_PENDING_SUMMARY
    assert "reason" not in hinted
    assert "confidence" not in hinted


def test_segment_ingest_does_not_expose_hint_to_a_different_block():
    import memory_engine.focus as focus

    payload = focus_ingest._segment_ingest_payload(
        {"slot_range": {"start": 7, "end": 8}, "slot_seqs": [7, 8], "records": []},
        [{"segment_id": "seg_a", "status": "active", "title": "A", "summary": "summary"}],
        {"segment_id": "seg_a", "start_slot": 5},
    )

    assert "new_focus_start_slot" not in payload["segments"][0]


def test_segment_ingest_can_override_detector_boundary_hint(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 5, 6)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        hinted_id = focus_store._create_segment(connection, "Hinted", "pending", "inactive")
        other_id = focus_store._create_segment(connection, "Better match", "semantic match", "active")
        focus_store._record_focus_ingest_hint(connection, hinted_id, 5)
        connection.commit()

    def fake_ingest(prompt):
        payload = json.loads(prompt[1]["content"])
        assert next(segment for segment in payload["segments"] if segment["segment_id"] == hinted_id)[
            "new_focus_start_slot"
        ] == 5
        return json.dumps(
            {
                "action": "assign",
                "target_segment_id": other_id,
                "confidence": 0.9,
                "reason": "another segment is a better semantic match",
                "updates": {"title": None, "summary": None, "mark_resolved": False},
            }
        )

    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", fake_ingest)

    result = focus.run_segment_ingest_maintenance(messages)
    status = focus.focus_segments_status()
    other = next(segment for segment in status["segments"] if segment["segment_id"] == other_id)

    assert result["last_target_segment_id"] == other_id
    assert other["slot_ranges"] == [{"start": 5, "end": 6}]


def test_focus_detector_unclear_does_not_shift(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: _detector_reply("unclear"))

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()
    active_titles = [segment["title"] for segment in status["segments"] if segment["status"] == "active"]

    assert result["action"] == "no_change"
    assert status["last_detector_decision"]["reason_code"] == "unclear"
    assert active_titles == ["F1"]


def test_focus_detector_retries_when_target_kind_does_not_match_segment_status(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        inactive_id = focus_store._create_segment(connection, "Parked work", "Parked summary", "inactive")
        active_id = next(segment["segment_id"] for segment in focus_store._load_segments(connection) if segment["status"] == "active")
        connection.commit()
    replies = [
        json.dumps(
            {
                "should_shift": True,
                "target_kind": "inactive",
                "target_segment_id": active_id,
                "confidence": 0.9,
                "proposed_title": "",
                "reason": "returns to Parked work",
            }
        ),
        json.dumps(
            {
                "should_shift": True,
                "target_kind": "inactive",
                "target_segment_id": inactive_id,
                "confidence": 0.9,
                "proposed_title": "",
                "reason": "returns to Parked work",
            }
        ),
    ]
    prompts = []

    def fake_detector(prompt):
        prompts.append(prompt)
        return replies.pop(0)

    monkeypatch.setattr(focus_detector, "_call_detector_json", fake_detector)

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()
    active_titles = [segment["title"] for segment in status["segments"] if segment["status"] == "active"]

    assert result["action"] == "activated_inactive"
    assert active_titles == ["Parked work"]
    assert len(prompts) == 2
    retry_payload = json.loads(prompts[1][1]["content"])
    assert "previous_output_validation_errors" in retry_payload


def test_focus_detector_invalid_retry_falls_back_to_unclear(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    _seed_active_segment(monkeypatch, focus, messages)

    monkeypatch.setattr(
        focus_detector,
        "_call_detector_json",
        lambda _prompt: json.dumps(
            {
                "should_shift": True,
                "target_kind": "inactive",
                "target_segment_id": "missing",
                "confidence": 0.9,
                "proposed_title": "",
                "reason": "bad target",
            }
        ),
    )

    result = focus.run_focus_detector_before_context(messages)
    status = focus.focus_segments_status()

    assert result["action"] == "no_change"
    assert result["detector_error"]
    assert status["last_detector_decision"]["target_kind"] == "unclear"
    assert status["last_detector_decision"]["validation_failed"] is True


def test_segmented_working_memory_renders_active_trunk_not_original_raw(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.event_stream as event_stream
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder.focus, "run_focus_detector_before_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda prompt: _detector_reply_from_prompt(prompt, "active"))
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)
    focus.run_focus_segment_maintenance(messages)
    event_stream.create_sum_event(
        2,
        {
            "source_role": "assistant",
            "summary": "compact old assistant",
            "reason": "test",
            "original_token_count": 1000,
            "summary_token_count": 100,
            "created_by": "test",
        },
    )
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"stm": 10000, "wm": 6000}, "scores": {}, "signals": {}, "budget_source": "dynamic"})
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))

    wm = context_builder.build_event_stream_context(messages)["messages"][1]["content"]

    assert "This is segmented working memory" in wm
    assert "F1: Primary focus for Phase 1 Focus Segment testing." in wm
    assert "[1] user: old user" in wm
    assert "[2] sema summary: compact old assistant" in wm
    assert "verbose original assistant body" not in wm


def test_segmented_working_memory_renders_resolved_title_only(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder.focus, "run_focus_detector_before_context", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)
    focus.run_focus_segment_maintenance(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Closed Branch", "Resolved details must stay out of normal context.", "resolved")
        connection.commit()
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"stm": 10000, "wm": 6000}, "scores": {}, "signals": {}, "budget_source": "dynamic"})
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))

    wm = context_builder.build_event_stream_context(messages)["messages"][1]["content"]

    assert "Resolved:" in wm
    assert "- Closed Branch" in wm
    assert "Resolved details must stay out" not in wm


def test_segmented_working_memory_bounds_resolved_titles_by_token_budget(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)
    focus.run_focus_segment_maintenance(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        for index in range(22):
            focus_store._create_segment(connection, f"Closed {index:02}", f"summary {index}", "resolved")
        connection.commit()
    records = [
        {"slot_seq": 1, "source": "user", "content": {"role": "user", "content": "old user"}, "active_kind": "raw"}
    ]

    projection = focus.build_working_memory_projection(
        records,
        lambda record: f"[{record['slot_seq']}] user: old user",
        500,
    )
    wm = projection["content"]

    assert "- Closed 00" not in wm
    assert "- Closed 21" in wm
    assert 0 < wm.count("- Closed ") < 22
    assert projection["selected_tokens"] <= 500


def test_context_status_does_not_mutate_focus_state(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(focus_context, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: _pressure())
    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda prompt: _detector_reply_from_prompt(prompt, "active"))
    focus.run_focus_segment_maintenance(messages)
    before = focus.focus_segments_status()["segments"]
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"stm": 10000, "wm": 6000}, "scores": {}, "signals": {}, "budget_source": "dynamic"})
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(1, 2))

    context_builder.context_status(messages)
    after = focus.focus_segments_status()["segments"]

    assert after == before


def test_segment_ingest_persists_assignment_audit_rows(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(focus_ingest, "_call_segment_ingest_json", _segment_ingest_reply_from_prompt)

    focus.run_focus_segment_maintenance(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        assignments = focus_store._load_assignments(connection)

    assert len(assignments) == 1
    assert assignments[0]["status"] == "assigned"
    assert assignments[0]["confidence"] == 0.9
    assert assignments[0]["reason"] == "ingest decision"
    assert assignments[0]["slot_start"] == 1
    assert assignments[0]["slot_end"] == 2

    monkeypatch.setattr(focus_context, "build_stm_projection", lambda _messages, budget=10000, **_kwargs: _projection_with_omitted(3, 4))
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, confidence=0.59),
    )
    focus.run_focus_segment_maintenance(messages)
    status = focus.focus_segments_status()

    assert status["assignment_health"]["assignment_count"] == 2
    assert status["assignment_health"]["unassigned_block_count"] == 1
    assert status["assignment_health"]["low_confidence_assignment_count"] == 0


def test_segment_ingest_skips_previously_audited_unassigned_block(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, confidence=0.59),
    )

    first = focus.run_focus_segment_maintenance(messages)
    second = focus.run_focus_segment_maintenance(messages)

    assert first["unassigned_count"] == 1
    assert second["processed_block_count"] == 0
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        assignments = focus_store._load_assignments(connection)
    assert len(assignments) == 1


def test_full_maintenance_trigger_thresholds(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages() + [_message("user", "extra")] * 20
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(focus_maintainer, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: {"budgets": {"wm": 10000}})

    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "A", "summary", "active")
        connection.commit()
        plan = focus_maintainer._full_maintenance_plan(connection, messages)

    assert plan["scheduled"] is False
    assert plan["reason"] == "below_capacity_threshold"

    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        for index in range(4):
            focus_store._create_segment(connection, f"B{index}", "summary", "inactive")
        connection.commit()
        plan = focus_maintainer._full_maintenance_plan(connection, messages)

    assert plan["scheduled"] is True
    assert plan["reason"] == "capacity_quarter_cooldown"


def test_full_maintenance_anomaly_trigger_uses_assignment_counts(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages() + [_message("user", "extra")] * 20
    session.ensure_project()
    session.save_last_session(messages)

    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "A", "summary", "active")
        for index in range(20):
            focus_store._record_assignment(
                connection,
                {"slot_range": {"start": 1, "end": 2}, "slot_seqs": [1, 2 + index]},
                {"confidence": 0.7, "reason": "low confidence"},
                {"assigned": True, "target_segment_id": segment_id, "reason": "assigned"},
            )
        connection.commit()
        plan = focus_maintainer._full_maintenance_plan(connection, messages)

    assert plan["scheduled"] is True
    assert plan["reason"] == "low_confidence_threshold"


def test_full_maintainer_loop_stops_at_max_tool_rounds(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "A", "summary", "active")
        connection.commit()

    monkeypatch.setattr(focus_maintainer, "_call_full_maintainer_json", lambda _prompt: json.dumps({"tool": "inspect_unassigned_blocks", "args": {}}))

    result = focus.run_full_segment_maintenance(messages, force=True)

    assert result["triggered"] is True
    assert result["tool_round_count"] == FULL_MAINTAINER_MAX_TOOL_ROUNDS
    assert result["applied_action_count"] == 0
    assert result["updated"] is False


def test_full_maintainer_repairs_unassigned_after_same_run_inspections(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Existing topic", "existing summary", "active")
        assignment = _record_unassigned(connection, focus, 1, 2)
        connection.commit()

    responses = [
        {"tool": "inspect_unassigned_blocks", "args": {"limit": 8}},
        {"tool": "inspect_segment", "args": {"segment_id": target_id}},
        {
            "repair_plan": [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "target_segment_id": target_id,
                    "confidence": 0.85,
                    "reason": "inspected semantic match",
                }
            ]
        },
    ]
    monkeypatch.setattr(
        focus_maintainer,
        "_call_full_maintainer_json",
        lambda _prompt: json.dumps(responses.pop(0)),
    )

    result = focus.run_full_segment_maintenance(messages, force=True)
    with event_stream.ensure_event_stream() as connection:
        repaired = focus_store._load_assignments(connection)[0]

    assert result["updated"] is True
    assert result["reassigned_unassigned_count"] == 1
    assert result["created_from_unassigned_segment_count"] == 0
    assert result["remaining_unassigned_count"] == 0
    assert repaired["status"] == "assigned"
    assert repaired["segment_id"] == target_id


def test_full_maintainer_backs_off_after_unassigned_no_progress(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = [_message("system", "system")]
    for index in range(20):
        messages.extend([_message("user", f"user {index}"), _message("assistant", f"assistant {index}")])
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current work", "summary", "active")
        for start in range(1, 41, 2):
            _record_unassigned(connection, focus, start, start + 1)
        connection.commit()

    model_call_count = 0

    def leave_unchanged(_prompt):
        nonlocal model_call_count
        model_call_count += 1
        return json.dumps({"repair_plan": [{"action": "leave_unchanged", "reason": "insufficient evidence"}]})

    monkeypatch.setattr(focus_maintainer, "_call_full_maintainer_json", leave_unchanged)

    first = focus.run_full_segment_maintenance(messages)
    second = focus.run_full_segment_maintenance(messages)
    manual = focus.run_full_segment_maintenance(messages, force=True)

    assert first["triggered"] is True
    assert first["reason"] == "unassigned_threshold"
    assert first["reassigned_unassigned_count"] == 0
    assert second["triggered"] is False
    assert second["reason"] == "unassigned_no_progress_cooldown_pending"
    assert manual["triggered"] is True
    assert model_call_count == 2


def test_full_maintainer_leave_unchanged_is_not_a_mutation(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Stable work", "summary", "active")
        connection.commit()

    monkeypatch.setattr(
        focus_maintainer,
        "_call_full_maintainer_json",
        lambda _prompt: json.dumps({"repair_plan": [{"action": "leave_unchanged", "reason": "state already consistent"}]}),
    )

    result = focus.run_full_segment_maintenance(messages, force=True)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["triggered"] is True
    assert result["updated"] is False
    assert result["proposed_action_count"] == 1
    assert result["applied_action_count"] == 0
    assert result["rejected_action_count"] == 0
    assert segment["segment_id"] == segment_id
    assert segment["title"] == "Stable work"


def test_full_maintainer_leave_unchanged_does_not_rewrite_segment_timestamps(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Stable work", "summary", "active")
        before = focus_store._segment_by_id(connection, segment_id)
        connection.commit()

    monkeypatch.setattr(focus_maintainer, "_now", lambda: "after-no-op")
    monkeypatch.setattr(
        focus_maintainer,
        "_call_full_maintainer_json",
        lambda _prompt: json.dumps({"repair_plan": [{"action": "leave_unchanged", "reason": "state already consistent"}]}),
    )

    result = focus.run_full_segment_maintenance(messages, force=True)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["updated"] is False
    assert segment["updated_at"] == before["updated_at"]
    assert segment["last_touched_at"] == before["last_touched_at"]


def test_inspect_unassigned_blocks_is_bounded_paginated_and_includes_detector_hint(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        hinted_id = focus_store._create_segment(connection, "Hinted topic", "pending", "inactive")
        focus_store._record_focus_ingest_hint(connection, hinted_id, 1)
        first_assignment = _record_unassigned(connection, focus, 1, 2)
        second_assignment = _record_unassigned(connection, focus, 3, 4)
        third_assignment = _record_unassigned(connection, focus, 5, 6)
        connection.commit()

        first_page = focus_maintainer._inspect_unassigned_blocks(connection, limit=2)
        second_page = focus_maintainer._inspect_unassigned_blocks(
            connection,
            cursor=first_page["next_cursor"],
            limit=2,
        )

    assert [item["assignment_id"] for item in first_page["assignments"]] == [
        first_assignment["assignment_id"],
        second_assignment["assignment_id"],
    ]
    assert first_page["has_more"] is True
    assert first_page["assignments"][0]["detector_hint"] == {
        "segment_id": hinted_id,
        "new_focus_start_slot": 1,
    }
    assert [item["assignment_id"] for item in second_page["assignments"]] == [third_assignment["assignment_id"]]
    assert second_page["has_more"] is False
    assert second_page["next_cursor"] is None


def test_full_maintainer_assigns_inspected_unassigned_block_and_records_repair_audit(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Existing topic", "existing summary", "active")
        assignment = _record_unassigned(connection, focus, 1, 2, confidence=0.4, reason="initial uncertainty")
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "target_segment_id": target_id,
                    "confidence": 0.82,
                    "reason": "clear existing-topic match",
                }
            ],
            inspected_assignment_ids=[assignment["assignment_id"]],
            inspected_segment_ids=[target_id],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        repaired = focus_store._load_assignments(connection)[0]
        target = focus_store._segment_by_id(connection, target_id)
        audit = connection.execute("SELECT * FROM focus_assignment_repairs").fetchone()

    assert simulation["rejected_actions"] == []
    assert repaired["status"] == "assigned"
    assert repaired["segment_id"] == target_id
    assert repaired["confidence"] == 0.82
    assert repaired["reason"] == "clear existing-topic match"
    assert target["slot_ranges"] == [{"start": 1, "end": 2}]
    assert audit["assignment_id"] == assignment["assignment_id"]
    assert audit["previous_status"] == "unassigned"
    assert audit["previous_reason"] == "initial uncertainty"
    assert audit["target_segment_id"] == target_id


def test_full_maintainer_creates_inactive_segment_from_inspected_unassigned_blocks(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Current work", "current summary", "active")
        first = _record_unassigned(connection, focus, 1, 2)
        second = _record_unassigned(connection, focus, 3, 4)
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "create_segment_from_unassigned",
                    "assignment_ids": [first["assignment_id"], second["assignment_id"]],
                    "title": "Film Discussion",
                    "summary": "Discussion of films, directors, and genres.",
                    "confidence": 0.9,
                    "reason": "coherent new historical topic",
                }
            ],
            inspected_assignment_ids=[first["assignment_id"], second["assignment_id"]],
            max_segments=20,
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        segments = focus_store._load_segments(connection)
        assignments = focus_store._load_assignments(connection)

    created = next(segment for segment in segments if segment["segment_id"] != active_id)
    assert simulation["rejected_actions"] == []
    assert created["status"] == "inactive"
    assert created["title"] == "Film Discussion"
    assert created["slot_ranges"] == [{"start": 1, "end": 4}]
    assert next(segment for segment in segments if segment["segment_id"] == active_id)["status"] == "active"
    assert {assignment["status"] for assignment in assignments} == {"assigned"}
    assert {assignment["segment_id"] for assignment in assignments} == {created["segment_id"]}


def test_full_maintainer_rejects_uninspected_and_resolved_unassigned_targets(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Closed topic", "closed summary", "resolved")
        assignment = _record_unassigned(connection, focus, 1, 2)
        connection.commit()
        uninspected = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "target_segment_id": target_id,
                    "confidence": 0.9,
                    "reason": "guess",
                }
            ],
            inspected_segment_ids=[target_id],
        )
        resolved = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "target_segment_id": target_id,
                    "confidence": 0.9,
                    "reason": "closed match",
                }
            ],
            inspected_assignment_ids=[assignment["assignment_id"]],
            inspected_segment_ids=[target_id],
        )

    assert "inspect_unassigned_blocks" in uninspected["rejected_actions"][0]["error"]
    assert "active or inactive" in resolved["rejected_actions"][0]["error"]


def test_full_maintainer_rejects_new_segment_when_at_capacity(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current work", "current summary", "active")
        assignment = _record_unassigned(connection, focus, 1, 2)
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "create_segment_from_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "title": "Historical Topic",
                    "summary": "A distinct historical topic.",
                    "confidence": 0.9,
                    "reason": "new topic",
                }
            ],
            inspected_assignment_ids=[assignment["assignment_id"]],
            max_segments=1,
        )

    assert simulation["applied_actions"] == []
    assert "capacity" in simulation["rejected_actions"][0]["error"]


def test_full_maintainer_rejects_entire_unassigned_batch_when_one_assignment_is_invalid(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Existing topic", "existing summary", "active")
        assignment = _record_unassigned(connection, focus, 1, 2)
        connection.commit()
        missing_id = "assignment_missing"
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"], missing_id],
                    "target_segment_id": target_id,
                    "confidence": 0.9,
                    "reason": "same topic",
                }
            ],
            inspected_assignment_ids=[assignment["assignment_id"], missing_id],
            inspected_segment_ids=[target_id],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        persisted_assignment = focus_store._load_assignments(connection)[0]
        target = focus_store._segment_by_id(connection, target_id)

    assert simulation["applied_actions"] == []
    assert "does not exist" in simulation["rejected_actions"][0]["error"]
    assert persisted_assignment["status"] == "unassigned"
    assert target["slot_ranges"] == []


def test_full_maintainer_rejects_low_confidence_unassigned_repair(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Existing topic", "existing summary", "active")
        assignment = _record_unassigned(connection, focus, 1, 2)
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "assign_unassigned",
                    "assignment_ids": [assignment["assignment_id"]],
                    "target_segment_id": target_id,
                    "confidence": 0.74,
                    "reason": "weak match",
                }
            ],
            inspected_assignment_ids=[assignment["assignment_id"]],
            inspected_segment_ids=[target_id],
        )

    assert simulation["applied_actions"] == []
    assert "confidence between 0.75 and 1.0" in simulation["rejected_actions"][0]["error"]


def test_focus_status_formats_full_maintainer_unassigned_metrics():
    import memory_engine.focus as focus

    rendered = focus.format_focus_segments_status(
        {
            "active": True,
            "segment_count": 0,
            "counts": {},
            "segments": [],
            "last_full_maintainer_result": {
                "triggered": True,
                "reassigned_unassigned_count": 3,
                "created_from_unassigned_segment_count": 1,
                "remaining_unassigned_count": 7,
            },
        }
    )

    assert "reassigned_unassigned_count: 3" in rendered
    assert "created_from_unassigned_segment_count: 1" in rendered
    assert "remaining_unassigned_count: 7" in rendered


def test_full_maintainer_validator_and_apply_merge_and_delete(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Target", "target summary", "active")
        source_id = focus_store._create_segment(connection, "Source", "source summary", "inactive")
        empty_id = focus_store._create_segment(connection, "Empty", "empty summary", "inactive")
        focus_store._update_segment_ranges(connection, target_id, [{"start": 1, "end": 1}])
        focus_store._update_segment_ranges(connection, source_id, [{"start": 2, "end": 2}])
        focus_store._record_assignment(
            connection,
            {"slot_range": {"start": 2, "end": 2}, "slot_seqs": [2]},
            {"confidence": 0.9, "reason": "assigned"},
            {"assigned": True, "target_segment_id": source_id, "reason": "assigned"},
        )
        actions = [
            {"action": "merge_segments", "target_segment_id": target_id, "source_segment_ids": [source_id], "summary": "merged summary"},
            {"action": "delete_empty_segment", "segment_id": empty_id},
        ]
        simulation = focus_maintainer._validate_and_simulate_repair_plan(connection, actions)
        focus_maintainer._apply_repair_simulation(connection, simulation)
        segments = focus_store._load_segments(connection)
        assignments = focus_store._load_assignments(connection, segment_id=target_id)

    assert simulation["rejected_actions"] == []
    assert {segment["segment_id"] for segment in segments} == {target_id}
    assert segments[0]["slot_ranges"] == [{"start": 1, "end": 2}]
    assert segments[0]["summary"] == "merged summary"
    assert len(assignments) == 1


def test_full_maintainer_rejects_deleting_an_empty_active_segment(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current work", "summary", "active")
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "delete_empty_segment", "segment_id": segment_id, "reason": "empty"}],
        )

    assert simulation["applied_actions"] == []
    assert simulation["rejected_actions"][0]["action"] == "delete_empty_segment"
    assert "inactive" in simulation["rejected_actions"][0]["error"]


def test_full_maintainer_preserves_detector_selected_active_segment(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Current work", "summary", "active")
        target_id = focus_store._create_segment(connection, "Archive target", "summary", "inactive")
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {
                    "action": "merge_segments",
                    "target_segment_id": target_id,
                    "source_segment_ids": [active_id],
                    "reason": "merge",
                }
            ],
            protected_active_segment_id=active_id,
        )

    assert simulation["applied_actions"] == []
    assert simulation["rejected_actions"][0]["action"] == "merge_segments"
    assert "Detector-selected active segment" in simulation["rejected_actions"][0]["error"]


def test_full_maintainer_preserves_segment_with_pending_detector_hint(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Pending", "awaiting ingress", "inactive")
        focus_store._record_focus_ingest_hint(connection, segment_id, 5)
        connection.commit()
        delete_simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "delete_empty_segment", "segment_id": segment_id, "reason": "empty"}],
        )
        resolve_simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "mark_resolved", "segment_id": segment_id, "reason": "closed"}],
            allow_mark_resolved=True,
            mark_resolved_segment_ids=[segment_id],
        )

    assert delete_simulation["applied_actions"] == []
    assert "pending Detector ingest hint" in delete_simulation["rejected_actions"][0]["error"]
    assert resolve_simulation["applied_actions"] == []
    assert "pending Detector ingest hint" in resolve_simulation["rejected_actions"][0]["error"]


def test_assignment_audit_consumes_pending_detector_hint(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Pending", "awaiting ingress", "inactive")
        focus_store._record_focus_ingest_hint(connection, segment_id, 5)
        focus_store._record_assignment(
            connection,
            {"slot_range": {"start": 5, "end": 6}, "slot_seqs": [5, 6]},
            {"confidence": 0.4, "reason": "uncertain"},
            {"assigned": False, "target_segment_id": "", "reason": "low_confidence"},
        )
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "delete_empty_segment", "segment_id": segment_id, "reason": "unused"}],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        hints = focus_store._load_focus_ingest_hints(connection)

    assert simulation["rejected_actions"] == []
    assert hints == []


def test_full_maintainer_transfers_detector_hints_during_merge(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        target_id = focus_store._create_segment(connection, "Target", "target summary", "active")
        source_id = focus_store._create_segment(connection, "Source", "source summary", "inactive")
        focus_store._record_focus_ingest_hint(connection, source_id, 5)
        connection.commit()
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "merge_segments", "target_segment_id": target_id, "source_segment_ids": [source_id]}],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        hints = focus_store._load_focus_ingest_hints(connection)

    assert simulation["rejected_actions"] == []
    assert hints[0]["start_slot"] == 5
    assert hints[0]["segment_id"] == target_id


def test_segment_ingest_does_not_resolve_detector_selected_active_segment(isolated_workspace, monkeypatch):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    runtime_state = {}
    session.ensure_project()
    session.save_last_session(messages)
    _enable_wm(monkeypatch, focus, 1, 2)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current work", "summary", "active")
        connection.commit()
    runtime_state["_focus_detector_active_segment_id"] = segment_id
    monkeypatch.setattr(
        focus_ingest,
        "_call_segment_ingest_json",
        lambda prompt: _segment_ingest_reply_from_prompt(prompt, mark_resolved=True),
    )

    result = focus.run_focus_segment_maintenance(messages, runtime_state)
    segment = focus.focus_segments_status()["segments"][0]

    assert result["assigned_count"] == 1
    assert result["resolved_count"] == 0
    assert segment["status"] == "active"


def test_full_maintainer_rejects_partial_assignment_move(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        source_id = focus_store._create_segment(connection, "Source", "source summary", "active")
        target_id = focus_store._create_segment(connection, "Target", "target summary", "inactive")
        focus_store._update_segment_ranges(connection, source_id, [{"start": 1, "end": 4}])
        focus_store._record_assignment(
            connection,
            {"slot_range": {"start": 1, "end": 4}, "slot_seqs": [1, 2, 3, 4]},
            {"confidence": 0.9, "reason": "assigned"},
            {"assigned": True, "target_segment_id": source_id, "reason": "assigned"},
        )
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "move_range", "source_segment_id": source_id, "target_segment_id": target_id, "start": 2, "end": 3}],
        )
        source = simulation["segments"][source_id]
        target = simulation["segments"][target_id]

    assert simulation["applied_actions"] == []
    assert simulation["rejected_actions"][0]["action"] == "move_range"
    assert "assignment audit units" in simulation["rejected_actions"][0]["error"]
    assert source["slot_ranges"] == [{"start": 1, "end": 4}]
    assert target["slot_ranges"] == []


def test_full_maintainer_allows_complete_assignment_move_and_updates_audit(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        source_id = focus_store._create_segment(connection, "Source", "source summary", "active")
        target_id = focus_store._create_segment(connection, "Target", "target summary", "inactive")
        focus_store._update_segment_ranges(connection, source_id, [{"start": 1, "end": 4}])
        focus_store._record_assignment(
            connection,
            {"slot_range": {"start": 2, "end": 3}, "slot_seqs": [2, 3]},
            {"confidence": 0.9, "reason": "assigned"},
            {"assigned": True, "target_segment_id": source_id, "reason": "assigned"},
        )
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [{"action": "move_range", "source_segment_id": source_id, "target_segment_id": target_id, "start": 2, "end": 3}],
        )
        focus_maintainer._apply_repair_simulation(connection, simulation)
        source = focus_store._segment_by_id(connection, source_id)
        target = focus_store._segment_by_id(connection, target_id)
        assignments = focus_store._load_assignments(connection, segment_id=target_id)

    assert simulation["rejected_actions"] == []
    assert source["slot_ranges"] == [{"start": 1, "end": 1}, {"start": 4, "end": 4}]
    assert target["slot_ranges"] == [{"start": 2, "end": 3}]
    assert len(assignments) == 1
    assert assignments[0]["slot_start"] == 2
    assert assignments[0]["slot_end"] == 3


def test_full_maintainer_rejects_partial_relocation_after_assignment_unit_was_moved(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        source_id = focus_store._create_segment(connection, "Source", "source summary", "active")
        middle_id = focus_store._create_segment(connection, "Middle", "middle summary", "inactive")
        focus_store._update_segment_ranges(connection, source_id, [{"start": 1, "end": 2}])
        focus_store._record_assignment(
            connection,
            {"slot_range": {"start": 1, "end": 2}, "slot_seqs": [1, 2]},
            {"confidence": 0.9, "reason": "assigned"},
            {"assigned": True, "target_segment_id": source_id, "reason": "assigned"},
        )
        simulation = focus_maintainer._validate_and_simulate_repair_plan(
            connection,
            [
                {"action": "move_range", "source_segment_id": source_id, "target_segment_id": middle_id, "start": 1, "end": 2},
                {"action": "move_range", "source_segment_id": middle_id, "target_segment_id": source_id, "start": 1, "end": 1},
            ],
        )

    assert len(simulation["applied_actions"]) == 1
    assert simulation["rejected_actions"][0]["action"] == "move_range"
    assert "assignment audit units" in simulation["rejected_actions"][0]["error"]
