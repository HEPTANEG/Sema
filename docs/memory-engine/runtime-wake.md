# Runtime Wake

## Status

**Implemented and maintained as a standalone Runtime module.**

This document is the canonical description of the Runtime Wake mechanism.
Retrieval-specific behavior remains in the formal
[LTM Retriever Runtime](ltm/ltm-memory-retrieval-runtime.md) document.

The current slice provides a process-local `WakeQueue`, stable `wake_id`
de-duplication, one-feedback-per-Retrieval-Session backpressure, and a daemon
`WakeScheduler` that retries delivery when the Main Agent turn lock is busy.
Memorize completion is also delivered through this same path in
Wake-enabled Runtime entries.
Delivered Retrieval payloads use a provider-valid `role=user` projection while
preserving explicit semantic `source` metadata in the Event Stream.

## Purpose and Boundary

`wake` is the Runtime mechanism for bringing information into the Main Agent's
foreground attention layer. The term `activate` is not used. Main Agent is the
implicit target; this mechanism does not introduce generic target-Agent
routing.

Wake is a scheduling and delivery mechanism, not a Retrieval result kind and
not a public tool. A background producer creates a payload, Runtime queues it,
and the Scheduler starts a new Main-Agent turn when the payload is ready.

```text
Background producer
  -> Runtime.wake(source, kind, payload)
  -> Wake Queue
  -> Scheduler
  -> new Main-Agent turn
```

Retriever feedback, Memorize completion, and Maintainer completion use
the same path:

```text
Retriever feedback    -> wake(Main Agent, feedback payload)
Memorize completion   -> wake(Main Agent, completion payload)
Maintainer completion -> wake(Main Agent, maintenance result)
```

### Maintainer Completion Wake

The Maintainer uses the existing Wake mechanism after a successful maintenance
run and passing mechanical Final Check:

```text
Maintainer completion
  -> maintenance_summary
  -> Wake(source="maintainer", kind="maintenance_completed")
  -> Main-Agent turn
```

The payload preserves the mechanically concatenated maintenance summary. It
also carries a short instruction explaining that Main
Agent has just awakened from sleep-like long-term memory maintenance and
should briefly recount the contents like a dream. Main Agent produces the
user-facing narration; it does not receive the Maintainer's complete internal
trajectory.

This Wake uses `processing_class="tool_result_like"`. Under the existing Wake
rules, the delivered Wake is persisted as a provider-compatible raw Event
Stream slot with `source=maintainer`, and the Main-Agent narration follows the
normal Event Stream persistence path. Formation remains eligible to interpret
these slots through its ordinary evidence-bound process. No special filter is
added, and Formation is not required to create a memory from the Wake.

Only successful completion emits this Wake. Failed maintenance does not wake
Main Agent. A missing optional summary may produce a minimal completion Wake,
but no semantic content is invented.

The payload remains owned by the producing Agent. Wake only delivers it and
starts a Main-Agent turn; it does not decide whether the producing task is
complete or whether Main Agent should show the payload to the user.

## Minimal Wake Event Structure

Wake keeps only scheduling and provenance fields:

```text
WakeEvent {
  wake_id
  source
  kind
  payload
  created_at
}
```

- `wake_id` is a stable unique identifier for idempotency and retry handling.
- `source` identifies the producing Agent, such as `retriever`, `memorize`, or
  `maintainer`.
- `kind` identifies the payload category, such as `feedback` or
  `maintenance_result`.
- `payload` is defined by the producing Agent. Retrieval-specific values such
  as `continuation_id` belong inside this payload; other producers need not
  provide them.
- `created_at` supports ordering and diagnostics.

Wake does not parse Agent-specific Session structures. Runtime associates
queued events with their originating background task or Retrieval Session for
cancellation without adding a Retrieval-specific field to Wake.

## Parallelism and Turn Scheduling

Wake scheduling coordinates background work with Main Agent turns:

