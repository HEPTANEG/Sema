import math

from .token_counter import count_message_tokens










STM_MIN_TOKENS = 1000
STM_MAX_TOKENS = 50000
WM_MIN_TOKENS = 1000
WM_MAX_TOKENS = 50000

TAU_TURNS = 4
TAU_SESSION_SECONDS = 1200
TAU_USER_GAP_SECONDS = 600
TAU_ACTIVITY = 4
TAU_TOKEN_TOTAL = 20000
TAU_TOKEN_AVERAGE = 4000
TAU_TOKEN_MAX = 10000
TAU_CURRENT_USER_INPUT = 4000
CONTINUOUS_SESSION_GAP_SECONDS = 600



def collect_context_pressure_signals(messages, runtime_state):
    runtime_state = runtime_state or {}
    temporal = _build_temporal_runtime_snapshot(runtime_state)
    last_run = runtime_state.get("last_run") if isinstance(runtime_state.get("last_run"), list) else []
    recent_tool_count = _recent_tool_count(last_run)
    token_stats = _continuous_session_token_stats()
    failure = runtime_state.get("context_build_failure") if isinstance(runtime_state.get("context_build_failure"), dict) else {}
    return {
        "recent_tool_count": recent_tool_count,
        "recent_turn_count": int(temporal.get("recent_turn_count") or 0),
        "continuous_session_duration_seconds": int(temporal.get("continuous_session_duration_seconds") or 0),
        "user_gap_seconds": _optional_number(temporal.get("user_gap_seconds")),
        "recent_session_token_total": int(token_stats.get("recent_session_token_total") or 0),
        "recent_session_token_average": int(token_stats.get("recent_session_token_average") or 0),
        "recent_session_token_max": int(token_stats.get("recent_session_token_max") or 0),
        "current_user_message_tokens": _current_user_message_tokens(messages),
        "context_build_failure": 1.0 if failure else 0.0,
        "failure_target": failure.get("target") or "",
        "failure_required_tokens": int(failure.get("required_tokens") or 0),
        "failure_failed_budget": int(failure.get("failed_budget") or 0),
    }



def compute_dynamic_context_budget(signals, ranges=None):
    signals = signals or {}
    ranges = ranges or {}
    stm_min = int(ranges.get("stm_min") or STM_MIN_TOKENS)
    stm_max = int(ranges.get("stm_max") or STM_MAX_TOKENS)
    wm_min = int(ranges.get("wm_min") or WM_MIN_TOKENS)
    wm_max = int(ranges.get("wm_max") or WM_MAX_TOKENS)
    f_turns = _saturating(signals.get("recent_turn_count"), TAU_TURNS)
    f_session = _saturating(signals.get("continuous_session_duration_seconds"), TAU_SESSION_SECONDS)
    f_user_gap = _decay(signals.get("user_gap_seconds"), TAU_USER_GAP_SECONDS)
    interaction = (
        0.30 * f_turns
        + 0.30 * f_session
        + 0.40 * f_user_gap
    )

    activity = int(signals.get("recent_tool_count") or 0)
    f_activity = _saturating(activity, TAU_ACTIVITY)
    state = f_activity

    f_token_total = _saturating(signals.get("recent_session_token_total"), TAU_TOKEN_TOTAL)
    f_token_average = _saturating(signals.get("recent_session_token_average"), TAU_TOKEN_AVERAGE)
    f_token_max = _saturating(signals.get("recent_session_token_max"), TAU_TOKEN_MAX)
    f_current_user_input = _saturating(signals.get("current_user_message_tokens"), TAU_CURRENT_USER_INPUT)
    token_load = (
        0.15 * f_token_total
        + 0.30 * f_token_average
        + 0.15 * f_token_max
        + 0.40 * f_current_user_input
    )

    stm_factor = 0.40 * interaction + 0.20 * state + 0.40 * token_load
    wm_factor = max(interaction, state, token_load)
    pressure = stm_factor
    failure_target = str(signals.get("failure_target") or "")
    failure_budget_action = "none"
    if signals.get("context_build_failure"):
        pressure = 1.0
        if failure_target == "stm":
            stm_factor = 1.0
            failure_budget_action = "force_stm_max"
        elif failure_target == "wm":
            wm_factor = 1.0
            failure_budget_action = "force_wm_max"
    stm_budget = _budget(stm_min, stm_max, stm_factor)
    wm_budget = _budget(wm_min, wm_max, wm_factor)
    return {
        "budget_source": "dynamic",
        "signals": dict(signals),
        "features": {
            "f_turns": _round(f_turns),
            "f_session": _round(f_session),
            "f_user_gap": _round(f_user_gap),
            "f_activity": _round(f_activity),
            "f_token_total": _round(f_token_total),
            "f_token_average": _round(f_token_average),
            "f_token_max": _round(f_token_max),
            "f_current_user_input": _round(f_current_user_input),
        },
        "scores": {
            "I": _round(interaction),
            "S": _round(state),
            "L": _round(token_load),
            "P_context": _round(pressure),
            "F_STM": _round(stm_factor),
            "F_WM": _round(wm_factor),
        },
        "budgets": {
            "stm_min": stm_min,
            "stm_max": stm_max,
            "stm": stm_budget,
            "wm_min": wm_min,
            "wm_max": wm_max,
            "wm": wm_budget,
        },
        "failure": {
            "context_build_failure": 1.0 if signals.get("context_build_failure") else 0.0,
            "target": failure_target,
            "required_tokens": int(signals.get("failure_required_tokens") or 0),
            "failed_budget": int(signals.get("failure_failed_budget") or 0),
            "budget_action": failure_budget_action,
        },
    }



