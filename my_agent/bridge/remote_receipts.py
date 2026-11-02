"""Small durable receipt ledger for idempotent Telegram requests.

The ledger is operational state, not chat history: it stores opaque request
identifiers and terminal delivery material only. The Event Stream remains the
authoritative record of the conversation.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


RECEIPT_DIR = Path(".sema") / "remote"
RECEIPT_FILE = "backend-receipts.json"
MAX_RECEIPTS = 200


def _now():
    return datetime.now(timezone.utc).isoformat()


class RemoteReceiptStore:
    def __init__(self, root=None):
        self.root = Path(root or Path.cwd())
        self.path = self.root / RECEIPT_DIR / RECEIPT_FILE
        self.lock = threading.RLock()
        self.data = self._load()
        self._interrupt_unfinished()

    def get(self, request_id):
        with self.lock:
            item = self.data["receipts"].get(str(request_id))
            return dict(item) if item else None

    def begin(self, request_id, turn_id):
        with self.lock:
            existing = self.data["receipts"].get(str(request_id))
            if existing:
                return dict(existing)
            item = {
                "request_id": str(request_id),
                "turn_id": str(turn_id),
                "status": "running",
                "created_at": _now(),
                "updated_at": _now(),
                "final_content": "",
                "error": "",
            }
            self.data["receipts"][str(request_id)] = item
            self._save()
            return dict(item)

    def complete(self, request_id, content):
        self._update(request_id, status="completed", final_content=str(content or ""), error="")

    def interrupt(self, request_id, error="Remote request interrupted before completion."):
        self._update(request_id, status="interrupted", error=str(error), final_content="")

    def fail(self, request_id, error):
        self._update(request_id, status="failed", error=str(error), final_content="")

    def _update(self, request_id, **changes):
        with self.lock:
            item = self.data["receipts"].get(str(request_id))
            if not item:
                return
            item.update(changes)
            item["updated_at"] = _now()
            self._save()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("receipts"), dict):
                return {"version": 1, "receipts": raw["receipts"]}
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": 1, "receipts": {}}

    def _interrupt_unfinished(self):
        changed = False
        for item in self.data["receipts"].values():
            if item.get("status") == "running":
                item["status"] = "interrupted"
                item["error"] = "Backend restarted before the request completed."
                item["updated_at"] = _now()
                changed = True
        if changed:
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        receipts = sorted(
            self.data["receipts"].values(),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )[:MAX_RECEIPTS]
        self.data["receipts"] = {item["request_id"]: item for item in receipts}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
