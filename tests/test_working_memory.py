import json


def _message(role, content):
    return {"role": role, "content": content}


def _long_text():
    return "Important detail. " * 1200


def _medium_text(label):
    return f"{label} detail. " * 25


def _omitted_first_block(_messages, **_kwargs):
    return {
        "budget": 10000,
        "selected_tokens": 10,
        "selected_block_count": 1,
        "omitted_block_count": 1,
        "latest_block_tokens": 10,
        "total_block_count": 2,
        "selected_blocks": [{"kind": "interaction", "start_index": 3, "end_index": 4, "message_count": 2, "token_count": 10}],
        "omitted_blocks": [{"kind": "interaction", "start_index": 1, "end_index": 2, "message_count": 2, "token_count": 1000}],
    }


def _omitted_two_blocks(_messages, **_kwargs):
    return {
        "budget": 10000,
        "selected_tokens": 10,
        "selected_block_count": 1,
        "omitted_block_count": 2,
        "latest_block_tokens": 10,
        "total_block_count": 3,
        "selected_blocks": [{"kind": "interaction", "start_index": 5, "end_index": 6, "message_count": 2, "token_count": 10}],
        "omitted_blocks": [
            {"kind": "interaction", "start_index": 1, "end_index": 2, "message_count": 2, "token_count": 1000},
            {"kind": "interaction", "start_index": 3, "end_index": 4, "message_count": 2, "token_count": 1000},
        ],
    }


def test_working_memory_summarizes_slot_without_changing_raw_projection(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Concise reusable summary.", "reason": "old long explanation"}),
    )

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["updated"] is True
    assert result["action"] == "summarize"
    assert session.load_last_session() == messages
    active = event_stream.load_active_projection()
    assert active[2]["summary"] == "Concise reusable summary."
    assert active[2]["source_role"] == "assistant"
    slot = event_stream.load_slot(2)
    assert slot["active_event_id"] != slot["original_event_id"]
    assert event_stream.load_event(slot["active_event_id"])["kind"] == "sum"


