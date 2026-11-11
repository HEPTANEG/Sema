import inspect
import os
from pathlib import Path
import subprocess
import sys

import memory_engine.wm as wm
import memory_engine.wm.ingress as ingress
import memory_engine.wm.working_memory as working_memory


EXPECTED_WM_API = {
    "working_memory_status": "(messages=None, runtime_state=None)",
    "run_working_memory_maintenance": "(messages=None, force=False, runtime_state=None)",
    "compact_exact_slot": "(slot_seq, runtime_state=None)",
    "format_working_memory_status": "(status)",
    "format_working_memory_run": "(result)",
}

EXPECTED_INGRESS_API = {
    "load_persisted_wm_ingress_watermark": "()",
    "effective_wm_ingress_watermark": "(runtime_state=None)",
    "stage_wm_ingress_watermark": "(runtime_state, slot_seq)",
    "stage_wm_ingress_from_projection": "(runtime_state, projection)",
    "current_stm_budget": "(messages=None, runtime_state=None)",
    "stage_wm_ingress_from_messages": "(messages, runtime_state=None)",
    "commit_staged_wm_ingress_watermark": "(runtime_state=None)",
    "wm_ingress_watermark_status": "(runtime_state=None)",
}


def test_wm_package_exports_canonical_public_contract():
    for name, expected_signature in EXPECTED_WM_API.items():
        assert getattr(wm, name) is getattr(working_memory, name)
        assert str(inspect.signature(getattr(wm, name))) == expected_signature
    for name, expected_signature in EXPECTED_INGRESS_API.items():
        assert getattr(wm, name) is getattr(ingress, name)
        assert str(inspect.signature(getattr(wm, name))) == expected_signature


def test_wm_package_imports_cleanly_in_a_fresh_interpreter():
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
                "import memory_engine.wm; "
                "import memory_engine.wm.working_memory; "
                "import memory_engine.wm.ingress"
            ),
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
