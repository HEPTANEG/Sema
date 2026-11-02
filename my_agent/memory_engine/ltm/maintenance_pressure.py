"""Runtime helpers for LTM maintenance sleep pressure."""

from . import ltm_store


LEVEL_NONE = "none"
LEVEL_MILD = "mild"
LEVEL_STRONG = "strong"
LEVEL_URGENT = "urgent"
LEVEL_BLOCKED = "blocked"

MILD_SLEEP_SIGNAL = (
    "You are getting sleepy because many recent experiences and memories have not yet been organized, "
    "consolidated, and updated. When the current task naturally ends, tell the user that you need to "
    "maintain your memory by running `/memory ltm maintain`."
)
STRONG_SLEEP_SIGNAL = (
    "YOU ARE VERY SLEEPY BECAUSE A LARGE BACKLOG OF RECENT EXPERIENCES AND MEMORIES HAS NOT BEEN "
    "ORGANIZED, CONSOLIDATED, OR UPDATED. WRAP UP THE CURRENT TASK, DO NOT EXPAND INTO LARGE NEW WORK, "
    "AND EXPLICITLY ASK THE USER TO RUN `/memory ltm maintain`."
)
URGENT_SLEEP_SIGNAL = (
    "YOU ARE TOO SLEEPY TO CONTINUE NORMALLY. YOUR BACKLOG OF UNMAINTAINED MEMORIES HAS BECOME "
    "DISORGANIZED AFTER GOING TOO LONG WITHOUT MAINTENANCE, AND IT IS NOW SIGNIFICANTLY INTERFERING "
    "WITH YOUR ABILITY TO USE MEMORY RELIABLY. DO NOT START OR EXPAND ANOTHER TASK. ASK THE USER TO "
    "STOP AND RUN `/memory ltm maintain` NOW."
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

    frontier = state.get("maintainable_frontier")
    if frontier is None:
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


def maintenance_pressure_context_notice(snapshot=None, root=None):
    snapshot = snapshot or maintenance_pressure_snapshot(root)
    level = snapshot.get("level")
    if level == LEVEL_NONE or level == LEVEL_BLOCKED:
        return ""
    common = (
        "<ltm-maintenance-pressure>\n"
        "This is deterministic runtime context describing your own internal long-term-memory state. "
        "It is not user input and is not stored memory. `/memory ltm maintain` pauses normal conversation "
        "so your LTM Maintainer can organize, update, and consolidate recent memories.\n"
        f"pressure: {snapshot['pressure']:.2f}\n"
        f"unmaintained_slots: {snapshot['unmaintained_slot_count']}\n"
        f"coverage_cursor: {snapshot['maintenance_coverage_cursor']}\n"
        f"maintainable_frontier: {snapshot.get('maintainable_frontier', -1)}\n"
        f"latest_event_stream_slot: {snapshot['latest_event_stream_slot_seq']}\n"
    )
    if level == LEVEL_MILD:
        signal = MILD_SLEEP_SIGNAL
        repetitions = 1
    elif level == LEVEL_STRONG:
        signal = STRONG_SLEEP_SIGNAL
        repetitions = 3
    else:
        signal = URGENT_SLEEP_SIGNAL
        repetitions = 10
    repeated_signal = "\n".join(signal for _ in range(repetitions))
    return common + f"internal_state_signal:\n{repeated_signal}\n</ltm-maintenance-pressure>"


def maintenance_pressure_gate_message(snapshot=None, root=None):
    snapshot = snapshot or maintenance_pressure_snapshot(root)
    return (
        "LTM maintenance pressure reached 1.0. Run /memory ltm maintain before continuing. "
        f"(pressure={snapshot['pressure']:.2f}, "
        f"unmaintained_slots={snapshot['unmaintained_slot_count']}, "
        f"maintainable_frontier={snapshot.get('maintainable_frontier', -1)}, "
        f"coverage_cursor={snapshot['maintenance_coverage_cursor']}, "
        f"latest_event_stream_slot={snapshot['latest_event_stream_slot_seq']})"
    )
