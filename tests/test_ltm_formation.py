import json
import sqlite3

import pytest

from memory_engine.focus import store as focus_store


def _tool_call(name, arguments, call_id="formation-call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _messages():
    return [
        {"role": "user", "content": "I prefer concise technical answers."},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": "Please remember that this project uses an Event Stream memory architecture."},
        {"role": "assistant", "content": "I will keep that as a durable project fact."},
    ]


def _system_root_tags():
    from memory_engine.ltm import meta_memory

    return [
        {
            "tag_key": key,
            "object_id": meta_memory.system_tag_object_id(key),
            "title": title,
        }
        for key, title in sorted(meta_memory.SYSTEM_TAG_DEFINITIONS)
    ]


def _outgoing_links(*tag_keys):
    from memory_engine.ltm import meta_memory

    return [
        {
            "relation_type": "tagged_with",
            "target_object_id": meta_memory.system_tag_object_id(tag_key),
        }
        for tag_key in tag_keys
    ]


def _object_draft(title="Durable memory", object_type="assertion", tag_keys=("others",)):
    return {
        "object_type": object_type,
        "title": title,
        "summary": None,
        "payload": {
            "schema_version": 1,
            "body": f"{title} body.",
            "structured_data": None,
        },
        "outgoing_links": _outgoing_links(*tag_keys),
    }


def _normalization_item():
    return {
        "pending_slot_ranges": json.dumps([{"start": 0, "end": 3}]),
    }


def _prepare_segment():
    from memory_engine import event_stream

    with event_stream.ensure_event_stream() as connection:
        focus_store._ensure_tables(connection)
        segment_id = focus_store._create_segment(
            connection,
            "Memory architecture",
            "The current project memory architecture and its durable design constraints.",
        )
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 0, "end": 3}])
        connection.commit()
    return segment_id


def test_formation_prompt_includes_core_store_payload_size_contract():
    from shared.prompt_loader import load_prompt

    prompt = load_prompt("formation")

    assert "At or below 2 KiB: summary must be null" in prompt
    assert "Above 2 KiB and at or below 8 KiB: summary is required" in prompt
    assert "Above 8 KiB: split" in prompt
    assert "UTF-8 byte size" in prompt
    assert "system_root_tags" in prompt
    assert "outgoing_links" in prompt
    assert "Others is only a fallback" in prompt


def test_formation_projection_contains_only_minimal_system_root_metadata():
    from memory_engine.ltm import formation

    item = {
        "source_kind": "unassigned",
        "segment_status": "unassigned",
        "segment_title": "",
        "segment_summary": "",
        "pending_slot_ranges": "[]",
        "segment_ranges": "[]",
    }
    projection = formation._agent_projection(item, [], _system_root_tags())

    assert len(projection["system_root_tags"]) == 4
    assert [tag["tag_key"] for tag in projection["system_root_tags"]] == [
        "others",
        "safety",
        "sema_self",
        "user_model",
    ]
    assert all(set(tag) == {"object_id", "tag_key", "title"} for tag in projection["system_root_tags"])


def test_historical_session_buffer_uses_explicit_session_range(isolated_workspace):
    from memory_engine.ltm import formation
    from persistence import session

    session.ensure_project()
    session.save_last_session(_messages())

    assert formation._historical_session_slots({"start": 1, "end": 3}, root=isolated_workspace) == {1, 2, 3}
    assert formation._historical_session_slots({"start": 0, "end": 3}, root=isolated_workspace) == {0, 1, 2, 3}
    assert formation.maintainable_slot_frontier(
        root=isolated_workspace,
        historical_slot_range={"start": 1, "end": 3},
    ) == 3


