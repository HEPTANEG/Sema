"""Shared user-turn maintenance pressure gate inspection."""

from memory_engine.ltm import maintenance_pressure


def user_turn_gate_status(root=None):
    """Return the same pressure decision used before a normal user turn."""
    from runtime.setup_capabilities import ltm_enabled
    if not ltm_enabled():
        return {"blocked": False, "pressure": {"blocked": False, "status": "unavailable"}}
    snapshot = maintenance_pressure.maintenance_pressure_snapshot(root=root)
    return {
        "blocked": bool(snapshot.get("blocked")),
        "pressure": snapshot,
    }
