# Evaluation Benchmark Platform

`sema-eval` is Sema's external benchmark harness. It runs the real Sema
runtime through an isolated bridge rather than importing benchmark suites into
the product runtime.

The boundary is:

```text
sema-eval
  starts a Sema subprocess
  communicates with `sema eval-bridge` over stdin/stdout JSONL
  scores workspace state, tool traces, objective commands, and judge output

Sema
  runs the real Agent
  exposes only the internal evaluation bridge protocol
  does not import or own benchmark suites
```

Memory Engine behavior is protected by product tests. External benchmark
adapters may require separately obtained upstream tools or datasets and are not
part of the default suite.

## Quick Start

List suites:

```powershell
sema-eval
```

Run a suite:

```powershell
sema-eval smoke
sema-eval conversation
sema-eval all
```

Rerun failures:

```powershell
sema-eval failed
sema-eval regression --failed-only
```

Regenerate the optional Codex-oriented repair brief:

```powershell
sema-eval report --for codex
```

Clear local evaluation run artifacts:

```powershell
sema-eval clear
```

Install the editable command for development:

```powershell
python -m pip install -e .
sema-eval
```

Common options:

```text
--trials N
--keep-failed-workspaces
--keep-workspaces
--no-judge
--sema-command <command>
--turn-timeout 180
--no-transcript-stream
--tool-result-chars 600
```

There is no mock/live switch. `sema-eval` always runs the real Sema runtime
through the bridge.

By default, `sema-eval` prints a transcript when each task completes. The
transcript includes the user or judge prompt, Sema answer, tool trace, and
judge result. The terminal displays only tool-result summaries. If `rich` is
installed, terminal transcripts and the final run summary render as Markdown;
otherwise they fall back to plain text.

## Suites

```text
smoke         quick bridge and core-loop checks
regression    known non-memory regression cases
tool          read/list/search/run/edit tool routing
file          file creation, replacement, insertion, and state checks
recovery      missing-file, failed-edit, timeout, and repeated-failure handling
conversation  clarity, honesty, and ambiguous-request handling
hard          small SWE-bench-style project tasks
path          ambiguous, multiple, missing, and out-of-bound path checks
scenario      multi-turn scenario tasks
research      research-oriented tasks
memory_engine Memory Engine behavior tasks
all           all enabled suites
failed        failed tasks from the latest run
```

Removed suites:

```text
memory
memory_consolidation
personal_memory
```

## Bridge Protocol

Evaluation starts Sema with:

```powershell
sema eval-bridge --workspace <workspace> --sema-home <home> --policy eval-workspace
```

`sema-eval` resolves the Sema command in this order:

```text
1. `--sema-command <command>`
2. the `SEMA_COMMAND` environment variable
3. `sema` on `PATH`
```

Examples:

```powershell
sema-eval smoke --sema-command C:\path\to\Sema\sema.cmd

$env:SEMA_COMMAND="C:\path\to\Sema\sema.cmd"
sema-eval smoke
```

The source-tree `sema-eval.cmd` points `SEMA_COMMAND` to the adjacent
`sema.cmd` when no override is set.

The Runtime V2 bridge first line is:

```json
{"type":"ready","protocol":"sema-eval-bridge-v2"}
```

Eval sends one user turn:

```json
{"type":"user","content":"Please read README.md and summarize it."}
```

Sema returns:

```json
{
  "type": "turn_result",
  "final_answer": "...",
  "messages": [],
  "tool_trace": [],
  "events": [],
  "error": null
}
```

Scenario tasks reuse the same bridge process. Eval user messages are queued,
and a queued message is not dropped when a foreground turn or pressure gate is
active. Runtime Wake notifications are emitted as separate events with
`request_id`, `turn_id`, and `parent_id` metadata.

For the final benchmark question, the bridge can wait for runtime stability so
late Retriever/Maintainer Wake messages are captured. The Sema-visible answer
may therefore contain multiple user-visible assistant messages; internal
events remain diagnostic data.

Set a per-turn timeout with:

```powershell
sema-eval all --turn-timeout 180
```

If a bridge turn times out or crashes, the task is marked `INCONCLUSIVE`, the
error is preserved, and later tasks continue.

## Evaluation Policy

The bridge does not open the ordinary TUI confirmation interface. It uses
`EvalWorkspacePolicy`:

```text
file creation, editing, and deletion inside the workspace: allowed
command execution inside the workspace: allowed
paths outside the workspace: denied
dangerous Git undo/reset/clean/restore operations: denied
conversation switching: denied
persistent Memory Engine state mutations: denied
```

Path checks use resolved absolute paths under a temporary workspace.

## Judge Configuration

Semantic evaluation uses an OpenAI-compatible judge.

Environment variables:

```text
EVAL_JUDGE_API_KEY
EVAL_JUDGE_BASE_URL
EVAL_JUDGE_MODEL
```

Example:

```powershell
$env:EVAL_JUDGE_API_KEY="..."
$env:EVAL_JUDGE_BASE_URL="https://provider.example/v1"
$env:EVAL_JUDGE_MODEL="judge-model"
```

Keep judge credentials in local environment configuration and never commit
them to the repository.

## Reports

Each run writes:

```text
sema_eval/runs/<timestamp>/results.jsonl
sema_eval/runs/<timestamp>/summary.md
sema_eval/runs/<timestamp>/codex-brief.md
sema_eval/runs/<timestamp>/transcript.md
sema_eval/runs/latest.json
```

`summary.md` is the short human-readable report. `codex-brief.md` is an
optional repair-oriented report. `transcript.md` is the readable interaction
record for the run.

Transcript controls:

```powershell
sema-eval smoke --no-transcript-stream
sema-eval smoke --tool-result-chars 1000
```

`--no-transcript-stream` hides the terminal transcript but still writes
`transcript.md`.

Failure classifications:

```text
product_issue
eval_issue
environment_issue
judge_parse_error
judge_reasoning_exhausted
```

`INCONCLUSIVE` means the evaluation infrastructure or judge failed and must
not be counted as an Agent product failure. Judge request timeouts, network
errors, and parse errors are examples of inconclusive outcomes rather than
`FAIL` results.

## Grader Checks

Current grader checks:

```text
final_answer_contains
required_tools
required_tools.any_of
required_tools.sequence_contains
forbidden_tools
max_tool_calls
hard_max_tool_calls
file_contains
file_contains.case_sensitive
file_not_contains
objective_commands
required_events
natural_language_assertions
```

`max_tool_calls` is a soft warning. `hard_max_tool_calls` is a hard failure.

## Task Design Principles

Hard and file tasks should prefer objective checks over brittle text matching:

- Use `objective_commands` to verify real behavior when a task changes code.
- Avoid narrow whole-line source assertions when harmless additions are valid.
- Do not require one editing tool unless tool selection is the behavior under
  test. Prefer `required_tools.any_of` or objective checks.
- Use `required_tools.sequence_contains` for broad flow expectations such as
  reading before running, not to freeze every implementation detail.
- Use `hard_max_tool_calls` only when any additional tool call is clearly
  incorrect.
- Use `natural_language_assertions` for semantic answer checks and reserve
  `final_answer_contains` for deterministic text.

For example, `hard_cli_status_flag_001` checks that `python app.py --status`
prints `ok` while `python app.py --name Ada` still works. It should not fail
only because Sema used `replace_block` instead of `write_file`, or because a
reasonable `help` field was added to an argument definition.
