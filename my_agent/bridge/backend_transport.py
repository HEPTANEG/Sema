"""Local Windows Named Pipe transport for the shared Sema backend."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pywintypes
import win32file
import win32pipe


PIPE_PREFIX = r"\\.\pipe\sema-backend-"
PIPE_BUFFER_SIZE = 65536


def workspace_root():
    return Path.cwd().resolve()


def pipe_name(root=None):
    value = str(Path(root or workspace_root()).resolve()).lower()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return PIPE_PREFIX + digest


def json_line(payload):
    return (json.dumps(payload, ensure_ascii=True, default=str) + "\n").encode("utf-8")


class NamedPipeConnection:
    def __init__(self, handle):
        self.handle = handle
        self.write_lock = threading.Lock()
        self.closed = threading.Event()

    def write(self, payload):
        data = json_line(payload)
        with self.write_lock:
            win32file.WriteFile(self.handle, data)

    def read_chunks(self, poll=False):
        while not self.closed.is_set():
            try:
                _, data = win32file.ReadFile(self.handle, PIPE_BUFFER_SIZE)
            except pywintypes.error as error:
                if error.winerror in {6, 109, 233}:
                    return
                if error.winerror == 232:
                    if self.closed.is_set():
                        return
                    if poll:
                        time.sleep(0.01)
                        yield b""
                        continue
                    time.sleep(0.01)
                    continue
                raise
            if not data:
                return
            yield data

    def close(self):
        self.closed.set()
        try:
            win32file.CloseHandle(self.handle)
        except Exception:
            pass


def create_server_pipe(name=None):
    flags = win32pipe.PIPE_ACCESS_DUPLEX
    mode = win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT
    handle = win32pipe.CreateNamedPipe(
        name or pipe_name(),
        flags,
        mode,
        win32pipe.PIPE_UNLIMITED_INSTANCES,
        PIPE_BUFFER_SIZE,
        PIPE_BUFFER_SIZE,
        0,
        None,
    )
    return handle


def set_blocking_mode(handle):
    win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_NOWAIT, None, None)


def connect_pipe(name=None, timeout_ms=3000, blocking=True):
    target = name or pipe_name()
    win32pipe.WaitNamedPipe(target, timeout_ms)
    handle = win32file.CreateFile(
        target,
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    win32pipe.SetNamedPipeHandleState(
        handle,
        win32pipe.PIPE_READMODE_BYTE | (win32pipe.PIPE_WAIT if blocking else win32pipe.PIPE_NOWAIT),
        None,
        None,
    )
    return NamedPipeConnection(handle)
