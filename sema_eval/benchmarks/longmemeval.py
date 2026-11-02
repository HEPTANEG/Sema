"""LongMemEval V1 adapter.

The dataset and task answers stay on the evaluation side. Sema receives only
sanitized historical turns and the final question through eval-bridge.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from .. import runner
except ImportError:
    import runner


DATASET_URLS = {
    "oracle": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json?download=true",
}
DEFAULT_DATA_DIR = Path(os.getenv("SEMA_EVAL_DATA_DIR", Path.home() / ".cache" / "sema-eval" / "longmemeval"))
DEFAULT_LIMIT = 20
DEFAULT_JUDGE_MAX_TOKENS = 4096


def build_parser():
    parser = argparse.ArgumentParser(prog="sema-eval longmemeval")
    parser.add_argument("--dataset", choices=sorted(DATASET_URLS), default="oracle")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--question-id")
    parser.add_argument("--data-file")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--sema-command")
    parser.add_argument("--turn-timeout", type=int, default=runner.DEFAULT_TURN_TIMEOUT_SECONDS)
    parser.add_argument("--judge", dest="judge", action="store_true", default=True)
    parser.add_argument("--no-judge", dest="judge", action="store_false")
    return parser


def main(argv=None):
    args = build_parser().parse_args(list(argv or []))
    if args.limit < 1:
        raise RuntimeError("--limit must be at least 1.")
    if args.trials < 1:
        raise RuntimeError("--trials must be at least 1.")
    if args.trials != 1:
        raise RuntimeError("LongMemEval currently requires --trials 1 so each question has one official prediction.")

    data_path = resolve_dataset_path(args.dataset, args.data_file, args.data_dir, args.download)
    entries = load_dataset(data_path)
    selected = select_entries(entries, args.limit, args.question_id)
    if not selected:
        raise RuntimeError("No LongMemEval questions matched the requested selection.")
    if args.judge:
        validate_judge_config()

    results = []
    total = len(selected) * args.trials
    counter = 0
    print(
        f"Running LongMemEval: dataset={args.dataset}, questions={len(selected)}, trials={args.trials}, judge={str(args.judge).lower()}",
        flush=True,
    )
    for entry in selected:
        for trial in range(1, args.trials + 1):
            counter += 1
            results.append(
                run_question(
                    entry,
                    trial=trial,
                    progress=f"{counter}/{total}",
                    sema_command=args.sema_command,
                    turn_timeout=args.turn_timeout,
                    keep_workspace=args.keep_workspaces,
                )
            )

    predictions = [
        {"question_id": result["question_id"], "hypothesis": result.get("hypothesis", "")}
        for result in results
    ]
    evaluation = evaluate_predictions(predictions, entries, judge=args.judge)
    latest = write_artifacts(
        results,
        predictions,
        evaluation,
        data_path=data_path,
        dataset=args.dataset,
        limit=args.limit,
        trials=args.trials,
        args=args,
    )
    print_longmemeval_summary(latest, results, evaluation)
    return 0 if evaluation.get("inconclusive_count", 0) == 0 else 1


def resolve_dataset_path(dataset, data_file=None, data_dir=None, download=False):
    if data_file:
        path = Path(data_file).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"LongMemEval data file not found: {path}")
        return path
    cache_path = Path(data_dir or DEFAULT_DATA_DIR).expanduser() / f"longmemeval_{dataset}.json"
    if cache_path.exists():
        return cache_path
    if not download:
        raise RuntimeError(
            f"LongMemEval dataset is missing: {cache_path}. Re-run with --download or pass --data-file."
        )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    request = urllib.request.Request(DATASET_URLS[dataset], headers={"User-Agent": "sema-eval/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        temporary.replace(cache_path)
    except (urllib.error.URLError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to download LongMemEval dataset: {error}") from error
    return cache_path


def load_dataset(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid LongMemEval dataset: {path}: {error}") from error
    if not isinstance(data, list):
        raise RuntimeError("LongMemEval dataset root must be a JSON array.")
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("question_id"):
            raise RuntimeError("LongMemEval dataset contains an invalid question entry.")
    return data


def select_entries(entries, limit, question_id=None):
    if question_id:
        return [entry for entry in entries if entry.get("question_id") == question_id]
    return list(entries[:limit])


def run_question(entry, trial, progress, sema_command, turn_timeout, keep_workspace):
    question_id = str(entry["question_id"])
    workspace_root = Path(tempfile.mkdtemp(prefix=f"sema-eval-longmemeval-{question_id}-"))
    workspace = workspace_root / "workspace"
    home = workspace_root / "home"
    workspace.mkdir()
    home.mkdir()
    result = {
        "question_id": question_id,
        "question_type": entry.get("question_type", ""),
        "question": entry.get("question", ""),
        "trial": trial,
        "status": "INCONCLUSIVE",
        "hypothesis": "",
        "workspace": str(workspace),
        "sema_home": str(home),
        "history_session_count": len(entry.get("haystack_sessions") or []),
        "history_maintenance": [],
        "tool_trace": [],
        "events": [],
        "memory_snapshot_before_history": {},
        "memory_snapshot_after_history": {},
        "memory_snapshot_before_question": {},
        "memory_snapshot_after_question": {},
        "context_preview": {},
        "maintenance_status": {},
        "error": "",
    }
    runner.print_progress(progress, question_id, "start", f"type={entry.get('question_type', '')}")
    bridge = None
    try:
        with runner.BridgeClient(workspace, home, sema_command=sema_command, turn_timeout=turn_timeout) as bridge:
            result["memory_snapshot_before_history"] = bridge.memory_snapshot()
            dates = entry.get("haystack_dates") or []
            sessions = entry.get("haystack_sessions") or []
            session_ids = entry.get("haystack_session_ids") or []
            for index, turns in enumerate(sessions):
                session_id = session_ids[index] if index < len(session_ids) else f"session_{index + 1}"
                session_date = dates[index] if index < len(dates) else None
                runner.print_progress(progress, question_id, "history", f"[{index + 1}/{len(sessions)}] {session_id}")
                history_result = bridge.history_session(session_id, session_date, turns)
                result["history_maintenance"].append(history_result.get("maintenance") or {})
            bridge.history_complete()
            result["memory_snapshot_after_history"] = bridge.memory_snapshot()
            runner.print_progress(progress, question_id, "question", "sending final question")
            turn = bridge.turn(
                entry.get("question", ""),
                created_at=entry.get("question_date"),
                phase="benchmark_question",
            )
            result["hypothesis"] = turn.get("final_answer", "")
            result["tool_trace"] = turn.get("tool_trace") or []
            result["events"] = turn.get("events") or []
            result["memory_snapshot_before_question"] = turn.get("memory_snapshot_before_question") or {}
            result["memory_snapshot_after_question"] = turn.get("memory_snapshot_after_question") or {}
            result["context_preview"] = turn.get("context_preview") or {}
            result["maintenance_status"] = turn.get("maintenance_status") or {}
            result["status"] = "COMPLETED" if not turn.get("error") else "INCONCLUSIVE"
            result["error"] = turn.get("error") or ""
        runner.print_progress(progress, question_id, result["status"], "history replay and question complete")
    except Exception as error:
        result["error"] = str(error)
        result["status"] = "INCONCLUSIVE"
        runner.print_progress(progress, question_id, "INCONCLUSIVE", str(error))
    finally:
        result["stderr"] = getattr(bridge, "stderr", "")
        result["workspace_removed"] = not keep_workspace
        if not keep_workspace:
            shutil.rmtree(workspace_root, ignore_errors=True)
    return result


def validate_judge_config():
    runner.load_eval_env_file()
    missing = [name for name in ("EVAL_JUDGE_API_KEY", "EVAL_JUDGE_BASE_URL") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "LongMemEval scoring requires " + ", ".join(missing) + ". Set them in PowerShell or my_agent/.env."
        )


def evaluate_predictions(predictions, references, judge=True):
    reference_map = {entry["question_id"]: entry for entry in references}
    if not judge:
        return {
            "status": "skipped",
            "inconclusive_count": 0,
            "items": [
                {"question_id": prediction["question_id"], "status": "SKIPPED"}
                for prediction in predictions
            ],
            "metrics": {},
        }
    runner.load_eval_env_file()
    url = os.getenv("EVAL_JUDGE_BASE_URL", "").rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    api_key = os.getenv("EVAL_JUDGE_API_KEY")
    model = os.getenv("EVAL_JUDGE_MODEL", runner.DEFAULT_JUDGE_MODEL)
    max_tokens = int(os.getenv("EVAL_JUDGE_MAX_TOKENS", DEFAULT_JUDGE_MAX_TOKENS))
    items = []
    for prediction in predictions:
        reference = reference_map.get(prediction["question_id"])
        if not reference:
            items.append({"question_id": prediction["question_id"], "status": "INCONCLUSIVE", "error": "reference not found"})
            continue
        prompt = build_official_answer_check_prompt(
            reference.get("question_type", ""),
            reference.get("question", ""),
            reference.get("answer", ""),
            prediction.get("hypothesis", ""),
            abstention=prediction["question_id"].endswith("_abs"),
        )
        response = runner._request_openai_compatible_judge(
            url,
            api_key,
            model,
            "Answer yes or no only. Do not explain.",
            {"official_prompt": prompt},
            max_tokens=max_tokens,
        )
        if response.get("status") == "error":
            items.append(
                {
                    "question_id": prediction["question_id"],
                    "question_type": reference.get("question_type", ""),
                    "status": "INCONCLUSIVE",
                    "error": response.get("error", "judge request failed"),
                    "judge_model": model,
                }
            )
            continue
        text = response.get("text", "").strip()
        label, parse_error = parse_official_judge_label(text)
        item = {
            "question_id": prediction["question_id"],
            "question_type": reference.get("question_type", ""),
            "judge_response": text,
            "judge_model": model,
        }
        if parse_error:
            item.update(
                {
                    "status": "INCONCLUSIVE",
                    "category": "judge_parse_error",
                    "error": parse_error,
                    "judge_raw_response": response.get("raw_response"),
                }
            )
        else:
            item.update({"status": "SCORED", "label": label})
        items.append(item)
    return {
        "status": "completed" if not any(item.get("status") == "INCONCLUSIVE" for item in items) else "partial",
        "judge_model": model,
        "judge_max_tokens": max_tokens,
        "inconclusive_count": sum(item.get("status") == "INCONCLUSIVE" for item in items),
        "items": items,
        "metrics": calculate_metrics(items),
    }


def build_official_answer_check_prompt(task, question, answer, response, abstention=False):
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Answer yes if the model correctly identifies the question as unanswerable. "
            "The model may say the information is incomplete or provide other information without the asked fact. "
            f"\n\nQuestion: {question}\n\nExplanation: {answer}\n\nModel Response: {response}"
            "\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    if task == "temporal-reasoning":
        extra = " Off-by-one errors for a number of days, weeks, or months should not be penalized."
    elif task == "knowledge-update":
        extra = " If the response includes old information but also gives the updated answer, count it as correct."
    elif task == "single-session-preference":
        extra = " The response is correct if it recalls and uses the user's personal information appropriately."
    else:
        extra = " If it contains all intermediate steps or an equivalent answer, count it as correct. A subset of required information is not enough."
    return (
        "I will give you a question, a correct answer, and a response from a model. "
        "Answer yes if the response contains the correct answer; otherwise answer no."
        f"{extra}\n\nQuestion: {question}\n\nCorrect Answer: {answer}"
        f"\n\nModel Response: {response}\n\nIs the model response correct? Answer yes or no only."
    )


def parse_official_judge_label(text):
    """Parse the official yes/no response without treating malformed output as no."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return None, "LongMemEval Judge returned empty content."
    if normalized.startswith("yes"):
        return True, None
    if normalized.startswith("no"):
        return False, None
    return None, "LongMemEval Judge response did not start with yes or no."


