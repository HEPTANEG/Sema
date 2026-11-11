import sqlite3
import pytest


def _messages(text="hello"):
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": text},
    ]


def test_event_stream_default_project_config(isolated_workspace):
    from persistence import session

    project = session.ensure_project()
    info = session.get_project_info()

    assert "memory_architecture" not in project
    assert "memory_architecture" not in info
    assert info["event_stream_dir"] == isolated_workspace / ".sema" / "memory" / "event_stream"


def test_event_stream_save_load_and_no_jsonl_mirror(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    messages = _messages("event stream")
    session.ensure_project()
    session.save_last_session(messages)

    assert session.load_last_session() == messages
    assert event_stream.event_stream_db_path().exists()
    assert not (isolated_workspace / ".sema" / "conversations").exists()
    assert session.get_project_info()["event_stream_message_count"] == 2


def test_event_stream_appends_only_new_tail_messages(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    messages = _messages("first")
    session.ensure_project()
    session.save_last_session(messages)
    session.save_last_session([*messages, {"role": "assistant", "content": "second"}])

    loaded = event_stream.load_messages()

    assert loaded == [*messages, {"role": "assistant", "content": "second"}]
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        rows = connection.execute("SELECT slot_seq FROM slots ORDER BY slot_seq ASC").fetchall()
    assert [row[0] for row in rows] == [0, 1, 2]


def test_event_stream_single_message_append_is_immediate_and_idempotent_checkpoint(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    session.ensure_project()
    messages = _messages("first")
    session.save_last_session(messages)

    session.append_message_and_persist(messages, {"role": "assistant", "content": "answer"})
    session.save_last_session(messages)
    session.save_last_session(messages)

    assert event_stream.load_messages() == messages
    assert event_stream.event_stream_message_count() == len(messages)


def test_event_stream_append_failure_does_not_expose_message_in_memory(isolated_workspace, monkeypatch):
    import memory_engine.event_stream as event_stream
    from persistence import session

    session.ensure_project()
    messages = _messages("saved")
    session.save_last_session(messages)
    before = list(messages)

    monkeypatch.setattr(event_stream, "append_message", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("disk full")))

    with pytest.raises(RuntimeError, match="disk full"):
        session.append_message_and_persist(messages, {"role": "user", "content": "blocked"})

    assert messages == before
    assert event_stream.load_messages() == before


def test_event_stream_rejects_slot_rewrite(isolated_workspace):
    import pytest
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages("first"))

    with pytest.raises(RuntimeError, match="rewriting existing slots"):
        session.save_last_session(_messages("changed"))


def test_event_stream_uses_inline_and_artifact_content(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    large_text = "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "tool", "tool_call_id": "call_1", "content": large_text},
    ]
    session.ensure_project()
    session.save_last_session(messages)

    assert session.load_last_session() == messages
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        inline_count = connection.execute("SELECT COUNT(*) FROM events WHERE content_ref = 'inline'").fetchone()[0]
        artifact_count = connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]

    assert inline_count == 1
    assert artifact_count == 1
    assert len(list(event_stream.artifacts_dir().glob("*.json"))) == 1


