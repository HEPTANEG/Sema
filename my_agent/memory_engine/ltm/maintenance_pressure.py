"""Runtime helpers for LTM maintenance sleep pressure."""

from . import ltm_store


LEVEL_NONE = "none"
LEVEL_MILD = "mild"
LEVEL_STRONG = "strong"
LEVEL_URGENT = "urgent"
LEVEL_BLOCKED = "blocked"

MILD_SLEEP_SIGNAL = (
    "Your memory-maintenance pressure is rising. At a safe pause point, you may call "
    "sleep_for_maintenance with a concise wake_prompt describing the task, progress, and next step."
)
STRONG_SLEEP_SIGNAL = (
    "Your memory-maintenance pressure is high. Find a safe pause point soon and call "
    "sleep_for_maintenance with a concise wake_prompt describing the task, progress, and next step."
)
URGENT_SLEEP_SIGNAL = (
    "Your memory-maintenance pressure is urgent. Finish only the immediate safe step, then call "
    "sleep_for_maintenance with a concise wake_prompt describing the task, progress, and next step."
)


def maintenance_pressure_level(pressure):
    pressure = float(pressure or 0.0)
    if pressure >= 1.0:
        return LEVEL_BLOCKED
    if pressure >= 0.9:
        return LEVEL_URGENT
    if pressure >= 0.75:
        return LEVEL_STRONG
    if pressure >= 0.5:
        return LEVEL_MILD
    return LEVEL_NONE


def maintenance_pressure_snapshot(root=None):
    state = ltm_store.get_maintenance_runtime_state(root)




    from . import formation

    frontier = formation.maintainable_slot_frontier(root=root)
    frontier = int(frontier)
    cursor = int(state.get("maintenance_coverage_cursor") or -1)
    unmaintained = max(0, frontier - cursor)
    pressure = min(1.0, unmaintained / ltm_store.MAINTENANCE_PRESSURE_THRESHOLD)
    level = maintenance_pressure_level(pressure)
    return {
        "level": level,
        "blocked": level == LEVEL_BLOCKED,
        "pressure": pressure,
        "unmaintained_slot_count": unmaintained,
        "unmaintained_maintainable_slot_count": unmaintained,
        "maintenance_coverage_cursor": cursor,
        "maintainable_frontier": frontier,
        "coverage_target_slot_seq": max(cursor, frontier),
        "latest_event_stream_slot_seq": int(state.get("latest_event_stream_slot_seq") or -1),
    }


def maintenance_pressure_context_notice(snapshot=None, root=None, runtime_state=None):
    from runtime.maintenance_sleep import continuation, prepare_request_decision, request_decision
    decision = request_decision(runtime_state)
    if decision is None:
        snapshot = snapshot or maintenance_pressure_snapshot(root)
        decision = prepare_request_decision(runtime_state, snapshot)
    else:
        snapshot = snapshot or decision.get("snapshot") or maintenance_pressure_snapshot(root)
    mandatory_gate = bool(decision and decision.get("mode") == "mandatory")
    level = LEVEL_BLOCKED if mandatory_gate else snapshot.get("level")
    active_continuation = continuation(runtime_state)
    maintenance_failed = bool(active_continuation and active_continuation.get("status") in {"failed", "handed_off"})
    if level == LEVEL_NONE and not maintenance_failed:
        return ""
    common = (
        "<ltm-maintenance-pressure>\n"
        "This is deterministic runtime context describing your own internal long-term-memory state. "
        "It is not user input and is not stored memory. `sleep_for_maintenance` pauses this logical task, "
        "runs the LTM Maintainer, and resumes from your wake_prompt.\n"
        f"pressure: {snapshot['pressure']:.2f}\n"
        f"unmaintained_slots: {snapshot['unmaintained_slot_count']}\n"
        f"coverage_cursor: {snapshot['maintenance_coverage_cursor']}\n"
        f"maintainable_frontier: {snapshot.get('maintainable_frontier', -1)}\n"
        f"latest_event_stream_slot: {snapshot['latest_event_stream_slot_seq']}\n"
    )
    if mandatory_gate:
        latch_snapshot = ((decision or {}).get("latch") or {}).get("snapshot") or {}
        trigger_frontier = latch_snapshot.get("maintainable_frontier")
        common += (
            "mandatory_gate_latched: true\n"
            f"gate_trigger_pressure: {float(latch_snapshot.get('pressure') or 0.0):.2f}\n"
            f"gate_trigger_frontier: {-1 if trigger_frontier is None else int(trigger_frontier)}\n"
        )
    if active_continuation and active_continuation.get("status") in {"failed", "handed_off"}:
        common += (
            f"maintenance_failure_count: {int(active_continuation.get('failure_count') or 0)}\n"
            f"maintenance_run_id: {active_continuation.get('run_id') or 'pending'}\n"
            f"maintenance_failure_reason: {active_continuation.get('failure_reason') or 'unknown'}\n"
            "The task is paused. Only retry_ltm_maintenance or return_maintenance_failure is allowed. "
            "Do not continue the original task until maintenance succeeds.\n"
        )
        return common + "</ltm-maintenance-pressure>"
    elif level == LEVEL_BLOCKED:
        common += (
            "Maintenance is mandatory at the next model decision boundary. Complete the already-returned "
            "tool batch, then call sleep_for_maintenance alone. No ordinary tool call or final answer is accepted.\n"
        )
    if level == LEVEL_MILD:
        signal = MILD_SLEEP_SIGNAL
        repetitions = 1
    elif level == LEVEL_STRONG:
        signal = STRONG_SLEEP_SIGNAL
        repetitions = 3
    elif level == LEVEL_URGENT:
        signal = URGENT_SLEEP_SIGNAL
        repetitions = 10
    else:
        return common + "</ltm-maintenance-pressure>"
    repeated_signal = "\n".join(signal for _ in range(repetitions))
    return common + f"internal_state_signal:\n{repeated_signal}\n</ltm-maintenance-pressure>"


def maintenance_pressure_gate_message(snapshot=None, root=None):
    snapshot = snapshot or maintenance_pressure_snapshot(root)
    return (
        "LTM maintenance pressure reached 1.0. Call sleep_for_maintenance before continuing. "
        f"(pressure={snapshot['pressure']:.2f}, "
        f"unmaintained_slots={snapshot['unmaintained_slot_count']}, "
        f"maintainable_frontier={snapshot.get('maintainable_frontier', -1)}, "
        f"coverage_cursor={snapshot['maintenance_coverage_cursor']}, "
        f"latest_event_stream_slot={snapshot['latest_event_stream_slot_seq']})"
    )