def test_formation_normalizes_independent_single_and_multi_root_links():
    from memory_engine.ltm import formation

    decision = {
        "action": "write_plan",
        "objects": [
            _object_draft("User preference", tag_keys=("user_model",)),
            _object_draft("Safety preference", object_type="episode", tag_keys=("safety", "user_model")),
            _object_draft("Project category", object_type="tag", tag_keys=("others",)),
        ],
    }
    plan = formation._normalized_write_plan(_normalization_item(), decision, _system_root_tags())

    assert len(plan["objects"]) == 3
    object_ids = [obj["object_id"] for obj in plan["objects"]]
    tag_links = [link for link in plan["links"] if link["relation_type"] == "tagged_with"]
    provenance_links = [link for link in plan["links"] if link["relation_type"] == "sourced_from"]
    assert len(tag_links) == 4
    assert len(provenance_links) == 3
    assert {link["source"]["object_id"] for link in tag_links} == set(object_ids)
    first_targets = [
        link["target"]["object_id"]
        for link in tag_links
        if link["source"]["object_id"] == object_ids[0]
    ]
    assert first_targets == [_outgoing_links("user_model")[0]["target_object_id"]]


@pytest.mark.parametrize(
    ("outgoing_links", "error_code"),
    [
        (None, "missing_root_tag_link"),
        ([], "missing_root_tag_link"),
        ([{"relation_type": "supports", "target_object_id": "mem_" + "a" * 32}], "invalid_formation_relation"),
        ([{"relation_type": "tagged_with", "target_object_id": "mem_" + "a" * 32}], "invalid_system_root_target"),
        (_outgoing_links("safety", "safety"), "duplicate_root_tag_link"),
        (_outgoing_links("others", "safety"), "others_conflict"),
    ],
)
def test_formation_rejects_invalid_root_link_intents(outgoing_links, error_code):
    from memory_engine.ltm import formation

    obj = _object_draft()
    obj["outgoing_links"] = outgoing_links
    with pytest.raises(RuntimeError) as caught:
        formation._normalized_write_plan(
            _normalization_item(),
            {"action": "write_plan", "objects": [obj]},
            _system_root_tags(),
        )

    errors = json.loads(str(caught.value))
    assert error_code in {error["code"] for error in errors}


def test_formation_creates_work_item_and_commits_runtime_provenance(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))

    runtime_state = {
        "eval_formation_responses": [
            {
                "content": "",
                "tool_calls": [
                    _tool_call(
                        "ltm_select_objects",
                        {"object_types": ["assertion"], "projection": "title_summary"},
                    )
                ],
            },
            {
                "content": json.dumps(
                    {
                        "action": "write_plan",
                        "objects": [
                            {
                                "object_type": "assertion",
                                "title": "Project uses Event Stream memory",
                                "summary": None,
                                "payload": {
                                    "schema_version": 1,
                                    "body": "The project uses an Event Stream memory architecture.",
                                    "structured_data": None,
                                },
                                "outgoing_links": _outgoing_links("sema_self"),
                            }
                        ],
                    }
                )
            }
        ]
    }
    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)

    assert result["triggered"] is True
    assert result["completed_count"] == 1
    assert result["object_count"] == 1
    assert result["model_round_count"] == 2
    assert result["retrieval_call_count"] == 1
    assert result["retrieval_success_count"] == 1
    assert result["terminal_actions"] == ["write_plan"]
    objects = ltm_store.list_objects("assertion")
    assert len(objects) == 1
    links = ltm_store.get_links(objects[0]["object_id"], "outgoing")
    assert {link["relation_type"] for link in links} == {"sourced_from", "tagged_with"}
    provenance = [link for link in links if link["relation_type"] == "sourced_from"]
    tag_links = [link for link in links if link["relation_type"] == "tagged_with"]
    assert [(link["slot_seq_start"], link["slot_seq_end"]) for link in provenance] == [(0, 3)]
    assert tag_links[0]["target_object_id"] == _outgoing_links("sema_self")[0]["target_object_id"]

    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        row = connection.execute(
            "SELECT status, attempts, segment_status, pending_slot_ranges FROM formation_work_items"
        ).fetchone()
    assert row[0] == "completed"
    assert row[1] == 1
    assert row[2] == "active"
    assert json.loads(row[3]) == [{"start": 0, "end": 3}]


