"""Project-local exclusion between memory work and explicit storage reclamation."""

from contextlib import contextmanager
from functools import wraps
from inspect import signature
from pathlib import Path
from threading import RLock
import os

_guard = RLock()
_states = {}


def _file_lock(root, *, shared=False, name=".activity.lock"):
    directory = Path(root or Path.cwd()) / ".sema" / "memory" / "ltm"
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / name).open("a+b")
    try:
        if os.name == "nt":
            import msvcrt


            offset, length = (os.getpid(), 1) if shared else (0, 2147483647)
            handle.seek(offset)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, length)
        else:
            import fcntl
            fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return handle
    except OSError as error:
        handle.close()
        raise RuntimeError("LTM storage is busy in another process; retry later.") from error


@contextmanager
def _write_lock(root, state):
    with state["writes"]:

        if state.get("write_depth", 0):
            yield
            return
        handle = _file_lock(root, name=".write.lock")
        state["write_depth"] = 1
        try:
            yield
        finally:
            state["write_depth"] = 0
            handle.close()


def _state(root):
    key = str(Path(root or Path.cwd()).resolve()).casefold()
    with _guard:
        return _states.setdefault(key, {"active": 0, "exclusive": False, "writes": RLock()})


@contextmanager
def activity(root=None):
    state = _state(root)
    with _guard:
        if state["exclusive"]:
            raise RuntimeError("LTM storage is busy with physical cleanup.")
        if not state["active"]:
            state["activity_handle"] = _file_lock(root, shared=True)
        state["active"] += 1
    try:
        yield
    finally:
        with _guard:
            state["active"] -= 1
            if not state["active"]:
                state.pop("activity_handle").close()


@contextmanager
def exclusive(root=None):
    state = _state(root)
    with _guard:
        if state["active"] or state["exclusive"]:
            raise RuntimeError("LTM storage is busy; retry when memory tasks finish.")
        handle = _file_lock(root)
        state["exclusive"] = True
    try:
        with state["writes"]:
            yield
    finally:
        with _guard:
            state["exclusive"] = False
            handle.close()


def memory_task(function):
    sig = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        root = bound.arguments.get("root")
        owner = bound.arguments.get("self")
        if owner is not None:
            root = getattr(owner, "session", {}).get("root", root)
        with activity(root):
            return function(*args, **kwargs)
    return wrapped


def serialized_write(function):
    sig = signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        root = sig.bind(*args, **kwargs).arguments.get("root")
        with activity(root), _write_lock(root, _state(root)):
            return function(*args, **kwargs)
    return wrapped
