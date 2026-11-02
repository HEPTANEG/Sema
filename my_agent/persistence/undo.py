import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


UNDO_DIR_NAME = "undo"
UNDO_RECORD_VERSION = 1



def undo_dir():
    return Path.cwd() / ".sema" / UNDO_DIR_NAME


def _now():
    return datetime.now(timezone.utc).isoformat()


def _record_path(record_id):
    return undo_dir() / f"{record_id}.json"


def _hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_file(file_path):

    file_path = Path(file_path)
    if not file_path.exists():
        return {
            "existed": False,
            "content": "",
            "hash": None,
        }

    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "existed": True,
        "content": content,
        "hash": _hash_text(content),
    }


def _current_file_state(file_path):
    if not file_path.exists():
        return {
            "exists": False,
            "hash": None,
        }

    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "exists": True,
        "hash": _hash_text(content),
    }


def record_edit(tool, snapshots, summary=""):
    if not snapshots:
        return None

    workspace_root = Path.cwd().resolve()
    files = []
    for file_path, before in snapshots:
        file_path = Path(file_path).resolve()
        current = _current_file_state(file_path)
        try:
            stored_path = file_path.relative_to(workspace_root).as_posix()
        except ValueError:

            stored_path = str(file_path)
        files.append(
            {
                "path": stored_path,
                "before_existed": before["existed"],
                "before_content": before["content"],
                "before_hash": before["hash"],
                "after_exists": current["exists"],
                "after_hash": current["hash"],
            }
        )

    record_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    record = {
        "version": UNDO_RECORD_VERSION,
        "id": record_id,
        "created_at": _now(),
        "tool": tool,
        "summary": summary,
        "files": files,
    }

    undo_dir().mkdir(parents=True, exist_ok=True)
    _record_path(record_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record


def load_undo_records(limit=20):
    path = undo_dir()
    if not path.exists():
        return []

    records = []
    for record_path in sorted(path.glob("*.json"), reverse=True):
        try:
            records.append(json.loads(record_path.read_text(encoding="utf-8-sig")))
        except (OSError, json.JSONDecodeError):
            continue
        if len(records) >= limit:
            break
    return records


def format_undo_records(records):
    if not records:
        return "No undo records."

    lines = ["Recent undo records:"]
    for record in records:
        files = ", ".join(item.get("path", "") for item in record.get("files", []))
        lines.append(
            f"{record.get('id')}  {record.get('tool')}  "
            f"{record.get('created_at', '')}  {files}"
        )
    return "\n".join(lines)


def apply_undo(record_id=None):
    records = load_undo_records(limit=100)
    if not records:
        raise RuntimeError("No undo records.")

    record = records[0] if not record_id else next(
        (item for item in records if item.get("id") == record_id),
        None,
    )
    if record is None:
        raise RuntimeError(f"Undo record not found: {record_id}")

    workspace_root = Path.cwd().resolve()
    files = record.get("files") or []
    for item in files:
        raw_path = Path(item["path"])
        file_path = raw_path.resolve() if raw_path.is_absolute() else (workspace_root / raw_path).resolve()
        current = _current_file_state(file_path)
        if current["exists"] != item.get("after_exists") or current["hash"] != item.get("after_hash"):
            raise RuntimeError(
                f"Cannot undo {record['id']}: file changed after edit: {item['path']}"
            )

    for item in files:
        raw_path = Path(item["path"])
        file_path = raw_path.resolve() if raw_path.is_absolute() else (workspace_root / raw_path).resolve()
        if item.get("before_existed"):
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(item.get("before_content", ""), encoding="utf-8")
        elif file_path.exists():
            file_path.unlink()

    return {
        "id": record["id"],
        "tool": record.get("tool", ""),
        "files": [item.get("path", "") for item in files],
    }


def format_undo_result(result):
    files = ", ".join(result.get("files", []))
    return f"Undid edit: {result.get('id')} ({result.get('tool')}) {files}".rstrip()