def test_working_memory_run_processes_all_current_candidates_serially(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request one"),
        _message("assistant", _long_text()),
        _message("user", "old request two"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_two_blocks)
    calls = []

    def fake_decision(prompt):
        calls.append(prompt)
        return json.dumps({"action": "summarize", "summary": f"Summary {len(calls)}.", "reason": "compress"})

    monkeypatch.setattr(working_memory, "_call_model_json", fake_decision)

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert len(calls) == 2
    assert result["processed_count"] == 2
    assert result["summarized_count"] == 2
    assert result["updated_count"] == 2
    assert result["processed_slot_seqs"] == [2, 4]
    active = event_stream.load_active_projection()
    assert active[2]["summary"] == "Summary 1."
    assert active[4]["summary"] == "Summary 2."


def test_working_memory_need_context_retries_with_evidence_then_summarizes(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    calls = []
    responses = iter(
        [
            {"action": "need_context", "summary": "", "reason": "candidate depends on surrounding discussion"},
            {"action": "summarize", "summary": "Evidence-backed summary.", "reason": "nearby context was enough"},
        ]
    )

    def fake_decision(prompt):
        calls.append(prompt)
        return json.dumps(next(responses))

    monkeypatch.setattr(working_memory, "_call_model_json", fake_decision)
    runtime_state = {}

    result = working_memory.run_working_memory_maintenance(messages, force=True, runtime_state=runtime_state)

    second_payload = json.loads(calls[1][1]["content"])
    audit = runtime_state.get("memory_engine_evidence_audit") or []
    assert len(calls) == 2
    assert "additional_evidence" in second_payload
    assert second_payload["additional_evidence"]["query"]["type"] == "expand_around"
    assert audit[-1]["query_type"] == "expand_around"
    assert audit[-1]["read_only"] is True
    assert result["action"] == "summarize"
    assert event_stream.load_active_projection()[2]["summary"] == "Evidence-backed summary."


def test_working_memory_need_context_retries_with_evidence_then_keeps_raw(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    responses = iter(
        [
            {"action": "need_context", "summary": "", "reason": "needs more"},
            {"action": "keep_raw", "summary": "", "reason": "still important with evidence"},
        ]
    )
    monkeypatch.setattr(working_memory, "_call_model_json", lambda _prompt: json.dumps(next(responses)))

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["action"] == "keep_raw"
    assert result["kept_raw_count"] == 1
    assert result["summarized_count"] == 0


def test_working_memory_repeated_need_context_downgrades_to_keep_raw(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "need_context", "summary": "", "reason": "still insufficient"}),
    )

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["action"] == "keep_raw"
    assert "need_context unresolved after evidence retrieval" in result["decision_reason"]


def test_working_memory_projection_uses_current_dynamic_stm_budget(isolated_workspace, monkeypatch):
    import memory_engine.wm.working_memory as working_memory

    captured = {}

    def fake_status(_messages, budget=10000, wm_ingress_watermark=-1):
        captured["budget"] = budget
        captured["watermark"] = wm_ingress_watermark
        return {"omitted_blocks": [], "omitted_block_count": 0}

    monkeypatch.setattr(working_memory.wm_ingress, "current_stm_budget", lambda _messages, _state: 4321)
    monkeypatch.setattr(working_memory.wm_ingress, "effective_wm_ingress_watermark", lambda _state: 17)
    monkeypatch.setattr(working_memory, "stm_projection_status", fake_status)

    status, slots = working_memory._omitted_slot_sequences([_message("user", "test")], {"turn": 1})

    assert status["omitted_block_count"] == 0
    assert slots == set()
    assert captured == {"budget": 4321, "watermark": 17}


def test_working_memory_status_uses_effective_stm_boundary_without_creating_state_tables(isolated_workspace):
    import sqlite3
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "locked user " * 40),
        _message("assistant", _long_text()),
        _message("user", "eligible user " * 40),
        _message("assistant", _long_text()),
        _message("user", "latest user"),
        _message("assistant", "latest assistant"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    runtime_state = {
        "context_pressure": {"budgets": {"stm": 10000}},
        "wm_ingress_watermark_slot_seq": 2,
    }

    status = working_memory.working_memory_status(messages, runtime_state=runtime_state)
    rendered = json.dumps(status, ensure_ascii=False)

    assert status["stm"]["wm_ingress_watermark"] == 2
    assert status["stm"]["watermark_locked_omitted_block_count"] == 1
    assert status["candidate_count"] == 1
    assert [candidate["slot_seq"] for candidate in status["current_candidates"]] == [2]
    assert "locked user" not in rendered
    assert "eligible user" not in rendered
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('working_memory_state', 'working_memory_slot_state')"
            ).fetchall()
        }
    assert tables == set()


def test_working_memory_status_explains_candidates_and_recent_processed(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request one"),
        _message("assistant", _long_text()),
        _message("user", "old request two"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_two_blocks)

    status = working_memory.working_memory_status(messages)

    assert status["candidate_count"] == 2
    assert status["candidate_tokens_total"] > 0
    assert status["active_raw_tokens"] > 0
    assert status["current_candidates"][0]["slot_seq"] == 2
    assert status["current_candidates"][0]["active_token_count"] > 400
    assert "Important detail" not in str(status)

    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Short summary.", "reason": "compress old detail"}),
    )
    working_memory.run_working_memory_maintenance(messages, force=True)

    rendered = working_memory.format_working_memory_status(working_memory.working_memory_status(messages))

    assert "current_candidates:" in rendered
    assert "recent_processed:" in rendered
    assert "last_candidate_policy:" in rendered
    assert "last_preflight_target:" in rendered
    assert "last_reprocessed_slot_seqs:" in rendered
    assert "last_fallback_candidate_count:" in rendered
    assert "summarized_count" in rendered
    assert "compression_ratio=" in rendered
    assert "compress old detail" in rendered
    assert "Important detail" not in rendered


def test_event_stream_can_append_after_active_sum(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Short summary.", "reason": "compress"}),
    )
    working_memory.run_working_memory_maintenance(messages, force=True)

    appended = [*messages, _message("user", "tail")]
    session.save_last_session(appended)

    assert session.load_last_session() == appended


