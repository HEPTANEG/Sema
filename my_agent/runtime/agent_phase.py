"""Project-local mutual exclusion between Main Agent and LTM Maintainer."""

from contextlib import contextmanager
from pathlib import Path
from threading import Condition, RLock


class AgentPhase:
    def __init__(self):
        self._condition = Condition(RLock())
        self._main_active = False
        self._maintainer_active = False

    def acquire_main(self, blocking=False):
        with self._condition:
            if blocking:
                while self._main_active or self._maintainer_active:
                    self._condition.wait()
            elif self._main_active or self._maintainer_active:
                return False
            self._main_active = True
            return True

    def release_main(self):
        with self._condition:
            self._main_active = False
            self._condition.notify_all()

    def acquire_maintainer(self):
        with self._condition:
            while self._main_active or self._maintainer_active:
                self._condition.wait()
            self._maintainer_active = True

    def release_maintainer(self):
        with self._condition:
            self._maintainer_active = False
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            if self._maintainer_active:
                phase = "maintainer_active"
            elif self._main_active:
                phase = "main_active"
            else:
                phase = "idle"
            return {
                "phase": phase,
                "main_active": self._main_active,
                "maintainer_active": self._maintainer_active,
            }


_guard = RLock()
_phases = {}


def get_agent_phase(root=None):
    key = str(Path(root or Path.cwd()).resolve()).casefold()
    with _guard:
        return _phases.setdefault(key, AgentPhase())


@contextmanager
def maintainer_phase(root=None):
    phase = get_agent_phase(root)
    phase.acquire_maintainer()
    try:
        yield
    finally:
        phase.release_maintainer()
