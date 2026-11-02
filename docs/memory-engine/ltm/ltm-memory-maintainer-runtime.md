# LTM Maintainer Runtime and Optimization

## Mechanical forgetting at completion

After finish coverage validation and before Final Check, Runtime refreshes live
Strengths, fixes one deletion batch per run, logically deletes objects below
0.01 (except protected roots), and recomputes surviving neighbors. No additional
model stage, tool or summary is introduced. Runtime notices show progress/counts;
diagnostic events record the batch. Final Check retries and manual resume reuse
the same durable batch, including retryable index cleanup. A later health failure
does not undo committed deletion. Cached Strength can lag between maintenance
passes. See [Core Store](ltm-core-store.md#logical-deletion-and-reclamation).

This document covers runtime strategy for the LTM Maintainer. The stable
behavior contract is in [LTM Memory Maintainer](ltm-memory-maintainer.md).

## Stage Execution

The Maintainer is executed as five Runtime-controlled stages:

```text
recent_review
-> global_duplicate_review
-> abstraction
-> episode_review
-> completion
```

The first four stages are semantic work. Each stage gets a fresh model context,
the current stage instructions, Runtime state, relevant references, and the
previous stage summary. The model must complete the stage with one exclusive
`complete_maintenance_stage` call. The Runtime rejects skipped, repeated, or
out-of-order stage reports and requires relevant read or retrieval evidence.
No stage needs to produce a Store change. `no_applicable_work` is valid only
when the stage's relevant material was inspected.

The completion stage is the only stage that accepts `finish_maintenance({})`.
It checks the fixed coverage target, runs the existing mechanical Final Check,
then completes the Store report. The target is derived from the newest slot in
the current Formation Buffer, not from the newest Event Stream slot. STM and
newer WM slots may be read as context but cannot be used as Maintainer
provenance. A text-only response never completes a stage or a maintenance run.

At invocation start Runtime fixes `maintainable_frontier` to the newest slot
in the Formation Buffer and sets `coverage_target` to the greater of that
frontier and the existing maintenance cursor. An empty Buffer leaves the
cursor unchanged while all semantic stages still run. Manual resume computes
the boundary again.

## Maintenance Pressure

Current maintenance pressure uses the same maintainable Formation Buffer
frontier as the Maintainer:

```text
unmaintained_maintainable_slots =
  max(0, maintainable_frontier - maintenance_coverage_cursor)
pressure = min(1.0, unmaintained_maintainable_slots / 100)
```

The latest Event Stream slot, STM, and newer WM do not increase this backlog.
At `0.5`, `0.75`, and `0.9`, Runtime injects progressively stronger internal
sleep signals into Main-Agent context. At `1.0`, an ordinary TUI message
automatically starts Maintenance and remains pending rather than entering a
Main-Agent turn; manual `/memory ltm maintain` remains available. The existing
Main-Agent projection is otherwise unchanged by these notices, and pressure
does not interrupt a turn that has already started.

A separate proposal to degrade the Main-Agent projection at `0.7`, `0.8`, and
`0.9` is intentionally deferred. If later implemented, it must affect only
the final projection sent to Main Agent: Event Stream persistence, Focus, WM
ingress, Formation, Formation Backfill, and other state-affecting processing
must continue to use their complete eligible inputs and normal budgets. The
proposal would suppress LTM Push at `0.7`, suppress LTM/WM injection at
`0.8`, and use a minimal no-new-tools projection with one-tenth STM budget at
`0.9`; none of those behaviors is part of the current contract.

## Stage Summaries

Every semantic stage returns a `stage_summary` of at most 500 tokens. The
summary is semantic material for the next stage: topics, facts, changes,
concrete leads, and IDs or slot ranges where useful. It is not a report of
which tools were called and should not describe the Maintainer's internal
process in the abstract.

Runtime mechanically concatenates the four summaries into the final
`maintenance_summary`:

```text
[recent_review]
...

[global_duplicate_review]
...

[abstraction]
...

[episode_review]
...
```

There is no completion summary and no additional synthesis model call. The
current invocation's preceding summary is passed to the next stage. A prior
completed run contributes only its matching stage section; the complete prior
summary is not injected into every stage. Old unstructured digest rows are
retained but ignored by staged injection.

### Summary Overflow Recovery

The model-facing `stage_summary` contract is a hard maximum of 500 tokens. The
Maintainer prompt states this limit explicitly, and Runtime counts the complete
submitted text before accepting the stage report. Runtime does not truncate the
first over-limit submission automatically, because the model should have an
opportunity to preserve the important semantic material.

For a summary from 500 through 1,000 tokens, Runtime returns a strong rewrite
notice and allows two rewrites. For a summary above 1,000 tokens, Runtime
returns a stronger notice repeated three times and allows four rewrites. The
notice includes the measured token count, the hard limit, and asks the model to
rewrite from scratch without calling observation tools again. If the applicable
rewrite allowance is exhausted, Runtime applies tokenizer-aware truncation and
accepts the stage if its stage, status, ordering, and evidence are otherwise
valid. The accepted summary remains within 500 tokens; the complete original
model response remains available in trajectory diagnostics. Overflow recovery
does not count as an ordinary stage-protocol violation and does not fail an
otherwise valid maintenance run.

This policy concerns a complete stage report whose summary is too large. It
does not change handling of provider truncation, missing or unknown finish
reasons, malformed tool calls, or other incomplete model responses.

## Summary Cache

The physical compatibility location remains:

```text
.sema/memory/ltm/maintenance_cache/
  pending_digest.json
  semantic_digests.jsonl
```

New completed rows use the `maintenance_summary_v1` format marker and contain
the stage sections. The cache is auxiliary and fail-open: read failures do not
block maintenance, and persistence failures do not roll back a successful
Store run. Failed runs do not write a completed summary. Existing
`semantic_digest_*` SQLite report columns remain for compatibility while new
Runtime and UI results use `maintenance_summary_*` names.

## Completion Wake

After Final Check passes and the Maintainer run is completed, Runtime queues:

```text
source = maintainer
kind = maintenance_completed
processing_class = tool_result_like
```

The payload contains the exact `maintenance_summary`, `run_id`, and completion
metadata. It also carries this short instruction:

```text
你刚从一次类似睡眠的长期记忆维护中醒来。下面是这次梦中保留下来的内容，请用简短、自然的梦境回忆告诉用户。
```

The existing Wake delivery persists a provider-compatible raw Event Stream
slot. Main Agent receives the original summary and instruction and produces
the user-facing narration. Formation sees the Wake under its existing normal
rules and may or may not form memory; no special filter is added. Wake failure
does not roll back completed maintenance.

## Runtime Safety and Recovery

- A manual resume keeps the same maintenance run ID, preserves committed Store
  work, and restarts the stage sequence from `recent_review`.
- Stage reports do not add a global turn, token, time, or tool budget.
- The existing five-plan-rejection and repeated-observation protections remain
  active inside the staged runner.
- Trajectory and UI events record stage starts/completions, protocol rejects,
  Final Check, summary persistence, and Wake enqueue best-effort.
- Trajectory start metadata, completion results, and maintenance reports retain
  the invocation's `maintainable_frontier` and `coverage_target` for diagnosis.
- Maintainer messages and stage summaries do not enter normal chat transcript
  directly; the completion Wake follows the existing Event Stream behavior.

## Deferred and Rejected Overdesign

This phase does not add a shared ReAct framework, a new profile subsystem,
global LTM overview, exhaustive duplicate enumeration, merge quotas, stage
recovery persistence, a streaming Maintainer parser, pressure-based projection
degradation, or special Formation filtering for Wake content. Automatic
maintenance at pressure `1.0` is implemented; it is not part of the deferred
list.
