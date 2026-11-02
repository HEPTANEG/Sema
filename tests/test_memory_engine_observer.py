import json
import sqlite3


def _message(role, content):
    return {"role": role, "content": content}


def _messages():
    return [
        _message("system", "system prompt"),
        _message("user", "old user body"),
        _message("assistant", "old assistant body"),
        _message("user", "latest user body"),
        _message("assistant", "latest assistant body"),
    ]


def _fake_pressure(stm=10000, wm=6000):
    return {
        "budget_source": "dynamic+stabilized",
        "scores": {"I": 0.1, "S": 0.2, "R": 0.0, "L": 0.3, "P_context": 0.3},
        "budgets": {
            "stm_min": 10000,
            "stm_max": 50000,
            "stm": stm,
            "stm_raw": stm,
            "wm_min": 10000,
            "wm_max": 50000,
            "wm": wm,
            "wm_raw": wm,
        },
        "signals": {},
        "stabilizer": {"context_build_seq": 7, "persisted": True},
    }


def _fake_stm_projection():
    return {
        "messages": [
            _message("system", "system prompt"),
            _message("user", "latest user body"),
            _message("assistant", "latest assistant body"),
        ],
        "budget": 10000,
        "selected_tokens": 12,
        "selected_blocks": [
            {
                "kind": "interaction",
                "messages": [_message("user", "latest user body"), _message("assistant", "latest assistant body")],
                "start_index": 3,
                "end_index": 4,
                "message_count": 2,
                "token_count": 12,
            }
        ],
        "omitted_blocks": [
            {"kind": "interaction", "start_index": 1, "end_index": 2, "message_count": 2, "token_count": 20}
        ],
        "latest_block_tokens": 12,
        "total_blocks": 2,
        "wm_ingress_watermark": -1,
        "watermark_locked_omitted_block_count": 0,
    }


def test_memory_engine_snapshot_is_structured_and_metadata_only(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.observer as observer
    from persistence import session

    secret = "UNIQUE_RAW_MESSAGE_BODY_SHOULD_NOT_LEAK"
    messages = [
        _message("system", "system prompt"),
        _message("user", secret),
        _message("assistant", "assistant reply"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, **_kwargs: _fake_stm_projection())

    snapshot = observer.memory_engine_snapshot(messages, runtime_state={"context_pressure": _fake_pressure()})
    rendered = json.dumps(snapshot, ensure_ascii=False)

    assert snapshot["observer"]["api_version"] == 1
    assert snapshot["observer"]["read_only"] is True
    assert snapshot["storage"]["slot_count"] == 3
    assert "working_memory" in snapshot
    assert "focus_segments" in snapshot
    assert secret not in rendered
    assert "latest assistant body" not in rendered


def test_observer_exposes_detector_reason_code(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    import memory_engine.observer as observer
    from memory_engine.focus import detector as focus_detector
    from memory_engine.focus import store as focus_store
    from persistence import session

    messages = _messages()
    session.ensure_project()
    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        focus_store._create_segment(connection, "Current", "current", "active")
        connection.commit()
    monkeypatch.setattr(focus_detector, "_call_detector_json", lambda _prompt: "NO_SHIFT")

    from memory_engine.focus import run_focus_detector_before_context

    run_focus_detector_before_context(messages)

    snapshot = observer._focus_segments_snapshot()
    assert snapshot["last_detector_reason_code"] == "no_shift"


def test_context_build_preview_reports_ranges_without_mutating_context_state(isolated_workspace, monkeypatch):
    import memory_engine.context_builder as context_builder
    import memory_engine.event_stream as event_stream
    import memory_engine.observer as observer
    from persistence import session

    messages = _messages()
    runtime_state = {"context_pressure": _fake_pressure()}
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(context_builder, "build_stm_projection", lambda _messages, **_kwargs: _fake_stm_projection())
    monkeypatch.setattr(
        context_builder.focus,
        "run_focus_detector_before_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("detector should not run")),
    )
    monkeypatch.setattr(
        context_builder.wm_ingress,
        "stage_wm_ingress_from_projection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("watermark should not stage")),
    )

    preview = observer.context_build_preview(messages, runtime_state=runtime_state)

    assert preview["ok"] is True
    assert preview["context_order"] == ["system", "working_memory", "long_term_memory", "short_term_memory"]
    assert preview["working_memory"]["selected_slot_seqs"] == [1, 2]
    assert preview["short_term_memory"]["selected_blocks"] == [
        {"kind": "interaction", "start_index": 3, "end_index": 4, "message_count": 2, "token_count": 12}
    ]
    assert "staged_wm_ingress_watermark_slot_seq" not in runtime_state
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'context_budget_state'"
        ).fetchone()
    assert table is None


def test_evidence_audit_is_runtime_only(isolated_workspace):
    import memory_engine.evidence as evidence
    import memory_engine.observer as observer
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())
    runtime_state = {}

    evidence.inspect_slot(1, runtime_state=runtime_state)
    evidence.expand_around(3, before_blocks=1, after_blocks=0, runtime_state=runtime_state)
    evidence.get_stm_evidence(_messages(), runtime_state=runtime_state, max_chars=200)

    audit = observer.evidence_audit(runtime_state)

    assert [row["query_type"] for row in audit] == ["slot", "expand_around", "stm_evidence"]
    assert all(row["read_only"] is True for row in audit)
    assert audit[1]["projection"] == "active"
    assert audit[1]["result_count"] > 0
    assert observer.evidence_audit({}) == []


def test_stm_evidence_excludes_watermark_locked_blocks_without_mutating_ingress(isolated_workspace):
    import memory_engine.evidence as evidence
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)
    runtime_state = {
        "context_pressure": {"budgets": {"stm": 10000}},
        "staged_wm_ingress_watermark_slot_seq": 2,
    }
    before = wm_ingress.wm_ingress_watermark_status({})

    result = evidence.get_stm_evidence(messages, runtime_state=runtime_state, max_chars=10000)
    payload = json.loads(result["content_preview"])

    assert result["query"]["wm_ingress_watermark"] == 2
    assert result["selected_block_count"] == 1
    assert "old user body" not in json.dumps(payload, ensure_ascii=False)
    assert "old assistant body" not in json.dumps(payload, ensure_ascii=False)
    assert "latest user body" in json.dumps(payload, ensure_ascii=False)
    assert "latest assistant body" in json.dumps(payload, ensure_ascii=False)
    assert runtime_state["staged_wm_ingress_watermark_slot_seq"] == 2
    assert "wm_ingress_watermark_slot_seq" not in runtime_state
    assert wm_ingress.wm_ingress_watermark_status({}) == before