def test_event_stream_uses_explicit_semantic_source_without_changing_provider_storage_kind(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    wake = {
        "role": "user",
        "source": "retriever",
        "content": "A Runtime Wake payload.",
    }
    large_tool = {
        "role": "tool",
        "source": "maintainer",
        "tool_call_id": "call_1",
        "content": "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100),
    }
    session.ensure_project()
    session.save_last_session([wake, large_tool])

    assert event_stream.load_slot(0)["source"] == "retriever"
    assert event_stream.load_slot(1)["source"] == "maintainer"
    assert event_stream.load_event(event_stream.load_slot(1)["active_event_id"])["kind"] == "ref"
    artifact = event_stream.load_artifact(event_stream.load_event(event_stream.load_slot(1)["active_event_id"])["content_ref"])
    assert artifact["artifact_type"] == "tool_output"


def test_event_stream_status_reports_storage_counts(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    large_text = "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "tool", "tool_call_id": "call_1", "content": large_text},
    ]
    session.ensure_project()
    session.save_last_session(messages)

    status = event_stream.event_stream_status()

    assert status["slot_count"] == 2
    assert status["event_count"] == 2
    assert status["inline_event_count"] == 1
    assert status["artifact_event_count"] == 1
    assert status["artifact_count"] == 1
    assert status["artifact_bytes"] > 0


def test_event_stream_inspect_helpers_load_slot_event_and_artifact(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    large_text = "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "tool", "tool_call_id": "call_1", "content": large_text},
    ]
    session.ensure_project()
    session.save_last_session(messages)

    slot = event_stream.load_slot(1)
    event = event_stream.load_event(slot["active_event_id"])
    artifact = event_stream.load_artifact(event["content_ref"])

    assert slot["slot_seq"] == 1
    assert event["kind"] == "ref"
    assert artifact["content"] == messages[1]


def test_event_stream_evidence_inspects_active_and_original_slot(isolated_workspace):
    import memory_engine.event_stream as event_stream
    import memory_engine.evidence as evidence
    from persistence import session

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "verbose original body"},
    ]
    session.ensure_project()
    session.save_last_session(messages)
    event_stream.create_sum_event(
        2,
        {
            "source_role": "assistant",
            "summary": "compact body",
            "reason": "test",
            "original_token_count": 10,
            "summary_token_count": 2,
        },
    )

    active = evidence.inspect_slot(2, projection="active")
    original = evidence.inspect_slot(2, projection="original")

    assert active["ok"] is True
    assert original["ok"] is True
    assert active["evidence"][0]["event_kind"] == "sum"
    assert active["evidence"][0]["content_preview"] == "compact body"
    assert original["evidence"][0]["event_kind"] == "raw"
    assert original["evidence"][0]["content_preview"] == "verbose original body"


def test_event_stream_evidence_range_block_and_expand_are_bounded(isolated_workspace):
    import memory_engine.evidence as evidence
    from persistence import session

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "user", "content": "second user"},
        {"role": "assistant", "content": "second assistant"},
        {"role": "user", "content": "third user"},
        {"role": "assistant", "content": "third assistant"},
    ]
    session.ensure_project()
    session.save_last_session(messages)

    range_result = evidence.inspect_range(1, 2, max_chars=5)
    block_result = evidence.inspect_interaction_block(3)
    expanded = evidence.expand_around(3, before_blocks=1, after_blocks=1)

    assert range_result["ok"] is True
    assert [item["slot_seq"] for item in range_result["evidence"]] == [1, 2]
    assert range_result["evidence"][0]["content_preview"] == "first"
    assert range_result["evidence"][0]["truncated"] is True
    assert [item["slot_seq"] for item in block_result["evidence"]] == [3, 4]
    assert [item["slot_seq"] for item in expanded["evidence"]] == [1, 2, 3, 4, 5, 6]


def test_event_stream_evidence_inspects_artifact_preview(isolated_workspace):
    import memory_engine.event_stream as event_stream
    import memory_engine.evidence as evidence
    from persistence import session

    large_text = "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100)
    session.ensure_project()
    session.save_last_session(
        [
            {"role": "system", "content": "system"},
            {"role": "tool", "tool_call_id": "call_1", "content": large_text},
        ]
    )
    artifact_id = event_stream.load_event(event_stream.load_slot(1)["active_event_id"])["content_ref"]

    result = evidence.inspect_artifact(artifact_id, max_chars=20)

    assert result["ok"] is True
    assert result["artifact"]["artifact_id"] == artifact_id
    assert result["artifact"]["content_preview"] == "x" * 20
    assert result["artifact"]["truncated"] is True


def test_event_stream_evidence_missing_items_return_structured_errors(isolated_workspace):
    import memory_engine.evidence as evidence
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages("hello"))

    missing_slot = evidence.inspect_slot(999)
    missing_artifact = evidence.inspect_artifact("art_missing")

    assert missing_slot["ok"] is False
    assert "slot not found" in missing_slot["errors"][0]["message"]
    assert missing_artifact["ok"] is False
    assert "artifact not found" in missing_artifact["errors"][0]["message"]


def test_event_stream_integrity_check_reports_ok(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages("healthy"))

    result = event_stream.check_integrity()

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["counts"]["slot_count"] == 2


def test_event_stream_integrity_check_reports_artifact_hash_error(isolated_workspace):
    import memory_engine.event_stream as event_stream
    from persistence import session

    large_text = "x" * (event_stream.INLINE_CONTENT_MAX_CHARS + 100)
    session.ensure_project()
    session.save_last_session(
        [
            {"role": "system", "content": "system"},
            {"role": "tool", "tool_call_id": "call_1", "content": large_text},
        ]
    )
    artifact_path = next(event_stream.artifacts_dir().glob("*.json"))
    artifact_path.write_text("corrupted", encoding="utf-8")

    result = event_stream.check_integrity()

    assert result["ok"] is False
    assert any(error["check"] == "artifact_hash" for error in result["errors"])


