# LTM Memory Maintainer

## Purpose

LTM Memory Maintainer is Sema's offline, sleep-like maintenance layer for
Long-Term Memory. It reviews Event Stream coverage, MemoryObjects,
MemoryLinks, derived strength metadata, and mechanical consistency after
normal interaction work has produced new memory material.

The Maintainer's job is consolidation, not online recall. It can repair,
reorganize, and extend the LTM graph when there is high-confidence evidence
from Event Stream slots and existing MemoryObjects.

## Scope

Maintainer can use the more powerful `retrieve_memory` tool with natural-language
retrieval targets and continuation guidance. It is useful when locating relevant
memories requires semantic discovery across expressions or languages, multiple
related objects, relationships, or source evidence; `get_object` remains appropriate
for known IDs, titles, and straightforward lookups. The retrieval target may cover a
substantial topic or connected group of memories, but it must have a meaningful
retrieval boundary and stopping point.

Retriever returns existing memory objects and Evidence. It does not decide whether
memories are equivalent or produce maintenance actions. Maintainer retains all
maintenance judgments; results do not guarantee exhaustive coverage, and sufficient
existing evidence does not require an additional Retriever call. Runtime isolates the
caller, waits for a conclusion or `need_clue`, suppresses recall side effects, and
closes the retrieval Session before semantic writes and on exit. Technical retrieval
errors leave other tools available; they do not prove the absence of memory.

Each Maintainer `retrieve_memory` delegation has one overall 120-second technical
deadline covering its initial search and all automatic tool-limit continuation batches.
An explicit `continuation_id` submitted later starts a new delegation and a new
deadline. A deadline result is technical and incomplete, not an empty search result
or a maintenance decision.

Each Maintainer provider response has a 120-second total response deadline. The
deadline covers response-body consumption as well as connection setup, so a
provider that keeps a response open cannot leave the maintenance run waiting
indefinitely.

Prompt step 3 is global duplicate review and consolidation, not optional broad
review. Check older memories by topic as well as new-memory neighborhoods, preserve
independent context, and merge only when justified. There is no exhaustive scan,
merge quota, or new mechanical completion gate. Empty/no-old-memory stores need no
forced search; unavailable checks retain uncertainty. Existing abstraction and
explicit completion behavior remain separate.

Maintainer may:

- review uncovered Event Stream slots and existing memories derived from them;
- inspect MemoryObjects, MemoryLinks, and graph neighborhoods;
- create replacement, merged, split, or abstracted MemoryObjects;
- create or deactivate MemoryLinks;
- retag or relink objects;
- mark support and contradiction relationships;
- update MemoryObject titles and summaries;
- advance the maintenance coverage cursor after semantic review.

Maintainer does not:

- run Formation, Recall, or LTM Push. It may invoke Memory Retrieval as a
  read-only tool, but it does not own the Retriever's search or semantic
  judgment;
- modify Payloads in place;
- directly edit derived strength, FTS, Vector Index, SQLite, or Event Stream;
- prune or physically delete memories;
- ask the user questions during maintenance;
- implement authorization.

All writes go through LTM Store Service.

## Relationship To Formation

Formation and Maintenance both inspect Event Stream evidence, but they do
different work.

Formation is online or near-online initial memory creation. It decides whether
recent Event Stream ranges should produce first-pass MemoryObjects and
provenance Links.

Maintainer is offline global review. It treats Formation-derived objects and
Links as inputs, then decides whether the broader LTM graph needs merge, split,
replacement, abstraction, retagging, relinking, support, contradiction, title
updates, or summary updates.

Formation coverage and Maintenance coverage are independent. A completed
Formation WorkItem does not advance `maintenance_coverage_cursor` and does not
make an Event Stream range maintained. Existing Formation output can make
Maintainer review faster, but it is not proof that the range can be skipped.

## Runtime Model

Maintainer runs through an isolated ReAct runner triggered manually by:

```text
/memory ltm maintain
```

It uses a dedicated prompt and Maintainer-only tools. It does not write its
internal messages, reasoning, tool calls, or progress events to Event Stream.
The runner is stage-oriented: each semantic stage receives a fresh context and
must report a semantic `stage_summary` before the next stage begins.

The runner context includes:

- Maintainer system prompt;
- maintenance runtime state;
- mechanical health snapshot;
- recent maintenance report summary;
- the previous stage summary and Runtime-collected references;
- the previous completed run's summary section for the same stage, when valid.

The loop is:

```text
start or resume run
-> recent_review
-> global_duplicate_review
-> abstraction
-> episode_review
-> completion and explicit finish_maintenance
-> run mechanical Final Check
-> persist maintenance_summary, complete report, and enqueue completion Wake
```

