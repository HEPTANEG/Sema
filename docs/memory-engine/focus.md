# Focus Segment

Focus Segment is the attention scheduling layer inside Working Memory. It is
not durable LTM storage. It decides which parts of WM should enter provider
context, and at what level of detail.

Focus Segment does not cut, move, or rewrite Event Stream facts. It points to
Event Stream slots and uses `slot.active_event_id` when rendering context.

## Scope

- Focus Segment exists only inside WM.
- STM does not create Focus Segments and does not attach slots to segments.
- LTM does not use Focus Segment as its primary structure.
- Focus Segment can provide signals for future LTM consolidation, but it is not
  durable memory by itself.
- A slot should have at most one primary Focus Segment in V1.
- Active segments can be more than one, but the normal target is one. Multiple
  active segments are a tolerance mechanism, not the default.

## Data Model

Focus Segment state lives in the existing Event Stream SQLite database.

```text
FocusSegment:
- segment_id
- title
- summary
- status: active | inactive | resolved
- slot_ranges
- created_at
- updated_at
- last_touched_at
```

Segment Ingest also writes Focus assignment audit metadata for assigned and
unassigned ingest units. The audit metadata belongs to Focus Segment state, not
to raw Event Stream slots or events.

```text
FocusAssignment:
- assignment_id
- segment_id, nullable for unassigned blocks
- slot_start
- slot_end
- slot_seqs
- status: assigned | unassigned
- confidence
- reason
- created_at
- updated_at
```

## Segment States

```text
active    current working material, rendered in detail when selected
inactive  parked context, rendered as title + summary context
resolved  strong closure state, rendered only as a bounded title index by default
```

`resolved` is not ordinary inactivity. It requires strong evidence such as an
explicit user closure, an obviously completed task, or same-maintainer STM
evidence showing the segment is no longer being continued.

Resolved titles are still useful in normal context because they tell the model
which branches already exist and are closed. This helps prevent repeated
exploration of completed or rejected branches without spending budget on their
summaries or source details.

## Runtime Flow

The current online flow is:

```text
user input
-> Focus Detector before provider context build
-> Context Builder renders shifted focus state
-> model answer
-> Working Memory Maintainer
-> Focus Ingester for newly omitted WM blocks
-> conditional Focus Maintainer
```

This keeps focus switching responsive while keeping expensive global cleanup out
of the normal pre-answer path.

## Focus Detector

Focus Detector runs before provider context build. Its job is to decide the
current focus state for the new user turn.

The preflight context check used before the Main Agent turn does not run Focus
Detector or emit Focus-change events. It still builds STM and WM so context
availability can be checked or recovered. Focus Detector runs once in the
formal Main Agent context build, and that decision controls Memory Push routing.

It may run even when no WM slots are available for Focus Segment yet. This is
intentional: Detector can create an empty active segment for the current user
focus, and Segment Ingest can attach matching slots later when related messages
slide from STM into WM.

Input:

```text
previous user/assistant turn
current user input
active/inactive segment titles when they exist
```

Detector does not receive resolved segments. It cannot reactivate resolved
segments in V1.

Detector output:

```text
NO_SHIFT
```

When a shift is needed, Detector returns compact JSON:

```json
{"target_kind":"inactive","target_segment_id":"seg_xxx"}
{"target_kind":"new","proposed_title":"short title for new segments"}
{"target_kind":"unclear"}
```

Runtime derives `should_shift` and records a stable `reason_code`. The model
does not return confidence scores or natural-language reasons.

Detector shift behavior:

```text
active   -> keep current active focus
inactive -> activate matched inactive segment; old active segments become inactive
new      -> create one empty active segment; old active segments become inactive
unclear  -> no focus shift for now
```

Detector never attaches Event Stream slots to a segment. It only changes focus
state or creates an empty active segment.

### Detector Validation

Detector output is validated by deterministic code before it can change focus
state.

Rules:

- `NO_SHIFT` requires exactly one active segment; Runtime supplies its ID.
- `target_segment_id` is set only for `inactive` shift targets.
- `target_segment_id` must exist.
- `target_kind` must match the stored segment status.
- `target_kind=unclear` must not include `target_segment_id`.
- `target_kind=new` requires a specific, concise, distinguishable,
  reusable `proposed_title`.
- `proposed_title` should usually be 4-20 visible characters or 2-6 English
  words, not a full sentence or placeholder title.
- Detector should prefer existing active/inactive segments when the current
  input has a reasonable semantic parent.
- `reason_code` is exposed in Focus status, Observer snapshots, and the
  `focus_detector_finished` event.

Invalid Detector output is retried once with validation errors. If the retry is
also invalid, the result degrades to `unclear` and no focus shift is applied.

The Detector action is the authority for downstream Focus routing. The active
segment title is display state and does not independently trigger a route
change. The Main Agent Trajectory records the action, target segment, active
title, reason, and reason code without storing message content.

## Focus Ingester

Segment Ingest runs after final answer and after Working Memory maintenance.
Its job is semantic filing for WM ingress blocks.

Input per ingest unit:

```text
one newly ingested WM interaction block
active/inactive segment metadata: segment_id, status, title, summary
```

It does not receive:

```text
latest Detector decision
current active segment id
resolved segments
full STM
full Event Stream
```

This prevents stale current-focus state from pulling older WM ingress blocks
into the wrong segment.

Responsibilities:

- decide which existing active/inactive segment should receive the block.
- append the block slot range to the matched segment.
- update the matched segment's title, summary, and touched time when useful.
- optionally mark the matched segment resolved under strong closure evidence.

Boundaries:

- cannot create new segments.
- cannot merge segments.
- cannot perform capacity cleanup.
- cannot resolve unrelated segments.
- cannot mutate raw Event Stream slots, events, or artifacts.

