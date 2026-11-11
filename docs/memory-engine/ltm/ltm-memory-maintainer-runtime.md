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

## Plan Commit Diagnostics and Recovery

`submit_maintenance_plan` remains a single atomic Store operation, but Runtime
records its internal progress in trajectory so a stalled submission can be
located without asking the model to emit diagnostic calls. The phases are
`compile`, `close_retriever`, `ensure_ltm_store`, `stage_vector_embedding`,
`install_vector_index`, and `sqlite_commit`, when applicable. These events are
Runtime-only; they are not injected into the Maintainer prompt and do not add
new model-visible tools.

`embedding_request` and `vector_index_staging` separately identify the two
parts of vector preparation. Native staged index mutations run in a bounded
worker process; an abnormal worker exit or timeout is a technical vector sync
failure and does not commit the Plan. A backend connection loss during an
active maintenance view is displayed as an interruption by the TUI.

Vector embedding and staging use a bounded technical timeout. A timeout returns
`vector_sync_timeout` before canonical rows are committed, and the local
Embedding Service is closed so a stalled subprocess is not retained. The
Maintainer reports the technical failure and does not treat it as a plan
rejection that the model can repair.

The durable maintenance state records the owning process ID and heartbeat. If
a new process finds a `running` run whose owner process no longer exists, it
marks the old Maintainer trajectory as interrupted and starts a new run while
preserving the coverage cursor and committed-plan checkpoint. A live owner is
not replaced; a normal failed-run retry continues to use its existing run ID.

## Maintenance Pressure

Current maintenance pressure uses the same maintainable Formation Buffer
frontier as the Maintainer:

```text
unmaintained_maintainable_slots =
  max(0, maintainable_frontier - maintenance_coverage_cursor)
pressure = min(1.0, unmaintained_maintainable_slots / 200)
```

The latest Event Stream slot, STM, and newer WM do not increase this backlog.
Runtime computes one pressure decision when building each Main-Agent request.
The notice, tool schema, response boundary, and sleep-tool authorization all
use that request-scoped decision; the executor does not independently revoke a
tool that was offered to the model. The dynamic notice is replaced with the
latest snapshot rather than accumulated in conversation history. At `0.5`,
`0.75`, and `0.9` (100, 150, and 180 unmaintained Slots with the current
200-slot denominator), the notice becomes progressively stronger, and the
`sleep_for_maintenance` tool becomes available. Below `0.5`, that tool is not
offered for a new request.

At `1.0`, maintenance is mandatory at the next model decision boundary. An
in-flight model response and its complete returned tool-call batch are allowed
to finish; subsequent ordinary tool calls and final answers are rejected.
Runtime then exposes only `sleep_for_maintenance` until the Main Agent submits
a valid request. Crossing `1.0` creates a process-local mandatory latch. A
later retreat of the dynamic Formation Buffer frontier may lower the live
pressure, but cannot restore ordinary tools or final answers. The latch is
released only after Bridge establishes the running maintenance continuation;
that continuation then owns the paused-task lifecycle. After a successful
maintenance Wake, the continuation enters `resuming` while the Main Agent
continues the paused task. If pressure reaches a sleep threshold again during
that resumed task, `sleep_for_maintenance` is accepted in `resuming`, reuses
the same continuation ID, updates the wake prompt, and starts a new maintenance
run. A continuation in `running` or `sleep_requested` remains non-reentrant.
The user message that reaches the threshold is first added to the Main-Agent
context. The sleep tool
must be the only tool call in its response and must provide a `wake_prompt`
describing the task, exact progress, and next step. Runtime suspends the task
in process memory, runs Maintainer, and uses the existing Wake path to resume
the Main Agent with that prompt and the maintenance summary. STM and WM are not
cleared, but the prompt is the explicit continuation pointer. No latch or
continuation is restored after a process restart. Manual `/memory ltm maintain`
keeps its existing completion narration Wake.

Trajectory records the request snapshot that authorized sleep, the latch
transition, accepted sleep requests, and any difference between the decision
snapshot and the live diagnostic snapshot at execution. The live snapshot is
diagnostic only and cannot invalidate the request-scoped authorization.

If maintenance fails, Runtime wakes Main Agent with the failure reason,
failure-attempt count, original `wake_prompt`, and `maintenance_run_id`. Only
`retry_ltm_maintenance` and `return_maintenance_failure` are available. Retry
uses the same run ID and continuation prompt. Returning the failure gives the
user a normal chat explanation while retaining the paused continuation in
process memory; ordinary task work remains unavailable. Runtime does not set
a retry-count ceiling.

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

Runtime accepts summaries up to 1,000 tokens. Above 1,000 through 2,000 tokens it
allows two rewrites; above 2,000 it allows four and repeats the notice three
times. Each model-facing notice requests a rewrite below 500 tokens without
revealing the runtime tolerance. If the applicable
rewrite allowance is exhausted, Runtime applies tokenizer-aware truncation and
accepts the stage if its stage, status, ordering, and evidence are otherwise
valid. The accepted summary remains within 1,000 tokens; the complete original
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