def test_event_stream_memory_status_and_commands(isolated_workspace):
    from persistence import session
    from commands.structured_commands import execute_slash_command, slash_command_list

    session.ensure_project()
    messages = _messages()
    permissions = {}
    runtime_state = {}

    names = {record["name"] for record in slash_command_list(developer_mode=True)}
    assert "/memory architecture" not in names
    assert "/memory context status" in names
    assert "/memory budget status" in names
    assert "/memory wm status" in names
    assert "/memory focus status" in names

    runtime_state["developer_mode"] = True
    help_result = execute_slash_command("/help", messages, permissions, runtime_state, lambda: _messages())
    assert "/memory context status" in help_result["content"]
    assert "/memory wm status" in help_result["content"]
    assert "/memory focus status" in help_result["content"]
    assert "/memory architecture" not in help_result["content"]

    status = execute_slash_command("/memory status", messages, permissions, runtime_state, lambda: _messages())
    assert "memory_root:" in status["content"]
    assert "Removed legacy layers" not in status["content"]
    assert "Summary Memory" not in status["content"]
    assert "Topic System" not in status["content"]
    event_status = execute_slash_command("/memory event-stream status", messages, permissions, runtime_state, lambda: _messages())
    assert "Event Stream status:" in event_status["content"]
    event_check = execute_slash_command("/memory event-stream check", messages, permissions, runtime_state, lambda: _messages())
    assert "ok: true" in event_check["content"]
    stm_status = execute_slash_command("/memory stm status", messages, permissions, runtime_state, lambda: _messages())
    assert "STM Projection status:" in stm_status["content"]
    assert "selected_unit_count:" in stm_status["content"]
    assert "hello" not in stm_status["content"]
    context_status = execute_slash_command("/memory context status", messages, permissions, runtime_state, lambda: _messages())
    assert "Context Builder status:" in context_status["content"]
    assert "context_order:" in context_status["content"]
    assert "budget_source: dynamic" in context_status["content"]
    assert "pressure_I:" in context_status["content"]
    assert "hello" not in context_status["content"]
    budget_status = execute_slash_command("/memory budget status", messages, permissions, runtime_state, lambda: _messages())
    assert "Context Pressure status:" in budget_status["content"]
    assert "stm_budget:" in budget_status["content"]
    assert "P_context:" in budget_status["content"]
    assert "hello" not in budget_status["content"]
    set_budget = execute_slash_command("/memory budget set stm 3000", messages, permissions, runtime_state, lambda: _messages())
    assert "budget_source: manual_max_override" in set_budget["content"]
    stm_budget = int(next(line.split(":", 1)[1] for line in set_budget["content"].splitlines() if line.startswith("stm_budget:")))
    assert 1000 <= stm_budget <= 3000
    assert "stm_max_override: 3000" in set_budget["content"]
    reset_budget = execute_slash_command("/memory budget reset", messages, permissions, runtime_state, lambda: _messages())
    assert "budget_source: dynamic" in reset_budget["content"]
    assert "stm_max_override: None" in reset_budget["content"]
    wm_status = execute_slash_command("/memory wm status", messages, permissions, runtime_state, lambda: _messages())
    assert "Working Memory status:" in wm_status["content"]
    assert "candidate_count:" in wm_status["content"]
    focus_status = execute_slash_command("/memory focus status", messages, permissions, runtime_state, lambda: _messages())
    assert "Focus Segment status:" in focus_status["content"]
    assert "active: false" in focus_status["content"]
    assert "segment_count: 0" in focus_status["content"]
    assert "hello" not in focus_status["content"]

def test_event_stream_tool_schemas_expose_current_tools(isolated_workspace):
    from runtime import agent
    from persistence import session

    session.ensure_project()

    names = {schema["function"]["name"] for schema in agent.tool_schemas_for_model({})}

    assert "read_file" in names
    assert "get_overview" in names
    assert "run_command" in names
    assert "show_undo_records" in names
    assert "show_skills" not in names
    assert "show_task_state" not in names
    assert "show_memory_state" not in names

def test_event_stream_stm_status_reports_projection_error(isolated_workspace):
    from persistence import session
    from commands.structured_commands import execute_slash_command

    session.ensure_project()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "测试" * 60000},
    ]

    result = execute_slash_command("/memory stm status", messages, {}, {}, lambda: messages)

    assert "STM Projection status:" in result["content"]
    assert "status: error" in result["content"]
    assert "Context capacity exceeded: target=stm" in result["content"]