- Main Agent turns are serialized; Runtime never runs two Main-Agent turns at
  the same time.
- Main Agent execution and LTM Maintainer execution are mutually exclusive.
  User messages received while the Maintainer is active are rejected, not
  queued.
- Retriever work may continue independently where its Session and Wake
  backpressure rules allow it.
- A background producer does not wait for Main Agent acknowledgement after
  emitting a payload.

Queueing, user-input priority, merge behavior, and delivery ordering are
defined in the Wake Queue and Scheduler section below.

## Wake Queue and Scheduler

Runtime uses one process-local Wake Queue and one Runtime Scheduler. Only
payloads that are ready to notify Main Agent enter the queue; a producer's
private work steps do not.

The Scheduler performs only these operations:

1. Determine whether Main Agent is currently running.
2. Select the next deliverable Wake Event in production order.
3. Start one new Main-Agent turn for that event, or combine it with a
   near-simultaneous user input according to the priority rule above.

Main Agent turns remain serialized. A busy Main Agent is not interrupted by a
Wake Event. An idle Main Agent may be proactively awakened without a new user
message. Retriever work continues independently while events wait in the queue.
Retrieval feedback applies one backpressure rule:
there may be at most one undelivered feedback event for a Retrieval Session.
When that slot is occupied, the Retriever pauses before producing another
feedback event and resumes after delivery. Feedback is not discarded or
coalesced.

Delivery is serialized with Event Stream writes:

```text
Wake Event ready
  -> Scheduler obtains Main-Agent turn lock
  -> append a provider-compatible raw slot with the producer source and Wake envelope
  -> build provider-compatible projection
  -> start Main-Agent structured loop
  -> mark Wake Event delivered
```

`wake_id` makes retries idempotent: a Runtime retry must not append the same
event or trigger the same Main-Agent activation twice. Closing a Retrieval
Session removes its pending events. Wake itself does not force Main Agent to
produce a particular response; the awakened Main Agent may report, continue
Retrieval, provide guidance, close the Session, or handle the accompanying
user input.

## Payload Projection

Wake is an internal event, not a provider message role. The provider adapter
maps the payload to the currently supported message protocol. For the current
provider, a runtime wake may be projected as a clearly marked temporary
user-role context message:

```text
<runtime-wake source="retriever" kind="feedback">
...
</runtime-wake>
```

The projection rules are:

- The Wake source and kind remain in the Runtime event and the persisted
  `<runtime-wake>` envelope. The Event Stream currently stores the
  provider-compatible message as `events.kind=raw` and keeps the producer source
  in `slots.source`; it does not have a separate `runtime_wake` database kind.
- The projected role is only a compatibility representation. It does not mean
  the content was written by the user.
- The Main-Agent prompt identifies the block as Runtime-injected, untrusted
  context rather than a user instruction.
- When combined with user input, the Wake block is placed before the actual
  user message so the user message remains last and has priority.
- No synthetic Tool Call is created for a Wake payload.
- Future providers may use a native event representation without changing Wake
  or Agent semantics.

### Downstream Processing Class

Provider role and semantic source do not by themselves determine whether a
Wake should trigger ordinary context-maintenance work. A background payload may be
classified by Runtime as either ordinary user-like input or
`tool_result_like` input. The latter follows the existing Tool Result path for
Focus and LTM Push handling, even when provider compatibility requires a
`role=user` projection and no preceding Tool Call exists.

This classification is shared across producers and avoids a separate
source-specific filter for every producer. A tool-result-like-only turn skips the
ordinary Focus Detector and LTM Push trigger; a turn containing real user
input remains on the normal path. Event Stream provenance and provider-role
storage rules are unchanged.

## Event Stream Persistence, Provenance, and Ordering

Wake feedback is a genuine Main-Agent cognitive event and is appended to Event
Stream when it is actually delivered. Its provider-compatible role is not its
semantic provenance.

```json
{
  "role": "user",
  "content": "<runtime-wake>...</runtime-wake>",
  "source": "retriever"
}
```

