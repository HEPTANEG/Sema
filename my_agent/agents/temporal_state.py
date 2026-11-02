from datetime import datetime, timezone

RUNTIME_KEY = "temporal_runtime"
CONTINUOUS_SESSION_GAP_SECONDS = 600


def _utc_now():
    return datetime.now(timezone.utc)


def _iso_utc(value):
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso_datetime(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(started_at, now):
    started = _parse_iso_datetime(started_at)
    if not started:
        return None
    return max(0, int((now - started).total_seconds()))


def _seconds_between(started_at, ended_at):
    started = _parse_iso_datetime(started_at)
    ended = _parse_iso_datetime(ended_at)
    if not started or not ended:
        return None
    return max(0, int((ended - started).total_seconds()))


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ensure_process_temporal_state(runtime_state):
    if runtime_state is None:
        return {}
    state = runtime_state.get(RUNTIME_KEY)
    if not isinstance(state, dict):
        state = {}
        runtime_state[RUNTIME_KEY] = state
    state.setdefault("process_started_at", _iso_utc(_utc_now()))
    return state


def build_runtime_clock_snapshot(now=None):
    now = now or _utc_now()
    now = now.astimezone(timezone.utc)
    local_now = now.astimezone()
    return {
        "utc_now": _iso_utc(now),
        "local_now": local_now.isoformat(),
        "local_date": local_now.date().isoformat(),
        "timezone": local_now.tzname() or "local",
    }


def mark_turn_started(runtime_state):
    state = ensure_process_temporal_state(runtime_state)
    if not state:
        return state
    now = _utc_now()
    now_text = _iso_utc(now)
    previous_user_turn_at = state.get("last_user_turn_at") or ""
    user_gap_seconds = _seconds_between(previous_user_turn_at, now_text)
    assistant_gap_seconds = _seconds_between(state.get("last_assistant_turn_at"), now_text)
    continue_session = user_gap_seconds is not None and user_gap_seconds <= CONTINUOUS_SESSION_GAP_SECONDS
    state["turn_started_at"] = now_text
    state["previous_user_turn_at"] = previous_user_turn_at
    state["last_user_turn_at"] = now_text
    state["user_gap_seconds"] = user_gap_seconds
    state["assistant_gap_seconds"] = assistant_gap_seconds
    if continue_session:
        if not state.get("continuous_session_started_at"):
            state["continuous_session_started_at"] = previous_user_turn_at
        state["recent_turn_count"] = int(state.get("recent_turn_count") or 1) + 1
    else:
        state["continuous_session_started_at"] = now_text
        state["recent_turn_count"] = 1
    return state


def mark_assistant_turn_finished(runtime_state):
    state = ensure_process_temporal_state(runtime_state)
    if not state:
        return state
    state["last_assistant_turn_at"] = _iso_utc(_utc_now())
    return state


def build_temporal_runtime_snapshot(runtime_state=None, now=None):
    now = now or _utc_now()
    now = now.astimezone(timezone.utc)
    state = ensure_process_temporal_state(runtime_state) if runtime_state is not None else {}
    clock = build_runtime_clock_snapshot(now)
    recent_turn_count = state.get("recent_turn_count")
    try:
        recent_turn_count = int(recent_turn_count)
    except (TypeError, ValueError):
        recent_turn_count = 1 if state.get("last_user_turn_at") else 0
    user_gap_seconds = _optional_int(state.get("user_gap_seconds"))
    if user_gap_seconds is None:
        user_gap_seconds = _seconds_between(
            state.get("previous_user_turn_at"),
            state.get("last_user_turn_at"),
        )
    assistant_gap_seconds = _optional_int(state.get("assistant_gap_seconds"))
    if assistant_gap_seconds is None:
        assistant_gap_seconds = _seconds_between(
            state.get("last_assistant_turn_at"),
            state.get("last_user_turn_at"),
        )
    return {
        "utc_now": clock["utc_now"],
        "local_now": clock["local_now"],
        "timezone": clock["timezone"],
        "process_started_at": state.get("process_started_at", ""),
        "process_age_seconds": _age_seconds(state.get("process_started_at"), now),
        "turn_started_at": state.get("turn_started_at", ""),
        "turn_age_seconds": _age_seconds(state.get("turn_started_at"), now),
        "previous_user_turn_at": state.get("previous_user_turn_at", ""),
        "last_user_turn_at": state.get("last_user_turn_at", ""),
        "last_assistant_turn_at": state.get("last_assistant_turn_at", ""),
        "continuous_session_started_at": state.get("continuous_session_started_at", ""),
        "continuous_session_duration_seconds": _seconds_between(
            state.get("continuous_session_started_at"),
            state.get("last_user_turn_at"),
        ),
        "user_gap_seconds": user_gap_seconds,
        "assistant_gap_seconds": assistant_gap_seconds,
        "recent_turn_count": recent_turn_count,
    }


def _format_age(value):
    if value is None:
        return "(unknown)"
    return f"{value}s"


def format_temporal_runtime_state(runtime_state=None):
    snapshot = build_temporal_runtime_snapshot(runtime_state)
    lines = [
        "Runtime rhythm:",
        f"local_now: {snapshot['local_now']}",
        f"utc_now: {snapshot['utc_now']}",
        f"timezone: {snapshot['timezone']}",
        f"process_started_at: {snapshot.get('process_started_at') or '(none)'}",
        f"process_age: {_format_age(snapshot.get('process_age_seconds'))}",
        f"turn_started_at: {snapshot.get('turn_started_at') or '(none)'}",
        f"turn_age: {_format_age(snapshot.get('turn_age_seconds'))}",
        f"continuous_session_started_at: {snapshot.get('continuous_session_started_at') or '(none)'}",
        f"continuous_session_duration: {_format_age(snapshot.get('continuous_session_duration_seconds'))}",
        f"recent_turn_count: {snapshot.get('recent_turn_count', 0)}",
        f"previous_user_turn_at: {snapshot.get('previous_user_turn_at') or '(none)'}",
        f"last_user_turn_at: {snapshot.get('last_user_turn_at') or '(none)'}",
        f"user_gap: {_format_age(snapshot.get('user_gap_seconds'))}",
        f"last_assistant_turn_at: {snapshot.get('last_assistant_turn_at') or '(none)'}",
        f"assistant_gap: {_format_age(snapshot.get('assistant_gap_seconds'))}",
    ]
    return "\n".join(lines)


def format_temporal_state(runtime_state=None):
    return "\n".join(["Sema time:", format_temporal_runtime_state(runtime_state)])