There is no defensive max-turn, max-tool, max-token, or max-time budget in V1.

## Maintenance Method

The prompt constrains strategy without hard-coding a fixed tool sequence:

1. `recent_review`: review recent Event Stream slots and their derived memories,
   then consolidate local neighborhoods. This combines Recent Replay and
   New-Centered Local Consolidation.
2. `global_duplicate_review`: inspect topic-related candidates across older LTM
   as well as recent material and merge equivalent facts when justified.
3. `abstraction`: create or apply a Tag for a stable organizing topic, or a
   supported higher-level assertion when it reduces future understanding cost.
4. `episode_review`: identify bounded work, debugging, decision, research, or
   failed-attempt processes with durable meaning; do not create episodes for
   isolated facts.
5. `completion`: mark coverage and call `finish_maintenance({})` alone.

The first four stages must each call `complete_maintenance_stage` as their only
tool call after relevant inspection. `no_applicable_work` still requires
evidence. Runtime does not require a Store modification or a fixed number of
changes. With no new slots, only the amount of `recent_review` work is reduced;
the other semantic stages still run.

Each semantic stage should provide a `stage_summary` of no more than 500 tokens,
which is the normal target stated in the Maintainer prompt. Runtime tolerates
occasional summaries up to 1,000 tokens without warning or rewriting. Above 1,000,
Runtime gives tiered rewrite feedback and, after the applicable retries, accepts
a tokenizer-truncated summary at the 1,000-token runtime limit when the stage
report is otherwise valid. This auxiliary recovery does not make maintenance
fail.

Root tags are protected system anchors. Modifying their structure is
prohibited.

## Coverage And Pressure

Maintenance coverage is a single continuous Event Stream cursor, bounded by
the dynamic Formation Buffer boundary fixed at the start of each invocation:

```text
maintainable_frontier = newest slot in the current Formation Buffer
coverage_target = max(maintenance_coverage_cursor, maintainable_frontier)
unmaintained_maintainable_slot_count =
    max(0, maintainable_frontier - maintenance_coverage_cursor)
maintenance_pressure = min(1.0, unmaintained_maintainable_slot_count / 200)
```

The initial cursor is `-1`, so first maintenance starts at the first eligible
slot. If the Formation Buffer is empty, the frontier is `-1` and no new
coverage target is created; global semantic review still runs. The frontier
and target are fixed for the invocation and recomputed on manual resume.
The latest Event Stream slot is not itself a Maintainer coverage target.

Maintainer may read STM and newer WM slots as reference material, but those
slots are not eligible Event Stream provenance for a Maintainer write. Older
WM overlap and stable history at or before the frontier are eligible.

Maintainer can only advance coverage continuously. If slot `N` fails, the
cursor can advance at most to `N - 1`.

Formation WorkItem status, Formation coverage, and historical backfill cursor
do not advance `maintenance_coverage_cursor`.

Pressure levels:

| pressure          | level   | behavior                                                                                                                                                                                                    |
| ----------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `< 0.5`           | none    | no Main Agent notice                                                                                                                                                                                        |
| `0.5 <= p < 0.75` | mild    | one internal notice; `sleep_for_maintenance` becomes available for a safe voluntary pause |
| `0.75 <= p < 0.9` | strong  | a stronger notice recommends arranging a pause soon |
| `0.9 <= p < 1.0`  | urgent  | an urgent notice recommends pausing at the next safe point |
| `>= 1.0`          | blocked | at the next model decision boundary only `sleep_for_maintenance` is accepted |

At `pressure >= 1.0`, the user message enters the Main-Agent context first.
Runtime does not interrupt an in-flight response or its returned tool batch;
at the next model decision boundary, ordinary tools and final answers are
rejected until the Main Agent calls `sleep_for_maintenance` alone with a
`wake_prompt`. The `1.0` decision is latched in process memory, so a later
frontier retreat cannot reopen ordinary tools before Bridge accepts the sleep
request and establishes the maintenance continuation. Successful maintenance
resumes the task through a dedicated Wake. During that resumed task, another
sleep request is allowed once the continuation is in `resuming`; it keeps the
same continuation ID and starts a new maintenance run. `running` and
`sleep_requested` remain non-reentrant. A failed run wakes Main Agent with
its failure count and allows only a same-run retry or a user-facing handoff;
ordinary task work remains paused. The latch and continuation are process-local
and are not recovered after restart. Manual `/memory ltm maintain` keeps its
existing behavior.

