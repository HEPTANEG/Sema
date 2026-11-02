"""Public API for the Focus Segments subsystem."""

from .detector import run_focus_detector_before_context
from .ingest import run_focus_segment_maintenance, run_segment_ingest_maintenance
from .lifecycle import (
    run_focus_consistency_audit,
    run_focus_range_retirement,
    run_focus_residency_reconciliation,
)
from .maintainer import run_full_segment_maintenance
from .projection import build_working_memory_projection, render_segmented_working_memory
from .status import focus_segments_status, format_focus_segments_status

__all__ = [
    "run_focus_detector_before_context",
    "run_segment_ingest_maintenance",
    "run_focus_segment_maintenance",
    "run_full_segment_maintenance",
    "run_focus_consistency_audit",
    "run_focus_residency_reconciliation",
    "run_focus_range_retirement",
    "focus_segments_status",
    "format_focus_segments_status",
    "build_working_memory_projection",
    "render_segmented_working_memory",
]
