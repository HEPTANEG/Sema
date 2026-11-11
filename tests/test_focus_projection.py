import json

import pytest

import memory_engine.event_stream as event_stream
from memory_engine.focus import store as focus_store


def _message(role, content):
    return {"role": role, "content": content}


def _messages():
    return [
        _message("system", "system"),
        _message("user", "active user detail"),
        _message("assistant", "active assistant detail"),
        _message("user", "inactive user detail"),
        _message("assistant", "inactive assistant detail"),
        _message("user", "resolved user detail"),
        _message("assistant", "resolved assistant detail"),
    ]


def _record_assignment(connection, focus, slots, segment_id=None, status="assigned"):
    focus_store._record_assignment(
        connection,
        {"slot_range": {"start": min(slots), "end": max(slots)}, "slot_seqs": list(slots)},
        {"confidence": 0.9, "reason": "projection test"},
        {
            "assigned": status == "assigned",
            "target_segment_id": segment_id or "",
            "reason": "projection test",
        },
    )


def _render(record):
    content = record.get("content") or {}
    return f"[{record['slot_seq']}] {content.get('role', record.get('source'))}: {content.get('content', '')}"


def _seed_focus(session, focus):
    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Active work", "active summary", "active")
        inactive_id = focus_store._create_segment(connection, "Parked work", "inactive summary", "inactive")
        resolved_id = focus_store._create_segment(connection, "Closed work", "resolved summary", "resolved")
        for segment_id, slots in ((active_id, [1, 2]), (inactive_id, [3, 4]), (resolved_id, [5, 6])):
            focus_store._update_segment(
                connection,
                segment_id,
                ranges=[{"start": min(slots), "end": max(slots)}],
            )
            _record_assignment(connection, focus, slots, segment_id)
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]
    return messages, records, active_id, inactive_id, resolved_id


def test_focus_projection_charges_metadata_instead_of_inactive_and_resolved_details(
    isolated_workspace,
):
    import memory_engine.focus as focus
    from persistence import session

    _messages_value, records, _active_id, _inactive_id, _resolved_id = _seed_focus(
        session,
        focus,
    )

    projection = focus.build_working_memory_projection(records, _render, 1000)

    assert projection["selected_tokens"] <= 1000
    assert projection["projection_mode"] == "focus"
    assert projection["selected_slot_seqs"] == [1, 2]
    assert projection["resident_slot_count"] == 6
    assert projection["resident_raw_token_count"] > projection["active_detail_tokens"]
    assert "active user detail" in projection["content"]
    assert "inactive user detail" not in projection["content"]
    assert "resolved user detail" not in projection["content"]
    assert "Parked work: inactive summary" in projection["content"]
    assert "- Closed work" in projection["content"]
    assert "resolved summary" not in projection["content"]


def test_focus_projection_reserves_bounded_space_for_unowned_records(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = [_message("system", "system")]
    for index in range(1, 21):
        messages.append(_message("user", f"active-{index} " + "detail " * 12))
    for index in range(21, 25):
        messages.append(_message("user", f"unowned-{index} compact"))
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        active_id = focus_store._create_segment(connection, "Active work", "summary", "active")
        focus_store._update_segment(connection, active_id, ranges=[{"start": 1, "end": 20}])
        _record_assignment(connection, focus, list(range(1, 21)), active_id)
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]

    projection = focus.build_working_memory_projection(records, _render, 500)
    detail_pool = 500 - projection["metadata_tokens"]

    assert projection["selected_tokens"] <= 500
    assert projection["active_detail_count"] > projection["unowned_detail_count"] > 0
    assert projection["unowned_detail_tokens"] <= int(detail_pool * 0.10)
    assert "Unassigned recent context:" in projection["content"]
    assert "unowned-24" in projection["content"]