Soft pressure notices describe the Main Agent's internal memory state and
explain the task-pause tool. Each provider request uses one decision snapshot
for the notice, offered tools, boundary checks, and tool authorization. Notices
are rebuilt rather than accumulated as historical messages. They do not enter
Event Stream or LTM as separate records.

Maintainer's source-range check is a Runtime policy, not a global Store
validator change. A `sourced_from` link whose Event Stream range ends after
the fixed Formation Buffer frontier is rejected without closing an active
Retriever Session. Existing-object operations such as merge, replace,
abstraction, tagging, and title/summary updates remain available when they do
not require an ineligible source range. Coverage beyond the frontier is
rejected without moving the cursor.

## Observation Tools

Maintainer has five runtime-bound read-only tools:

```text
get_object
get_links
traverse_objects
read_event_slots
get_overall_health
```

These tools accept sparse arguments. The model supplies only meaningful
parameters; handlers normalize defaults, validate combinations, apply caps, and
compile to Store queries.

`get_object` reads objects by ID, text/title search, object type, strength,
tag, source-slot, time, or adjacency filters. Projections include a single
object `bundle` view with core data, payload/summary, derived strength
metadata, source slot preview, and direct neighborhood titles.

`get_links` is the generic relation query surface. Relation-specific tools such
as `get_contradictions` are intentionally not added; callers filter
`get_links` by relation type.

`traverse_objects` performs bounded graph traversal with visited-set, depth,
and node limits.

`read_event_slots` defaults to `maintenance_coverage_cursor + 1` and can
include adjacent context plus already derived objects/links. Only target slots
count toward future coverage; context slots do not.

`get_overall_health` performs mechanical consistency checks only. It does not
produce semantic maintenance suggestions or modify LTM.

## Action Plans And Store Commit

Maintainer writes with a `MaintainerActionPlan` containing semantic intents
and/or explicit atomic operations.

Supported intents:

```text
replace
merge
split
abstract
retag
relink
mark_contradiction
resolve_contradiction
mark_support
update_title
update_summary
```

The `abstract` intent requires an explicit `relation_type`:

```text
tagged_with -> create a tag and link each source object to that tag
supports     -> create an assertion and link each assertion or episode source to it
```

For `supports`, every source must be an assertion or episode; a Tag cannot
support an assertion. The new object must be an assertion. To create an
Episode from Event Stream evidence, use atomic `create_object` with
`object_type: episode` and `create_link` with `relation_type: sourced_from` to
the relevant slot or range. `abstract` cannot create Episodes.

Object provenance is a separate operation. A new object sourced from Event
Stream evidence uses a `sourced_from` Link whose target is an Event Stream slot
or contiguous range. The Store rejects `sourced_from` links targeting another
MemoryObject and rejects any other abstract relation.

Links created through `create_link` or `relink` use the following endpoint
contract: `sourced_from` is MemoryObject -> Event Stream slot; `tagged_with` is
MemoryObject -> tag; `supports` and `contradicts` are assertion or episode ->
assertion; and `supersedes` is MemoryObject -> MemoryObject. The tool schema
describes this contract, while Runtime resolves and validates existing IDs and
same-plan temporary IDs.

Supported explicit atomic operations:

```text
create_object
create_link
deactivate_link
update_title
update_summary
```

The model-visible schema is strict:

- no `maintenance_plan` wrapper;
- no action aliases such as `add_object` or `upsert_object`;
- `create_object` is an atomic operation, not an intent;
- major schema levels reject unknown fields.

Episode `payload.structured_data` is constrained in the same schema: it may be
`null`, `{}`, or an object whose only field is `time_scope`. A non-empty
`time_scope` must contain `start`, `end`, or `anchor`, and optional `precision`
must be `exact`, `approximate`, or `relative`.

The Compiler deterministically validates schema shape, resolves plan-local
`temp_id` references, allocates real object IDs, expands intents, deduplicates
identical operations, rejects conflicting updates, verifies references, and
assigns the plan-level idempotency key:

```text
maintenance:{run_id}:plan:{plan_index}
```

The Compiler does not decide semantic truth, choose tags, fill provenance,
modify strength, update cursor, write SQLite, or repair indexes.

Store commits the whole compiled plan atomically. Any validator, embedding,
Vector, or index failure rejects the entire plan. Infrastructure write failures
fail the maintenance run. New-object titles are short index labels and must
contain 1-50 Runtime-counted tokens; detailed facts belong in the payload. A
title-length rejection includes its field path, actual token count, and limit.
Payload size is the UTF-8 byte size of `body` plus canonical JSON for
`structured_data`: at or below 2048 bytes `summary` must be empty, above 2048
and at or below 8192 bytes it is required, and above 8192 bytes the payload
must be split. Payload validation errors report the measured size and limits.
If all reported errors are unnecessary summaries on small new-object payloads,
Runtime clears only those summaries and revalidates the whole compiled Plan.
The normalized fields and measured sizes are recorded in trajectory diagnostics.

