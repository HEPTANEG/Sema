"""Human- and test-facing registry for the Python-to-TUI event contract.

This module intentionally does not validate or transform events at runtime.
The bridge remains permissive in V1 so protocol behavior stays unchanged.
"""


EVENT_SCHEMAS = {
    "turn_started": {"category": "turn", "required": (), "producer": "ui_bridge_server", "user_visible": False},
    "user_message": {"category": "turn", "required": ("content",), "producer": "ui_bridge_server", "user_visible": True},
    "assistant_stage": {"category": "assistant", "required": ("label",), "producer": "ui_bridge_server/agent", "user_visible": True},
    "assistant_delta": {"category": "assistant", "required": ("delta",), "producer": "agent", "user_visible": True},
    "assistant_content": {"category": "assistant", "required": ("content",), "producer": "agent", "user_visible": True},
    "final_answer": {"category": "turn", "required": ("content",), "producer": "agent/ui_bridge_server", "user_visible": True},
    "model_thinking": {"category": "model", "required": (), "producer": "model_runtime", "user_visible": True},
    "model_finished": {"category": "model", "required": (), "producer": "model_runtime", "user_visible": False},
    "model_error": {"category": "error", "required": ("error",), "producer": "model_runtime", "user_visible": True},
    "tool_started": {"category": "tool", "required": ("tool",), "producer": "structured_loop", "user_visible": True},
    "tool_finished": {"category": "tool", "required": ("tool", "status"), "producer": "structured_loop", "user_visible": True},
    "confirmation_requested": {"category": "confirmation", "required": ("confirmation_id", "choices"), "producer": "ui_bridge_confirmation", "user_visible": True},
    "context_pressure": {"category": "context", "required": (), "producer": "runtime", "user_visible": False},
    "working_memory_preflight_started": {"category": "background", "required": (), "producer": "agent", "user_visible": False},
    "working_memory_preflight_finished": {"category": "background", "required": (), "producer": "agent", "user_visible": False},
    "focus_detector_started": {"category": "background", "required": (), "producer": "context_builder", "user_visible": False},
    "focus_detector_finished": {"category": "background", "required": (), "producer": "context_builder", "user_visible": False},
    "focus_shifted": {"category": "background", "required": ("title",), "producer": "context_builder", "user_visible": False},
    "working_memory_maintenance_started": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "working_memory_maintenance_finished": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "segment_ingest_started": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "segment_ingest_finished": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "full_segment_maintenance_started": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "full_segment_maintenance_finished": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "retriever_started": {"category": "background", "required": (), "producer": "memory_retrieval", "user_visible": False},
    "retriever_finished": {"category": "background", "required": (), "producer": "memory_retrieval", "user_visible": False},
    "memory_push_started": {"category": "background", "required": (), "producer": "context_builder", "user_visible": False},
    "memory_push_finished": {"category": "background", "required": (), "producer": "context_builder", "user_visible": False},
    "ltm_formation_started": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "ltm_formation_finished": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "formation_backfill_started": {"category": "background", "required": (), "producer": "agent/background_tasks", "user_visible": False},
    "formation_backfill_finished": {"category": "background", "required": (), "producer": "agent/background_tasks", "user_visible": False},
    "memorize_started": {"category": "background", "required": (), "producer": "memorize", "user_visible": False},
    "memorize_finished": {"category": "background", "required": (), "producer": "memorize", "user_visible": False},
    "ltm_maintenance_started": {"category": "maintenance", "required": ("run_id",), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_stage_started": {"category": "maintenance", "required": ("run_id", "stage"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_stage_completed": {"category": "maintenance", "required": ("run_id", "stage", "status"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_model_turn": {"category": "maintenance", "required": ("run_id",), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_model_error": {"category": "maintenance", "required": ("run_id", "error"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_reasoning_available": {"category": "maintenance", "required": ("run_id", "content"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_message_delta": {"category": "maintenance", "required": ("run_id", "content"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_repetition_warning": {"category": "maintenance", "required": ("run_id", "content", "repeated_rounds"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_runtime_notice": {"category": "maintenance", "required": ("run_id", "content"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_tool_started": {"category": "maintenance", "required": ("run_id", "tool"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_tool_finished": {"category": "maintenance", "required": ("run_id", "tool", "status"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_plan_committed": {"category": "maintenance", "required": ("run_id", "tool", "status"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_coverage_advanced": {"category": "maintenance", "required": ("run_id", "tool", "status"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_final_check_started": {"category": "maintenance", "required": ("run_id",), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_final_check_failed": {"category": "maintenance", "required": ("run_id",), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_completed": {"category": "maintenance", "required": ("run_id", "status"), "producer": "maintenance_runner", "user_visible": True},
    "ltm_maintenance_failed": {"category": "maintenance", "required": ("run_id", "status"), "producer": "maintenance_runner", "user_visible": True},
    "wm_ingress_watermark_status": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "working_memory_status": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "focus_segment_status": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "ltm_formation_status": {"category": "background", "required": (), "producer": "background_tasks", "user_visible": False},
    "cancel_requested": {"category": "turn", "required": (), "producer": "ui_bridge_server", "user_visible": True},
    "turn_cancelled": {"category": "turn", "required": ("message",), "producer": "ui_bridge_server", "user_visible": True},
    "agent_stopped": {"category": "turn", "required": ("message",), "producer": "ui_bridge_server", "user_visible": True},
    "agent_error": {"category": "error", "required": ("error",), "producer": "ui_bridge_server", "user_visible": True},
    "error": {"category": "error", "required": ("message",), "producer": "ui_bridge_server", "user_visible": True},
    "wake_delivered": {"category": "background", "required": ("wake_id", "source", "kind"), "producer": "wake_scheduler", "user_visible": False},
    "wake_failed": {"category": "background", "required": ("wake_id", "error"), "producer": "wake_scheduler", "user_visible": False},
}

EVENT_TYPES = tuple(EVENT_SCHEMAS)


def event_schema(event_type):
    """Return metadata for documentation/tests without enforcing it at runtime."""
    return EVENT_SCHEMAS.get(event_type)
