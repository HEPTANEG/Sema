"""Small Runtime wake queue used by background agents.

Wake is deliberately a delivery primitive: it preserves source, kind and
payload, while the Main Agent decides what the notification means.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import threading
import time
import uuid


@dataclass
class WakeEvent:
    source: str
    kind: str
    payload: dict
    session_key: str | None = None
    wake_id: str = field(default_factory=lambda: "wake_" + uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)



    processing_class: str = "user_like"
    parent_turn_id: str | None = None
    parent_request_id: str | None = None


class WakeQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._pending = deque()
        self._in_flight = set()
        self._known = set()
        self._delivered = set()
        self._feedback_sessions = set()

    def put(self, event: WakeEvent):
        if not isinstance(event, WakeEvent):
            raise TypeError("event must be WakeEvent")
        with self._condition:
            if event.wake_id in self._known or event.wake_id in self._delivered:
                return False
            if event.source == "retriever" and event.kind == "feedback" and event.session_key:
                if event.session_key in self._feedback_sessions:
                    return False
                self._feedback_sessions.add(event.session_key)
            self._known.add(event.wake_id)
            self._pending.append(event)
            self._condition.notify_all()
            return True

    def get(self, timeout=None):
        with self._condition:
            if not self._pending:
                self._condition.wait(timeout)
            event = self._pending.popleft() if self._pending else None
            if event is not None:
                self._in_flight.add(event.wake_id)
            return event

    def mark_delivered(self, event):
        with self._condition:
            self._delivered.add(event.wake_id)
            self._in_flight.discard(event.wake_id)
            if event.source == "retriever" and event.kind == "feedback" and event.session_key:
                self._feedback_sessions.discard(event.session_key)
            self._condition.notify_all()

    def requeue(self, event):
        with self._condition:
            self._in_flight.discard(event.wake_id)
            if event.wake_id in self._delivered:
                return False
            self._pending.appendleft(event)
            self._condition.notify_all()
            return True

    def discard_session(self, session_key):
        with self._condition:
            kept = deque(item for item in self._pending if item.session_key != session_key)
            self._pending = kept
            self._feedback_sessions.discard(session_key)

    def snapshot(self):
        with self._condition:
            return list(self._pending)

    def has_work(self):
        with self._condition:
            return bool(self._pending or self._in_flight)


class WakeScheduler:
    def __init__(self, queue: WakeQueue, deliver, *, name="wake-scheduler"):
        self.queue = queue
        self.deliver = deliver
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self.queue._condition:
            self.queue._condition.notify_all()

    def _run(self):
        while not self._stop.is_set():
            event = self.queue.get(timeout=0.5)
            if event is None:
                continue
            try:
                delivered = bool(self.deliver(event))
            except Exception:
                delivered = False
            if delivered:
                self.queue.mark_delivered(event)
            elif not self._stop.is_set():

                time.sleep(0.01)
                self.queue.requeue(event)


def wake_event(
    source,
    kind,
    payload,
    *,
    session_key=None,
    wake_id=None,
    processing_class="user_like",
    parent_turn_id=None,
    parent_request_id=None,
):
    return WakeEvent(
        source=source,
        kind=kind,
        payload=dict(payload or {}),
        session_key=session_key,
        wake_id=wake_id or "wake_" + uuid.uuid4().hex,
        processing_class=processing_class,
        parent_turn_id=parent_turn_id,
        parent_request_id=parent_request_id,
    )