Independent modification groups should be submitted as separate Plans. Objects,
Links, and intents that must remain atomic stay together; Runtime does not
automatically split a large Plan or impose a fixed object-count limit.

For a supported title-length, summary, or Episode structured-data rejection, Runtime
saves the rejected plan as an invocation-local draft and exposes
`repair_maintenance_plan`. The tool accepts `repairs` containing only
Runtime-marked `{target, field, value}` entries. The supported fields are
`title`, `summary`, and `payload.structured_data`; payloads outside those
fields, object types, links, intents, and temp IDs cannot be changed through
it. Runtime then recompiles and validates the complete plan. Titles, summaries,
and structured data are not automatically truncated, guessed, or split.

If the repaired submission still has a small number of supported title-length,
summary, or Episode structured-data errors,
Runtime may partially commit independent complete units: an invalid object and
its dependent links are skipped, while unrelated valid objects, links, and
intents are committed atomically. The response identifies committed and
skipped groups and preserves the skipped draft for later repair. Partial
commit is limited to title, summary, and Episode structured-data error
families; malformed JSON, top-level schema errors, dependency ambiguity,
idempotency conflicts, payloads over 8192 bytes, and all other errors retain
the existing whole-plan rejection behavior. Five consecutive rejected plan attempts still
fail the run when no safe progress is made.

## Semantic Stop And Final Check

Malformed tool-argument JSON is rejected without automatic repair. Feedback
includes the parser message, 1-based line/column, 0-based Unicode character
position, and a literal excerpt of at most 160 characters with its start and
error offsets. The parser location indicates where parsing failed, not necessarily
the originating mistake. The model must regenerate arguments matching the tool
schema, without adding a wrapper. Full raw arguments remain in trajectory
artifacts; the existing five-consecutive-invalid-arguments guard is unchanged.

Maintainer declares Semantic Stop by calling `finish_maintenance({})` as the
only tool call in a complete response. Text-only replies never complete a run.

The stop condition is:

```text
Stop the maintenance loop when there is no high-confidence, in-scope,
actionable semantic maintenance left.
```

Semantic Stop is not final success. Each invocation snapshots the dynamic
Formation Buffer frontier and records the resulting
`coverage_target_slot_seq` (refreshed on manual resume). Runtime
first requires coverage through that target, then runs mechanical Final Check.
Insufficient coverage returns the cursor, target, and remaining range; failed
health returns the actual health snapshot. Neither advances coverage implicitly.
Only passing both checks allows report/digest completion. Missing digest remains
nonblocking. Coverage is a declared review boundary, not proof of semantic quality.

Only `stop` and `tool_calls` response finish reasons permit tool execution.
Truncated, missing, or unknown reasons execute no tools, even if arguments are
parseable. Text-only replies, finish mixed with other calls, and nonempty or
invalid finish arguments also violate the completion protocol. The first two
consecutive violations receive a Maintainer-only Runtime notice; the third fails
with `maintenance_completion_protocol`. Accepted tool rounds reset the counter;
coverage/health rejection is actionable feedback, not a protocol violation.
Manual resume preserves the run ID and committed work and resets the counter.
No global loop budget is introduced. Failures before Final Check report
`final_check_status=not_run`, rather than claiming mechanical inconsistency.

### Observation Parameter Contract

Tools declare all currently implemented parameters, with sparse optional inputs
and `additionalProperties: false`. Runtime validates the same contract before
query execution; unknown fields, wrong types, invalid enums, empty ID lists,
negative counts, and reversed ranges return `invalid_arguments` with a field
path and allowed fields. Optional nulls mean omitted. Numeric strings are not
coerced. Positive counts above caps retain existing clipping behavior.

- `get_object`: query, object_id or object_ids (not both), title, min_strength,
  max_strength, linked_to_object_id, tag_object_ids, source_slot_after/before,
  projection, limit.
- `get_links`: relation_types, source_object_ids, target_object_ids,
  endpoint_object_ids, include_inactive, limit.
- `traverse_objects`: required start_object_ids, max_depth, max_nodes,
  projection, min_strength. Traversal is bidirectional over active links and
  final results retain the existing object projection caps.