@pytest.mark.parametrize("existing_status", ["pending", "completed"])
def test_formation_deduplicates_slots_across_unassigned_and_segment_sources(
    isolated_workspace,
    monkeypatch,
    existing_status,
):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1}, 100))
    with event_stream.ensure_event_stream() as connection:
        formation._ensure_tables(connection)
        focus_store._ensure_tables(connection)
        formation._create_work_item(
            connection,
            "unassigned",
            None,
            [{"start": 0, "end": 1}],
            formation.FORMATION_PRIORITIES["unassigned"],
        )
        connection.execute("UPDATE formation_work_items SET status = ?", (existing_status,))
        segment_id = focus_store._create_segment(connection, "Recovered topic", "Recovered summary", "inactive")
        focus_store._update_segment(connection, segment_id, ranges=[{"start": 0, "end": 1}])
        connection.commit()

    discovery = formation._refresh_work_items(_messages())
    with event_stream.ensure_event_stream() as connection:
        rows = connection.execute("SELECT source_kind, status FROM formation_work_items").fetchall()

    assert discovery["created_count"] == 0
    assert [(row["source_kind"], row["status"]) for row in rows] == [("unassigned", existing_status)]


def test_completed_formation_does_not_advance_maintenance_coverage(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store, maintenance_observation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    runtime_state = {
        "eval_formation_responses": [
            {
                "content": "",
                "tool_calls": [
                    _tool_call(
                        "ltm_select_objects",
                        {"object_types": ["assertion"], "projection": "title_summary"},
                    )
                ],
            },
            {
                "content": json.dumps(
                    {
                        "action": "write_plan",
                        "objects": [
                            {
                                "object_type": "assertion",
                                "title": "Formation output is maintainer input",
                                "summary": None,
                                "payload": {
                                    "schema_version": 1,
                                    "body": "Formation output should not move the LTM maintenance cursor.",
                                    "structured_data": None,
                                },
                                "outgoing_links": _outgoing_links("sema_self"),
                            }
                        ],
                    }
                )
            },
        ]
    }

    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)
    state = ltm_store.get_maintenance_runtime_state(isolated_workspace)
    slots = maintenance_observation.run_maintenance_observation_tool(
        "read_event_slots",
        {"limit": 4},
        isolated_workspace,
    )

    assert result["completed_count"] == 1
    assert state["maintenance_coverage_cursor"] == -1
    assert slots["target_slots"][0]["slot_seq"] == 0
    assert slots["coverage_eligible_slot_seqs"] == [0, 1, 2, 3]
    assert [item["title"] for item in slots["existing_sourced_objects"]] == [
        "Formation output is maintainer input"
    ]


def test_formation_failed_work_item_is_retained_for_retry(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))

    failed = formation.run_formation_maintenance(
        _messages(),
        runtime_state={"eval_formation_decisions": [{"action": "invalid"}]},
    )
    assert failed["failed_count"] == 1

    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        row = connection.execute("SELECT status, attempts, last_error FROM formation_work_items").fetchone()
    assert row[0] == "failed"
    assert row[1] == 1
    assert row[2]

    retried = formation.run_formation_maintenance(
        _messages(),
        runtime_state={"eval_formation_decisions": [{"action": "no_memory"}]},
    )
    assert retried["completed_count"] == 1
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        row = connection.execute("SELECT status, attempts FROM formation_work_items").fetchone()
    assert row == ("completed", 2)


def test_formation_status_is_read_only_and_observer_exposes_metadata(isolated_workspace, monkeypatch):
    from memory_engine import event_stream, observer
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    formation.run_formation_maintenance(
        _messages(), runtime_state={"eval_formation_decisions": [{"action": "no_memory"}]}
    )

    before = formation.formation_status()
    snapshot = observer.memory_engine_snapshot(_messages(), runtime_state={})
    after = formation.formation_status()

    assert before == after
    assert snapshot["formation"]["work_item_count"] == 1
    assert snapshot["formation"]["completed_count"] == 1


