import json

import pytest


def _messages(text="hello"):
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": text},
    ]


def test_ensure_project_creates_event_stream_project_file(isolated_workspace):
    from persistence import session

    project = session.ensure_project()
    project_file = isolated_workspace / ".sema" / "project.json"

    assert project_file.exists()
    assert project["app"] == "Sema"
    assert project["root"] == str(isolated_workspace.resolve())
    assert "memory_architecture" not in project


def test_project_unattended_preference_round_trips(isolated_workspace):
    from persistence import session

    session.ensure_project()
    assert session.project_unattended_enabled() is False

    session.save_project_unattended(True)

    assert session.project_unattended_enabled() is True
    project = json.loads((isolated_workspace / ".sema" / "project.json").read_text(encoding="utf-8"))
    assert project["settings"] == {"unattended": True}


def test_save_and_load_event_stream_session(isolated_workspace):
    from persistence import session

    messages = _messages("remember this")
    session.ensure_project()
    session.save_last_session(messages)

    loaded = session.load_last_session()
    project = json.loads((isolated_workspace / ".sema" / "project.json").read_text(encoding="utf-8"))

    assert loaded == messages
    assert "memory_architecture" not in project
    assert project["active_conversation_id"] == "event-stream"
    assert project["conversations"] == []
    assert not (isolated_workspace / ".sema" / "conversations").exists()


def test_save_last_session_replaces_broken_surrogates(isolated_workspace):
    from persistence import session

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "broken emoji \ud83d"},
    ]
    session.ensure_project()
    session.save_last_session(messages)

    loaded = session.load_last_session()

    assert loaded[1]["content"] == "broken emoji ���"


def test_load_conversation_message_reads_event_stream_message(isolated_workspace):
    from persistence import session

    messages = _messages("remember this")
    session.ensure_project()
    session.save_last_session(messages)

    assert session.load_conversation_message("event-stream", 1) == {"role": "user", "content": "remember this"}


def test_load_conversation_message_reports_out_of_range(isolated_workspace):
    from persistence import session

    messages = _messages("remember this")
    session.ensure_project()
    session.save_last_session(messages)

    with pytest.raises(RuntimeError, match="Message index out of range: 9"):
        session.load_conversation_message("event-stream", 9)


def test_load_conversation_message_prefers_active_in_memory_messages(isolated_workspace):
    from persistence import session

    persisted_messages = _messages("persisted")
    current_messages = [*persisted_messages, {"role": "user", "content": "unsaved current turn"}]
    session.ensure_project()
    session.save_last_session(persisted_messages)

    message = session.load_conversation_message("event-stream", 2, messages=current_messages)

    assert message == {"role": "user", "content": "unsaved current turn"}