def test_working_memory_ignores_short_candidates(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old"),
        _message("assistant", "short"),
        _message("user", "new"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    calls = []
    monkeypatch.setattr(working_memory, "_call_model_json", lambda _prompt: calls.append(_prompt))

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["reason"] == "no_candidates"
    assert calls == []


def test_working_memory_retries_once_when_summary_is_too_long(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    responses = iter(
        [
            json.dumps({"action": "summarize", "summary": "too long " * 1000, "reason": "first"}),
            json.dumps({"action": "summarize", "summary": "Short valid summary.", "reason": "second"}),
        ]
    )
    monkeypatch.setattr(working_memory, "_call_model_json", lambda _prompt: next(responses))

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["updated"] is True
    active = event_stream.load_active_projection()
    assert active[2]["summary"] == "Short valid summary."


def test_working_memory_failed_compression_keeps_raw(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "too long " * 1000, "reason": "still long"}),
    )

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["updated"] is False
    assert result["action"] == "keep_raw"
    assert event_stream.load_active_projection() == messages


def test_working_memory_need_context_is_recorded_as_keep_raw(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "need_context", "summary": "", "reason": "needs nearby messages"}),
    )

    result = working_memory.run_working_memory_maintenance(messages, force=True)

    assert result["updated"] is False
    assert result["action"] == "keep_raw"
    assert "need_context unresolved after evidence retrieval" in working_memory.working_memory_status(messages)["last_result"]["decision_reason"]
    assert event_stream.load_active_projection() == messages


def test_preflight_working_memory_recovery_summarizes_latest_oversized_block(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "previous short request"),
        _message("assistant", "previous short answer"),
        _message("user", _long_text()),
        _message("assistant", "short tail stays raw"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Preflight summary.", "reason": "oversized latest block"}),
    )

    result = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000."),
    )

    assert result["triggered"] is True
    assert result["preflight"] is True
    assert result["candidate_count"] == 1
    assert result["summarized_count"] == 1
    assert result["processed_slot_seqs"] == [3]
    active = event_stream.load_active_projection()
    assert active[3]["summary"] == "Preflight summary."
    assert active[4] == messages[4]


def test_preflight_working_memory_recovery_runs_once_and_can_keep_raw(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", _long_text()),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "need_context", "summary": "", "reason": "needs context"}),
    )

    result = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000."),
    )

    assert result["triggered"] is True
    assert result["processed_count"] == 1
    assert result["kept_raw_count"] == 1
    assert result["updated"] is False
    assert event_stream.load_active_projection() == messages


def test_preflight_recovery_can_reprocess_prior_kept_raw_slot(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", _long_text()),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    original_slot = event_stream.load_slot(1)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "need_context", "summary": "", "reason": "first pass keeps raw"}),
    )
    first = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000."),
    )
    assert first["kept_raw_count"] == 1
    assert event_stream.load_active_projection() == messages

    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Recovered summary.", "reason": "preflight retry"}),
    )

    second = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000."),
    )

    slot = event_stream.load_slot(1)
    active_event = event_stream.load_event(slot["active_event_id"])
    assert second["updated"] is True
    assert second["candidate_policy"] == "preflight_threshold_or_kept_raw"
    assert second["preflight_target"] == "stm_latest_block"
    assert second["reprocessed_slot_seqs"] == [1]
    assert slot["original_event_id"] == original_slot["original_event_id"]
    assert slot["active_event_id"] != slot["original_event_id"]
    assert active_event["kind"] == "sum"
    assert event_stream.load_active_projection()[1]["summary"] == "Recovered summary."
    assert event_stream.load_messages() == messages


def test_working_memory_maintenance_still_skips_prior_kept_raw_slot(isolated_workspace, monkeypatch):
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "keep_raw", "summary": "", "reason": "ordinary keep raw"}),
    )
    first = working_memory.run_working_memory_maintenance(messages, force=True)
    calls = []
    monkeypatch.setattr(working_memory, "_call_model_json", lambda prompt: calls.append(prompt))

    second = working_memory.run_working_memory_maintenance(messages, force=True)

    assert first["kept_raw_count"] == 1
    assert second["reason"] == "no_candidates"
    assert calls == []


