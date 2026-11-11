"""Process-local control state for Main-Agent initiated LTM maintenance."""


REQUEST_DECISION_KEY = "_maintenance_sleep_request_decision"
MANDATORY_LATCH_KEY = "_maintenance_sleep_mandatory_latch"
REQUEST_COUNT_KEY = "_maintenance_sleep_request_count"
AUTHORIZATION_TRACE_KEY = "_maintenance_sleep_authorization_trace"


class MaintenanceControlRequested(Exception):
    """The Main Agent has reached a Runtime maintenance control boundary."""

    def __init__(self, action):
        super().__init__(action)
        self.action = action


def continuation(runtime_state):
    value = (runtime_state or {}).get("maintenance_continuation")
    return value if isinstance(value, dict) else None


def prepare_request_decision(runtime_state, snapshot=None):
    if snapshot is None:
        from memory_engine.ltm.maintenance_pressure import maintenance_pressure_snapshot
        snapshot = maintenance_pressure_snapshot()
    current_snapshot = dict(snapshot or {})
    if not isinstance(runtime_state, dict):
        pressure = float(current_snapshot.get("pressure") or 0.0)
        return {
            "mode": "mandatory" if pressure >= 1.0 else "soft" if pressure >= 0.5 else "normal",
            "request_index": 0,
            "snapshot": current_snapshot,
            "latch": None,
            "latched_now": False,
        }
    request_index = int(runtime_state.get(REQUEST_COUNT_KEY) or 0) + 1
    runtime_state[REQUEST_COUNT_KEY] = request_index
    active = continuation(runtime_state)
    if active and active.get("status") in {"failed", "handed_off"}:
        runtime_state.pop(MANDATORY_LATCH_KEY, None)
        mode = "failure"
        latch = None
        latched_now = False
    else:
        latch = mandatory_latch(runtime_state)
        latched_now = False
        if latch is None and float(current_snapshot.get("pressure") or 0.0) >= 1.0:
            latch = {
                "request_index": request_index,
                "snapshot": current_snapshot,
            }
            runtime_state[MANDATORY_LATCH_KEY] = latch
            latched_now = True
        if latch is not None:
            mode = "mandatory"
        elif float(current_snapshot.get("pressure") or 0.0) >= 0.5:
            mode = "soft"
        else:
            mode = "normal"
    decision = {
        "mode": mode,
        "request_index": request_index,
        "snapshot": current_snapshot,
        "latch": dict(latch) if isinstance(latch, dict) else None,
        "latched_now": latched_now,
    }
    runtime_state[REQUEST_DECISION_KEY] = decision
    return decision


def request_decision(runtime_state):
    value = (runtime_state or {}).get(REQUEST_DECISION_KEY)
    return value if isinstance(value, dict) else None


def mandatory_latch(runtime_state):
    value = (runtime_state or {}).get(MANDATORY_LATCH_KEY)
    return value if isinstance(value, dict) else None


def sleep_is_authorized(runtime_state):
    decision = request_decision(runtime_state)
    if decision and decision.get("mode") in {"soft", "mandatory"}:
        return True
    return mandatory_latch(runtime_state) is not None


def clear_request_decision(runtime_state):
    if isinstance(runtime_state, dict):
        runtime_state.pop(REQUEST_DECISION_KEY, None)


def release_mandatory_latch(runtime_state):
    if not isinstance(runtime_state, dict):
        return None
    return runtime_state.pop(MANDATORY_LATCH_KEY, None)


def pressure_trace_fields(snapshot, prefix=""):
    snapshot = snapshot or {}

    def integer(name):
        value = snapshot.get(name)
        return -1 if value is None else int(value)

    return {
        f"{prefix}pressure": float(snapshot.get("pressure") or 0.0),
        f"{prefix}coverage_cursor": integer("maintenance_coverage_cursor"),
        f"{prefix}maintainable_frontier": integer("maintainable_frontier"),
        f"{prefix}latest_event_stream_slot": integer("latest_event_stream_slot_seq"),
    }


def control_mode(runtime_state):
    value = continuation(runtime_state)
    if value and value.get("status") in {"failed", "handed_off"}:
        return "maintenance_failure"
    if value and value.get("status") in {"sleep_requested", "running", "resuming"}:
        return "maintenance_resume"
    return "normal"