def test_event_stream_context_status_reports_projection_error(isolated_workspace, monkeypatch):
    from persistence import session
    from commands import structured_commands
    from commands.structured_commands import execute_slash_command

    session.ensure_project()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    monkeypatch.setattr(
        structured_commands,
        "context_status",
        lambda _messages, runtime_state=None: (_ for _ in ()).throw(RuntimeError("Context Builder WM slot exceeds budget: slot_seq=1, tokens=7000, budget=6000.")),
    )

    result = execute_slash_command("/memory context status", messages, {}, {}, lambda: messages)

    assert "Context Builder status:" in result["content"]
    assert "status: error" in result["content"]
    assert "Context Builder WM slot exceeds budget" in result["content"]


def test_event_stream_stm_status_shows_omitted_unit_metadata(isolated_workspace):
    from persistence import session
    from commands.structured_commands import execute_slash_command

    session.ensure_project()
    old_text = "第一篇旧内容" * 2000
    latest_text = "第二篇最新正文"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": old_text},
        {"role": "user", "content": "latest request"},
        {"role": "assistant", "content": latest_text},
    ]

    result = execute_slash_command("/memory stm status", messages, {}, {}, lambda: messages)

    assert "omitted_units:" in result["content"]
    assert "range=1..1" in result["content"]
    assert "tokens=" in result["content"]
    assert old_text not in result["content"]
    assert latest_text not in result["content"]


def test_unknown_memory_command_uses_standard_error(isolated_workspace):
    from persistence import session
    from commands.structured_commands import execute_slash_command, slash_command_list

    session.ensure_project()

    with pytest.raises(RuntimeError, match="Unknown /memory command"):
        execute_slash_command("/memory unavailable", _messages(), {}, {}, lambda: _messages())

    assert not (isolated_workspace / ".sema" / "conversations").exists()
    names = {record["name"] for record in slash_command_list(developer_mode=True)}
    assert "/memory architecture" not in names


def test_event_stream_status_commands_remain_available(isolated_workspace):
    from persistence import session
    from commands.structured_commands import execute_slash_command

    session.ensure_project()

    result = execute_slash_command("/memory event-stream status", _messages(), {}, {}, lambda: _messages())
    context_result = execute_slash_command("/memory context status", _messages(), {}, {}, lambda: _messages())
    budget_result = execute_slash_command("/memory budget status", _messages(), {}, {}, lambda: _messages())
    stm_result = execute_slash_command("/memory stm status", _messages(), {}, {}, lambda: _messages())
    wm_result = execute_slash_command("/memory wm status", _messages(), {}, {}, lambda: _messages())
    focus_result = execute_slash_command("/memory focus status", _messages(), {}, {}, lambda: _messages())

    assert "Event Stream status:" in result["content"]
    assert "Context Builder status:" in context_result["content"]
    assert "Context Pressure status:" in budget_result["content"]
    assert "STM Projection status:" in stm_result["content"]
    assert "Working Memory status:" in wm_result["content"]
    assert "Focus Segment status:" in focus_result["content"]


def test_event_stream_messages_for_model_uses_context_builder_only(isolated_workspace):
    from runtime.context import messages_for_model
    from persistence import session

    session.ensure_project()
    (isolated_workspace / "SEMA.md").write_text("Project instruction should be disabled.", encoding="utf-8")
    messages = _messages("hello")

    assert messages_for_model(messages) == messages


def test_event_stream_model_runtime_builds_without_retrieval_plan(isolated_workspace):
    from runtime import model_runtime
    from persistence import session

    session.ensure_project()
    calls = []
    dependencies = {
        "trace_retrieval_plan": lambda _state, plan: calls.append(("trace", plan)),
        "messages_with_runtime_directives": lambda messages, _state: messages,
        "messages_for_model": lambda messages, retrieval_plan=None, runtime_state=None: messages,
        "tool_schemas_for_model": lambda _state=None: [],
        "build_chat_payload": lambda messages, tools=None: {"messages": messages, "tools": tools},
    }

    payload = model_runtime.build_model_tools_payload(_messages(), dependency_source=dependencies)

    assert payload["messages"] == _messages()
    assert payload["tools"] == []
    assert calls == [("trace", None)]


