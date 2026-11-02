import json

import memory_engine.event_stream as event_stream


MIN_MANUAL_BUDGET_TOKENS = 2000
MAX_MANUAL_BUDGET_TOKENS = 200000
STATE_KEY = "context_budget_max_override"


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


def load_budget_override(create=True):
    if not event_stream.event_stream_db_path().exists():
        return {}
    with event_stream.ensure_event_stream() as connection:
        if not create and not _has_table(connection, "context_budget_state"):
            return {}
        _ensure_table(connection)
        row = connection.execute("SELECT value FROM context_budget_state WHERE key = ?", (STATE_KEY,)).fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["value"] or "{}")
        except json.JSONDecodeError:
            return {}
        return {
            key: int(value)
            for key, value in data.items()
            if key in {"stm", "wm"} and isinstance(value, int)
        }


def set_budget_override(target, tokens):
    target = str(target or "").strip().lower()
    if target not in {"stm", "wm", "both"}:
        raise RuntimeError("Usage: /memory budget set <stm|wm|both> <tokens>")
    try:
        tokens = int(tokens)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Budget max override must be an integer token count.") from error
    if tokens < MIN_MANUAL_BUDGET_TOKENS or tokens > MAX_MANUAL_BUDGET_TOKENS:
        raise RuntimeError(
            f"Budget max override must be between {MIN_MANUAL_BUDGET_TOKENS} and {MAX_MANUAL_BUDGET_TOKENS} tokens."
        )

    current = load_budget_override()
    if target in {"stm", "both"}:
        current["stm"] = tokens
    if target in {"wm", "both"}:
        current["wm"] = tokens
    _save_budget_override(current)
    return current


def reset_budget_override():
    if not event_stream.event_stream_db_path().exists():
        return {}
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        connection.execute("DELETE FROM context_budget_state WHERE key = ?", (STATE_KEY,))
        connection.commit()
    try:
        from .budget_stabilizer import reset_budget_stabilizer_state

        reset_budget_stabilizer_state()
    except Exception:
        pass
    return {}


def effective_budget_ranges(defaults, create=True):
    defaults = defaults or {}
    override = load_budget_override(create=create)
    stm_max = int(override.get("stm") or defaults.get("stm_max") or 0)
    wm_max = int(override.get("wm") or defaults.get("wm_max") or 0)
    return {
        "stm_min": min(int(defaults.get("stm_min") or 0), stm_max),
        "stm_max": stm_max,
        "wm_min": min(int(defaults.get("wm_min") or 0), wm_max),
        "wm_max": wm_max,
        "stm_max_override": override.get("stm"),
        "wm_max_override": override.get("wm"),
        "manual_min": MIN_MANUAL_BUDGET_TOKENS,
        "manual_max": MAX_MANUAL_BUDGET_TOKENS,
    }


def annotate_budget_override(status, create=True):
    status = dict(status or {})
    budgets = dict(status.get("budgets") or {})
    override = load_budget_override(create=create)
    if not override:
        status["budget_source"] = status.get("budget_source") or "dynamic"
        budgets.setdefault("manual_min", MIN_MANUAL_BUDGET_TOKENS)
        budgets.setdefault("manual_max", MAX_MANUAL_BUDGET_TOKENS)
        budgets.setdefault("stm_max_override", None)
        budgets.setdefault("wm_max_override", None)
        status["budgets"] = budgets
        return status
    budgets["stm_max_override"] = override.get("stm")
    budgets["wm_max_override"] = override.get("wm")
    budgets["manual_min"] = MIN_MANUAL_BUDGET_TOKENS
    budgets["manual_max"] = MAX_MANUAL_BUDGET_TOKENS
    status["budget_source"] = "manual_max_override"
    status["budgets"] = budgets
    return status


def _save_budget_override(data):
    with event_stream.ensure_event_stream() as connection:
        _ensure_table(connection)
        connection.execute(
            """
            INSERT INTO context_budget_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (STATE_KEY, json.dumps(data, ensure_ascii=False, sort_keys=True)),
        )
        connection.commit()


def _has_table(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None
