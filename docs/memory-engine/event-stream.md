# Event Stream

## Event Stream

Event Stream is the only factual source of Sema's memory system.

It is not Markdown, not a chat transcript, and not limited to the pattern of:

```
user message -> assistant message
```

### Immediate Append Persistence

Once a formal runtime message is accepted into the in-memory message history,
it is appended to Event Stream immediately. The append order is therefore the
same as runtime history order:

```text
user
-> assistant tool-call
-> tool result
-> assistant tool-call
-> tool result
-> final accepted assistant
```

The single-message append API uses the current tail position and never rewrites
or deletes an existing slot. `save_last_session(messages)` remains an
idempotent final-consistency checkpoint; repeated checkpoints do not create
duplicate slots.

Rejected assistant drafts are not formal history messages and are not stored.
If a model, tool, or process fails after accepted messages were
appended, the stored tail remains ordinary Event Stream history and is restored
on the next startup. There is no incomplete-turn marker or rollback. If an
append itself fails, the message is not added to in-memory history and the
current turn stops with the persistence error.

### Structure

Event Stream = trunk + branch artifacts.

- trunk
  - the linear active context stream.
  - contains events that can be directly injected into context.
  - each trunk item is either RAW or SUM.

- branch artifact
  - detailed evidence that is traceable but not injected by default.
  - examples: original raw content behind a SUM, full tool output, raw file,
    HTML block, PDF content, image metadata.

### RAW / SUM Rule

RAW / SUM should be decided before injection.

- In STM, recent RAW can remain RAW even if it is very large, because STM is a
  short-term high-fidelity zone.
- After content leaves STM and enters WM, a trunk RAW that exceeds the current
  token threshold should be represented by SUM.
- If exact detail is needed, Sema can dereference the branch artifact or raw
  evidence during deep recall.
- SUM is only for one message / one slot.
- Multi-message summaries belong to LTM Context Artifacts.

### Core Rules

- Events are append-only.
- Slots are lightweight trunk positions. A Slot may update `active_event_id`
  when RAW is replaced by SUM.
- It should be optimized for fast range query, recent-context lookup,
  token-window construction, and source tracing.
- It is logically linear, but events may point to branch artifacts.
- V1 does not support editing old events, branching, or rollback.
- Future correction is represented by new correction events, not by rewriting
  history.
- Project is a tag/scope, not a permanent top-level container.
- Long-term memory objects must be traceable back to Event Stream when they
  come from user interaction or external evidence.
- Internal reflection must be marked as hypothesis, self-state, plan, or
  evaluation. It is not external evidence.
- `source` means who produced the event.
- `kind` means how the event is represented on the trunk.
- `artifact_type` means what kind of content the event points to.

### Data Model Sketch

**Slot**:
- slot_id
- slot_seq
- created_at
- source: user | sema | tool | file | system
- active_event_id
- original_event_id

**Event**:
- event_id
- slot_id
- kind: raw | sum | ref
- content_ref
- token_count
- created_at

**Artifact**:
- artifact_id
- artifact_type: text | tool_output | file | html | pdf | image | binary
- storage_path
- size_bytes
- token_count
- content_hash
- created_at

Artifact is the large content body. Event is the lightweight record that points
to it. Artifacts should be immutable.

**CorrectionLink**:
- correction_event_id
- target_type: slot | event | memory_object | edge | skill
- target_id
- correction_type: clarify | replace | negate | supersede
- created_at

A correction is still an Event. CorrectionLink records what the correction
points to and how it changes the previous understanding.

## Event Stream V1

V1 uses one message per slot.

### Slot

A Slot is a linear trunk position in Event Stream.

```text
slot_id
slot_seq
created_at
source
active_event_id
original_event_id
```

Field notes:

- `slot_id`: stable unique id.
- `slot_seq`: linear order in the stream.
- `source`: `user`, `sema`, `tool`, `file`, or `system`.
- `active_event_id`: event currently used for projection.
- `original_event_id`: first event written for this slot.

### Event

An Event is a content representation attached to a slot.

```text
event_id
slot_id
kind
content_ref
token_count
created_at
```

V1 writes only original message events. It does not perform RAW -> SUM.

### Artifact

An Artifact is large content referenced by an event.

```text
artifact_id
artifact_type
storage_path
size_bytes
token_count
content_hash
created_at
```