def test_event_stream_background_tasks_run_event_stream_maintenance(isolated_workspace):
    from runtime import background_tasks
    from persistence import session

    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    session.ensure_project()
    calls = []
    result = background_tasks.schedule_event_stream_memory_maintenance(
        _messages(),
        {"ui_event_sink": None},
        dependency_source={
            "ui_event": lambda *args, **kwargs: calls.append((args, kwargs)),
            "trace_event": lambda *args, **kwargs: calls.append(("trace", args, kwargs)),
            "threading": type("Threading", (), {"Thread": ImmediateThread}),
            "stage_wm_ingress_from_messages": lambda current, state=None: calls.append(("stage", list(current), state)) or {"staged_watermark": 2, "stm_budget": 10000, "omitted_unit_count": 1},
            "commit_staged_wm_ingress_watermark": lambda state=None: calls.append(("commit", state)) or {"updated": True, "persisted_watermark": 2, "staged_watermark": 2, "effective_watermark": 2},
            "run_working_memory_maintenance": lambda current, runtime_state=None: calls.append(("wm", list(current))) or {"triggered": True, "updated": True, "reason": "wm"},
            "run_segment_ingest_maintenance": lambda current, state=None: calls.append(("ingest", list(current), state)) or {"triggered": True, "updated": True, "reason": "ingest", "processed_block_count": 1, "assigned_count": 1, "unassigned_count": 0, "resolved_count": 0},
            "run_full_segment_maintenance": lambda current, state=None: calls.append(("full", list(current), state)) or {"triggered": False, "updated": False, "reason": "not_needed"},
            "run_formation_maintenance": lambda current, runtime_state=None: calls.append(("formation", list(current), runtime_state)) or {"triggered": False, "reason": "not_needed"},
            "run_formation_backfill": lambda current, runtime_state=None: calls.append(("historical", list(current), runtime_state)) or {"triggered": False, "reason": "not_needed", "status": "completed"},
            "run_focus_residency_reconciliation": lambda current, state=None: calls.append(("reconciliation", list(current), state)) or {"triggered": True, "updated": False, "reason": "already_reconciled"},
            "run_focus_range_retirement": lambda current, state=None, pre_audit_report=None: calls.append(("retirement", list(current), state, pre_audit_report)) or {"triggered": True, "updated": False, "reason": "no_eligible_focus_ranges"},
            "run_focus_consistency_audit": lambda current, state=None, persist=False: calls.append(("audit", list(current), state, persist)) or {
                "status": "ok",
                "error_count": 0,
                "warning_count": 0,
                "repairable_issue_count": 0,
                "should_trigger_full_maintainer": False,
            },
            },
        )

    assert result.daemon is True
    assert ("wm", _messages()) in calls
    assert ("ingest", _messages(), {"ui_event_sink": None}) in calls
    assert calls.index(("stage", _messages(), {"ui_event_sink": None})) < calls.index(("commit", {"ui_event_sink": None}))
    assert calls.index(("commit", {"ui_event_sink": None})) < calls.index(("wm", _messages()))
    assert calls.index(("wm", _messages())) < calls.index(("ingest", _messages(), {"ui_event_sink": None}))
    assert calls.index(("formation", _messages(), {"ui_event_sink": None})) < calls.index(
        ("historical", _messages(), {"ui_event_sink": None})
    )
    pre_audit = ("audit", _messages(), {"ui_event_sink": None}, False)
    retirement_call = next(call for call in calls if isinstance(call, tuple) and call[0] == "retirement")
    final_audit = ("audit", _messages(), {"ui_event_sink": None}, True)
    reconciliation = ("reconciliation", _messages(), {"ui_event_sink": None})
    assert calls.index(("historical", _messages(), {"ui_event_sink": None})) < calls.index(reconciliation)
    assert calls.index(reconciliation) < calls.index(pre_audit)
    assert calls.index(pre_audit) < calls.index(retirement_call) < calls.index(final_audit)
    lifecycle = [
        call[0][1]
        for call in calls
        if len(call) == 2 and isinstance(call[0], tuple) and len(call[0]) > 1 and call[0][1] in {
            "segment_ingest_started",
            "segment_ingest_finished",
            "full_segment_maintenance_started",
            "full_segment_maintenance_finished",
            "ltm_formation_started",
            "ltm_formation_finished",
            "formation_backfill_started",
            "formation_backfill_finished",
        }
    ]
    assert lifecycle == [
        "segment_ingest_started",
        "segment_ingest_finished",
        "full_segment_maintenance_started",
        "full_segment_maintenance_finished",
        "ltm_formation_started",
        "ltm_formation_finished",
        "formation_backfill_started",
        "formation_backfill_finished",
    ]