def test_formation_query_error_is_returned_to_same_agent_and_can_recover(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    runtime_state = {
        "eval_formation_responses": [
            {
                "content": "",
                "tool_calls": [_tool_call("ltm_search_objects", {"text": ""}, "bad-search")],
            },
            {
                "content": "",
                "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"}, "good-select")],
            },
            {"content": json.dumps({"action": "no_memory"})},
        ]
    }
    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)
    assert result["completed_count"] == 1
    assert result["retrieval_call_count"] == 2
    assert result["retrieval_error_count"] == 1
    assert result["retrieval_success_count"] == 1
    assert result["model_round_count"] == 3


def test_write_plan_without_retrieval_is_not_committed_until_query_succeeds(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    write_plan = {
        "action": "write_plan",
        "objects": [
            {
                "object_type": "assertion",
                "title": "Delayed write plan",
                "summary": None,
                "payload": {"schema_version": 1, "body": "Durable delayed memory.", "structured_data": None},
                "outgoing_links": _outgoing_links("others"),
            }
        ],
    }
    runtime_state = {
        "eval_formation_responses": [
            {"content": json.dumps(write_plan)},
            {
                "content": "",
                "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})],
            },
            {"content": json.dumps(write_plan)},
        ]
    }
    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)
    assert result["completed_count"] == 1
    assert result["model_round_count"] == 3
    assert result["retrieval_success_count"] == 1
    assert result["object_count"] == 1


