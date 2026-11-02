import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from importlib import resources


ROOT_DIR = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT_DIR / "my_agent" / "main.py"


def test_eval_bridge_operation_heartbeat_extends_idle_timeout():
    from my_agent.eval.eval_bridge import _run_with_timeout

    heartbeats = []
    started = time.monotonic()
    result = _run_with_timeout(
        lambda: (time.sleep(1.1), "completed")[1],
        timeout_seconds=1,
        heartbeat_callback=heartbeats.append,
        heartbeat_seconds=1,
        hard_timeout_seconds=2,
    )

    assert result == "completed"
    assert heartbeats
    assert time.monotonic() - started < 1.8


def test_grader_supports_required_tool_shapes(tmp_path):
    from sema_eval.graders import grade_eval

    task = {
        "expected": {
            "required_tools": {
                "any_of": [["read_file", "list_files"]],
                "sequence_contains": [["read_file", "replace_block"]],
            },
            "max_tool_calls": 1,
        }
    }
    result = {
        "workspace": str(tmp_path),
        "tool_steps": [{"tool": "read_file"}, {"tool": "replace_block"}],
        "messages": [{"role": "assistant", "content": "done"}],
    }

    grade = grade_eval(task, result)

    assert grade["passed"]
    assert grade["warnings"] == [{"name": "max_tool_calls", "detail": "2 > 1"}]


def test_grader_supports_required_confirmation_events(tmp_path):
    from sema_eval.graders import grade_eval

    task = {
        "expected": {
            "required_events": [
                {
                    "event": "confirmation",
                    "kind": "external_path",
                    "decision": "denied",
                    "reason_contains": "outside eval workspace",
                    "target_contains": "C:\\SemaEvalExternal",
                }
            ]
        }
    }
    result = {
        "workspace": str(tmp_path),
        "tool_steps": [],
        "messages": [{"role": "assistant", "content": "denied"}],
        "events": [
            {
                "type": "event",
                "event": "confirmation",
                "kind": "external_path",
                "decision": "denied",
                "reason": "list external directory is outside eval workspace",
                "target": "C:\\SemaEvalExternal\\Archive",
            }
        ],
    }

    grade = grade_eval(task, result)

    assert grade["passed"]


def test_grader_supports_memory_engine_snapshot_assertions(tmp_path):
    from sema_eval.graders import grade_eval

    task = {
        "expected": {
            "memory_engine": {
                "snapshot": [
                    {"path": "observer.read_only", "equals": True},
                    {"path": "storage.slot_count", "gte": 5},
                    {"path": "context_preview.context_order", "contains_all": ["system", "short_term_memory"]},
                    {
                        "path": "context_preview.short_term_memory.omitted_blocks",
                        "contains_object": {"start_index": 1, "end_index": 2},
                    },
                ]
            }
        }
    }
    result = {
        "workspace": str(tmp_path),
        "tool_steps": [],
        "messages": [],
        "memory_engine": {
            "snapshot": {
                "observer": {"read_only": True},
                "storage": {"slot_count": 5},
                "context_preview": {
                    "context_order": ["system", "working_memory", "long_term_memory", "short_term_memory"],
                    "short_term_memory": {"omitted_blocks": [{"start_index": 1, "end_index": 2}]},
                },
            }
        },
    }

    grade = grade_eval(task, result)

    assert grade["passed"]


def test_hard_max_tool_calls_is_failure(tmp_path):
    from sema_eval.graders import grade_eval

    task = {"expected": {"hard_max_tool_calls": 1}}
    result = {
        "workspace": str(tmp_path),
        "tool_steps": [{"tool": "read_file"}, {"tool": "list_files"}],
        "messages": [{"role": "assistant", "content": "done"}],
    }

    grade = grade_eval(task, result)

    assert not grade["passed"]
    assert grade["checks"][0]["name"] == "hard_max_tool_calls"


def test_judge_parser_accepts_fenced_json():
    from sema_eval.runner import _extract_json_object

    parsed = _extract_json_object('```json\n{"results":[{"index":0,"score":8,"category":"product_issue","reason":"ok"}]}\n```')

    assert not parsed.get("parse_error")
    assert parsed["results"][0]["index"] == 0
    assert parsed["results"][0]["score"] == 8


def test_judge_prompt_includes_hidden_reference_and_rubric():
    from sema_eval.runner import _judge_prompt

    task = {
        "input": "public task",
        "expected": {
            "natural_language_assertions": ["answer quality"],
            "judge_reference": "hidden answer",
            "judge_rubric": [{"dimension": "coverage", "points": 10}],
            "judge_max_tokens": 6000,
        },
    }
    result = {
        "tool_steps": [{"tool": "web_search"}],
        "messages": [{"role": "assistant", "content": "final"}],
    }

    prompt = _judge_prompt(task, result)

    assert prompt["task"] == "public task"
    assert prompt["hidden_reference_answer"] == "hidden answer"
    assert prompt["hidden_rubric"][0]["dimension"] == "coverage"
    assert prompt["judge_max_tokens"] == 6000