- `read_event_slots`: start_slot_seq, limit, context_before/after,
  include_adjacent_context, include_existing_sourced_objects. Defaults do not
  advance automatically; next-window reads use the last target slot plus one.
- `get_overall_health`: empty arguments only.

Historical pagination, time filters, and additional traversal filters are not
implemented by this repair and must not be advertised as available parameters.

## UI And Reports

`/memory ltm maintain` switches the TUI into a dedicated Maintainer view. The
normal composer and Main Agent status are hidden while maintenance runs.

The UI shows model turns, provider-exposed reasoning/content, observation tool
calls, plan commits/rejections, coverage advancement, Final Check state, and
completion/failure summary.

Maintainer progress is UI-only. It does not enter the main chat transcript or
Event Stream.

While the view is active, the bottom status bar shows the Runtime-provided
stage, for example:

```text
maintaining LTM · stage 2/5: global duplicate review · 8m 05s
```

The five display stages are `recent review`, `global duplicate review`,
`abstraction`, `episode review`, and `completion & final check`. Before the
first stage event it shows `maintaining LTM · preparing`. These labels are
display-only and do not replace the stage protocol or expose future-stage
instructions to the model.

Every completed run writes one machine-readable report. Reports are not
MemoryObjects and are excluded from Retrieval and LTM Push. The Store keeps
the latest 50 reports.

Report fields include:

```text
maintenance_run_id
started_at
finished_at
start_cursor
end_cursor
coverage_status
processed_slot_count
plan_commit_count
created_object_count
created_link_count
deactivated_link_count
updated_title_count
updated_summary_count
supersedes_count
contradiction_count
failure_slot_seq
failure_reason
final_check_status
semantic_digest_status
semantic_digest_id
semantic_digest_token_count
```

## Staged Summaries And Completion Wake

Each semantic stage targets a semantic `stage_summary` of at most 500 tokens.
Runtime accepts up to 1,000 tokens as a bounded tolerance and mechanically joins
the four summaries into `maintenance_summary` with stage boundaries. It does
not ask a fifth model call to rewrite the result. The theoretical maximum of
the four-stage summary is therefore about 4000 tokens, although the Prompt
continues to target 500 tokens per stage.
Summaries contain useful memory topics, facts, changes, clues, and IDs where
needed, rather than a diary of Maintainer actions.

Only a passing Final Check persists the staged summary. Failed runs do not
create a completed summary. Legacy unstructured semantic-digest rows remain
available for compatibility but are not injected into staged contexts. The
summary is auxiliary navigation material, not canonical memory or evidence.

After completion, Runtime enqueues a Wake with source `maintainer`, kind
`maintenance_completed`, and processing class `tool_result_like`. The Wake
contains the exact maintenance summary and a short instruction to recount it as
a dream after waking. Existing Wake delivery persists the raw Wake slot and
lets Main Agent and Formation process it through their normal mechanisms.

## Observation Search and Repetition Recovery

`get_object(query)` is a plain-text keyword query, not an FTS expression.
Whitespace-separated, case-insensitively deduplicated terms use OR matching
across title, summary, and payload body. Trigram FTS handles terms of at least
three characters; shorter terms use literal substring matching. Special syntax
is quoted, and empty/unsearchable queries are rejected rather than listing all
objects. Each distinct term counts once across all fields. Results expose
`matched_query_term_count` only for queries and sort by that count descending,
then strength descending, creation time descending, and object ID. Filters and
ranking precede the existing projection limits. Other Agents are unchanged.

The staged Runner uses the shared [Context Repetition Detection](../../context-repetition-detection.md)
module. It compares accepted assistant output and tool calls, not Tool results,
with exact and lightweight near-duplicate fingerprints. A ten-output sliding
window emits warnings at three, five, and eight consecutive similar outputs.
Warnings do not fail or stop maintenance; the current Maintainer decides
whether repeated inspection is necessary. Stage boundaries cannot be combined
into one repetition cluster, and a new invocation or resume starts a new
detector. The legacy Maintainer path is documented and handled separately.

Trajectory records each threshold immediately with compact fingerprints,
matched rounds, model and stage metadata, tool spans, and artifact references.
The existing `ltm_maintenance_repetition_warning` event remains outside
ordinary chat and Event Stream. Diagnostic persistence is fail-open.

## Deferred Work

- prune/delete/GC for low-strength memories;
- user-question workflow for unresolved high-value contradictions;
- pressure-based projection degradation that suppresses LTM Push, WM, or
  tools at higher pressure levels;
- generic shared ReAct kernel;
- streaming Maintainer parser;
- defensive runtime budgets;
- relation-specific observation tools.