def calculate_metrics(items):
    scored = [item for item in items if item.get("status") == "SCORED"]
    if not scored:
        return {
            "overall_accuracy": None,
            "task_averaged_accuracy": None,
            "abstention_accuracy": None,
            "by_question_type": {},
        }
    by_type = {}
    for item in scored:
        by_type.setdefault(item.get("question_type", ""), []).append(int(bool(item.get("label"))))
    type_metrics = {key: sum(values) / len(values) for key, values in sorted(by_type.items())}
    abstention = [item for item in scored if item.get("question_id", "").endswith("_abs")]
    return {
        "overall_accuracy": sum(int(bool(item.get("label"))) for item in scored) / len(scored),
        "task_averaged_accuracy": sum(type_metrics.values()) / len(type_metrics),
        "abstention_accuracy": (
            sum(int(bool(item.get("label"))) for item in abstention) / len(abstention) if abstention else None
        ),
        "by_question_type": type_metrics,
    }


def write_artifacts(results, predictions, evaluation, data_path, dataset, limit, trials, args):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = runner.RUNS_DIR / "longmemeval" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "benchmark": "LongMemEval",
        "dataset": dataset,
        "dataset_path": str(data_path),
        "dataset_sha256": sha256_file(data_path),
        "question_count": len(predictions),
        "limit": limit,
        "trials": trials,
        "sema_command": runner.resolve_sema_command(args.sema_command),
        "sema_commit": git_commit(),
        "judge_model": os.getenv("EVAL_JUDGE_MODEL", runner.DEFAULT_JUDGE_MODEL),
        "judge_base_url": os.getenv("EVAL_JUDGE_BASE_URL", ""),
        "evaluator": "LongMemEval-compatible official evaluation logic",
        "evaluator_source": "https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py",
        "arguments": vars(args),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(run_dir / "metadata.json", metadata)
    write_jsonl(run_dir / "predictions.jsonl", predictions)
    write_json(run_dir / "evaluation.json", evaluation)
    write_jsonl(run_dir / "per_question.jsonl", merge_evaluation(results, evaluation))
    write_jsonl(run_dir / "diagnostics.jsonl", [diagnostic_row(result) for result in results])
    (run_dir / "transcript.md").write_text(build_transcript(results), encoding="utf-8")
    (run_dir / "summary.md").write_text(build_summary(metadata, results, evaluation), encoding="utf-8")
    latest = {
        "benchmark": "longmemeval",
        "run_id": stamp,
        "run_dir": str(run_dir),
        "predictions_path": str(run_dir / "predictions.jsonl"),
        "evaluation_path": str(run_dir / "evaluation.json"),
        "summary_path": str(run_dir / "summary.md"),
    }
    latest_dir = runner.RUNS_DIR / "longmemeval"
    latest_dir.mkdir(parents=True, exist_ok=True)
    write_json(latest_dir / "latest.json", latest)
    return latest


