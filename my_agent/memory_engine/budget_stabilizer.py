import json

import memory_engine.event_stream as event_stream


STATE_KEY = "budget_stabilizer"
DECAY_PER_CONTEXT_BUILD = 0.80
DEADBAND_RATIO = 0.10
HIGH_WATERMARK_RATIO = 0.80
HIGH_WATERMARK_HOLD_STEPS = 3


def _ensure_table(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS context_budget_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()


def load_budget_stabilizer_state(create=True):
    if not event_stream.event_stream_db_path().exists():
        return _empty_state()
    with event_stream.ensure_event_stream() as connection:
        if not create and not _has_table(connection, "context_budget_state"):
            return _empty_state()
        _ensure_table(connection)
        row = connection.execute("SELECT value FROM context_budget_state WHERE key = ?", (STATE_KEY,)).fetchone()
        if row is None:
            return _empty_state()
        try:
            data = json.loads(row["value"] or "{}")
        except json.JSONDecodeError:
            return _empty_state()
        return _normalize_state(data)


def reset_budget_stabilizer_state():
    if not event_stream.event_stream_db_path().exists():
        return _empty_state()
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        connection.execute("DELETE FROM context_budget_state WHERE key = ?", (STATE_KEY,))
        connection.commit()
    return _empty_state()


def apply_budget_stabilizer(status, persist=True):
    return apply_capacity_stabilizer(status, persist=persist)


def apply_capacity_stabilizer(status, candidates=None, session_key="", failure_increment=False, persist=True, advance_seq=False):
    status = dict(status or {})
    budgets = dict(status.get("budgets") or {})
    candidates = dict(candidates or {})
    raw = {
        "stm": int(candidates.get("stm") or budgets.get("stm") or 0),
        "wm": int(candidates.get("wm") or budgets.get("wm") or 0),
    }
    ranges = {
        "stm": (int(budgets.get("stm_min") or 0), int(budgets.get("stm_max") or 0)),
        "wm": (int(budgets.get("wm_min") or 0), int(budgets.get("wm_max") or 0)),
    }
    state = load_budget_stabilizer_state(create=bool(persist))
    session_key = str(session_key or "")
    if session_key and state.get("failure_session_key") != session_key:
        state["context_budget_failure_count"] = 0
        state["failure_session_key"] = session_key
    if persist or advance_seq:
        state["context_build_seq"] = int(state.get("context_build_seq") or 0) + 1
    seq = int(state.get("context_build_seq") or 0)
    stabilized = {}
    holds = {}
    for name in ("stm", "wm"):
        minimum, maximum = ranges[name]
        previous = state.get(f"{name}_stable_budget")
        hold_until = int(state.get(f"{name}_hold_until_seq") or 0)
        raw_budget = _clamp(raw[name], minimum, maximum)
        if maximum > 0 and raw_budget >= int(maximum * HIGH_WATERMARK_RATIO):
            hold_until = max(hold_until, seq + HIGH_WATERMARK_HOLD_STEPS)
        stable = _stabilize_one(raw_budget, previous, minimum, maximum, seq, hold_until)
        stabilized[name] = stable
        holds[name] = hold_until
        state[f"{name}_stable_budget"] = stable
        state[f"{name}_hold_until_seq"] = hold_until
        state[f"last_raw_{name}_budget"] = raw_budget

    if persist and failure_increment:
        state["context_budget_failure_count"] = int(state.get("context_budget_failure_count") or 0) + 1
    if persist:
        _save_state(state)

    budgets["stm_raw"] = raw["stm"]
    budgets["wm_raw"] = raw["wm"]
    budgets["stm"] = stabilized["stm"]
    budgets["wm"] = stabilized["wm"]
    budgets["stm_stable"] = stabilized["stm"]
    budgets["wm_stable"] = stabilized["wm"]
    status["budgets"] = budgets
    status["stabilizer"] = {
        "enabled": True,
        "context_build_seq": seq,
        "persisted": bool(persist),
        "decay_per_context_build": DECAY_PER_CONTEXT_BUILD,
        "deadband_ratio": DEADBAND_RATIO,
        "high_watermark_ratio": HIGH_WATERMARK_RATIO,
        "high_watermark_hold_steps": HIGH_WATERMARK_HOLD_STEPS,
        "stm_hold_until_seq": holds["stm"],
        "wm_hold_until_seq": holds["wm"],
        "stm_raw_budget": raw["stm"],
        "wm_raw_budget": raw["wm"],
        "stm_stable_budget": stabilized["stm"],
        "wm_stable_budget": stabilized["wm"],
        "continuous_session_context_budget_failure_count": int(state.get("context_budget_failure_count") or 0),
        "failure_session_key": state.get("failure_session_key") or "",
    }
    status["budget_source"] = _budget_source(status.get("budget_source"), persist)
    return status


def _stabilize_one(raw_budget, previous, minimum, maximum, seq, hold_until):
    if previous is None:
        return _clamp(raw_budget, minimum, maximum)
    previous = _clamp(int(previous), minimum, maximum)
    if raw_budget > previous:
        return _clamp(raw_budget, minimum, maximum)
    if seq <= hold_until:
        return previous
    if previous > 0 and abs(raw_budget - previous) / previous < DEADBAND_RATIO:
        return previous
    decayed = int(previous * DECAY_PER_CONTEXT_BUILD)
    return _clamp(max(raw_budget, decayed), minimum, maximum)


def _budget_source(source, persist):
    base = str(source or "dynamic")
    suffix = "stabilized" if persist else "stabilized_preview"
    if suffix in base:
        return base
    return f"{base}+{suffix}"


def _save_state(state):
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        connection.execute(
            """
            INSERT INTO context_budget_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (STATE_KEY, json.dumps(state, ensure_ascii=False, sort_keys=True)),
        )
        connection.commit()


def _has_table(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _empty_state():
    return {
        "context_build_seq": 0,
        "stm_stable_budget": None,
        "wm_stable_budget": None,
        "stm_hold_until_seq": 0,
        "wm_hold_until_seq": 0,
        "last_raw_stm_budget": None,
        "last_raw_wm_budget": None,
        "context_budget_failure_count": 0,
        "failure_session_key": "",
    }


def _normalize_state(data):
    empty = _empty_state()
    if not isinstance(data, dict):
        return empty
    result = dict(empty)
    for key in result:
        value = data.get(key)
        if key == "failure_session_key":
            result[key] = str(value or "")
            continue
        if value is None:
            result[key] = None
            continue
        try:
            result[key] = int(value)
        except (TypeError, ValueError):
            pass
    return result


def context_budget_failure_count(session_key=""):
    state = load_budget_stabilizer_state(create=False)
    session_key = str(session_key or "")
    if session_key and state.get("failure_session_key") != session_key:
        return 0
    return int(state.get("context_budget_failure_count") or 0)


def _clamp(value, minimum, maximum):
    if maximum < minimum:
        maximum = minimum
    return max(minimum, min(maximum, int(value or 0)))
