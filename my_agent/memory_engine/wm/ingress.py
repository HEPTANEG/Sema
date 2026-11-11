import json

import memory_engine.event_stream as event_stream


STATE_TABLE = "event_stream_state"
WATERMARK_KEY = "wm_ingress_watermark_slot_seq"
RUNTIME_WATERMARK_KEY = "wm_ingress_watermark_slot_seq"
RUNTIME_STAGED_WATERMARK_KEY = "staged_wm_ingress_watermark_slot_seq"
NO_WATERMARK = -1


def _ensure_table(connection):
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()


def load_persisted_wm_ingress_watermark():
    if not event_stream.event_stream_db_path().exists():
        return NO_WATERMARK
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        row = connection.execute(
            f"SELECT value FROM {STATE_TABLE} WHERE key = ?",
            (WATERMARK_KEY,),
        ).fetchone()
        if row is None:
            return NO_WATERMARK
        try:
            return int(json.loads(row["value"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return NO_WATERMARK


def effective_wm_ingress_watermark(runtime_state=None):
    values = [load_persisted_wm_ingress_watermark()]
    if isinstance(runtime_state, dict):
        for key in (RUNTIME_WATERMARK_KEY, RUNTIME_STAGED_WATERMARK_KEY):
            try:
                values.append(int(runtime_state.get(key, NO_WATERMARK)))
            except (TypeError, ValueError):
                pass
    return max(values or [NO_WATERMARK])


def stage_wm_ingress_watermark(runtime_state, slot_seq):
    if runtime_state is None:
        return effective_wm_ingress_watermark(None)
    try:
        slot_seq = int(slot_seq)
    except (TypeError, ValueError):
        return effective_wm_ingress_watermark(runtime_state)
    current = effective_wm_ingress_watermark(runtime_state)
    next_value = max(current, slot_seq)
    runtime_state[RUNTIME_WATERMARK_KEY] = next_value
    runtime_state[RUNTIME_STAGED_WATERMARK_KEY] = next_value
    return next_value


def stage_wm_ingress_from_projection(runtime_state, projection):
    omitted = projection.get("omitted_units") or []
    if not omitted:
        return effective_wm_ingress_watermark(runtime_state)
    max_end = max(int(block.get("end_index", NO_WATERMARK)) for block in omitted)
    return stage_wm_ingress_watermark(runtime_state, max_end)


def current_stm_budget(messages=None, runtime_state=None):
    """Return the effective STM budget for the current provider-context step."""
    current_messages = messages or event_stream.load_messages()
    pressure = (runtime_state or {}).get("context_pressure") if isinstance(runtime_state, dict) else None
    budgets = pressure.get("budgets") if isinstance(pressure, dict) else None
    try:
        budget = int((budgets or {}).get("stm") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        from ..dynamic_context_budget import compute_and_store_dynamic_context_budget

        pressure = compute_and_store_dynamic_context_budget(
            current_messages,
            runtime_state,
            emit_observer=False,
            stabilize=False,
            apply_stabilizer=False,
        )
        budgets = pressure.get("budgets") or {}
        budget = int(budgets.get("stm") or 10000)

    from ..capacity_planner import CapacityLimitError
    from ..context_budget_override import effective_budget_ranges
    from ..dynamic_context_budget import STM_MAX_TOKENS, STM_MIN_TOKENS, WM_MAX_TOKENS, WM_MIN_TOKENS
    from ..stm_projection import latest_eligible_unit_tokens

    ranges = effective_budget_ranges(
        {
            "stm_min": STM_MIN_TOKENS,
            "stm_max": STM_MAX_TOKENS,
            "wm_min": WM_MIN_TOKENS,
            "wm_max": WM_MAX_TOKENS,
        },
        create=False,
    )
    required = latest_eligible_unit_tokens(
        current_messages,
        effective_wm_ingress_watermark(runtime_state),
    )
    maximum = int(ranges["stm_max"])
    if required > maximum:
        raise CapacityLimitError("stm", required, maximum, "reject_current_input")
    return max(budget, required)


def stage_wm_ingress_from_messages(messages, runtime_state=None):
    """Stage omissions introduced after the final assistant message was appended."""
    from ..capacity_planner import CapacityLimitError
    from ..context_budget_override import effective_budget_ranges
    from ..dynamic_context_budget import STM_MAX_TOKENS, STM_MIN_TOKENS, WM_MAX_TOKENS, WM_MIN_TOKENS
    from ..stm_projection import build_stm_projection, latest_eligible_unit_tokens

    current_messages = messages or event_stream.load_messages()
    watermark = effective_wm_ingress_watermark(runtime_state)
    ranges = effective_budget_ranges(
        {
            "stm_min": STM_MIN_TOKENS,
            "stm_max": STM_MAX_TOKENS,
            "wm_min": WM_MIN_TOKENS,
            "wm_max": WM_MAX_TOKENS,
        },
        create=False,
    )
    required = latest_eligible_unit_tokens(current_messages, watermark)
    if required > int(ranges["stm_max"]):
        raise CapacityLimitError("stm", required, int(ranges["stm_max"]), "model_output_guard_deferred")
    budget = max(current_stm_budget(current_messages, runtime_state), required)
    projection = build_stm_projection(
        current_messages,
        budget=budget,
        wm_ingress_watermark=watermark,
    )
    staged = stage_wm_ingress_from_projection(runtime_state, projection)
    return {
        "staged_watermark": staged,
        "stm_budget": budget,
        "omitted_unit_count": len(projection.get("omitted_units") or []),
    }


def commit_staged_wm_ingress_watermark(runtime_state=None):
    staged = NO_WATERMARK
    if isinstance(runtime_state, dict):
        try:
            staged = int(runtime_state.get(RUNTIME_STAGED_WATERMARK_KEY, NO_WATERMARK))
        except (TypeError, ValueError):
            staged = NO_WATERMARK
    persisted = load_persisted_wm_ingress_watermark()
    next_value = max(persisted, staged)
    if next_value <= persisted:
        return {
            "updated": False,
            "persisted_watermark": persisted,
            "staged_watermark": staged,
            "effective_watermark": max(persisted, staged),
        }
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        connection.execute(
            f"""
            INSERT INTO {STATE_TABLE} (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (WATERMARK_KEY, json.dumps(next_value)),
        )
        connection.commit()
    return {
        "updated": True,
        "persisted_watermark": next_value,
        "staged_watermark": staged,
        "effective_watermark": next_value,
    }


def wm_ingress_watermark_status(runtime_state=None):
    persisted = load_persisted_wm_ingress_watermark()
    runtime = NO_WATERMARK
    staged = NO_WATERMARK
    if isinstance(runtime_state, dict):
        try:
            runtime = int(runtime_state.get(RUNTIME_WATERMARK_KEY, NO_WATERMARK))
        except (TypeError, ValueError):
            runtime = NO_WATERMARK
        try:
            staged = int(runtime_state.get(RUNTIME_STAGED_WATERMARK_KEY, NO_WATERMARK))
        except (TypeError, ValueError):
            staged = NO_WATERMARK
    return {
        "persisted_watermark": persisted,
        "runtime_watermark": runtime,
        "staged_watermark": staged,
        "effective_watermark": max(persisted, runtime, staged),
    }
