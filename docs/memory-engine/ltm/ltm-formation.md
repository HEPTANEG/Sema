# LTM Formationer

## Purpose and Boundary

Formation creates new durable MemoryObjects from Event Stream evidence. It is
the initial formation layer, not the general maintenance layer. LTM Core Store
remains responsible for deterministic validation, atomic writes, idempotency,
structured reads, Link lifecycle, derived strength, and index consistency.

Formation does not update, merge, forget, or delete existing objects. LTM
Maintainer owns those maintenance operations. Formation, Memorize, LTM Push,
Memory Retrieval, and Maintainer are separate runtime layers.

Formation runs only with the Event Stream architecture. Event Stream
persistence precedes Formation scheduling, so accepted user, tool-call, tool
result, and final assistant messages are available before a Formation decision
is made. Trusted Runtime code, not the LTM Formationer, determines the
permitted provenance scope.

## Responsibilities

The LTM Formationer:

- decides whether evidence should become long-term memory;
- chooses an allowed object type;
- generates title, summary, and Payload;
- selects the minimal sufficient system-owned Root Tag set;
- returns `no_memory` or a `MemoryWritePlan`.

The Formation Runtime:

- discovers and schedules Formation work;
- binds provenance and allocates object IDs;
- validates Root Tag link intents;
- validates the complete write plan;
- commits through LTM Core Store.

The LTM Formationer never writes directly to SQLite. It may inspect existing
objects through the read-only Structured Retrieval Service, but does not own
Retrieval policy, Link lifecycle, derived strength, or index consistency.

## Evidence and Projection

Normal Formation receives evidence after it has passed through STM and WM and
entered the Formation Buffer. The Buffer uses the effective WM budget,
overlaps the older side of WM, admits complete slots after the older-side
boundary is crossed, and does not duplicate Event Stream facts.

Formation uses Focus Segment ranges as its primary semantic work unit. Content
not covered by a Focus Segment becomes an `unassigned` WorkItem. Large ranges
are split only at complete slot boundaries and retain their segment summary.

The model-facing projection contains:

```text
segment status/title/summary when needed
ordered evidence content
system_root_tags: object_id, tag_key, title
```

It excludes batch IDs, segment IDs, priorities, cursors, slot ranges, and
internal provenance metadata. The fixed Root Tag list is the only intentional
system-owned identifier input.

## Agent Output and Commit

The terminal outcomes are:

```text
no_memory
write_plan
```

Each new assertion, episode, or ordinary tag requires at least one
system-owned Root Tag Link. Only injected active roots may be targeted;
`Others` cannot coexist with another root. Runtime generates object IDs,
canonical Links, and `sourced_from` provenance, then validates and commits the
complete plan atomically.

Before `write_plan`, Formation must complete at least one successful
Structured Retrieval query. `no_memory` may terminate without retrieval when
the evidence is not durable enough to store. Formation retrieval uses the
private `ltm_select_objects`, `ltm_search_objects`, and `ltm_traverse_objects`
tools with the Runtime-bound `formation_existing_memory` profile.

The Core Store rejects invalid object types, Payloads, Root Tag intents,
provenance, duplicate or malformed plans, and partial writes. A successful
commit is idempotent. Formation does not infer a missing Root Tag or partially
commit a plan.

### Episode Criteria

Formation may create an `episode` only when the supplied WorkItem range forms a
bounded work, debugging, decision, research, or failed-attempt process with
durable meaning. An isolated fact, preference, constraint, or conclusion
remains an `assertion`. A WorkItem may contain multiple slots, but Formation
does not automatically aggregate across WorkItems, and Episode count is not a
target. Runtime anchors every created Episode to its Event Stream range with a
`sourced_from` Link.

## Failure Semantics

### Bounded Write-Plan Repair