def test_stm_evidence_without_watermark_keeps_eligible_blocks(isolated_workspace):
    import memory_engine.evidence as evidence
    from persistence import session

    messages = _messages()
    session.ensure_project()
    session.save_last_session(messages)

    result = evidence.get_stm_evidence(
        messages,
        runtime_state={"context_pressure": {"budgets": {"stm": 10000}}},
        max_chars=10000,
    )
    payload = json.loads(result["content_preview"])
    rendered = json.dumps(payload, ensure_ascii=False)

    assert result["query"]["wm_ingress_watermark"] == -1
    assert result["selected_block_count"] == 2
    assert "old user body" in rendered
    assert "latest assistant body" in rendered


def test_stm_consumers_share_effective_watermark_boundary(isolated_workspace):
    import copy
    import json
    import sqlite3
    import memory_engine.evidence as evidence
    import memory_engine.event_stream as event_stream
    import memory_engine.observer as observer
    import memory_engine.wm.ingress as wm_ingress
    from persistence import session

    messages = [
        _message("system", "system prompt"),
        _message("user", "persisted locked user"),
        _message("assistant", "persisted locked assistant"),
        _message("user", "runtime locked user"),
        _message("assistant", "runtime locked assistant"),
        _message("user", "staged locked user"),
        _message("assistant", "staged locked assistant"),
        _message("user", "eligible user"),
        _message("assistant", "eligible assistant"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    wm_ingress.stage_wm_ingress_watermark({}, 2)
    wm_ingress.commit_staged_wm_ingress_watermark({"staged_wm_ingress_watermark_slot_seq": 2})
    runtime_state = {
        "context_pressure": _fake_pressure(stm=10000, wm=10000),
        "wm_ingress_watermark_slot_seq": 4,
        "staged_wm_ingress_watermark_slot_seq": 6,
    }
    before_runtime = copy.deepcopy(runtime_state)
    before_persisted = wm_ingress.wm_ingress_watermark_status({})

    stm_result = evidence.get_stm_evidence(messages, runtime_state=runtime_state, max_chars=10000)
    preview = observer.context_build_preview(messages, runtime_state=runtime_state)
    snapshot = observer.memory_engine_snapshot(messages, runtime_state=runtime_state)
    payload = json.loads(stm_result["content_preview"])
    rendered = json.dumps(payload, ensure_ascii=False)

    assert stm_result["query"]["wm_ingress_watermark"] == 6
    assert stm_result["selected_block_count"] == 1
    assert preview["short_term_memory"]["wm_ingress_watermark"] == 6
    assert preview["short_term_memory"]["watermark_locked_omitted_block_count"] == 3
    assert snapshot["context_preview"]["short_term_memory"]["wm_ingress_watermark"] == 6
    assert snapshot["wm_ingress"]["persisted_watermark"] == 2
    assert snapshot["wm_ingress"]["runtime_watermark"] == 4
    assert snapshot["wm_ingress"]["staged_watermark"] == 6
    assert snapshot["wm_ingress"]["effective_watermark"] == 6
    assert "persisted locked user" not in rendered
    assert "runtime locked user" not in rendered
    assert "staged locked user" not in rendered
    assert "eligible user" in rendered
    assert "eligible assistant" in rendered
    audit = runtime_state.pop("memory_engine_evidence_audit")
    assert runtime_state == before_runtime
    assert audit[-1]["query_type"] == "stm_evidence"
    assert wm_ingress.wm_ingress_watermark_status({}) == before_persisted
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'context_budget_state'"
        ).fetchone()
    assert table is None
