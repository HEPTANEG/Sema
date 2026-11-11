from .budget_stabilizer import apply_capacity_stabilizer, load_budget_stabilizer_state
from .stm_projection import latest_eligible_unit_tokens


class CapacityLimitError(RuntimeError):
    def __init__(self, target, required_tokens, maximum, action):
        self.target = str(target)
        self.required_tokens = int(required_tokens)
        self.maximum = int(maximum)
        self.action = str(action)
        super().__init__(
            f"Context capacity exceeded: target={self.target}, "
            f"required_tokens={self.required_tokens}, maximum={self.maximum}, "
            f"action={self.action}."
        )


def preview_stm_capacity(messages, pressure, wm_ingress_watermark, runtime_state=None, advance_seq=False):
    pressure = dict(pressure or {})
    budgets = dict(pressure.get("budgets") or {})
    base = int(budgets.get("stm") or 0)
    maximum = int(budgets.get("stm_max") or base)
    required = latest_eligible_unit_tokens(messages, wm_ingress_watermark)
    if required > maximum:
        raise CapacityLimitError("stm", required, maximum, "reject_current_input")
    session_key = str(((pressure.get("signals") or {}).get("continuous_session_key")) or "")
    base_preview = apply_capacity_stabilizer(
        pressure,
        candidates={"stm": base, "wm": int(budgets.get("wm") or 0)},
        session_key=session_key,
        persist=False,
        advance_seq=advance_seq,
    )
    stabilized_base = _covered_base_budget("stm", int((base_preview.get("budgets") or {}).get("stm") or base))
    candidate = max(base, required)
    preview = apply_capacity_stabilizer(
        pressure,
        candidates={"stm": candidate, "wm": int(budgets.get("wm") or 0)},
        session_key=session_key,
        persist=False,
        advance_seq=advance_seq,
    )
    return {
        "base_budget": base,
        "stabilized_base_budget": stabilized_base,
        "required_tokens": required,
        "candidate_budget": candidate,
        "effective_budget": int((preview.get("budgets") or {}).get("stm") or candidate),
        "floor_applied": required > stabilized_base,
        "preview": preview,
    }


def finalize_capacity_plan(pressure, stm_required, wm_required, persist=True):
    pressure = dict(pressure or {})
    budgets = dict(pressure.get("budgets") or {})
    stm_base = int(budgets.get("stm") or 0)
    wm_base = int(budgets.get("wm") or 0)
    stm_max = int(budgets.get("stm_max") or stm_base)
    wm_max = int(budgets.get("wm_max") or wm_base)
    stm_required = int(stm_required or 0)
    wm_required = int(wm_required or 0)
    if stm_required > stm_max:
        raise CapacityLimitError("stm", stm_required, stm_max, "reject_current_input")
    if wm_required > wm_max:
        raise CapacityLimitError("wm", wm_required, wm_max, "compact_exact_slot")
    session_key = str(((pressure.get("signals") or {}).get("continuous_session_key")) or "")
    base_preview = apply_capacity_stabilizer(
        pressure,
        candidates={"stm": stm_base, "wm": wm_base},
        session_key=session_key,
        persist=False,
        advance_seq=True,
    )
    base_effective = base_preview.get("budgets") or {}
    stm_stabilized_base = _covered_base_budget("stm", int(base_effective.get("stm") or stm_base))
    wm_stabilized_base = _covered_base_budget("wm", int(base_effective.get("wm") or wm_base))
    stm_candidate = max(stm_base, stm_required)
    wm_candidate = max(wm_base, wm_required)
    stm_floor = stm_required > stm_stabilized_base
    wm_floor = wm_required > wm_stabilized_base
    failed = stm_floor or wm_floor
    status = apply_capacity_stabilizer(
        pressure,
        candidates={"stm": stm_candidate, "wm": wm_candidate},
        session_key=session_key,
        failure_increment=failed,
        persist=persist,
        advance_seq=not persist,
    )
    effective = status.get("budgets") or {}
    count = int(((status.get("stabilizer") or {}).get("continuous_session_context_budget_failure_count")) or 0)
    plan = {
        "continuous_session_context_budget_failure_count": count,
        "stm_base_budget": stm_base,
        "stm_stabilized_base_budget": stm_stabilized_base,
        "stm_required_tokens": stm_required,
        "stm_candidate_budget": stm_candidate,
        "stm_effective_budget": int(effective.get("stm") or stm_candidate),
        "stm_floor_applied": stm_floor,
        "wm_base_budget": wm_base,
        "wm_stabilized_base_budget": wm_stabilized_base,
        "wm_required_tokens": wm_required,
        "wm_candidate_budget": wm_candidate,
        "wm_effective_budget": int(effective.get("wm") or wm_candidate),
        "wm_floor_applied": wm_floor,
        "context_budget_failed": failed,
        "capacity_plan": "floor_applied" if failed else "base",
        "capacity_recovery_action": "none",
    }
    status["capacity"] = plan
    status["failure"] = {
        **dict(status.get("failure") or {}),
        "continuous_session_context_budget_failure_count": count,
        "context_budget_failed": failed,
    }
    return status


def _covered_base_budget(target, preview_budget):
    state = load_budget_stabilizer_state(create=False)
    previous = state.get(f"{target}_stable_budget")
    if previous is None:
        return int(preview_budget)
    return max(int(preview_budget), int(previous))