def test_focus_segment_background_task_emits_ingester_lifecycle(isolated_workspace):
    from runtime import background_tasks

    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    calls = []
    messages = _messages()
    state = {"trace_id": "trace-1"}

    result = background_tasks.schedule_focus_segment_maintenance(
        messages,
        state,
        dependency_source={
            "ui_event": lambda *args, **kwargs: calls.append(("ui", args[1], kwargs)),
            "trace_event": lambda *_args, **kwargs: calls.append(("trace", kwargs)),
            "threading": type("Threading", (), {"Thread": ImmediateThread}),
            "run_focus_segment_maintenance": lambda current, runtime_state: calls.append(
                ("worker", list(current), runtime_state)
            ) or {"triggered": True, "updated": True, "reason": "updated"},
        },
    )

    assert result.daemon is True
    ui_events = [call for call in calls if call[0] == "ui"]
    assert [event[1] for event in ui_events] == [
        "segment_ingest_started",
        "segment_ingest_finished",
        "focus_segment_status",
    ]
    assert calls.index(("worker", messages, state)) > 0
    assert ui_events[1][2]["status"] == "ok"


def test_event_stream_background_tasks_emit_focus_ingester_and_maintainer_lifecycle(isolated_workspace):
    from runtime import background_tasks

    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    calls = []
    background_tasks.schedule_event_stream_memory_maintenance(
        _messages(),
        {},
        dependency_source={
            "ui_event": lambda *args, **kwargs: calls.append({"type": args[1], **kwargs}),
            "trace_event": lambda *_args, **_kwargs: None,
            "threading": type("Threading", (), {"Thread": ImmediateThread}),
            "stage_wm_ingress_from_messages": lambda *_args, **_kwargs: {},
            "commit_staged_wm_ingress_watermark": lambda *_args, **_kwargs: {},
            "run_working_memory_maintenance": lambda *_args, **_kwargs: {},
            "run_segment_ingest_maintenance": lambda *_args, **_kwargs: {},
            "run_full_segment_maintenance": lambda *_args, **_kwargs: {},
            "run_formation_maintenance": lambda *_args, **_kwargs: {},
            "run_formation_backfill": lambda *_args, **_kwargs: {},
            "run_focus_residency_reconciliation": lambda *_args, **_kwargs: {},
            "run_focus_range_retirement": lambda *_args, **_kwargs: {},
            "run_focus_consistency_audit": lambda *_args, **_kwargs: {"status": "ok"},
        },
    )

    lifecycle = [event["type"] for event in calls if event.get("type") in {
        "segment_ingest_started",
        "segment_ingest_finished",
        "full_segment_maintenance_started",
        "full_segment_maintenance_finished",
    }]
    assert lifecycle == [
        "segment_ingest_started",
        "segment_ingest_finished",
        "full_segment_maintenance_started",
        "full_segment_maintenance_finished",
    ]


def test_event_stream_background_tasks_fail_open_after_retirement_error(isolated_workspace):
    from runtime import background_tasks

    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    calls = []

    def audit(_current, _state=None, persist=False):
        calls.append(("audit", persist))
        return {
            "status": "ok",
            "error_count": 0,
            "warning_count": 0,
            "repairable_issue_count": 0,
            "should_trigger_full_maintainer": False,
            "issues": [],
        }

    background_tasks.schedule_event_stream_memory_maintenance(
        _messages(),
        {},
        dependency_source={
            "ui_event": lambda *_args, **_kwargs: None,
            "trace_event": lambda *_args, **_kwargs: None,
            "threading": type("Threading", (), {"Thread": ImmediateThread}),
            "stage_wm_ingress_from_messages": lambda *_args, **_kwargs: {},
            "commit_staged_wm_ingress_watermark": lambda *_args, **_kwargs: {},
            "run_working_memory_maintenance": lambda *_args, **_kwargs: {},
            "run_segment_ingest_maintenance": lambda *_args, **_kwargs: {},
            "run_full_segment_maintenance": lambda *_args, **_kwargs: {},
            "run_formation_maintenance": lambda *_args, **_kwargs: {},
            "run_formation_backfill": lambda *_args, **_kwargs: {},
            "run_focus_residency_reconciliation": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("reconciliation failed")
            ),
            "run_focus_consistency_audit": audit,
            "run_focus_range_retirement": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("retirement failed")
            ),
        },
    )

    assert calls == [("audit", False), ("audit", True)]
