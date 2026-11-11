import json
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import memory_engine.event_stream as event_stream





















APP_DIR = Path(__file__).resolve().parent
SESSION_APP_NAME = "Sema"
PROJECT_DIR_NAME = ".sema"
PROJECT_FILE_NAME = "project.json"
EVENT_STREAM_CONVERSATION_ID = "event-stream"


_active_project = None
_active_conversation_id = None



def _now():
    return datetime.now(timezone.utc).isoformat()







def _project_dir():
    return Path.cwd() / PROJECT_DIR_NAME



def project_path():
    return _project_dir() / PROJECT_FILE_NAME



def _new_project_data():
    workspace_root = Path.cwd().resolve()
    return {
        "app": SESSION_APP_NAME,
        "project_id": uuid4().hex,
        "name": workspace_root.name or str(workspace_root),
        "root": str(workspace_root),
        "created_at": _now(),
        "updated_at": _now(),
        "active_conversation_id": None,
        "conversations": [],
    }



def _load_project_file():
    path = project_path()
    if not path.exists():
        return _new_project_data()

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("app") != SESSION_APP_NAME:
        raise RuntimeError(".sema/project.json is not a Sema project file.")
    return data



def _save_project_file(project):
    project["updated_at"] = _now()
    project_path().parent.mkdir(parents=True, exist_ok=True)
    project_path().write_text(
        json.dumps(project, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )



def ensure_project():
    global _active_project


    _active_project = _load_project_file()
    _save_project_file(_active_project)
    return _active_project



def get_project_info():
    project = _active_project or ensure_project()
    active_conversation_id = _active_conversation_id or project.get("active_conversation_id")
    event_stream_message_count = event_stream.event_stream_message_count()
    if event_stream_message_count > 0:
        active_conversation_id = EVENT_STREAM_CONVERSATION_ID
    return {
        "name": project.get("name"),
        "project_id": project.get("project_id"),
        "root": project.get("root"),
        "memory_root": event_stream.memory_root(),
        "event_stream_dir": event_stream.event_stream_dir(),
        "event_stream_message_count": event_stream_message_count,
        "active_conversation_id": active_conversation_id,
        "conversation_count": 1 if active_conversation_id else 0,
        "project_path": project_path(),
    }


def project_unattended_enabled():
    """Return the project-local unattended preference."""
    project = _active_project or ensure_project()
    settings = project.get("settings")
    return bool(settings.get("unattended")) if isinstance(settings, dict) else False


def save_project_unattended(enabled):
    """Persist unattended mode for the current project only."""
    global _active_project

    project = _active_project or ensure_project()
    settings = project.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    settings["unattended"] = bool(enabled)
    project["settings"] = settings
    _active_project = project
    _save_project_file(project)



def _sanitize_for_utf8(value):
    if isinstance(value, str):
        return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value



def active_conversation_record():
    count = event_stream.event_stream_message_count()
    if count == 0:
        return None
    return {
        "id": EVENT_STREAM_CONVERSATION_ID,
        "title": "Event Stream",
        "created_at": "",
        "updated_at": "",
        "message_count": count,
    }



def load_conversation_message(conversation_id, message_index, messages=None):
    message_index = int(message_index)
    if messages is not None:
        if message_index < 0 or message_index >= len(messages):
            raise RuntimeError(f"Message index out of range: {message_index}")
        return messages[message_index]
    return event_stream.load_message(message_index)



def load_last_session():
    project = _active_project or ensure_project()
    messages = event_stream.load_messages()
    if messages:
        project["active_conversation_id"] = EVENT_STREAM_CONVERSATION_ID
        _save_project_file(project)
        return messages
    raise RuntimeError("No saved session found.")



def save_last_session(messages, created_at_by_index=None):
    safe_messages = _sanitize_for_utf8(messages)
    project = _active_project or ensure_project()
    status = event_stream.save_messages(safe_messages, created_at_by_index=created_at_by_index)
    project["active_conversation_id"] = EVENT_STREAM_CONVERSATION_ID
    project["conversations"] = []
    _save_project_file(project)
    return status



def append_message_and_persist(messages, message, created_at=None):
    safe_message = _sanitize_for_utf8([message])[0]
    project = _active_project or ensure_project()
    try:
        event_stream.append_message(
            safe_message,
            expected_slot_seq=len(messages),
            created_at=created_at,
        )
    except RuntimeError as error:



        if not str(error).startswith("Event Stream append position mismatch:"):
            raise
        event_stream.save_messages([*messages, safe_message], created_at_by_index={len(messages): created_at})
    messages.append(safe_message)
    from memory_engine.wm import on_message_persisted
    on_message_persisted(safe_message)
    project["active_conversation_id"] = EVENT_STREAM_CONVERSATION_ID
    project["conversations"] = []
    _save_project_file(project)
    return safe_message