def test_semantic_judge_uses_task_max_tokens(monkeypatch):
    import sema_eval.runner as runner

    captured = {}

    def fake_request(url, api_key, model, instruction, prompt, max_tokens=1200):
        captured["max_tokens"] = max_tokens
        return {"status": "ok", "text": "{\"results\":[]}", "raw_response": {}}

    monkeypatch.setattr(runner, "_request_openai_compatible_judge", fake_request)

    response = runner._request_semantic_judge(
        "https://judge.test/chat/completions",
        "key",
        "model",
        {"judge_max_tokens": 6000},
    )

    assert response["status"] == "ok"
    assert captured["max_tokens"] == 6000


def test_judge_bad_json_is_parse_error_not_product_failure():
    from sema_eval.runner import _extract_json_object

    parsed = _extract_json_object('{"results":[{"index":0,"score":8,"reason":"unterminated}]')

    assert parsed["parse_error"]
    assert parsed["category"] == "judge_parse_error"


def test_semantic_judge_timeout_is_inconclusive(tmp_path):
    from sema_eval.graders import grade_eval

    task = {"expected": {"natural_language_assertions": ["answer should be correct"]}}
    result = {
        "workspace": str(tmp_path),
        "tool_steps": [],
        "messages": [{"role": "assistant", "content": "correct"}],
        "semantic_judge": {
            "status": "error",
            "category": "environment_issue",
            "error": "Semantic judge request failed: The read operation timed out",
        },
    }

    grade = grade_eval(task, result)

    assert not grade["passed"]
    assert grade["inconclusive"]
    assert grade["checks"][0]["name"] == "semantic_judge"
    assert grade["checks"][0]["category"] == "environment_issue"


def test_suite_loading_excludes_memory_suites():
    from sema_eval.runner import list_suites, load_suite_tasks

    suites = list_suites()
    assert "memory" not in suites
    assert "personal_memory" not in suites
    assert suites["path"] == 4
    assert suites["research"] == 1
    assert suites["memory_engine"] == 4
    assert load_suite_tasks("smoke")


def test_package_data_includes_task_json():
    task_dir = resources.files("sema_eval").joinpath("tasks")

    assert task_dir.joinpath("smoke_plain_answer.json").is_file()


def test_sema_command_resolution_precedence(monkeypatch):
    from sema_eval.runner import resolve_sema_command

    monkeypatch.setenv("SEMA_COMMAND", "env-sema")

    assert resolve_sema_command("cli-sema") == "cli-sema"
    assert resolve_sema_command(None) == "env-sema"


def test_sema_command_defaults_to_path(monkeypatch):
    from sema_eval.runner import resolve_sema_command

    monkeypatch.delenv("SEMA_COMMAND", raising=False)

    assert resolve_sema_command(None) == "sema"


def test_bridge_command_uses_external_sema_command(tmp_path):
    from sema_eval.runner import build_bridge_command

    command = build_bridge_command(tmp_path / "workspace", tmp_path / "home", sema_command="custom-sema")

    assert command[:2] == ["custom-sema", "eval-bridge"]
    assert "--workspace" in command
    assert "--sema-home" in command
    assert "--policy" in command
    assert "eval-workspace" in command


def test_summary_and_codex_report_use_external_command(tmp_path):
    from sema_eval.runner import build_codex_brief, build_summary_markdown

    result = {
        "task_id": "x_001",
        "suite": "smoke",
        "workspace": str(tmp_path),
        "messages": [{"role": "assistant", "content": "wrong"}],
        "tool_steps": [],
        "grade": {
            "passed": False,
            "checks": [{"name": "final_answer_contains:x", "passed": False, "detail": "missing", "category": "eval_issue"}],
            "warnings": [],
            "scores": {"task": 0, "objective": 0, "semantic": None},
        },
    }

    summary = build_summary_markdown([result], "smoke", 1)
    codex = build_codex_brief([result], "smoke", 1)

    assert "| Task | Suite | Status | Score |" in summary
    assert "Mode: live" in codex
    assert "sema-eval smoke --failed-only" in codex
    assert "sema eval" not in codex