def test_formation_repairs_missing_summary_before_commit(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    oversized_body = "x" * 2049
    invalid = _object_draft("Needs a summary")
    invalid["payload"]["body"] = oversized_body
    invalid["summary"] = None
    repaired = _object_draft("Needs a summary")
    repaired["payload"]["body"] = oversized_body
    repaired["summary"] = "A concise summary."
    runtime_state = {
        "eval_formation_responses": [
            {"content": "", "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})]},
            {"content": json.dumps({"action": "write_plan", "objects": [invalid]})},
            {"content": json.dumps({"action": "write_plan", "objects": [repaired]})},
        ]
    }

    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)

    assert result["completed_count"] == 1
    assert result["failed_count"] == 0
    assert result["validation_repair_count"] == 1
    assert result["validation_error_count"] == 1
    assert result["validation_error_codes"] == {"missing_summary": 1}
    assert result["validation_error_paths"] == {"objects[0].summary": 1}
    assert result["last_validation_errors"] == [
        {
            "code": "missing_summary",
            "path": "objects[0].summary",
            "message": "summary is required for payloads over 2 KiB.",
        }
    ]
    assert len(ltm_store.list_objects("assertion")) == 1


@pytest.mark.parametrize("code,body,summary", [
    ("missing_summary", "中" * 1000, None),
    ("unexpected_summary", "short", "redundant"),
    ("payload_too_large", "x" * 8193, None),
])
def test_repair_feedback_reports_store_measured_bytes(code, body, summary):
    from memory_engine.ltm import formation, ltm_store

    obj = _object_draft()
    obj["payload"]["body"] = body
    obj["summary"] = summary
    message = formation._validation_repair_message(
        [{"code": code, "path": "objects[0].payload", "message": "invalid size"}], {"objects": [obj]}
    )
    assert f'"actual_payload_bytes": {ltm_store._payload_bytes(obj["payload"])}' in message
    assert '"summary_threshold_bytes": 2048' in message
    assert '"max_payload_bytes": 8192' in message
    assert "Keep unrelated objects and fields unchanged" in message


def test_title_constraint_is_known_before_generation_and_measured_in_feedback():
    from memory_engine.ltm import formation
    from shared.prompt_loader import load_prompt

    obj = _object_draft("long title " * 60)
    text = formation._validation_repair_message(
        [{"code": "title_length", "path": "objects[0].title", "message": "too long"}], {"objects": [obj]}
    )
    assert f'"actual_tokens": {formation.count_text_tokens(obj["title"])}' in text
    assert '"max_tokens": 50' in text
    assert "Each object title must contain 1–50 tokens." in load_prompt("formation")


def test_oversized_payload_can_be_split_during_repair(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({0, 1, 2, 3}, 100))
    bad = _object_draft("Large source")
    bad["payload"]["body"] = "x" * 8193
    split = [_object_draft("Part one"), _object_draft("Part two")]
    for obj, size in zip(split, (4096, 4097)):
        obj["payload"]["body"] = "x" * size
        obj["summary"] = "Part of the source."
    outcome = formation.run_formation_maintenance(_messages(), runtime_state={"eval_formation_responses": [
        {"tool_calls": [_tool_call("ltm_select_objects", {})]},
        {"content": json.dumps({"action": "write_plan", "objects": [bad]})},
        {"content": json.dumps({"action": "write_plan", "objects": split})},
    ]})
    assert outcome["failed_count"] == 0
    assert outcome["validation_error_codes"] == {"payload_too_large": 1}
    assert outcome["validation_repair_count"] == 1
    assert len(ltm_store.list_objects("assertion")) == 2


def test_exhausted_repairs_do_not_block_other_work_items(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    with event_stream.ensure_event_stream() as connection:
        formation._ensure_tables(connection)
        failed_id, _ = formation._create_work_item(connection, "unassigned", None, [{"start": 0, "end": 1}], 0)
        good_id, _ = formation._create_work_item(connection, "unassigned", None, [{"start": 2, "end": 3}], 1)
        connection.commit()
    monkeypatch.setattr(formation, "_refresh_work_items", lambda *args: {"buffer_slot_count": 4, "created_count": 0})
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({0, 1, 2, 3}, 100))
    bad = {**_object_draft("Bad"), "summary": "redundant"}
    plan = {"content": json.dumps({"action": "write_plan", "objects": [bad]})}
    tool = {"tool_calls": [_tool_call("ltm_select_objects", {})]}
    outcome = formation.run_formation_maintenance(_messages(), runtime_state={"eval_formation_responses": [
        tool, plan, plan, plan,
        tool, {"content": json.dumps({"action": "write_plan", "objects": [_object_draft("Independent good object")]})},
    ]})
    assert outcome["failed_count"] == outcome["completed_count"] == 1
    assert len(ltm_store.list_objects("assertion")) == 1
    with event_stream.ensure_event_stream() as connection:
        assert formation._load_work_item(connection, failed_id)["status"] == "failed"
        assert formation._load_work_item(connection, good_id)["status"] == "completed"


def test_two_plan_repairs_survive_exhausted_normal_round_budget(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({0, 1, 2, 3}, 100))
    good = _object_draft("Durable repaired object")
    bad = {**good, "summary": "Must be null"}
    plan = lambda obj: {"content": json.dumps({"action": "write_plan", "objects": [obj]})}
    replies = iter([
        plan(good),
        {"tool_calls": [_tool_call("ltm_select_objects", {})]},
        {"tool_calls": [_tool_call("ltm_select_objects", {})]},
        plan(bad),
        plan(bad), plan(good),
    ])
    calls = []
    def provider(messages, **kwargs):
        calls.append((json.loads(json.dumps(messages)), kwargs))
        return {"choices": [{"message": next(replies)}]}
    monkeypatch.setattr(formation, "call_chat_completion", provider)
    outcome = formation.run_formation_maintenance(_messages(), max_tool_calls=2)
    assert outcome["failed_count"] == 0
    assert outcome["model_round_count"] == 6
    assert outcome["validation_repair_count"] == 2
    assert outcome["validation_error_count"] == 2
    assert len(ltm_store.list_objects("assertion")) == 1
    assert all(kwargs["tools"] for _, kwargs in calls[:4])
    assert all(kwargs["tools"] is None for _, kwargs in calls[4:])
    assert "Repair attempt 2 of 2" in calls[-1][0][-1]["content"]
    assert any(m["role"] == "tool" for m in calls[-1][0])


@pytest.mark.parametrize("repair_kind", ["exhausted", "tool_call", "no_memory", "mixed_error"])
def test_plan_repair_failures_and_no_memory_are_atomic(isolated_workspace, monkeypatch, repair_kind):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda *args: ({0, 1, 2, 3}, 100))
    bad = {**_object_draft("Bad"), "summary": "redundant"}
    if repair_kind == "mixed_error":
        bad["payload"] = {**bad["payload"], "schema_version": 2}
    plan = {"content": json.dumps({"action": "write_plan", "objects": [_object_draft("Good"), bad]})}
    tool = {"tool_calls": [_tool_call("ltm_select_objects", {})]}
    extra = {
        "exhausted": [plan, plan], "tool_call": [tool],
        "no_memory": [{"content": '{"action":"no_memory"}'}], "mixed_error": [],
    }[repair_kind]
    outcome = formation.run_formation_maintenance(_messages(), runtime_state={"eval_formation_responses": [tool, plan, *extra]})
    assert ltm_store.list_objects("assertion") == []
    assert outcome["retrieval_call_count"] == 1
    assert outcome["validation_repair_count"] == (2 if repair_kind == "exhausted" else 0 if repair_kind == "mixed_error" else 1)
    assert outcome["failed_count"] == (0 if repair_kind == "no_memory" else 1)
    if repair_kind == "exhausted":
        assert "after 2 repair attempts" in outcome["error"]
    if repair_kind == "tool_call":
        assert "retrieval is unavailable" in outcome["error"]


