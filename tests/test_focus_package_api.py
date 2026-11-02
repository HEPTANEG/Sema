import inspect
import os
from pathlib import Path
import subprocess
import sys

import memory_engine.focus as focus


EXPECTED_SIGNATURES = {
    "run_focus_detector_before_context": "(messages=None, runtime_state=None)",
    "run_segment_ingest_maintenance": "(messages=None, runtime_state=None)",
    "run_focus_segment_maintenance": "(messages=None, runtime_state=None)",
    "run_full_segment_maintenance": "(messages=None, runtime_state=None, force=False)",
    "run_focus_consistency_audit": "(messages=None, runtime_state=None, persist=False)",
    "run_focus_residency_reconciliation": "(messages=None, runtime_state=None)",
    "run_focus_range_retirement": "(messages=None, runtime_state=None, pre_audit_report=None)",
    "focus_segments_status": "(messages=None, runtime_state=None)",
    "format_focus_segments_status": "(status)",
    "build_working_memory_projection": "(records, render_record, budget)",
    "render_segmented_working_memory": "(records, render_record, budget=50000)",
}


def test_focus_package_exports_the_public_contract():
    assert focus.__all__ == list(EXPECTED_SIGNATURES)
    for name, expected_signature in EXPECTED_SIGNATURES.items():
        canonical = getattr(focus, name)
        assert str(inspect.signature(canonical)) == expected_signature


def test_focus_package_imports_cleanly_in_a_fresh_interpreter():
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "my_agent"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import memory_engine.focus; "
                "import memory_engine.focus.common; "
                "import memory_engine.focus.ranges; "
                "import memory_engine.focus.store; "
                "import memory_engine.focus.context; "
                "import memory_engine.focus.detector; "
                "import memory_engine.focus.ingest; "
                "import memory_engine.focus.lifecycle; "
                "import memory_engine.focus.projection; "
                "import memory_engine.focus.maintainer; "
                "import memory_engine.focus.status"
            ),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