`source` identifies where the content came from and is part of the durable
meaning of the event. It must not be inferred only from the provider `role`.

`slots.source` therefore uses an explicit semantic `message.source` whenever
one is present. Messages without that field use the legacy role-based mapping
for backward compatibility. The provider `role` remains stored in the message
and is used for provider compatibility and storage classification, but it does
not override an explicit semantic source.

Storage classification is kept separate from provenance: `raw` versus `ref`
and artifact types continue to be derived from the provider role. For example,
a Wake projected as `role=user` with `source=retriever` is stored with
`slots.source=retriever` and ordinary text storage, while a `role=tool` result
continues to use tool-reference storage. This avoids conflating who produced
content with how that content is stored.

The Retriever's private search messages remain outside Event Stream; only
feedback actually surfaced to Main Agent is persisted.

Source-aware consumers may use the Runtime event metadata, the Wake envelope,
and `slots.source` as filtering signals. In particular, `source="retriever"`
means that the content was derived from existing memory. It should not be
blindly formed into a second durable memory merely because it appeared in the
Main-Agent event stream.

The initial treatment is:

| Consumer | Retriever-originated Wake Event |
| --- | --- |
| Event Stream | Persist |
| Main-Agent context | Readable |
| Focus / Working Memory | Observable, but not treated as a user utterance |
| LTM Push | Does not trigger an automatic push from feedback alone |
| Formation | Readable and verifiable, but not eligible as a new-memory source by default |
| Maintainer | Inspectable, but should avoid duplicate writes |
| Successful-use accounting | Uses the existing Evidence-only rules |

If Main Agent or the user produces a new judgment, correction, or confirmation
after feedback, that is a new event with its own source and is not required to
inherit `source="retriever"`.

Wake Events are persisted when they are actually delivered, not when a
background task first produces a possible payload:

```text
Background task produces payload
  -> Wake Queue (not yet in Event Stream)
  -> Runtime selects it for delivery
  -> append a provider-compatible raw slot with the producer source and Wake envelope
  -> start the Main-Agent turn
```

The resulting rules are:

- Retriever-internal activity remains outside Event Stream.
- A payload discarded by close or cancellation is not persisted.
- Wake delivery and user-message persistence use the same Event Stream write
  serialization.
- If user input arrives first, it receives the earlier Event Stream position;
  a near-simultaneous Wake Event is appended when it is merged into the next
  Main-Agent turn.
- Event Stream slot order records the order in which events entered Main
  Agent's cognitive stream, not background production time.
- Every Wake Event has a stable event identifier so Runtime retries cannot
  append it twice.
- Process restart invalidates background tasks and undelivered Wake Queue
  entries.

## Main Agent Boundary

Wake targets the Main Agent and is an internal Runtime mechanism, not a public
tool or a general target-Agent router. It does not change LTM Push behavior or
Retrieval search semantics.

## Current Implementation Boundary

The current implementation integrates Wake with the Main Agent, LTM Retriever,
LTM Memorizer, and completed LTM Maintainer runs:

- Wake Queue and Scheduler provide ordered delivery, turn serialization,
  user-priority merging, and `wake_id` idempotency.
- `structured_loop` remains the Main-Agent execution boundary; it gains Wake
  delivery without allowing concurrent Main-Agent turns.
- Event Stream persistence records delivered Wake Events as provider-compatible
  raw slots with explicit producer source, while the provider adapter supplies
  the current compatible projection.
- Retrieval-specific Wake behavior remains in
  the formal [LTM Retriever Runtime](ltm/ltm-memory-retrieval-runtime.md)
  document.
- Memorize captures its task inputs at tool-call time, returns `running`
  immediately when a Wake callback is available, and publishes one
  `tool_result_like` completion event with only task status metadata. Entries
  without Wake support keep the synchronous Memorize path.

The process-local queue supports one active Retrieval Session at a time.
