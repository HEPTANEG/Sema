import re
import shlex
from pathlib import Path


GIT_DANGEROUS_RE = re.compile(r"\bgit\s+(reset|clean|checkout|restore)\b", re.IGNORECASE)
WINDOWS_ABSOLUTE_RE = re.compile(r"[A-Za-z]:\\[^\s'\"<>|]+")


class EvalWorkspacePolicy:
    def __init__(self, workspace, event_sink=None):
        self.workspace = Path(workspace).resolve()
        self.event_sink = event_sink

    def _decision(self, kind, allowed, reason, target=""):
        event = {
            "type": "event",
            "event": "confirmation",
            "kind": kind,
            "decision": "allowed" if allowed else "denied",
            "reason": reason,
            "target": target,
        }
        if self.event_sink is not None:
            self.event_sink(event)
        return {"allowed": bool(allowed), "reason": reason}

    def _resolve_inside_workspace(self, path_text):
        try:
            path = Path(str(path_text or "").strip())
            if not path.is_absolute():
                path = self.workspace / path
            resolved = path.resolve()
        except OSError as error:
            return False, str(path_text), f"path resolution failed: {error}"

        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            return False, str(resolved), "path is outside eval workspace"
        return True, str(resolved), "inside eval workspace"

    def confirm_write(self, path_text, _content, permissions=None):
        if not (permissions or {}).get("allow_edits", True):
            return self._decision("file_write", False, "task permissions deny file edits", str(path_text))
        allowed, target, reason = self._resolve_inside_workspace(path_text)
        return self._decision("file_write", allowed, reason, target)

    def confirm_insert(self, path_text, _anchor, _content, _anchor_context=None, permissions=None, allow_external=False):

        if not (permissions or {}).get("allow_edits", True):
            return self._decision("file_edit", False, "task permissions deny file edits", str(path_text))
        allowed, target, reason = self._resolve_inside_workspace(path_text)
        return self._decision("file_edit", allowed, reason, target)

    def confirm_replace(self, path_text, _old_block, _new_block, permissions=None, allow_external=False):

        if not (permissions or {}).get("allow_edits", True):
            return self._decision("file_edit", False, "task permissions deny file edits", str(path_text))
        allowed, target, reason = self._resolve_inside_workspace(path_text)
        return self._decision("file_edit", allowed, reason, target)

    def confirm_patch(self, patch, permissions=None):
        if not (permissions or {}).get("allow_edits", True):
            return self._decision("file_patch", False, "task permissions deny file edits")
        paths = self._patch_paths(patch)
        if not paths:
            return self._decision("file_patch", False, "patch did not declare file paths")
        for item in paths:
            allowed, target, reason = self._resolve_inside_workspace(item)
            if not allowed:
                return self._decision("file_patch", False, reason, target)
        return self._decision("file_patch", True, "all patch paths are inside eval workspace", ", ".join(paths))

    def confirm_run_command(self, command, permissions=None):
        if not (permissions or {}).get("allow_run_commands", True):
            return self._decision("run_command", False, "task permissions deny command execution", command)
        if GIT_DANGEROUS_RE.search(command or ""):
            return self._decision("run_command", False, "dangerous git undo/reset command is denied", command)
        outside = self._outside_paths_in_command(command)
        if outside:
            return self._decision("run_command", False, f"command references outside path: {outside[0]}", command)
        return self._decision("run_command", True, "command allowed in eval workspace", command)

    def confirm_file_mutating_command(self, command, permissions=None):
        if GIT_DANGEROUS_RE.search(command or ""):
            return self._decision("file_mutating_command", False, "dangerous git undo/reset command is denied", command)
        outside = self._outside_paths_in_command(command)
        if outside:
            return self._decision("file_mutating_command", False, f"command references outside path: {outside[0]}", command)
        return self._decision("file_mutating_command", True, "mutating command allowed in eval workspace", command)

    def confirm_session_control(self, action_name, details, permissions=None):
        return self._decision("session_control", False, f"{action_name} is denied in eval bridge", details)

    def confirm_undo_edit(self, details, permissions=None):
        return self._decision("undo_edit", False, "undo is denied in eval bridge", details)

    def confirm_tool_checkpoint(self, _tool_count):
        return "Yes for all checkpoints in this run"

    def confirm_persistent_action(self, action, target):
        return self._decision("persistent_state", False, f"{action} is denied in eval bridge", target)

    def confirm_external_path_access(self, action_name, details, permissions=None):
        return self._decision("external_path", False, f"{action_name} is outside eval workspace", details)

    def _patch_paths(self, patch):
        paths = []
        for line in str(patch or "").splitlines():
            for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
                if line.startswith(prefix):
                    paths.append(line[len(prefix) :].strip())
        return paths

    def _outside_paths_in_command(self, command):
        outside = []
        for path_text in WINDOWS_ABSOLUTE_RE.findall(command or ""):
            allowed, target, _reason = self._resolve_inside_workspace(path_text)
            if not allowed:
                outside.append(target)

        for token in self._command_tokens(command):
            if ".." not in token:
                continue
            if any(ch in token for ch in ("/", "\\")):
                allowed, target, _reason = self._resolve_inside_workspace(token)
                if not allowed:
                    outside.append(target)
        return outside

    def _command_tokens(self, command):
        try:
            return shlex.split(command or "", posix=False)
        except ValueError:
            return str(command or "").split()