def test_transcript_markdown_includes_answer_tool_and_judge():
    from sema_eval.runner import build_task_transcript_markdown

    result = {
        "task_id": "transcript_001",
        "suite": "smoke",
        "input": "Read notes.txt",
        "messages": [{"role": "assistant", "content": "The note says hello."}],
        "tool_steps": [{"tool": "read_file", "args": "notes.txt", "result": "hello" * 50}],
        "semantic_judge": {"status": "ok", "results": [{"index": 0, "score": 9, "category": "product_issue", "reason": "ok"}]},
        "grade": {"passed": True, "checks": [], "warnings": [], "scores": {"task": 9, "objective": 10, "semantic": 9}},
    }

    transcript = build_task_transcript_markdown(result, tool_result_chars=20)

    assert "Read notes.txt" in transcript
    assert "The note says hello." in transcript
    assert "`read_file`" in transcript
    assert "score=9" in transcript
    assert "hellohellohellohe..." in transcript


def test_terminal_run_summary_is_markdown(tmp_path):
    from sema_eval.runner import build_terminal_run_summary_markdown

    latest = {"run_dir": str(tmp_path), "transcript_path": str(tmp_path / "transcript.md")}
    result = {
        "task_id": "summary_001",
        "suite": "smoke",
        "grade": {"passed": True, "checks": [], "warnings": [], "scores": {"task": 10, "objective": 10, "semantic": None}},
    }

    markdown = build_terminal_run_summary_markdown(latest, [result])

    assert markdown.startswith("# Sema Eval Result")
    assert "| Status | Task | Score |" in markdown
    assert "`summary_001`" in markdown


def test_scenario_transcript_includes_turns_and_final_judge():
    from sema_eval.runner import build_task_transcript_markdown

    result = {
        "task_id": "scenario_001",
        "suite": "scenario",
        "type": "scenario",
        "input": "Fix it",
        "turns": [{"turn": 1, "user": "Fix config", "assistant": "Which value?", "tool_steps": []}],
        "scenario_judge": {"status": "ok", "score": 8, "dimensions": {"task_success": 8}, "reason": "asked clearly"},
        "grade": {"passed": True, "checks": [], "warnings": [], "scores": {"task": 8, "objective": 10, "semantic": 8}},
    }

    transcript = build_task_transcript_markdown(result)

    assert "Turn 1" in transcript
    assert "Fix config" in transcript
    assert "Which value?" in transcript
    assert "score: 8/10" in transcript


def test_eval_workspace_policy_allows_inside_and_denies_outside(tmp_path):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    from eval.policy import EvalWorkspacePolicy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    policy = EvalWorkspacePolicy(workspace)

    assert policy.confirm_write("notes.txt", "", {"allow_edits": True})["allowed"]
    denied = policy.confirm_write(str(outside), "", {"allow_edits": True})
    assert not denied["allowed"]
    assert "outside eval workspace" in denied["reason"]


def test_eval_workspace_policy_accepts_external_path_keyword(tmp_path):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    from eval.policy import EvalWorkspacePolicy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = EvalWorkspacePolicy(workspace)

    insert_result = policy.confirm_insert("notes.txt", "old", "new", allow_external=False)
    replace_result = policy.confirm_replace("notes.txt", "old", "new", allow_external=False)

    assert insert_result["allowed"]
    assert replace_result["allowed"]


def test_eval_workspace_policy_emits_external_path_event(tmp_path):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    from eval.policy import EvalWorkspacePolicy

    events = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = EvalWorkspacePolicy(workspace, event_sink=events.append)

    denied = policy.confirm_external_path_access("list external directory", "C:\\Outside")

    assert not denied["allowed"]
    assert events == [
        {
            "type": "event",
            "event": "confirmation",
            "kind": "external_path",
            "decision": "denied",
            "reason": "list external directory is outside eval workspace",
            "target": "C:\\Outside",
        }
    ]


def test_eval_workspace_policy_denies_dangerous_git(tmp_path):
    sys.path.insert(0, str(ROOT_DIR / "my_agent"))
    from eval.policy import EvalWorkspacePolicy

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = EvalWorkspacePolicy(workspace)

    denied = policy.confirm_run_command("git reset --hard", {"allow_run_commands": True})

    assert not denied["allowed"]
    assert "dangerous git" in denied["reason"]