The Formation prompt states the 1-50 token title limit before generation.
For `title_length`, `missing_summary`, `unexpected_summary`, and
`payload_too_large`, Runtime reports the object field path, measured title
tokens or Payload UTF-8 bytes, and the applicable limits. Byte measurement uses
the same canonical Payload calculation as Core Store validation.

When all reported validation errors belong to these four categories, Formation
may make at most two correction requests after the initial invalid plan. These
requests have an independent allowance: earlier retrieval/model rounds do not
consume the repair opportunities. The original evidence, retrieved results,
invalid plan, and validation feedback remain in context. Corrections return a
complete plan with unrelated objects and fields unchanged; Runtime does not
automatically compress, truncate, or remove content.

Retrieval tools are omitted from repair requests, and tool calls returned during
repair are rejected without execution. Each corrected plan undergoes complete
validation. A valid `no_memory` remains an accepted model decision. Exhausted
repairs, malformed responses, or errors outside these four categories follow
the existing WorkItem failure/retry path. Mixed repairable/nonrepairable errors
do not enter this repair flow. No partial plan is committed, and other WorkItems
retain their independent outcomes.

Existing diagnostics distinguish validation errors, correction attempts, and
terminal outcomes. A recovered validation error is not a failed WorkItem.
`formation.plan.validation_rejected` records eligible rejections and the repair
attempt count on the WorkItem trajectory span; full feedback remains in the
existing Provider context snapshots.

### WorkItem Failure Isolation

Formation failure never invalidates the user-facing answer. A failed work item
does not advance its cursor and remains eligible for later retry. `no_memory`
and successful commits complete their work item. Agent, retrieval, parsing,
validation, and Core Store errors produce no partial LTM write.

Formation coverage is independent from LTM Maintainer coverage. Completing a
Formation WorkItem means that the Event Stream range was judged for initial
formation; it does not mean that the range has been globally maintained.

The newest slot in the dynamic Formation Buffer is also the boundary used by
LTM Maintainer for Event Stream provenance and pressure. This shared boundary
is computed from the current STM/WM budgets and WM ingress watermark at the
start of a Maintainer invocation. Maintainer may inspect newer STM or WM
content as reference, but cannot create `sourced_from` provenance beyond the
frontier or advance its coverage cursor there. Formation itself keeps its
existing WM-overlap write behavior and is not subject to this Maintainer-only
Runtime restriction.

## Stable Limits and Non-Goals

### Retrieval Progress and Budget Reminders

Each WorkItem execution tracks returned memory material in memory. New object
IDs, new or changed nonempty content fields, and newly observed traversal Links
count as new material. Repeated subsets, query changes, result order, cursors,
strength, timestamps, and ranking metadata do not. Reading a body after its
title counts as progress. Structured content is compared by field path and
value; traversal Links are compared independently of path order or direction.

After three consecutive retrieval rounds without new material, Runtime issues
one reminder to reassess the retrieval strategy and missing information. New
material resets this cycle. Error-only rounds count, but the reminder reports
failed calls separately and never describes them as successful empty searches.
Non-tool replies and plan corrections neither increment nor reset the counter.

Runtime also reminds the model at 50%, 75%, and 87.5% of its actual retrieval
tool-call limit, rounding thresholds upward. Each tier is emitted once, using
actual used and remaining counts. Crossing multiple tiers in one round emits
only the highest tier. Both reminder types are added after the complete round
of tool results; simultaneous reminders share one message. The existing hard
limit still rejects excess calls, including calls in the middle of a round.

These limits and counters apply to each WorkItem attempt, not an entire run
or Focus Segment. WorkItems are built from eligible Segment portions or
unassigned slots, split at full-slot boundaries using the dynamic WM budget.
The default retrieval limit remains 20 and callers may override it.

Reminders do not establish evidence sufficiency or justify `no_memory`.
There is no new fifth-round stop, forced finalization, or resumable search
batch. Existing terminal validation, model-round limits, and failure retries
remain in effect. Retry starts with fresh progress and reminder state.