def merge_evaluation(results, evaluation):
    scored = {item.get("question_id"): item for item in evaluation.get("items") or []}
    merged = []
    for result in results:
        item = {
            "question_id": result["question_id"],
            "question_type": result.get("question_type", ""),
            "sut_status": result.get("status"),
            "hypothesis": result.get("hypothesis", ""),
            "sut_error": result.get("error", ""),
        }
        item.update(scored.get(result["question_id"], {"status": "INCONCLUSIVE", "error": "missing evaluation"}))
        merged.append(item)
    return merged


def diagnostic_row(result):
    before = result.get("memory_snapshot_before_question") or result.get("memory_snapshot_after_history") or {}
    after = result.get("memory_snapshot_after_question") or {}
    return {
        "question_id": result["question_id"],
        "question_type": result.get("question_type", ""),
        "sut_status": result.get("status"),
        "history_session_count": result.get("history_session_count", 0),
        "tool_trace": result.get("tool_trace") or [],
        "maintenance_status": result.get("maintenance_status") or {},
        "before": observer_metrics(before),
        "after": observer_metrics(after),
        "context_preview": result.get("context_preview") or {},
        "history_maintenance": result.get("history_maintenance") or [],
        "error": result.get("error", ""),
    }


def observer_metrics(snapshot):
    storage = snapshot.get("storage") or {}
    context = snapshot.get("context") or {}
    ltm = snapshot.get("ltm") or {}
    return {
        "slot_count": storage.get("slot_count", 0),
        "event_count": storage.get("event_count", 0),
        "ltm_object_count": ltm.get("object_count", 0),
        "formation": snapshot.get("formation") or {},
        "memory_push": snapshot.get("memory_push") or {},
        "retrieval": snapshot.get("retrieval") or {},
        "context_pressure": context.get("pressure") or context.get("context_pressure") or {},
        "context_preview": snapshot.get("context_preview") or {},
    }


