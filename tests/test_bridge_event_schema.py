from bridge.event_schema import EVENT_SCHEMAS, EVENT_TYPES, event_schema


def test_event_registry_has_unique_types_and_metadata():
    assert len(EVENT_TYPES) == len(set(EVENT_TYPES))
    assert set(EVENT_TYPES) == set(EVENT_SCHEMAS)
    for event_type, metadata in EVENT_SCHEMAS.items():
        assert metadata["category"]
        assert metadata["producer"]
        assert isinstance(metadata["required"], tuple)
        assert isinstance(metadata["user_visible"], bool)
        assert event_schema(event_type) is metadata


def test_current_bridge_event_contract_covers_runtime_and_tui_events():
    expected = {
        "turn_started", "user_message",
        "assistant_stage", "assistant_delta", "assistant_content", "final_answer",
        "model_thinking", "model_finished", "model_error", "tool_started", "tool_finished",
        "confirmation_requested", "context_pressure", "working_memory_preflight_started",
        "working_memory_preflight_finished", "focus_detector_started", "focus_detector_finished", "focus_shifted",
        "working_memory_maintenance_started", "working_memory_maintenance_finished",
        "segment_ingest_started", "segment_ingest_finished",
        "full_segment_maintenance_started", "full_segment_maintenance_finished",
        "memory_push_started", "memory_push_finished",
        "ltm_formation_started", "ltm_formation_finished",
        "formation_backfill_started", "formation_backfill_finished",
        "memorize_started", "memorize_finished",
        "ltm_maintenance_started", "ltm_maintenance_model_turn", "ltm_maintenance_model_error",
            "ltm_maintenance_stage_started", "ltm_maintenance_stage_completed",
        "ltm_maintenance_reasoning_available", "ltm_maintenance_message_delta",
        "ltm_maintenance_repetition_warning",
        "ltm_maintenance_runtime_notice",
        "ltm_maintenance_tool_started", "ltm_maintenance_tool_finished",
        "ltm_maintenance_plan_committed", "ltm_maintenance_coverage_advanced",
        "ltm_maintenance_final_check_started", "ltm_maintenance_final_check_failed",
            "ltm_maintenance_completed", "ltm_maintenance_failed",
            "retriever_started", "retriever_finished",
        "wm_ingress_watermark_status", "working_memory_status",
        "focus_segment_status", "ltm_formation_status", "cancel_requested", "turn_cancelled",
            "agent_stopped", "agent_error", "error", "wake_delivered", "wake_deferred", "wake_failed",
    }
    assert expected == set(EVENT_SCHEMAS)