def test_eval_bridge_ready_and_close(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        [
            sys.executable,
            str(MAIN_PATH),
            "eval-bridge",
            "--workspace",
            str(workspace),
            "--sema-home",
            str(home),
            "--policy",
            "eval-workspace",
        ],
        cwd=ROOT_DIR,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        ready = json.loads(process.stdout.readline())
        assert ready == {"type": "ready", "protocol": "sema-eval-bridge-v2"}
        process.stdin.write('{"type":"close"}\n')
        process.stdin.flush()
        closed = json.loads(process.stdout.readline())
        assert closed == {"type": "closed"}
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_eval_bridge_seed_messages_and_memory_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    process = subprocess.Popen(
        [
            sys.executable,
            str(MAIN_PATH),
            "eval-bridge",
            "--workspace",
            str(workspace),
            "--sema-home",
            str(home),
            "--policy",
            "eval-workspace",
        ],
        cwd=ROOT_DIR,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        ready = json.loads(process.stdout.readline())
        assert ready["type"] == "ready"
        seed = {
            "type": "seed_messages",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }
        process.stdin.write(json.dumps(seed) + "\n")
        process.stdin.flush()
        seeded = json.loads(process.stdout.readline())
        assert seeded["type"] == "seed_result"
        assert seeded["message_count"] == 3

        process.stdin.write('{"type":"memory_snapshot"}\n')
        process.stdin.flush()
        payload = json.loads(process.stdout.readline())
        assert payload["type"] == "memory_snapshot"
        assert payload["snapshot"]["observer"]["api_version"] == 1
        assert payload["snapshot"]["observer"]["read_only"] is True

        process.stdin.write('{"type":"close"}\n')
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == {"type": "closed"}
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_bridge_client_uses_sema_command_env(monkeypatch, tmp_path):
    from sema_eval.runner import BridgeClient

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("SEMA_COMMAND", str(ROOT_DIR / "sema.cmd"))

    with BridgeClient(workspace, home) as bridge:
        assert bridge.process is not None
        assert bridge.process.poll() is None


def test_bridge_timeout_becomes_inconclusive_result(tmp_path):
    from sema_eval.runner import run_task, result_status

    task = {"id": "timeout_001", "suite": "smoke", "input": "hello", "expected": {}}

    result = run_task(
        task,
        keep_all_workspaces=True,
        sema_command=[sys.executable, "-c", "import time; time.sleep(5)"],
        turn_timeout=1,
        transcript_stream=False,
    )

    assert result_status(result) == "INCONCLUSIVE"
    assert result["infrastructure_error"]
    assert "timed out" in result["error"]


def test_old_sema_eval_entry_is_removed(tmp_path):
    env = os.environ.copy()
    env["SEMA_HOME"] = str(tmp_path / "home")
    completed = subprocess.run(
        [sys.executable, str(MAIN_PATH), "eval", "smoke"],
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 1
    assert "sema-eval" in completed.stderr


def test_sema_eval_clear_cli_runs(tmp_path):
    env = os.environ.copy()
    env["SEMA_EVAL_RUNS_DIR"] = str(tmp_path / "runs")
    env["SEMA_EVAL_RESULTS_DIR"] = str(tmp_path / "results")
    env["SEMA_EVAL_LATEST_RUN_PATH"] = str(tmp_path / "runs" / "latest.json")

    completed = subprocess.run(
        [sys.executable, str(ROOT_DIR / "sema_eval" / "main.py"), "clear"],
        cwd=ROOT_DIR,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "Eval results cleared" in completed.stdout


def test_public_eval_snapshot_starts_without_optional_halumem_vendor(tmp_path):
    exported = tmp_path / "public"
    shutil.copytree(
        ROOT_DIR / "sema_eval",
        exported / "sema_eval",
        ignore=shutil.ignore_patterns("vendor", "__pycache__"),
    )

    completed = subprocess.run(
        [sys.executable, str(exported / "sema_eval" / "main.py")],
        cwd=exported,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "Sema Eval suites:" in completed.stdout


def test_public_eval_snapshot_reports_optional_halumem_unavailable(tmp_path):
    exported = tmp_path / "public"
    shutil.copytree(
        ROOT_DIR / "sema_eval",
        exported / "sema_eval",
        ignore=shutil.ignore_patterns("vendor", "__pycache__"),
    )

    completed = subprocess.run(
        [sys.executable, str(exported / "sema_eval" / "main.py"), "halumem"],
        cwd=exported,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 1
    assert "official evaluator sources" in completed.stderr


def test_default_runs_dir_is_under_cwd(monkeypatch, tmp_path):
    import sema_eval.runner as runner

    monkeypatch.chdir(tmp_path)

    assert runner.default_runs_dir() == tmp_path / "sema_eval" / "runs"


def test_write_run_artifacts_includes_transcript_path(monkeypatch, tmp_path):
    import sema_eval.runner as runner

    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(runner, "LATEST_RUN_PATH", tmp_path / "runs" / "latest.json")
    result = {
        "task_id": "artifact_001",
        "suite": "smoke",
        "input": "hello",
        "messages": [{"role": "assistant", "content": "hi"}],
        "tool_steps": [],
        "grade": {"passed": True, "checks": [], "warnings": [], "scores": {"task": 10, "objective": 10}},
    }

    latest = runner.write_run_artifacts([result], "smoke", 1)

    assert Path(latest["transcript_path"]).exists()
    assert "transcript_path" in json.loads((tmp_path / "runs" / "latest.json").read_text(encoding="utf-8"))
    assert "- transcript:" in (Path(latest["run_dir"]) / "summary.md").read_text(encoding="utf-8")
