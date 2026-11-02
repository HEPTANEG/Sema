"""Access to the pinned, unmodified HaluMem evaluator sources.

The adapter owns transport, retries, and Sema-specific diagnostics. Prompt
text and aggregation code are loaded from the vendored official revision so a
local paraphrase cannot silently become the benchmark definition.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


OFFICIAL_REPOSITORY = "https://github.com/MemTensor/HaluMem"
OFFICIAL_COMMIT = "718f16ff0c83413b1c86fa83fc13cc1a639871f9"
VENDOR_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "halumem" / OFFICIAL_COMMIT
SOURCE_MANIFEST_PATH = VENDOR_ROOT / "SOURCE_MANIFEST.json"
PROMPTS_PATH = VENDOR_ROOT / "eval" / "prompts.py"
EVAL_TOOLS_PATH = VENDOR_ROOT / "eval" / "eval_tools.py"
EVALUATION_PATH = VENDOR_ROOT / "eval" / "evaluation.py"


def _read_assignments(path: Path, names: set[str]) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, str] = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        for target in targets:
            if target in names:
                values[target] = ast.literal_eval(value)
    missing = names - values.keys()
    if missing:
        raise RuntimeError(f"Pinned HaluMem source is missing constants: {sorted(missing)}")
    return values


def _load_aggregate_function():
    tree = ast.parse(EVALUATION_PATH.read_text(encoding="utf-8"), filename=str(EVALUATION_PATH))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"compute_f1", "aggregate_eval_results"}:
            selected.append(node)
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(EVALUATION_PATH), "exec"), namespace)
    return namespace["aggregate_eval_results"]


PROMPTS = _read_assignments(
    PROMPTS_PATH,
    {"PROMPT_MEMOS"},
)
EVALUATOR_PROMPTS = _read_assignments(
    EVAL_TOOLS_PATH,
    {
        "EVALUATION_PROMPT_FOR_QUESTION",
        "EVALUATION_PROMPT_FOR_MEMORY_INTEGRITY",
        "EVALUATION_PROMPT_FOR_MEMORY_ACCURACY",
        "EVALUATION_PROMPT_FOR_UPDATE_MEMORY",
    },
)
official_aggregate_eval_results = _load_aggregate_function()


def aggregate_question_answering(records: list[dict]) -> dict:
    """Apply the official QA aggregation to a QA-only record set.

    The pinned official aggregate function also divides by optional
    memory-integrity/update/interference categories. A Sema session may
    legitimately have none of those records, so this adapter preserves the
    official QA denominator and valid-record rules without inventing records
    for unrelated categories.
    """
    correct = 0
    hallucination = 0
    omission = 0
    valid = 0
    for record in records:
        result_type = record.get("result_type")
        if result_type not in {"Correct", "Hallucination", "Omission"}:
            continue
        valid += 1
        if result_type == "Correct":
            correct += 1
        elif result_type == "Hallucination":
            hallucination += 1
        else:
            omission += 1

    total = len(records)

    def ratio(value, denominator):
        return value / denominator if denominator else None

    return {
        "correct_qa_ratio(all)": ratio(correct, total),
        "correct_qa_ratio(valid)": ratio(correct, valid),
        "hallucination_qa_ratio(all)": ratio(hallucination, total),
        "hallucination_qa_ratio(valid)": ratio(hallucination, valid),
        "omission_qa_ratio(all)": ratio(omission, total),
        "omission_qa_ratio(valid)": ratio(omission, valid),
        "qa_valid_num": valid,
        "qa_num": total,
    }


def source_manifest() -> dict:
    files = {}
    for path in (PROMPTS_PATH, EVAL_TOOLS_PATH, EVALUATION_PATH, VENDOR_ROOT / "eval" / "eval_memos.py", VENDOR_ROOT / "eval" / "llms.py"):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[str(path.relative_to(VENDOR_ROOT)).replace("\\", "/")] = digest
    manifest = {
        "repository": OFFICIAL_REPOSITORY,
        "commit": OFFICIAL_COMMIT,
        "files": files,
    }
    if SOURCE_MANIFEST_PATH.is_file():
        recorded = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
        if recorded.get("repository") != manifest["repository"] or recorded.get("commit") != manifest["commit"]:
            raise RuntimeError("Pinned HaluMem source manifest identity does not match the adapter")
        if recorded.get("files") != files:
            raise RuntimeError("Pinned HaluMem source file hash changed")
        manifest["downloaded_at_utc"] = recorded.get("downloaded_at_utc")
    return manifest
