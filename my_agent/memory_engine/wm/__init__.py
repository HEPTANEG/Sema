"""Public API for the Working Memory subsystem."""

from .ingress import (
    NO_WATERMARK,
    RUNTIME_STAGED_WATERMARK_KEY,
    RUNTIME_WATERMARK_KEY,
    STATE_TABLE,
    WATERMARK_KEY,
    commit_staged_wm_ingress_watermark,
    current_stm_budget,
    effective_wm_ingress_watermark,
    load_persisted_wm_ingress_watermark,
    stage_wm_ingress_from_messages,
    stage_wm_ingress_from_projection,
    stage_wm_ingress_watermark,
    wm_ingress_watermark_status,
)
from .working_memory import (
    compact_exact_slot,
    format_working_memory_run,
    format_working_memory_status,
    run_working_memory_maintenance,
    working_memory_status,
)
from .pins import (
    clear_current_pin,
    load_current_pin,
    on_message_persisted,
    pin_record,
    pin_status,
    resolve_pin_for_projection,
)

__all__ = [
    "working_memory_status",
    "compact_exact_slot",
    "run_working_memory_maintenance",
    "format_working_memory_status",
    "format_working_memory_run",
    "STATE_TABLE",
    "WATERMARK_KEY",
    "RUNTIME_WATERMARK_KEY",
    "RUNTIME_STAGED_WATERMARK_KEY",
    "NO_WATERMARK",
    "load_persisted_wm_ingress_watermark",
    "effective_wm_ingress_watermark",
    "stage_wm_ingress_watermark",
    "stage_wm_ingress_from_projection",
    "current_stm_budget",
    "stage_wm_ingress_from_messages",
    "commit_staged_wm_ingress_watermark",
    "wm_ingress_watermark_status",
    "clear_current_pin",
    "load_current_pin",
    "on_message_persisted",
    "pin_record",
    "pin_status",
    "resolve_pin_for_projection",
]