def compute_and_store_dynamic_context_budget(messages, runtime_state=None, emit_observer=True, stabilize=False):
    signals = collect_context_pressure_signals(messages, runtime_state)
    try:
        from .context_budget_override import annotate_budget_override, effective_budget_ranges

        ranges = effective_budget_ranges(
            {
                "stm_min": STM_MIN_TOKENS,
                "stm_max": STM_MAX_TOKENS,
                "wm_min": WM_MIN_TOKENS,
                "wm_max": WM_MAX_TOKENS,
            },
            create=bool(stabilize),
        )
    except Exception:
        ranges = {
            "stm_min": STM_MIN_TOKENS,
            "stm_max": STM_MAX_TOKENS,
            "wm_min": WM_MIN_TOKENS,
            "wm_max": WM_MAX_TOKENS,
        }
        annotate_budget_override = None
    status = compute_dynamic_context_budget(signals, ranges=ranges)
    try:
        if annotate_budget_override is not None:
            status = annotate_budget_override(status, create=bool(stabilize))
    except Exception:
        pass
    try:
        from .budget_stabilizer import apply_budget_stabilizer

        status = apply_budget_stabilizer(status, persist=bool(stabilize))
    except Exception:
        pass
    if runtime_state is not None:
        runtime_state["context_pressure"] = status
        if emit_observer:
            _emit_context_pressure_observer(runtime_state, status)
    return status



def context_pressure_status(runtime_state=None):
    status = (runtime_state or {}).get("context_pressure")
    return status if isinstance(status, dict) else {}



def format_context_pressure_status(status):
    if not status:
        return "Context Pressure status:\nstatus: unavailable"
    budgets = status.get("budgets") or {}
    scores = status.get("scores") or {}
    signals = status.get("signals") or {}
    lines = [
        "Context Pressure status:",
        f"budget_source: {status.get('budget_source') or '(none)'}",
        f"stm_budget: {budgets.get('stm', 0)}",
        f"stm_range: {budgets.get('stm_min', 0)}..{budgets.get('stm_max', 0)}",
        f"wm_budget: {budgets.get('wm', 0)}",
        f"wm_range: {budgets.get('wm_min', 0)}..{budgets.get('wm_max', 0)}",
        f"stm_raw_budget: {budgets.get('stm_raw', budgets.get('stm', 0))}",
        f"wm_raw_budget: {budgets.get('wm_raw', budgets.get('wm', 0))}",
        f"stm_max_override: {budgets.get('stm_max_override')}",
        f"wm_max_override: {budgets.get('wm_max_override')}",
        f"manual_allowed_range: {budgets.get('manual_min', 0)}..{budgets.get('manual_max', 0)}",
        f"I: {scores.get('I', 0)}",
        f"S: {scores.get('S', 0)}",
        f"L: {scores.get('L', 0)}",
        f"P_context: {scores.get('P_context', 0)}",
        f"failure_budget_action: {(status.get('failure') or {}).get('budget_action', 'none')}",
        f"context_build_seq: {(status.get('stabilizer') or {}).get('context_build_seq', 0)}",
        f"stm_hold_until_seq: {(status.get('stabilizer') or {}).get('stm_hold_until_seq', 0)}",
        f"wm_hold_until_seq: {(status.get('stabilizer') or {}).get('wm_hold_until_seq', 0)}",
        "signals:",
    ]
    for key in sorted(signals.keys()):
        lines.append(f"- {key}: {signals[key]}")
    return "\n".join(lines)


def _saturating(value, tau):
    value = max(0.0, float(value or 0.0))
    return 1.0 - math.exp(-value / tau)


def _build_temporal_runtime_snapshot(runtime_state):
    from agents.temporal_state import build_temporal_runtime_snapshot

    return build_temporal_runtime_snapshot(runtime_state)


def _continuous_session_token_stats():
    try:
        import memory_engine.event_stream as event_stream

        if not event_stream.event_stream_db_path().exists():
            return _empty_token_stats()
        return event_stream.continuous_session_token_stats(gap_seconds=CONTINUOUS_SESSION_GAP_SECONDS)
    except Exception:
        return _empty_token_stats()


def _empty_token_stats():
    return {
        "recent_session_token_total": 0,
        "recent_session_token_average": 0,
        "recent_session_token_max": 0,
    }


def _decay(value, tau):
    if value is None:
        return 0.0
    return math.exp(-max(0.0, float(value)) / tau)


def _budget(minimum, maximum, factor):
    factor = max(0.0, min(1.0, float(factor or 0.0)))
    return int(minimum + factor * (maximum - minimum))


def _round(value):
    return round(float(value or 0.0), 4)


def _optional_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _recent_tool_count(last_run):
    return sum(1 for step in last_run if isinstance(step, dict))


def _current_user_message_tokens(messages):
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") == "user":
            try:
                return count_message_tokens(message)
            except Exception:
                return 0
    return 0


def _emit_context_pressure_observer(runtime_state, status):
    event = {
        "budget_source": status.get("budget_source"),
        **(status.get("budgets") or {}),
        **(status.get("scores") or {}),
        **(status.get("failure") or {}),
    }
    try:
        from bridge.ui_events import emit_ui_event

        emit_ui_event(runtime_state, "context_pressure", **event)
    except Exception:
        pass

    context = runtime_state.get("trajectory_context")
    if not (context or {}).get("enabled"):
        return
    try:
        from persistence.trajectory import record_event

        record_event(context, "context_pressure", status="ok", summary=event)
    except Exception:
        return
