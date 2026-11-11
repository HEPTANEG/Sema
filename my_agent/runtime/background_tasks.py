from runtime.dependency_validation import validate_dependencies


WORKING_MEMORY_REQUIRED_DEPENDENCIES = (
    "run_working_memory_maintenance",
    "threading",
    "trace_event",
    "ui_event",
)
FOCUS_SEGMENT_REQUIRED_DEPENDENCIES = (
    "run_focus_segment_maintenance",
    "threading",
    "trace_event",
    "ui_event",
)
EVENT_STREAM_REQUIRED_DEPENDENCIES = (
    "commit_staged_wm_ingress_watermark",
    "run_formation_maintenance",
    "run_focus_consistency_audit",
    "run_focus_range_retirement",
    "run_focus_residency_reconciliation",
    "run_formation_backfill",
    "run_segment_ingest_maintenance",
    "run_working_memory_maintenance",
    "stage_wm_ingress_from_messages",
    "threading",
    "trace_event",
    "ui_event",
)


def schedule_working_memory_maintenance(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        WORKING_MEMORY_REQUIRED_DEPENDENCIES,
        "background_tasks.schedule_working_memory_maintenance",
    )
    snapshot = [dict(message) for message in messages]
    trace_id = (runtime_state or {}).get("trace_id")

    def worker():
        try:
            dependencies["ui_event"](runtime_state, "working_memory_maintenance_started", path="background")
            result = dependencies["run_working_memory_maintenance"](snapshot, runtime_state=runtime_state)
        except Exception as error:
            result = {
                "triggered": False,
                "updated": False,
                "reason": "background working memory maintenance failed",
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "working_memory_maintenance_finished",
            path="background",
            status="error" if result.get("error") else "ok",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            error=result.get("error") or "",
        )
        dependencies["ui_event"](
            runtime_state,
            "working_memory_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            action=result.get("action", ""),
            slot_seq=result.get("slot_seq", ""),
            error=result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "working_memory",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                action=result.get("action", ""),
                slot_seq=result.get("slot_seq", ""),
                error=result.get("error") or "",
            )

    thread = dependencies["threading"].Thread(target=worker, daemon=True)
    from runtime.setup_capabilities import register_thread
    register_thread(thread)
    thread.start()
    return thread


