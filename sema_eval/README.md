# sema-eval

External benchmark harness for Sema.

`sema-eval` does not import Sema internals. It starts Sema as an external command and talks to:

```powershell
sema eval-bridge --workspace <workspace> --sema-home <home> --policy eval-workspace
```

## Install For Development

From the repo root:

```powershell
python -m pip install -e .
```

Then:

```powershell
sema-eval
```

## Sema Command Discovery

`sema-eval` finds Sema in this order:

```text
1. --sema-command <command>
2. SEMA_COMMAND environment variable
3. sema from PATH
```

Examples:

```powershell
sema-eval smoke --sema-command C:\path\to\Sema\sema.cmd

$env:SEMA_COMMAND="C:\path\to\Sema\sema.cmd"
sema-eval smoke
```

Useful runtime controls:

```powershell
sema-eval smoke --turn-timeout 180
sema-eval smoke --no-transcript-stream
sema-eval smoke --tool-result-chars 1000
```

By default, `sema-eval` prints each task transcript to the terminal when the task finishes.
When `rich` is installed, terminal transcripts and the final run summary are rendered as Markdown.

The `path` suite checks fuzzy path resolution, ambiguous candidate handling, missing-file honesty, and external-path permission events.
The `research` suite checks web-backed deep research quality with hidden answer keys and rubrics.

The source-tree `sema-eval.cmd` sets `SEMA_COMMAND` to the adjacent `sema.cmd` when it is unset.

## Optional Benchmark Adapters

The public Sema snapshot does not redistribute third-party evaluator source,
prompts, datasets, or fixtures. Sema-authored adapter code may remain available
for reference, but an adapter that depends on excluded upstream material is not
part of the supported public command set. Ordinary local suites do not import
that optional adapter.

Sema's MIT license does not cover independently obtained benchmark material.
Before enabling an optional adapter, obtain its required sources from the
upstream project and review the upstream license and usage terms.

## Judge Config

Semantic tasks require an OpenAI-compatible judge:

```text
EVAL_JUDGE_API_KEY
EVAL_JUDGE_BASE_URL
EVAL_JUDGE_MODEL
EVAL_JUDGE_MAX_TOKENS
```

`EVAL_JUDGE_MAX_TOKENS` is optional. Individual tasks can override it when a
large hidden rubric needs a larger judge response budget.

## Reports

By default, runs are written under the directory from which `sema-eval` is
started:

```text
<cwd>/sema_eval/runs/<timestamp>/
```

Set `SEMA_EVAL_RUNS_DIR` to override this location.

Main files:

```text
results.jsonl
summary.md
codex-brief.md
transcript.md
```