def test_preflight_latest_block_fallback_selects_top_three_largest_subthreshold_slots(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", _medium_text("one")),
        _message("assistant", _medium_text("two") * 2),
        _message("tool", _medium_text("three") * 3),
        _message("assistant", _medium_text("four") * 4),
        _message("assistant", _medium_text("five") * 5),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    records = event_stream.load_active_trunk_records()
    for slot_seq in range(1, 6):
        assert records[slot_seq]["active_token_count"] <= working_memory.SUM_TRIGGER_TOKENS
    calls = []

    def fake_decision(prompt):
        payload = json.loads(prompt[1]["content"])
        calls.append(payload["candidate"]["slot_seq"])
        return json.dumps(
            {
                "action": "summarize",
                "summary": f"Fallback summary {payload['candidate']['slot_seq']}.",
                "reason": "fallback",
            }
        )

    monkeypatch.setattr(working_memory, "_call_model_json", fake_decision)

    result = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("STM latest interaction block exceeds budget: latest_block_tokens=12000, budget=10000."),
    )

    active = event_stream.load_active_projection()
    assert result["candidate_policy"] == "preflight_top_n_largest"
    assert result["fallback_candidate_count"] == 3
    assert result["processed_slot_seqs"] == [3, 4, 5]
    assert calls == [3, 4, 5]
    assert active[1] == messages[1]
    assert active[2] == messages[2]
    assert active[3]["summary"] == "Fallback summary 3."
    assert active[4]["summary"] == "Fallback summary 4."
    assert active[5]["summary"] == "Fallback summary 5."
    assert event_stream.event_stream_status()["sum_event_count"] == 3


def test_preflight_wm_slot_retries_exact_prior_kept_raw_slot(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "keep_raw", "summary": "", "reason": "ordinary keep raw"}),
    )
    working_memory.run_working_memory_maintenance(messages, force=True)
    original_slot = event_stream.load_slot(2)
    calls = []

    def fake_decision(prompt):
        payload = json.loads(prompt[1]["content"])
        calls.append(payload["candidate"]["slot_seq"])
        return json.dumps({"action": "summarize", "summary": "Exact slot summary.", "reason": "preflight exact retry"})

    monkeypatch.setattr(working_memory, "_call_model_json", fake_decision)

    result = working_memory.run_preflight_working_memory_recovery(
        messages,
        RuntimeError("Context Builder WM slot exceeds budget: slot_seq=2, tokens=7000, budget=6000."),
    )

    slot = event_stream.load_slot(2)
    assert result["candidate_policy"] == "preflight_exact_slot"
    assert result["preflight_target"] == "wm_slot"
    assert result["reprocessed_slot_seqs"] == [2]
    assert result["processed_slot_seqs"] == [2]
    assert calls == [2]
    assert slot["original_event_id"] == original_slot["original_event_id"]
    assert event_stream.load_event(slot["active_event_id"])["kind"] == "sum"
    assert event_stream.load_active_projection()[2]["summary"] == "Exact slot summary."


def test_messages_for_model_uses_context_builder_after_working_memory(isolated_workspace, monkeypatch):
    from persistence import session
    import runtime.context as runtime_context
    import memory_engine.wm.working_memory as working_memory

    messages = [
        _message("system", "system"),
        _message("user", "old request"),
        _message("assistant", _long_text()),
        _message("user", "new request"),
        _message("assistant", "new answer"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    monkeypatch.setattr(working_memory, "stm_projection_status", _omitted_first_block)
    monkeypatch.setattr(
        working_memory,
        "_call_model_json",
        lambda _prompt: json.dumps({"action": "summarize", "summary": "Short summary.", "reason": "compress"}),
    )
    working_memory.run_working_memory_maintenance(messages, force=True)
    projected = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "<working-memory>\n[2] sema summary: Short summary.\n</working-memory>"},
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]
    monkeypatch.setattr(runtime_context, "build_event_stream_context", lambda _messages, runtime_state=None: {"messages": projected})

    outbound = runtime_context.messages_for_model(messages)

    assert outbound == projected


def test_working_memory_model_call_does_not_use_provider_context_cache(monkeypatch):
    import memory_engine.wm.working_memory as working_memory

    captured = {}

    def fake_call(_messages, **kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(working_memory, "call_chat_completion", fake_call)

    working_memory._call_model_json([_message("user", "summarize")])

    assert "payload_observer" not in captured
    assert "payload_metadata" not in captured