def schedule_focus_segment_maintenance(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        FOCUS_SEGMENT_REQUIRED_DEPENDENCIES,
        "background_tasks.schedule_focus_segment_maintenance",
    )
    snapshot = [dict(message) for message in messages]
    trace_id = (runtime_state or {}).get("trace_id")

    def worker():
        try:
            dependencies["ui_event"](runtime_state, "segment_ingest_started", path="background")
            result = dependencies["run_focus_segment_maintenance"](snapshot, runtime_state)
        except Exception as error:
            result = {
                "triggered": False,
                "updated": False,
                "reason": "background focus segment maintenance failed",
                "detector_error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "segment_ingest_finished",
            path="background",
            status="error" if result.get("detector_error") else "ok",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            error=result.get("detector_error") or "",
        )
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            range_updated=result.get("range_updated", False),
            detector_recorded=result.get("detector_recorded", False),
            detector_error=result.get("detector_error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_segment",
                status="error" if result.get("detector_error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                range_updated=result.get("range_updated", False),
                detector_recorded=result.get("detector_recorded", False),
                detector_error=result.get("detector_error") or "",
            )

    thread = dependencies["threading"].Thread(target=worker, daemon=True)
    from runtime.setup_capabilities import register_thread
    register_thread(thread)
    thread.start()
    return thread


def schedule_event_stream_memory_maintenance(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        EVENT_STREAM_REQUIRED_DEPENDENCIES,
        "background_tasks.schedule_event_stream_memory_maintenance",
    )
    snapshot = [dict(message) for message in messages]
    trace_id = (runtime_state or {}).get("trace_id")

    def emit_working_memory(result):
        dependencies["ui_event"](
            runtime_state,
            "working_memory_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            action=result.get("action", ""),
            slot_seq=result.get("slot_seq", ""),
            error=result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "working_memory",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                action=result.get("action", ""),
                slot_seq=result.get("slot_seq", ""),
                error=result.get("error") or "",
            )

    def emit_segment_ingest(result):
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            range_updated=False,
            detector_recorded=False,
            detector_error=result.get("error") or "",
            processed_block_count=result.get("processed_block_count", 0),
            assigned_count=result.get("assigned_count", 0),
            unassigned_count=result.get("unassigned_count", 0),
            resolved_count=result.get("resolved_count", 0),
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_segment",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                range_updated=False,
                detector_recorded=False,
                detector_error=result.get("error") or "",
                processed_block_count=result.get("processed_block_count", 0),
                assigned_count=result.get("assigned_count", 0),
                unassigned_count=result.get("unassigned_count", 0),
                resolved_count=result.get("resolved_count", 0),
            )

    def emit_full_segment_maintainer(result):
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            detector_error=result.get("error") or "",
            tool_round_count=result.get("tool_round_count", 0),
            proposed_action_count=result.get("proposed_action_count", 0),
            applied_action_count=result.get("applied_action_count", 0),
            rejected_action_count=result.get("rejected_action_count", 0),
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_segment_full_maintainer",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                tool_round_count=result.get("tool_round_count", 0),
                proposed_action_count=result.get("proposed_action_count", 0),
                applied_action_count=result.get("applied_action_count", 0),
                rejected_action_count=result.get("rejected_action_count", 0),
                error=result.get("error") or "",
            )

    def emit_formation(result):
        dependencies["ui_event"](
            runtime_state,
            "ltm_formation_status",
            path="background",
            triggered=result.get("triggered", False),
            reason=result.get("reason", ""),
            processed_count=result.get("processed_count", 0),
            completed_count=result.get("completed_count", 0),
            failed_count=result.get("failed_count", 0),
            object_count=result.get("object_count", 0),
            link_count=result.get("link_count", 0),
            error=result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "ltm_formation",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                reason=result.get("reason", ""),
                processed_count=result.get("processed_count", 0),
                completed_count=result.get("completed_count", 0),
                failed_count=result.get("failed_count", 0),
                object_count=result.get("object_count", 0),
                link_count=result.get("link_count", 0),
                error=result.get("error") or "",
            )

    def emit_focus_consistency(result):
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            consistency_status=result.get("status", "error"),
            consistency_error_count=result.get("error_count", 0),
            consistency_warning_count=result.get("warning_count", 0),
            consistency_repairable_issue_count=result.get("repairable_issue_count", 0),
            consistency_should_trigger_full=bool(result.get("should_trigger_full_maintainer")),
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_segment_consistency",
                status="error" if result.get("status") == "error" else "ok",
                error_count=result.get("error_count", 0),
                warning_count=result.get("warning_count", 0),
                repairable_issue_count=result.get("repairable_issue_count", 0),
                should_trigger_full=bool(result.get("should_trigger_full_maintainer")),
            )

    def emit_focus_range_retirement(result):
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            retired_assignment_count=result.get("retired_assignment_count", 0),
            retired_slot_count=result.get("retired_slot_count", 0),
            error=result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_range_retirement",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                retired_assignment_count=result.get("retired_assignment_count", 0),
                retired_slot_count=result.get("retired_slot_count", 0),
                error=result.get("error") or "",
            )

    def emit_focus_residency_reconciliation(result):
        dependencies["ui_event"](
            runtime_state,
            "focus_segment_status",
            path="background",
            triggered=result.get("triggered", False),
            updated=result.get("updated", False),
            reason=result.get("reason", ""),
            restored_assignment_count=result.get("restored_assignment_count", 0),
            restored_slot_count=result.get("restored_slot_count", 0),
            skipped_assignment_count=result.get("skipped_assignment_count", 0),
            error=result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "focus_residency_reconciliation",
                status="error" if result.get("error") else "ok",
                triggered=result.get("triggered", False),
                updated=result.get("updated", False),
                reason=result.get("reason", ""),
                restored_assignment_count=result.get("restored_assignment_count", 0),
                restored_slot_count=result.get("restored_slot_count", 0),
                skipped_assignment_count=result.get("skipped_assignment_count", 0),
                error=result.get("error") or "",
            )

    def worker():
        try:
            post_answer_stage = dependencies["stage_wm_ingress_from_messages"](snapshot, runtime_state)
        except Exception as error:
            post_answer_stage = {
                "staged_watermark": -1,
                "stm_budget": 0,
                "omitted_unit_count": 0,
                "error": str(error),
            }
        try:
            watermark_result = dependencies["commit_staged_wm_ingress_watermark"](runtime_state)
        except Exception as error:
            watermark_result = {
                "updated": False,
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "wm_ingress_watermark_status",
            path="background",
            updated=bool(watermark_result.get("updated", False)),
            persisted_watermark=watermark_result.get("persisted_watermark", -1),
            staged_watermark=watermark_result.get("staged_watermark", -1),
            effective_watermark=watermark_result.get("effective_watermark", -1),
            post_answer_omitted_unit_count=post_answer_stage.get("omitted_unit_count", 0),
            stm_budget=post_answer_stage.get("stm_budget", 0),
            error=post_answer_stage.get("error") or watermark_result.get("error") or "",
        )
        if trace_id:
            dependencies["trace_event"](
                {"trace_id": trace_id},
                "wm_ingress_watermark",
                status="error" if watermark_result.get("error") else "ok",
                updated=bool(watermark_result.get("updated", False)),
                persisted_watermark=watermark_result.get("persisted_watermark", -1),
                staged_watermark=watermark_result.get("staged_watermark", -1),
                effective_watermark=watermark_result.get("effective_watermark", -1),
                post_answer_omitted_unit_count=post_answer_stage.get("omitted_unit_count", 0),
                stm_budget=post_answer_stage.get("stm_budget", 0),
                error=post_answer_stage.get("error") or watermark_result.get("error") or "",
            )

        try:
            dependencies["ui_event"](runtime_state, "working_memory_maintenance_started", path="background")
            wm_result = dependencies["run_working_memory_maintenance"](snapshot, runtime_state=runtime_state)
        except Exception as error:
            wm_result = {
                "triggered": False,
                "updated": False,
                "reason": "background working memory maintenance failed",
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "working_memory_maintenance_finished",
            path="background",
            status="error" if wm_result.get("error") else "ok",
            triggered=wm_result.get("triggered", False),
            updated=wm_result.get("updated", False),
            error=wm_result.get("error") or "",
        )
        emit_working_memory(wm_result)
        from runtime.setup_capabilities import observe_window
        observe_window(snapshot, runtime_state)

        try:
            dependencies["ui_event"](runtime_state, "segment_ingest_started", path="background")
            ingest_result = dependencies["run_segment_ingest_maintenance"](snapshot, runtime_state)
        except Exception as error:
            ingest_result = {
                "triggered": False,
                "updated": False,
                "reason": "background segment ingest failed",
                "processed_block_count": 0,
                "assigned_count": 0,
                "unassigned_count": 0,
                "resolved_count": 0,
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "segment_ingest_finished",
            path="background",
            status="error" if ingest_result.get("error") else "ok",
            triggered=ingest_result.get("triggered", False),
            updated=ingest_result.get("updated", False),
            error=ingest_result.get("error") or "",
        )
        emit_segment_ingest(ingest_result)

        try:
            dependencies["ui_event"](runtime_state, "full_segment_maintenance_started", path="background")
            if "run_full_segment_maintenance" not in dependencies:
                full_result = {
                    "triggered": False,
                    "updated": False,
                    "reason": "focus maintainer dependency missing",
                    "tool_round_count": 0,
                    "proposed_action_count": 0,
                    "applied_action_count": 0,
                    "rejected_action_count": 0,
                    "error": "",
                }
            else:
                full_result = dependencies["run_full_segment_maintenance"](snapshot, runtime_state)
        except Exception as error:
            full_result = {
                "triggered": False,
                "updated": False,
                "reason": "background focus maintainer failed",
                "tool_round_count": 0,
                "proposed_action_count": 0,
                "applied_action_count": 0,
                "rejected_action_count": 0,
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "full_segment_maintenance_finished",
            path="background",
            status="error" if full_result.get("error") else "ok",
            triggered=full_result.get("triggered", False),
            updated=full_result.get("updated", False),
            error=full_result.get("error") or "",
        )
        emit_full_segment_maintainer(full_result)

        try:
            dependencies["ui_event"](runtime_state, "ltm_formation_started", path="background", mode="incremental")
            formation_result = dependencies["run_formation_maintenance"](snapshot, runtime_state=runtime_state)
        except Exception as error:
            formation_result = {
                "triggered": False,
                "reason": "background formation failed",
                "processed_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "object_count": 0,
                "link_count": 0,
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "ltm_formation_finished",
            path="background",
            mode="incremental",
            status="error" if formation_result.get("error") else "ok",
            error=formation_result.get("error") or "",
        )
        emit_formation(formation_result)

        try:
            dependencies["ui_event"](runtime_state, "formation_backfill_started", path="background", mode="backfill")
            historical_result = dependencies["run_formation_backfill"](
                snapshot, runtime_state=runtime_state
            )
        except Exception as error:
            historical_result = {
                "triggered": False,
                "reason": "background formation backfill failed",
                "status": "pending",
                "processed_count": 0,
                "completed_count": 0,
                "failed_count": 0,
                "error": str(error),
            }
        dependencies["ui_event"](
            runtime_state,
            "formation_backfill_finished",
            path="background",
            mode="backfill",
            status="error" if historical_result.get("error") else "ok",
            error=historical_result.get("error") or "",
        )
        dependencies["ui_event"](
            runtime_state,
            "ltm_formation_status",
            path="background",
            triggered=historical_result.get("triggered", False),
            reason=historical_result.get("reason", ""),
            historical=True,
            status=historical_result.get("status", ""),
            processed_count=historical_result.get("processed_count", 0),
            completed_count=historical_result.get("completed_count", 0),
            failed_count=historical_result.get("failed_count", 0),
            error=historical_result.get("error") or "",
        )

        try:
            reconciliation_result = dependencies["run_focus_residency_reconciliation"](
                snapshot,
                runtime_state,
            )
        except Exception as error:
            reconciliation_result = {
                "triggered": False,
                "updated": False,
                "reason": "background focus residency reconciliation failed",
                "error": str(error),
            }
        emit_focus_residency_reconciliation(reconciliation_result)

        try:
            pre_audit_result = dependencies["run_focus_consistency_audit"](
                snapshot,
                runtime_state,
                persist=False,
            )
        except Exception as error:
            pre_audit_result = {
                "status": "error",
                "error_count": 1,
                "issues": [],
                "error": str(error),
            }

        try:
            retirement_result = dependencies["run_focus_range_retirement"](
                snapshot,
                runtime_state,
                pre_audit_report=pre_audit_result,
            )
        except Exception as error:
            retirement_result = {
                "triggered": False,
                "updated": False,
                "reason": "background focus range retirement failed",
                "error": str(error),
            }
        emit_focus_range_retirement(retirement_result)

        try:
            consistency_result = dependencies["run_focus_consistency_audit"](
                snapshot,
                runtime_state,
                persist=True,
            )
        except Exception as error:
            consistency_result = {
                "status": "error",
                "error_count": 1,
                "warning_count": 0,
                "repairable_issue_count": 0,
                "should_trigger_full_maintainer": False,
                "error": str(error),
            }
        emit_focus_consistency(consistency_result)

    thread = dependencies["threading"].Thread(target=worker, daemon=True)
    from runtime.setup_capabilities import register_thread
    register_thread(thread)
    thread.start()
    return thread


def schedule_final_answer_background_tasks(messages, runtime_state=None, dependency_source=None):
    dependencies = validate_dependencies(
        dependency_source,
        ("schedule_event_stream_memory_maintenance",),
        "background_tasks.schedule_final_answer_background_tasks",
    )
    return [dependencies["schedule_event_stream_memory_maintenance"](messages, runtime_state)]
