"""Exclusive per-workspace process lock for the Telegram Gateway."""

import hashlib
from pathlib import Path


class GatewayInstanceLock:
    def __init__(self, workspace):
        normalized = str(Path(workspace).resolve()).casefold().encode("utf-8")
        self.name = "Local\\SemaTelegramGateway-" + hashlib.sha256(normalized).hexdigest()[:20]
        self.handle = None

    def acquire(self):
        import win32api
        import win32event
        import winerror

        handle = win32event.CreateMutex(None, True, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            win32api.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is None:
            return
        import win32api
        import win32event

        try:
            win32event.ReleaseMutex(self.handle)
        except Exception:
            pass
        win32api.CloseHandle(self.handle)
        self.handle = None
