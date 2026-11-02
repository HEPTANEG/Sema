import threading
import time

from runtime.agent_phase import AgentPhase


def test_main_and_maintainer_are_mutually_exclusive():
    phase = AgentPhase()
    assert phase.acquire_main()
    acquired = threading.Event()

    def acquire_maintainer():
        phase.acquire_maintainer()
        acquired.set()
        phase.release_maintainer()

    worker = threading.Thread(target=acquire_maintainer)
    worker.start()
    time.sleep(0.02)
    assert not acquired.is_set()
    assert phase.snapshot()["phase"] == "main_active"

    phase.release_main()
    worker.join(timeout=1)
    assert acquired.is_set()
    assert phase.snapshot()["phase"] == "idle"


def test_main_cannot_start_while_maintainer_is_active():
    phase = AgentPhase()
    phase.acquire_maintainer()
    assert not phase.acquire_main()
    assert phase.snapshot() == {
        "phase": "maintainer_active",
        "main_active": False,
        "maintainer_active": True,
    }
    phase.release_maintainer()
    assert phase.acquire_main()
    phase.release_main()