def test_formation_repairs_unexpected_summary_before_commit(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    invalid = _object_draft("Short memory")
    invalid["summary"] = "Redundant summary."
    repaired = _object_draft("Short memory")
    repaired["summary"] = None
    runtime_state = {
        "eval_formation_responses": [
            {"content": "", "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})]},
            {"content": json.dumps({"action": "write_plan", "objects": [invalid]})},
            {"content": json.dumps({"action": "write_plan", "objects": [repaired]})},
        ]
    }

    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)

    assert result["completed_count"] == 1
    assert result["failed_count"] == 0
    assert result["validation_repair_count"] == 1
    assert result["validation_error_codes"] == {"unexpected_summary": 1}
    assert result["validation_error_paths"] == {"objects[0].summary": 1}
    assert len(ltm_store.list_objects("assertion")) == 1


def test_formation_repairs_title_length_before_commit(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    long_title = "".join(f"token{index} " for index in range(60)).strip()
    invalid = _object_draft(long_title)
    repaired = _object_draft("Concise durable memory title")
    runtime_state = {
        "eval_formation_responses": [
            {"content": "", "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})]},
            {"content": json.dumps({"action": "write_plan", "objects": [invalid]})},
            {"content": json.dumps({"action": "write_plan", "objects": [repaired]})},
        ]
    }

    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state)

    assert result["completed_count"] == 1
    assert result["failed_count"] == 0
    assert result["validation_repair_count"] == 1
    assert result["validation_error_codes"] == {"title_length": 1}
    assert result["validation_error_paths"] == {"objects[0].title": 1}
    assert ltm_store.list_objects("assertion")[0]["title"] == "Concise durable memory title"


def test_invalid_tag_plan_is_atomic_and_work_item_can_retry(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    invalid = _object_draft("Retryable tag validation")
    invalid["outgoing_links"] = _outgoing_links("others", "safety")
    first = formation.run_formation_maintenance(
        _messages(),
        runtime_state={
            "eval_formation_responses": [
                {"content": "", "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})]},
                {"content": json.dumps({"action": "write_plan", "objects": [invalid]})},
            ]
        },
    )

    assert first["failed_count"] == 1
    assert "others_conflict" in first["error"]
    assert ltm_store.list_objects("assertion") == []

    second = formation.run_formation_maintenance(
        _messages(),
        runtime_state={
            "eval_formation_responses": [
                {"content": "", "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})]},
                {
                    "content": json.dumps(
                        {"action": "write_plan", "objects": [_object_draft("Retryable tag validation")]}
                    )
                },
            ]
        },
    )

    assert second["completed_count"] == 1
    assert len(ltm_store.list_objects("assertion")) == 1


def test_historical_retry_reconciles_existing_formation_commit(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation, ltm_store

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    response = {
        "content": "",
        "tool_calls": [_tool_call("ltm_select_objects", {"projection": "title"})],
    }
    write_response = {"content": json.dumps({"action": "write_plan", "objects": [_object_draft()]})}
    first = formation.run_formation_maintenance(
        _messages(),
        runtime_state={"eval_formation_responses": [response, write_response]},
    )
    assert first["completed_count"] == 1
    assert len(ltm_store.list_objects("assertion")) == 1

    with event_stream.ensure_event_stream() as connection:
        item = connection.execute(
            "SELECT work_item_id FROM formation_work_items ORDER BY work_item_id LIMIT 1"
        ).fetchone()
        formation._set_item_state(connection, item["work_item_id"], "failed", error="retry")
        connection.commit()

    second = formation._process_item(
        item["work_item_id"],
        _system_root_tags(),
        runtime_state={"eval_formation_responses": [response, write_response]},
        reconcile_idempotency_conflicts=True,
    )

    assert second["status"] == "completed"
    assert second["commit_status"] == "already_committed"
    assert second["idempotency_reconciled"] is True
    assert second["object_count"] == 1
    assert len(ltm_store.list_objects("assertion")) == 1


def test_formation_stops_when_native_tool_call_limit_is_exceeded(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    runtime_state = {
        "eval_formation_responses": [
            {
                "content": "",
                "tool_calls": [
                    _tool_call("ltm_select_objects", {}, "one"),
                    _tool_call("ltm_select_objects", {}, "two"),
                    _tool_call("ltm_select_objects", {}, "three"),
                ],
            }
        ]
    }
    result = formation.run_formation_maintenance(_messages(), runtime_state=runtime_state, max_tool_calls=2)
    assert result["failed_count"] == 1
    assert result["retrieval_call_count"] == 2
    assert "tool-call limit" in result["error"]


def test_formation_retrieval_tools_are_not_main_agent_tools(isolated_workspace):
    from tools import tool_registry

    exposed = set(tool_registry.tool_schema_names())
    assert {
        "ltm_select_objects",
        "ltm_search_objects",
        "ltm_traverse_objects",
    }.isdisjoint(exposed)


def test_formation_uses_provider_native_tool_history(isolated_workspace, monkeypatch):
    from memory_engine import event_stream
    from memory_engine.ltm import formation

    event_stream.save_messages(_messages())
    _prepare_segment()
    monkeypatch.setattr(formation, "_buffer_slots", lambda _messages, _runtime_state=None: ({0, 1, 2, 3}, 100))
    calls = []

    def fake_call(messages, tools=None, **_kwargs):
        calls.append({"messages": list(messages), "tools": tools})
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [_tool_call("ltm_select_objects", {"limit": 1}, "native-call")],
                        }
                    }
                ]
            }
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({"action": "no_memory"})}}]}

    monkeypatch.setattr(formation, "call_chat_completion", fake_call)
    result = formation.run_formation_maintenance(_messages(), runtime_state={})
    assert result["completed_count"] == 1
    assert len(calls) == 2
    tool_names = {schema["function"]["name"] for schema in calls[0]["tools"]}
    assert tool_names == {"ltm_select_objects", "ltm_search_objects", "ltm_traverse_objects"}
    replay = calls[1]["messages"]
    assert replay[-2]["role"] == "assistant"
    assert replay[-2]["tool_calls"][0]["id"] == "native-call"
    assert replay[-1]["role"] == "tool"
    assert replay[-1]["tool_call_id"] == "native-call"
    assert json.loads(replay[-1]["content"])["status"] in {"ok", "partial"}