`formation.retrieval.progress` and `formation.retrieval.reminder` events on the
WorkItem trajectory span record counts, model round, reminder kind, and budget.
Material fingerprints remain in memory; existing tool artifacts retain the
source results. Reminder messages enter only Formation context, not Event
Stream or LTM.

### Existing Boundaries

The LTM Formationer uses a bounded private retrieval loop and the immutable
Formation Retrieval Profile. Runtime controls all resource limits and model
tool access. Formation does not provide ordinary Tag hierarchy formation,
general object-to-object relation formation, semantic conflict resolution,
retry backoff, a shared ReAct framework, or existing-object maintenance.

## Automatic Runtime

Automatic Formation has two execution paths:

```text
Online Formation        -> delayed background processing
Formation Backfill     -> resumable processing of older uncovered ranges
```

Both paths reuse this Formation contract, the private Structured Retrieval
boundary, provenance validation, Root Tag rules, and atomic Core Store commit.

After normal working-memory maintenance, Online Formation discovers eligible
ranges from the Formation Buffer and schedules non-blocking Formation
WorkItems. It does not block the Main Agent reply. The Formation Buffer uses
the effective WM budget and the older-side WM boundary.

Focus Segment ranges are preferred work units. Uncovered continuous ranges
become `unassigned` WorkItems. Large ranges are split at complete slot
boundaries. WorkItems track pending ranges, cursors, projection records,
status, and provenance association. Scheduling priority is:

```text
resolved -> inactive -> active -> unassigned
```

Formation Backfill handles Event Stream ranges created before Formation was
available and now strictly older than the current Formation Buffer. Runtime fixes
`historical_backfill_end_slot` at the newest eligible slot before the Buffer.
Discovery and queued execution must stay within that fixed bound and strictly
before the current Buffer's minimum slot. Missing or empty Buffer defers backfill;
it does not authorize processing the entire Event Stream. Existing oversized legacy
bounds do not bypass the live boundary. The shared Online Formation queue likewise
defers tasks containing slots newer than or outside its eligible Buffer/history.
Evaluation's explicit session-range execution remains unchanged.

Formation Backfill is a scheduling responsibility of the same LTM Formationer,
not an independent Agent. Its implementation is `formation_backfill.py`, with
`run_formation_backfill()` as the entry point. Existing `historical_backfill_*`
persistence keys remain compatible; new trajectory records use mode `backfill`.

Each backfill invocation processes at most one WM-budget-sized Batch. Progress
is persisted in `formation_state`; interrupted processing is recovered and
failed WorkItems remain eligible for later retry. Legacy runtime does not
initialize or run Formation Backfill.

Source ranges from logically deleted objects remain in forgetting-batch
metadata, so physically purging those objects does not make Backfill rediscover
their Event Stream slots.

Runtime performs Meta-Memory Bootstrap before discovering or processing work.
The four protected system-owned Root Tags are created exactly once and then
provided as trusted metadata to the LTM Formationer. This does not count as a
Structured Retrieval query.

## Automatic Failure and Observability

Agent, retrieval, parsing, validation, and Core Store errors produce no
partial LTM write. A failed automatic WorkItem does not advance its cursor and
is retried on a later background cycle. `no_memory` and successful commits
complete their WorkItems. Formation coverage is independent from LTM
Maintenance coverage.

Formation status is metadata-only. It may report WorkItem state, cursor,
batch/range counts, model rounds, retrieval counts, validation status, commit
status, and the last error category. It must not expose query text, tool-result
bodies, Payload content, Event Stream content, or model reasoning.

Memorize is the separate explicit write path documented in
[`ltm-memorize.md`](ltm-memorize.md).

The Core Store contract is defined in
[`ltm-core-store.md`](ltm-core-store.md), and the storage-facing retrieval
contract is defined in
[`ltm-structured-retrieval.md`](ltm-structured-retrieval.md).
