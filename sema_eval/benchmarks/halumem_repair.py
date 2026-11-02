"""Offline, append-only repair of saved HaluMem Judge responses."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from .halumem_response import decode_judgment


def repair_saved_judgments(run_directory):
    from . import halumem

    source = Path(run_directory).resolve()
    checkpoint = json.loads((source / "checkpoint.json").read_text(encoding="utf-8"))
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    original_evaluation = json.loads((source / "evaluation.json").read_text(encoding="utf-8"))
    original_hashes = {p.name: halumem.sha256_file(p) for p in source.iterdir() if p.is_file()}
    result = checkpoint["result"]
    cache = result["evaluation_checkpoint"]
    changes = []
    for group, items in cache.items():
        for key, judgment in items.items():
            if judgment.get("status") != "INCONCLUSIVE" or judgment.get("category") != "judge_parse_error":
                continue
            raw = judgment.get("raw_response") or judgment.get("judge_raw_response")
            if not raw:
                continue
            schema = "qa" if group == "qa" else key.split(":")[0]
            parsed, response_source, error = decode_judgment({"raw_response": raw}, schema)
            if error:
                continue
            changes.append(
                {
                    "cache_key": key,
                    "group": group,
                    "before": dict(judgment),
                    "decoded": parsed,
                    "response_source": response_source,
                }
            )
            judgment.update(parsed)
            judgment.update(status="SCORED", response_source=response_source, repaired_offline=True)
            if schema == "qa":
                judgment["label"] = parsed["evaluation_result"]
            judgment.pop("error", None)
            judgment.pop("category", None)
    evaluation = halumem.evaluate_all(
        result,
        config={
            "model": original_evaluation.get("judge_model") or metadata.get("judge_model"),
            "max_tokens": original_evaluation.get("judge_max_tokens"),
            "temperature": metadata.get("model_temperature", 0),
            "timeout": metadata.get("model_timeout", 60),
        },
        allow_requests=False,
    )
    if "runtime_request_stats" not in result:
        result["legacy_request_stats"] = dict(result.get("request_stats") or {})
    halumem.refresh_request_stats(result, evaluation)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    revision = source / "revisions" / stamp
    revision.mkdir(parents=True, exist_ok=False)
    for filename in (
        "predictions.jsonl",
        "halumem_eval_results.jsonl",
        "diagnostics.jsonl",
        "transcript.md",
    ):
        if (source / filename).is_file():
            shutil.copy2(source / filename, revision / filename)
    audit = {
        "repair_type": "saved_judge_json_decode_v1",
        "source_run": str(source),
        "source_hashes": original_hashes,
        "repair_time": stamp,
        "repair_code_commit": halumem.git_revision(),
        "repair_source_sha256": halumem.sha256_file(Path(__file__)),
        "adapter_source_sha256": halumem.sha256_file(Path(halumem.__file__)),
        "parser_sha256": halumem.sha256_file(Path(__file__).with_name("halumem_response.py")),
        "changes": changes,
        "new_api_requests": 0,
        "original_inconclusive": original_evaluation.get("inconclusive_count"),
        "revised_inconclusive": evaluation.get("inconclusive_count"),
        "original_metrics": original_evaluation.get("metrics"),
        "revised_metrics": evaluation.get("metrics"),
    }
    metadata.update(
        {
            "artifact_type": "offline_scoring_revision",
            "source_run": str(source),
            "request_stats": result["request_stats"],
            "actual_requests": None,
            "legacy_request_stats": result.get("legacy_request_stats"),
            "repair_audit": "repair_audit.json",
        }
    )
    halumem.write_json(revision / "metadata.json", metadata)
    halumem.write_json(revision / "evaluation.json", evaluation)
    halumem.write_json(revision / "evaluation_checkpoint.json", cache)
    halumem.write_json(revision / "repair_audit.json", audit)
    halumem.write_jsonl(
        revision / "per_question.jsonl",
        halumem.merge_questions(result["questions"], evaluation),
    )
    summary = halumem.build_summary(metadata, result, evaluation)
    summary += "\n## Offline Scoring Revision\n\n"
    summary += f"Source run: {source}\n\nRepaired judgments: {len(changes)}. New API requests: 0. Original files unchanged.\n"
    summary += "Legacy runtime request/token totals cannot be verified; displayed totals cover observed records only. See repair_audit.json.\n"
    (revision / "summary.md").write_text(summary, encoding="utf-8")
    for filename, digest in original_hashes.items():
        if halumem.sha256_file(source / filename) != digest:
            raise RuntimeError(f"Source artifact changed during repair: {filename}")
    return {
        "revision_dir": str(revision),
        "repaired": len(changes),
        "inconclusive_count": evaluation["inconclusive_count"],
        "new_api_requests": 0,
    }
