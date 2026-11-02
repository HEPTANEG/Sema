import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


MAX_OUTPUT_LINES = 400
MAX_OUTPUT_CHARS = 8000
START_OUTPUT_WAIT_SECONDS = 1.0

_records = {}
_lock = threading.Lock()


@dataclass
class BackgroundCommandRecord:
    command_id: str
    command: str
    cwd: str
    process: subprocess.Popen
    started_at: str
    output_lines: deque = field(default_factory=lambda: deque(maxlen=MAX_OUTPUT_LINES))
    output_chars: int = 0


def start_background_command(command, cwd=None):

    command_text = str(command or "").strip()
    if not command_text:
        raise RuntimeError("start_background_command requires a command.")

    cwd_path = _resolve_cwd(cwd)
    popen_kwargs = {}
    if _is_windows() and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command_text,
        cwd=cwd_path,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        **popen_kwargs,
    )

    command_id = uuid4().hex[:8]
    record = BackgroundCommandRecord(
        command_id=command_id,
        command=command_text,
        cwd=str(cwd_path),
        process=process,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    with _lock:
        _records[command_id] = record

    reader = threading.Thread(target=_read_output, args=(record,), daemon=True)
    reader.start()
    time.sleep(START_OUTPUT_WAIT_SECONDS)
    return format_background_command(record)


def list_background_commands():
    records = _snapshot_records()
    if not records:
        return "No background commands."
    lines = ["Background commands:"]
    for record in records:
        lines.append(_format_record_summary(record))
    return "\n".join(lines)


def has_running_commands():
    return any(record.process.poll() is None for record in _snapshot_records())


def read_background_command_output(command_id, max_chars=None):
    record = _get_record(command_id)
    output = _record_output(record, int(max_chars or MAX_OUTPUT_CHARS))
    header = _format_record_summary(record)
    if not output:
        output = "(no output captured yet)"
    return f"{header}\n\n{output}"


def stop_background_command(command_id):
    record = _get_record(command_id)
    if record.process.poll() is None:
        _terminate_process_tree(record.process.pid)
        try:
            record.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            record.process.kill()
    return format_background_command(record)


def format_background_command(record):
    output = _record_output(record, 1200)
    lines = [
        "Background command:",
        f"id: {record.command_id}",
        f"pid: {record.process.pid}",
        f"status: {_record_status(record)}",
        f"cwd: {record.cwd}",
        f"command: {record.command}",
    ]
    if output:
        lines.extend(["", "Recent output:", output])
    return "\n".join(lines)


def reset_background_commands_for_tests():

    for record in _snapshot_records():
        if record.process.poll() is None:
            _terminate_process_tree(record.process.pid)
    with _lock:
        _records.clear()


def _read_output(record):
    if record.process.stdout is None:
        return
    for line in record.process.stdout:
        with _lock:
            record.output_lines.append(line.rstrip("\n"))
            record.output_chars += len(line)


def _resolve_cwd(cwd):
    workspace = Path.cwd().resolve()
    if not cwd:
        return workspace
    candidate = Path(str(cwd))
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise RuntimeError(f"Background command cwd does not exist: {cwd}")
    if candidate != workspace and workspace not in candidate.parents:
        raise RuntimeError("Background command cwd must be inside the current workspace.")
    return candidate


def _snapshot_records():
    with _lock:
        return list(_records.values())


def _get_record(command_id):
    key = str(command_id or "").strip()
    with _lock:
        record = _records.get(key)
    if record is None:
        raise RuntimeError(f"Unknown background command id: {key}")
    return record


def _record_status(record):
    return "running" if record.process.poll() is None else f"exited {record.process.returncode}"


def _record_output(record, max_chars):
    with _lock:
        text = "\n".join(record.output_lines)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _format_record_summary(record):
    return (
        f"{record.command_id} pid={record.process.pid} status={_record_status(record)} "
        f"cwd={record.cwd} command={record.command}"
    )


def _terminate_process_tree(pid):
    if _is_windows():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return
    try:
        subprocess.run(["pkill", "-TERM", "-P", str(pid)], capture_output=True, text=True)
    except FileNotFoundError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _is_windows():
    return os.name == "nt"
