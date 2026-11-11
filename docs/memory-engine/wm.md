# Working Memory (WM)

Working Memory is the compressed working area for Event Stream material that
has left STM but may still matter to the current task. It is not a second
source of truth. It is a derived layer that changes which Event representation
is active on the trunk while preserving the original RAW evidence.

## Responsibilities And Boundaries

WM owns compression after STM. It can contain:

- recent Event Stream material outside STM;
- intermediate execution details that are no longer in STM;
- single-slot SUM items;
- Focus Segment scheduling state; and
- future promoted LTM objects that expire when they stop being useful.

WM does not rewrite Event Stream history, run semantic LTM retrieval, or create
multi-message summaries. Focus Segment schedules Event Stream material inside
WM. Future promoted LTM objects should be managed by the WM or memory-push
layer, not by Focus Segment.

## RAW, SUM, And Source Tracing

WM V1 can replace one long active RAW/ref event with one SUM:

- SUM summarizes exactly one slot, not a conversation range.
- SUM is reusable and must not be regenerated during every context build.
- SUM lives on the Event Stream trunk by updating `slots.active_event_id`.
- `slots.original_event_id` remains the RAW evidence pointer.
- The original RAW event is not deleted and remains reachable through
  `slots.original_event_id` or artifact-backed content.
- V1 does not add `source_event_id`; `slot.original_event_id` is enough for
  source tracing.

SUM content is metadata:

```text
source_role
summary
reason
original_token_count
summary_token_count
created_by
slot_seq
original_event_id
```

SUM is not a provider `user`, `assistant`, or `tool` message. Context Builder
renders active SUM records into the WM Timeline Reminder before provider calls.

## Candidate Selection

Normal WM maintenance uses this deterministic filter:

```text
slot is outside STM
active event is raw/ref
token_count > 400
slot has not already been processed by Working Memory
process current candidates serially, oldest candidate first
```

Candidate selection uses the same effective dynamic/manual STM budget as
Context Builder. It must not use a fixed projection boundary.

The deterministic filter chooses candidates. The Working Memory Maintainer
Agent decides `summarize | keep_raw | need_context`. If the first decision is
`need_context`, WM calls Event Stream Evidence Retrieval with
`expand_around(candidate.slot_seq, before_blocks=1, after_blocks=1)` and asks
the same maintainer for one final decision. If evidence retrieval fails or the
second decision is still `need_context`, WM keeps the slot RAW and records the
reason.

When runtime state is available, Evidence Retrieval records a runtime-only
audit row under the Memory Engine observer data. The audit includes query
metadata such as query type, projection, range, max preview size, result count,
error count, and `read_only=true`; it does not persist to Event Stream.

## Maintenance And Capacity Recovery

Normal WM maintenance runs after final answers when STM has omitted units.
Each maintenance trigger processes all current candidates serially. It does not
send parallel provider requests in V1.

Capacity Planner measures the minimum valid WM projection before provider
assembly. When the newest required RAW/ref record exceeds the configured WM
maximum, it may run one exact-slot compaction attempt against that record. A
successful SUM is measured once more. A `keep_raw`, missing candidate, or
internal-model failure creates a bounded active REF that preserves the
immutable original event and exposes bounded retrieval guidance. Recovery does
not process an interaction block, select unrelated large slots, or rebuild the
entire Context after an exception.

## Current-Turn Pin

WM has one deterministic current-turn external-user pin. When the latest
external user slot leaves the current STM projection, the pin references that
slot's immutable original event. It renders before ordinary WM details as:

```text
Pinned current-turn user instruction (this might be important for completing the current task):
[slot <source_slot_seq>] <original content>
```

The pin counts against the normal WM budget and is deduplicated from the
ordinary WM record for the same slot. It is cleared when the next external user
message is persisted. Runtime-originated `user` messages do not create or
clear it. If the pin itself exceeds WM maximum, it is removed and its source
continues through ordinary WM oversized-record handling; no pin summary is
created.

## Handoffs

Context Builder reads active trunk records before STM and injects WM as a
Timeline Reminder or segmented WM message. It uses `slot.active_event_id`, so a
selected WM record may be RAW/ref or SUM.

Focus Segment runs after WM maintenance for newly omitted WM interaction
blocks. It points to Event Stream slot ranges and uses active trunk content
when rendering segmented WM. It does not move, cut, or rewrite Event Stream
facts.

LTM Formationer is delayed until evidence has passed through STM and WM. Its
Formation Buffer overlaps with the older side of injectable WM and reads active
Event Stream records. Formation creates durable MemoryObjects through the LTM
Core Store; it does not write Event Stream facts directly.

The same dynamic Formation Buffer boundary is used by LTM Maintainer to decide
which Event Stream slots are stable enough for Maintainer provenance and
coverage. STM and newer WM remain readable reference material, but Maintainer
cannot write `sourced_from` links or advance coverage beyond the Buffer
frontier. Formation's own WM-overlap path is unchanged.

UI/session loading still uses the raw/original projection. Provider context
uses Context Builder. V1 does not implement precise user-reference protection:
phrases such as "above", "the previous plan", or "continue that" are future
signals, not current guarantees.

## Interfaces And Status

Primary implementation entrypoints:

```text
working_memory_status(messages=None, runtime_state=None)
run_working_memory_maintenance(messages=None, force=False, runtime_state=None)
compact_exact_slot(slot_seq, runtime_state=None)
format_working_memory_status(status)
format_working_memory_run(result)
```

Debug commands:

```text
/memory wm status
/memory wm run
```

`/memory wm status` is metadata-only. It reports active RAW/ref/SUM token
totals, current candidates, candidate token totals, recent processed slots,
status counts, last result metadata, and summary compression ratios. It does
not show message bodies.

## Implementation Map

- `memory_engine.wm.working_memory` owns candidate selection, Maintainer calls,
  SUM creation, persistent WM slot state, status, manual runs, and exact-slot
  recovery. The package entrypoint `memory_engine.wm` re-exports the formal WM
  operations.
- `memory_engine.wm.pins` owns current-turn pin persistence, derivation, and
  immutable-original rendering.
- `memory_engine.event_stream` stores RAW/ref/SUM events, active/original
  projection state, and immutable artifacts.
- `memory_engine.evidence` provides bounded read-only evidence lookup for
  `need_context` retries.
- `memory_engine.context_builder` consumes active WM records for provider
  context.
- `memory_engine.wm.ingress` provides the monotonic STM -> WM boundary used by
  STM projection and WM candidate selection.