Artifacts are immutable in V1. Reads verify `content_hash`.

## Event Stream V1.1 Observability

V1.1 adds read-only visibility and integrity checks before STM/WM work begins.

### Status

Debug command:

```text
/memory event-stream status
```

It reports:

```text
memory_root
event_stream_dir
db_path
artifacts_dir
slot_count
event_count
inline_event_count
artifact_event_count
artifact_count
artifact_bytes
inline_content_max_chars
```

Purpose: quickly confirm that Event Stream is saving data and whether content is
stored inline or as artifacts.

If project configuration contains a removed architecture value, runtime
selection rejects it before this command can initialize Event Stream storage.

### Inspect API

Read-only helpers:

```text
load_slot(slot_seq)
load_event(event_id)
load_artifact(artifact_id)
```

Purpose: support debugging, future STM/WM/LTM source tracing, and safe local
inspection without manually opening SQLite.

### Evidence Retrieval

Event Stream Evidence Retrieval V1 is the reusable, read-only service for
bounded evidence lookup from Event Stream.

Core APIs:

```text
inspect_slot(slot_seq, projection=active|original)
inspect_range(start, end, projection=active|original)
inspect_interaction_block(slot_seq, projection=active|original)
expand_around(slot_seq, before_blocks, after_blocks, projection=active|original)
inspect_artifact(artifact_id, max_chars)
get_stm_evidence(messages, runtime_state, max_chars)
```

It uses `slot_seq` and slot ranges as primary pointers. It is projection-aware:
`active` reads the current trunk event, while `original` follows the immutable
RAW evidence pointer.

Evidence Retrieval is not semantic search, not LTM retrieval, and not a ReAct
framework. It does not write Event Stream state, trigger maintenance, stage or
commit watermarks, or mutate budget stabilizer state.

### Observer API

Memory Engine Observer API V1 is an internal, Eval-facing structured observer.
It is meant for white-box and trace-style tests that need stable metadata
without parsing slash-command text or querying private SQLite tables directly.

Core APIs:

```text
memory_engine_snapshot(messages=None, runtime_state=None)
context_build_preview(messages=None, runtime_state=None)
evidence_audit(runtime_state=None)
format_memory_engine_snapshot(snapshot)
```

The observer reports storage counts, STM/WM ranges, context budget metadata,
Focus Segment metadata, assignment health, removed-layer status, and
runtime-only evidence audit rows. `context_build_preview()` reports section
metadata and selected slot/block ranges, not provider message bodies.

Observer API V1 is not a user-facing memory feature, not semantic evaluation,
not trace replay, and not LTM retrieval. It is read-only and must not run Focus
Detector, Working Memory maintenance, Full Maintainer, watermark staging or
commit, or budget stabilizer persistence.

### Integrity Check

Debug command:

```text
/memory event-stream check
```

Read-only helper:

```text
check_integrity()
```

Current checks:

- `slot_seq` starts at 0 and is contiguous.
- `active_event_id` and `original_event_id` point to existing events.
- every event points to an existing slot.
- inline events contain inline content.
- artifact-backed events point to existing artifacts.
- artifact files exist and match their stored hash.

Purpose: make Event Stream a trustworthy factual source before higher memory
layers depend on it.

If project configuration contains a removed architecture value, runtime
selection rejects it before this command can initialize Event Stream storage.

## Projection

Event Stream V1 originally used Full Stream Projection:

```text
Event Stream -> provider-compatible messages
```

Context Builder V1 replaces direct STM Projection for provider context:

```text
provider context = system prompt + WM Timeline Reminder + STM Projection
```

The provider API receives normal `messages` only. Event Stream metadata is not
sent to the model.

After Working Memory V1, Event Stream has two projections:

```text
raw projection     = reads slots.original_event_id
active projection  = reads slots.active_event_id
```

UI/session loading and append-only save checks use raw projection for now.
Context Builder uses active projection for WM Timeline Reminder.

The current user message is already a real Event Stream slot before
routing or the first provider model call. This gives Formation and other
runtime consumers a valid provenance source without allowing them to bind
arbitrary slots.

Not sent to the provider:

```text
slot_id
slot_seq
event_id
content_ref
artifact_id
storage_path
content_hash
created_at
```

Current provider projection does not implement:

- Focus Segment scheduling
- LTM retrieval

Those are later layers.