def test_focus_projection_without_active_ranges_keeps_metadata_and_bounded_unowned_context(
    isolated_workspace,
):
    import memory_engine.focus as focus
    from persistence import session

    messages = [
        _message("system", "system"),
        _message("user", "parked raw body"),
        _message("user", "unowned recent body"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        inactive_id = focus_store._create_segment(connection, "Parked", "compact summary", "inactive")
        focus_store._update_segment(connection, inactive_id, ranges=[{"start": 1, "end": 1}])
        _record_assignment(connection, focus, [1], inactive_id)
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]

    projection = focus.build_working_memory_projection(records, _render, 500)

    assert "Parked: compact summary" in projection["content"]
    assert "parked raw body" not in projection["content"]
    assert "unowned recent body" in projection["content"]
    assert projection["selected_slot_seqs"] == [2]


def test_focus_projection_places_newest_active_focus_closest_to_stm(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    messages = [
        _message("system", "system"),
        _message("user", "older active detail"),
        _message("user", "newer active detail"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        older_id = focus_store._create_segment(connection, "Older focus", "older summary", "active")
        newer_id = focus_store._create_segment(connection, "Newer focus", "newer summary", "active")
        focus_store._update_segment(connection, older_id, ranges=[{"start": 1, "end": 1}])
        focus_store._update_segment(connection, newer_id, ranges=[{"start": 2, "end": 2}])
        _record_assignment(connection, focus, [1], older_id)
        _record_assignment(connection, focus, [2], newer_id)
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]

    projection = focus.build_working_memory_projection(records, _render, 1000)

    assert projection["selected_slot_seqs"] == [1, 2]
    assert projection["content"].index("Older focus") < projection["content"].index("Newer focus")
    assert projection["content"].index("older active detail") < projection["content"].index(
        "newer active detail"
    )


def test_context_builder_uses_legacy_projection_without_focus_state(isolated_workspace):
    import memory_engine.context_builder as context_builder
    from persistence import session

    session.ensure_project()
    session.save_last_session([_message("system", "system"), _message("user", "legacy detail")])
    projection = context_builder._build_working_memory_projection(
        {
            "selected_units": [],
            "wm_ingress_watermark": 1,
        },
        budget=500,
    )

    assert projection["projection_mode"] == "legacy"
    assert projection["selected_slot_seqs"] == [1]
    assert "Older active working-memory timeline" in projection["content"]
    assert "legacy detail" in projection["content"]


def test_focus_projection_truncates_metadata_without_mutating_segment(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    session.save_last_session([_message("system", "system"), _message("user", "detail")])
    summary = "very long summary " * 500
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current work", summary, "active")
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]

    projection = focus.build_working_memory_projection(records, _render, 500)
    with event_stream.ensure_event_stream() as connection:
        stored = focus_store._segment_by_id(connection, segment_id)

    assert projection["selected_tokens"] <= 500
    assert "..." in projection["content"]
    assert stored["summary"] == " ".join(summary.split())


def test_focus_projection_oversized_active_slot_keeps_preflight_error_contract(isolated_workspace):
    import memory_engine.focus as focus
    from persistence import session

    session.ensure_project()
    session.save_last_session(
        [_message("system", "system"), _message("user", "oversized " * 5000)]
    )
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(connection, "Current work", "summary", "active")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 1, "end": 1}])
        _record_assignment(connection, focus, [1], segment_id)
        connection.commit()
    records = [
        record
        for record in event_stream.load_active_trunk_records()
        if record.get("source") != "system"
    ]

    with pytest.raises(
        RuntimeError,
        match=r"Context Builder WM slot exceeds budget: slot_seq=1, tokens=\d+, budget=2000",
    ):
        focus.build_working_memory_projection(records, _render, 2000)


def test_context_builder_and_observer_share_focus_projection(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.focus as focus
    import memory_engine.observer as observer
    from persistence import session

    messages, _records, _active_id, _inactive_id, _resolved_id = _seed_focus(
        session,
        focus,
    )
    pressure = {
        "budget_source": "dynamic",
        "scores": {},
        "budgets": {"stm_min": 1000, "stm_max": 50000, "stm": 1000, "wm_min": 1000, "wm_max": 50000, "wm": 1000},
        "signals": {},
    }
    stm = {
        "messages": [messages[0]],
        "budget": 1000,
        "selected_tokens": 1,
        "selected_units": [],
        "omitted_units": [{"start_index": 1, "end_index": 6}],
        "latest_unit_tokens": 0,
        "total_units": 1,
        "wm_ingress_watermark": -1,
    }
    monkeypatch.setattr(context_builder, "compute_and_store_dynamic_context_budget", lambda *_args, **_kwargs: pressure)
    monkeypatch.setattr(observer, "_observer_pressure", lambda *_args, **_kwargs: pressure)
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda *_args, **_kwargs: stm)
    monkeypatch.setattr(focus, "run_focus_detector_before_context", lambda *_args, **_kwargs: {})

    context = context_builder.build_event_stream_context(messages)
    preview = observer.context_build_preview(messages)

    assert context["working_memory"]["selected_tokens"] == preview["working_memory"]["selected_tokens"]
    assert context["working_memory"]["selected_slot_seqs"] == preview["working_memory"]["selected_slot_seqs"]
    assert preview["working_memory"]["projection_mode"] == "focus"
    assert json.dumps(preview, ensure_ascii=False).find("active user detail") == -1


def test_formation_buffer_keeps_using_raw_wm_window(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.ltm.formation as formation

    record = {
        "slot_seq": 1,
        "active_token_count": 20,
        "rendered_tokens": 20,
        "rendered": "raw window record",
    }
    monkeypatch.setattr(
        formation,
        "compute_and_store_dynamic_context_budget",
        lambda *_args, **_kwargs: {"budgets": {"stm": 100, "wm": 100}},
    )
    monkeypatch.setattr(
        formation,
        "build_stm_projection",
        lambda *_args, **_kwargs: {"selected_units": [], "omitted_units": []},
    )
    monkeypatch.setattr(
        context_builder,
        "_select_working_memory_records",
        lambda *_args, **_kwargs: {"selected_records": [record]},
    )
    monkeypatch.setattr(
        context_builder,
        "_build_working_memory_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Formation must not use the provider Focus projection")
        ),
    )

    slots, budget = formation._buffer_slots([_message("user", "message")])

    assert slots == {1}
    assert budget == 100
