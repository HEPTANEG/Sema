"""Non-secret operational state for the Telegram Gateway."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


STATE_PATH = Path(".sema") / "remote" / "gateway-state.json"
MAX_COMPLETED_UPDATES = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


class GatewayState:
    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.path = self.workspace / STATE_PATH
        self.lock = threading.RLock()
        self.data = self._load()

    def record_inbound(self, update_id, chat_id, message_id, prompt):
        request_id = f"telegram-update-{int(update_id)}"
        with self.lock:
            item = self.data["pending"].get(request_id)
            if item:
                return dict(item)
            item = {
                "request_id": request_id,
                "update_id": int(update_id),
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "prompt": str(prompt),
                "status": "received",
                "final_content": "",
                "updated_at": _now(),
            }
            self.data["pending"][request_id] = item
            self._save()
            return dict(item)

    def get(self, request_id):
        with self.lock:
            item = self.data["pending"].get(str(request_id))
            return dict(item) if item else None

    def pending(self):
        with self.lock:
            return [dict(item) for item in self.data["pending"].values()]

    def pending_ready(self):
        with self.lock:
            return [
                dict(item)
                for item in self.data["pending"].values()
                if item.get("status") == "received" and not item.get("batch_id")
            ]

    def assign_batch(self, request_ids, batch_id):
        with self.lock:
            for request_id in request_ids:
                item = self.data["pending"].get(str(request_id))
                if item:
                    item["batch_id"] = str(batch_id)
                    item["status"] = "batched"
                    item["updated_at"] = _now()
            self._save()

    def batch_entries(self, batch_id):
        with self.lock:
            return [
                dict(item)
                for item in self.data["pending"].values()
                if item.get("batch_id") == str(batch_id)
            ]

    def has_batch(self, batch_id):
        with self.lock:
            return any(item.get("batch_id") == str(batch_id) for item in self.data["pending"].values())

    def has_request(self, request_id):
        with self.lock:
            return str(request_id) in self.data["pending"] or any(
                item.get("batch_id") == str(request_id) for item in self.data["pending"].values()
            )

    def update_batch(self, batch_id, **changes):
        with self.lock:
            changed = False
            for item in self.data["pending"].values():
                if item.get("batch_id") == str(batch_id):
                    item.update(changes)
                    item["updated_at"] = _now()
                    changed = True
            if changed:
                self._save()

    def update_request(self, request_id, **changes):
        with self.lock:
            item = self.data["pending"].get(str(request_id))
            if not item:
                return
            item.update(changes)
            item["updated_at"] = _now()
            self._save()

    def complete_delivery(self, request_id):
        with self.lock:
            item = self.data["pending"].pop(str(request_id), None)
            if item:
                self.data["completed_updates"].append({
                    "request_id": item["request_id"],
                    "update_id": item["update_id"],
                    "completed_at": _now(),
                })
                self.data["completed_updates"] = self.data["completed_updates"][-MAX_COMPLETED_UPDATES:]
                self._save()

    def complete_batch(self, batch_id):
        with self.lock:
            completed = []
            for request_id, item in list(self.data["pending"].items()):
                if item.get("batch_id") == str(batch_id):
                    completed.append(item)
                    self.data["pending"].pop(request_id, None)
            if completed:
                self.data["completed_updates"].extend({
                    "request_id": item["request_id"],
                    "update_id": item["update_id"],
                    "completed_at": _now(),
                } for item in completed)
                self.data["completed_updates"] = self.data["completed_updates"][-MAX_COMPLETED_UPDATES:]
                self._save()

    def discard_batch(self, batch_id):
        with self.lock:
            removed = False
            for request_id, item in list(self.data["pending"].items()):
                if item.get("batch_id") == str(batch_id):
                    self.data["pending"].pop(request_id, None)
                    removed = True
            if removed:
                self._save()

    def discard(self, request_id):
        """Forget an update that was never accepted by Backend/Core."""
        with self.lock:
            if self.data["pending"].pop(str(request_id), None) is not None:
                self._save()

    def health(self, **changes):
        with self.lock:
            self.data["health"].update(changes)
            self._save()

    def snapshot(self):
        with self.lock:
            return {
                "workspace": str(self.workspace),
                "health": dict(self.data["health"]),
                "pending_count": len(self.data["pending"]),
                "pending_request_ids": sorted(self.data["pending"]),
            }

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    "version": 1,
                    "pending": raw.get("pending") if isinstance(raw.get("pending"), dict) else {},
                    "completed_updates": raw.get("completed_updates") if isinstance(raw.get("completed_updates"), list) else [],
                    "health": raw.get("health") if isinstance(raw.get("health"), dict) else {},
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "pending": {}, "completed_updates": [], "health": {}}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