def build_transcript(results):
    lines = ["# LongMemEval Transcript", "", "History content is omitted from this report to keep the artifact readable.", ""]
    for result in results:
        lines.extend(
            [
                f"## {result['question_id']} [{result.get('status')}]",
                "",
                f"- question_type: {result.get('question_type', '')}",
                f"- history_sessions: {result.get('history_session_count', 0)}",
                "",
                "### Question",
                "",
                fenced(result.get("question", "")),
                "",
                "### Sema Answer",
                "",
                fenced(result.get("hypothesis", "")),
                "",
                f"- tool_trace: {json.dumps(result.get('tool_trace') or [], ensure_ascii=False)}",
                f"- maintenance_status: {json.dumps(result.get('maintenance_status') or {}, ensure_ascii=False)}",
                "",
            ]
        )
    return "\n".join(lines)


def build_summary(metadata, results, evaluation):
    metrics = evaluation.get("metrics") or {}
    lines = [
        "# LongMemEval V1 Summary",
        "",
        f"- dataset: {metadata['dataset']}",
        f"- questions: {metadata['question_count']}",
        f"- judge: {evaluation.get('judge_model', metadata.get('judge_model', ''))}",
        f"- evaluator: {metadata['evaluator']}",
        f"- overall accuracy: {format_metric(metrics.get('overall_accuracy'))}",
        f"- task-averaged accuracy: {format_metric(metrics.get('task_averaged_accuracy'))}",
        f"- abstention accuracy: {format_metric(metrics.get('abstention_accuracy'))}",
        f"- evaluator inconclusive: {evaluation.get('inconclusive_count', 0)}",
        "",
        "## By Question Type",
        "",
        "| Question type | Accuracy |",
        "| --- | ---: |",
    ]
    for key, value in sorted((metrics.get("by_question_type") or {}).items()):
        lines.append(f"| {key} | {format_metric(value)} |")
    lines.extend(["", "## Tasks", "", "| Question | Sema status | Judge |", "| --- | --- | --- |"])
    scored = {item.get("question_id"): item for item in evaluation.get("items") or []}
    for result in results:
        item = scored.get(result["question_id"], {})
        judge_status = item.get("label") if item.get("status") == "SCORED" else item.get("status", "INCONCLUSIVE")
        lines.append(f"| {result['question_id']} | {result.get('status')} | {judge_status} |")
    lines.extend(["", "Diagnostic note: a correct answer is not by itself proof that LTM was used; inspect diagnostics.jsonl.", ""])
    return "\n".join(lines)


def print_longmemeval_summary(latest, results, evaluation):
    metrics = evaluation.get("metrics") or {}
    text = "\n".join(
        [
            "# LongMemEval Result",
            "",
            f"- questions: **{len(results)}**",
            f"- overall accuracy: **{format_metric(metrics.get('overall_accuracy'))}**",
            f"- task-averaged accuracy: **{format_metric(metrics.get('task_averaged_accuracy'))}**",
            f"- abstention accuracy: **{format_metric(metrics.get('abstention_accuracy'))}**",
            f"- evaluator inconclusive: **{evaluation.get('inconclusive_count', 0)}**",
            f"- run directory: {latest['run_dir']}",
            f"- summary: {latest['summary_path']}",
            f"- predictions: {latest['predictions_path']}",
        ]
    )
    runner.render_terminal_markdown(text)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, values):
    with Path(path).open("w", encoding="utf-8") as output:
        for value in values:
            output.write(json.dumps(value, ensure_ascii=False) + "\n")


def fenced(value):
    marker = chr(96) * 3
    return marker + "text\n" + str(value or "").rstrip() + "\n" + marker


def format_metric(value):
    return "n/a" if value is None else f"{float(value) * 100:.2f}%"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=runner.ROOT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
