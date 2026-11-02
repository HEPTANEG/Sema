from memory_engine import event_stream
from memory_engine import context_builder
from memory_engine.ltm import memory_push_ranking


def _save_messages(messages, created_at_by_index):
    from persistence import session

    session.ensure_project()
    session.save_last_session(messages, created_at_by_index=created_at_by_index)


def test_current_session_messages_keep_all_non_system_slots_in_order(isolated_workspace):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second"},
        {"role": "tool", "content": "tool result"},
    ]
    _save_messages(
        messages,
        {
            0: "2026-10-01T10:00:00+00:00",
            1: "2026-10-01T10:01:00+00:00",
            2: "2026-10-01T10:01:01+00:00",
            3: "2026-10-01T10:02:00+00:00",
            4: "2026-10-01T10:02:01+00:00",
        },
    )

    result = event_stream.load_current_session_messages(
        process_started_at="2026-10-01T10:00:00+00:00",
    )

    assert [message["content"] for message in result] == [
        "first",
        "first answer",
        "second",
        "tool result",
    ]


def test_current_session_messages_start_after_user_gap(isolated_workspace):
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new answer"},
    ]
    _save_messages(
        messages,
        {
            0: "2026-10-01T10:00:00+00:00",
            1: "2026-10-01T10:01:00+00:00",
            2: "2026-10-01T10:01:01+00:00",
            3: "2026-10-01T10:12:00+00:00",
            4: "2026-10-01T10:12:01+00:00",
        },
    )

    result = event_stream.load_current_session_messages(
        process_started_at="2026-10-01T10:00:00+00:00",
    )

    assert [message["content"] for message in result] == ["new", "new answer"]


def test_current_session_messages_exclude_pre_restart_slots(isolated_workspace):
    messages = [
        {"role": "user", "content": "before restart"},
        {"role": "assistant", "content": "before answer"},
        {"role": "user", "content": "after restart"},
    ]
    _save_messages(
        messages,
        {
            0: "2026-10-01T10:00:00+00:00",
            1: "2026-10-01T10:00:01+00:00",
            2: "2026-10-01T10:00:02+00:00",
        },
    )

    result = event_stream.load_current_session_messages(
        process_started_at="2026-10-01T10:00:02+00:00",
    )

    assert result == [{"role": "user", "content": "after restart"}]


def test_rank_3_messages_use_context_without_working_memory():
    messages, references = memory_push_ranking._rank_3_messages(
        [{"role": "user", "content": "current session"}],
        {
            "mem_a": {
                "title": "Memory A",
                "payload": {"body": "Payload A"},
            },
        },
    )

    content = messages[1]["content"]
    assert "current session" in content
    assert "working memory" not in content.lower()
    assert '"object_id"' not in content
    assert references.resolve(1) == "mem_a"


def test_memory_push_forwards_rank_3_context(monkeypatch):
    captured = []
    monkeypatch.setattr(context_builder.memory_push_cache, "load_latest", lambda _root=None: None)
    monkeypatch.setattr(
        context_builder.memory_push_ranking,
        "run_memory_push",
        lambda *_args, **kwargs: captured.append(kwargs) or {"status": "ok", "content": None, "metrics": {}},
    )

    context_builder._memory_push_for_turn(
        [],
        [],
        {"_memory_push_enabled": True},
        rank_3_context_messages=[{"role": "user", "content": "session"}],
    )

    assert captured[0]["rank_3_context_messages"] == [{"role": "user", "content": "session"}]