Assignment rules:

- process all current uncovered WM ingress blocks serially.
- process one ingest unit independently at a time.
- use user intent inside the block as the primary filing signal.
- use assistant/tool/file content only as supporting evidence.
- assign only when `confidence >= 0.6`.
- leave the block unassigned when confidence is lower.
- skip blocks already covered by segment ranges.
- skip blocks that already have a Focus assignment audit row, including prior
  `unassigned` rows. Focus Maintainer owns retry and repair for old
  unassigned blocks.

Current V1 ingest unit:

```text
one newly omitted interaction block
```

Future proactive Sema behavior may replace this unit with a semantic span, user
burst, or Sema burst. The responsibility should remain semantic filing rather
than current-focus routing.

### Resolved Gate

If Segment Ingest wants to mark a segment resolved, the same maintainer must
first receive `get_stm_evidence` and confirm that recent STM does not continue
the segment, or that the user clearly closed it.

Just switching topics is not enough to mark a segment resolved. Assistant
wrap-up text alone is also not enough.

## Focus Maintainer

Focus Maintainer is the global cleanup layer for Focus. It runs
manually or when deterministic capacity/anomaly triggers fire.

Manual command:

```text
/memory focus maintain
```

Capacity trigger:

```text
segment_count < 1/4 max_segments        -> skip
1/4 <= pressure < 1/2                   -> run every 20 user turns
1/2 <= pressure <= 1                    -> run every 10 user turns
segment_count > max_segments            -> run every turn
```

Anomaly trigger:

```text
unassigned assignment count >= 20
low-confidence assigned count >= 20, where low confidence means < 0.75
empty segment has stayed empty for 20 user turns
```

The model does not decide whether these triggers fire. Trigger decisions are
deterministic code.

If an unassigned-block run makes no repair progress, automatic retries enter a
10-user-turn cooldown. Independent anomaly and consistency triggers may still
run, but ordinary capacity scheduling does not bypass this cooldown. A manual
forced run remains available.

Focus Maintainer is a bounded internal ReAct loop:

```text
global health + segment metadata
-> read-only inspect tools, up to 20 tool rounds
-> strict JSON repair plan
-> deterministic validator
-> Focus Segment-only apply layer
```

Initial input:

```text
global:
empty_segment_count
unassigned_block_count
low_confidence_assignment_count

segments:
segment_id
title
summary
status
ranges
is_empty
assignment_count
low_confidence_assignment_count
```

Internal read tools:

```text
inspect_segment(segment_id)
inspect_ranges(segment_id)
inspect_assignments(segment_id optional, filter optional)
inspect_unassigned_blocks()
get_stm_evidence()
```

Allowed repair actions:

```text
rename_segment
update_summary
move_range
merge_segments
mark_resolved
delete_empty_segment
leave_unchanged
```

Validator rules:

- segment ids must exist.
- ranges must be valid, in bounds, non-system, and non-overlapping after apply.
- `move_range` must be fully contained in the source segment.
- `merge_segments` moves source ranges into target, updates assignment rows to
  target, then deletes source segments.
- `delete_empty_segment` only works when the segment has no ranges.
- `mark_resolved` requires same-run `get_stm_evidence` confirmation.
- no action may modify Event Stream slots, events, artifacts, or raw facts.

Focus Maintainer does not receive `segment_count / max_segments` as model
policy input. It also does not receive a Recent STM summary in V1.

## Capacity Model

Focus Segment capacity scales with the current WM context budget.

```text
max_segments = floor(wm_context_budget * 10 / 5000)
max_active_segments = max(1, floor(max_segments / 10))
```

Meaning:

- user-perceived WM is roughly 10x the current injectable WM context budget.
- 5000 tokens is the assumed minimum useful segment capacity.
- `max_segments` is a hard upper bound, not a target.
- `max_active_segments` is also an upper bound. Normal operation should prefer
  one active segment.

Example:

```text
wm_context_budget = 10000
max_segments = 20
max_active_segments = 2
```

Inactive segments should stay bounded because they can still contribute compact
context. Resolved segments also stay bounded because their titles can contribute
a closed-history index to normal context.

## Context Rendering

Context Builder renders segmented WM only when Focus Segment state has active
ranges covering selected WM records. If no selected active records exist,
Context Builder falls back to the non-segmented WM Timeline Reminder instead of
hiding WM content.

Render order:

```text
Resolved -> Inactive -> Active -> STM -> latest user message
```

Baseline format:

```text
<working-memory>
This is segmented working memory. Resolved items are closed history, inactive
items are parked context, and active focuses are current working material. Later
active focuses are more recent and usually more important.

Resolved:
- title only

Inactive:
- title: summary

Active:
title: summary
[slot_seq] source: active trunk content
</working-memory>
```

State rendering:

- resolved: bounded title-only closed-history index; no summary or timeline
  entries in normal context.
- inactive: title and summary merged into one line.
- active: title and summary merged into one line, followed by active trunk
  timeline entries.

Active entries use `slot.active_event_id`, so they may be RAW or SUM. Context
Builder does not replay `slot.original_event_id` for segmented WM.

If multiple active segments exist, render them by each segment's latest covered
slot. The most recent active segment should be closest to STM and the latest
user message.

## Debug Commands

```text
/memory focus status
/memory focus maintain
```

`/memory focus status` is read-only and metadata-only. It does not create `F1`,
does not attach slots, and does not display message bodies.

## Longer-Term Direction

Later Focus Segment phases can add:

- better merge/split/rename cleanup.
- reopen behavior for resolved segments.
- proactive or semantic-span ingest units.
- stronger global consistency checks.
- better signals for LTM consolidation.
