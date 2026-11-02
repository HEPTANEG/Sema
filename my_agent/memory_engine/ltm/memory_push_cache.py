"""Best-effort, developer-readable cache for Memory Push results."""

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .. import event_stream
from . import ltm_store


MAX_ENTRIES = 5
_LOCK = threading.RLock()


def cache_dir(root=None):
    return event_stream.memory_root(root) / "memory_push_cache"


def index_path(root=None):
    return cache_dir(root) / "index.json"


def state_path(root=None):
    return cache_dir(root) / "state.json"


def load_state(root=None):
    with _LOCK:
        try:
            data = json.loads(state_path(root).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"stable_turn_count": 0, "latest_revision": -1}
        return data if isinstance(data, dict) else {"stable_turn_count": 0, "latest_revision": -1}


def save_state(root=None, **updates):
    try:
        with _LOCK:
            state = load_state(root)
            state.update(updates)
            directory = cache_dir(root)
            directory.mkdir(parents=True, exist_ok=True)
            state_path(root).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            return state
    except Exception:
        return None


def _read_index(root=None):
    try:
        data = json.loads(index_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("entries") if isinstance(data, dict) else data
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def load_latest(root=None):
    with _LOCK:
        generation = ltm_store.forgetting_generation(root)
        rows = sorted(_read_index(root), key=lambda row: int(row.get("sequence") or 0), reverse=True)
        for row in rows:
            if row.get("status") not in {"ok", "empty"}:
                continue
            path = Path(row.get("path") or "")
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("forgetting_generation", 0) != generation:
                continue
            return entry if isinstance(entry, dict) else None
    return None


def save(root, *, revision, focus_state, mode, result):
    """Persist only reusable result data; failures are intentionally ignored."""
    try:
        with _LOCK:
            directory = cache_dir(root)
            directory.mkdir(parents=True, exist_ok=True)
            rows = _read_index(root)
            sequence = max((int(row.get("sequence") or 0) for row in rows), default=0) + 1
            entry_id = f"push-{sequence:08d}-{uuid4().hex[:8]}"
            entry_path = directory / f"{entry_id}.json"
            payload = {
                "sequence": sequence,
                "forgetting_generation": ltm_store.forgetting_generation(root),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "revision": int(revision),
                "focus_state": str(focus_state or "stable"),
                "mode": str(mode or ""),
                "status": result.get("status") if isinstance(result, dict) else "error",
                "content": result.get("content") if isinstance(result, dict) else None,
                "used_object_ids": list(result.get("used_object_ids") or []) if isinstance(result, dict) else [],
                "candidate_pool": copy.deepcopy(result.get("candidate_pool") or {}) if isinstance(result, dict) else {},
                "selected_objects": copy.deepcopy(result.get("selected_objects") or []) if isinstance(result, dict) else [],
            }
            entry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            rows.insert(0, {"sequence": sequence, "path": str(entry_path), "status": payload["status"], "revision": int(revision)})
            rows.sort(key=lambda row: int(row.get("sequence") or 0), reverse=True)
            kept = rows[:MAX_ENTRIES]
            index_path(root).write_text(json.dumps({"limit": MAX_ENTRIES, "entries": kept}, ensure_ascii=False, indent=2), encoding="utf-8")
            for row in rows[MAX_ENTRIES:]:
                try:
                    Path(row.get("path") or "").unlink(missing_ok=True)
                except OSError:
                    pass
            return payload
    except Exception:
        return None
