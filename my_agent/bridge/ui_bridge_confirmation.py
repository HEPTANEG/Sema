import threading
import uuid

from runtime import agent


class BridgeConfirmationPolicy:
    def __init__(self, server):
        self.server = server

    def ask(self, kind, title, details="", choices=None, preview=None, permissions=None):
        if (permissions or {}).get("allow_all"):
            return {"allowed": True, "reason": ""}
        confirmation_id = uuid.uuid4().hex
        choices = choices or [
            {"id": "yes", "label": "Yes"},
            {"id": "no", "label": "No and explain why", "requires_reason": True},
        ]
        self.server.pending_confirmations[confirmation_id] = {
            "event": threading.Event(),
            "response": None,
        }
        self.server.send_event(
            "confirmation_requested",
            {
                "confirmation_id": confirmation_id,
                "kind": kind,
                "title": title,
                "details": details,
                "choices": choices,
                "preview": preview or "",
            },
        )

        pending = self.server.pending_confirmations[confirmation_id]
        pending["event"].wait()
        response = pending.get("response") or {}
        self.server.pending_confirmations.pop(confirmation_id, None)
        return self._map_confirmation_response(response, permissions)

    def _map_confirmation_response(self, response, permissions=None):
        choice = response.get("choice")
        reason = response.get("reason") or ""
        if choice in {"yes", "yes_once", "continue"}:
            return {"allowed": True, "reason": ""}
        if choice == "allow_edits":
            if permissions is not None:
                permissions["allow_edits"] = True
            return {"allowed": True, "reason": ""}
        if choice == "allow_run_commands":
            if permissions is not None:
                permissions["allow_run_commands"] = True
            return {"allowed": True, "reason": ""}
        if choice == "allow_checkpoints":
            return {"allowed": True, "reason": "", "allow_checkpoints": True}
        if choice == "hold":
            return {
                "allowed": False,
                "reason": "Hold on and discuss the requested action before continuing.",
            }
        return {"allowed": False, "reason": reason}

    def confirm_write(self, path_text, content, permissions=None):
        if (permissions or {}).get("allow_edits"):
            return {"allowed": True, "reason": ""}
        details = (
            f"path: {path_text}\n"
            f"full file replacement; new size: {len((content or '').splitlines())} lines, {len(content or '')} chars"
        )
        return self.ask(
            "edit",
            "write_file",
            details,
            choices=self._edit_choices(),
            preview=content,
            permissions=permissions,
        )

    def confirm_patch(self, patch, permissions=None):
        if (permissions or {}).get("allow_edits"):
            return {"allowed": True, "reason": ""}
        return self.ask(
            "edit",
            "apply_patch",
            "Apply proposed patch",
            choices=self._edit_choices(),
            preview=patch,
            permissions=permissions,
        )

    def confirm_insert(self, path_text, anchor, content, anchor_context=None, permissions=None, allow_external=False):
        if (permissions or {}).get("allow_edits"):
            return {"allowed": True, "reason": ""}
        edit = agent.build_insert_after_edit(path_text, anchor, content, anchor_context, allow_external=allow_external)
        return self.ask(
            "edit",
            "insert_after",
            f"path: {path_text}",
            choices=self._edit_choices(),
            preview=edit["diff"],
            permissions=permissions,
        )

    def confirm_replace(self, path_text, old_block, new_block, permissions=None, allow_external=False):
        if (permissions or {}).get("allow_edits"):
            return {"allowed": True, "reason": ""}
        edit = agent.build_replace_block_edit(path_text, old_block, new_block, allow_external=allow_external)
        return self.ask(
            "edit",
            "replace_block",
            f"path: {path_text}",
            choices=self._edit_choices(),
            preview=edit["diff"],
            permissions=permissions,
        )

    def confirm_run_command(self, command, permissions=None):
        if (permissions or {}).get("allow_run_commands"):
            return {"allowed": True, "reason": ""}
        return self.ask(
            "run_command",
            "run_command",
            command,
            choices=[
                {"id": "yes", "label": "Yes"},
                {"id": "allow_run_commands", "label": "Allow all run_command for this session"},
                {"id": "no", "label": "No and explain why", "requires_reason": True},
            ],
            permissions=permissions,
        )

    def confirm_file_mutating_command(self, command, permissions=None):
        return self.ask(
            "dangerous_command",
            "file-mutating run_command",
            command,
            choices=[
                {"id": "yes", "label": "Yes, run this command once"},
                {"id": "no", "label": "No and explain why", "requires_reason": True},
            ],
            permissions=permissions,
        )

    def confirm_external_path_access(self, action_name, details, permissions=None):
        return self.confirm_risk_action(
            f"{action_name} outside workspace",
            details,
            permissions,
            yes_label=f"Yes, {action_name}",
        )

    def confirm_session_control(self, action_name, details, permissions=None):
        return self.confirm_risk_action(action_name, details, permissions)

    def confirm_undo_edit(self, details, permissions=None):
        return self.confirm_risk_action("undo this edit", details, permissions)

    def confirm_risk_action(self, action_name, details, permissions=None, yes_label=None):
        return self.ask(
            "risk",
            action_name,
            details,
            choices=[
                {"id": "yes", "label": yes_label or f"Yes, {action_name}"},
                {"id": "no", "label": "No and explain why", "requires_reason": True},
            ],
            permissions=permissions,
        )

    def confirm_persistent_action(self, action_name, details, preview=""):
        return self.ask(
            "persistent_state",
            action_name,
            details,
            choices=[
                {"id": "yes", "label": "Yes"},
                {"id": "hold", "label": "Hold on and discuss"},
                {"id": "no", "label": "No and explain why", "requires_reason": True},
            ],
            preview=preview,
        )

    def confirm_tool_checkpoint(self, tool_count):
        result = self.ask(
            "checkpoint",
            "Continue tool run?",
            f"This task has used {tool_count} tools.",
            choices=[
                {"id": "continue", "label": "Yes"},
                {"id": "allow_checkpoints", "label": "Yes for all checkpoints in this run"},
                {"id": "stop", "label": "Stop and summarize"},
            ],
        )
        if result.get("allow_checkpoints"):
            return "Yes for all checkpoints in this run"
        if result.get("allowed"):
            return "Yes"
        return "Stop and summarize"

    def _edit_choices(self):
        return [
            {"id": "yes", "label": "Yes"},
            {"id": "preview", "label": "Preview modification", "local_preview": True},
            {"id": "no", "label": "No and explain why", "requires_reason": True},
            {"id": "hold", "label": "Hold on and discuss"},
            {"id": "allow_edits", "label": "Allow all edits for this session"},
        ]
