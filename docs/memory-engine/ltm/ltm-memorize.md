# LTM Memorizer

## Purpose and Boundary

Memorize is the explicit memory-write path. It is invoked only when the user
directly asks Sema to remember or save information. In Wake-enabled Runtime
entries it runs as a background Worker; entries without Wake support retain a
synchronous fallback. It is separate from automatic Formation, Memory
Retrieval, LTM Push, and LTM Maintenance.

The request itself authorizes the operation; no second confirmation is
required. Memorize creates new MemoryObjects only. It never updates, merges,
forgets, or deletes existing objects.

## Main Agent Interface

The Main Agent exposes one tool:

```text
memorize(instruction)
```

The tool accepts one required string argument, `instruction`. The Main Agent
cannot access the private Structured Retrieval tools or the LTM Core Store
directly.

The immediate result in a Wake-enabled Runtime contains only:

```text
status: running
memorize_id
```

This result means that the task has started, not that the memory has already
been committed. The Main Agent may acknowledge that Memorize is in progress,
but must wait for the completion Wake before claiming that the memory was
saved.

The completion result delivered through Runtime Wake contains only:

```text
status: committed | no_memory | rejected | failed
memorize_id
reason_code
```

Payloads, write plans, retrieval results, internal errors, and model reasoning
are not returned to the Main Agent.

## Evidence and Provenance

Runtime captures the provider context immediately before the `memorize` tool
call. The snapshot is deep-copied, excludes system messages, is not persisted,
and does not include the current tool call.

The factual evidence projection contains the current User message and the
immediately preceding Assistant message when one exists. Runtime verifies
that these messages are present in Event Stream and binds their exact slots as
the only permitted `sourced_from` provenance.

The LTM Memorizer may use the snapshot to understand context, but every
MemoryObject must be supported by the evidence projection. The Agent cannot
choose arbitrary slots, database paths, or provenance scopes.

## Formation and Commit

Memorize reuses the Formation Retrieval Profile and private Structured
Retrieval tools. `no_memory` may finish without retrieval. A `write_plan` must
include at least one successful Structured Retrieval call so exact duplicates
and obvious overlap can be checked.

Runtime generates object IDs and provenance Links, injects the permitted
system-owned Root Tags, validates the complete plan, and commits through the
LTM Core Store atomically. Invalid object types, Payloads, Root Tag links,
provenance, or plan structure reject the entire operation without partial
writes. Commit is idempotent for the same instruction and evidence slots.

## Runtime Wake

Memorize completion uses the shared [Runtime Wake](../runtime-wake.md)
mechanism when a Wake callback is available.

The Worker captures the instruction, message history, provider context
snapshot, and provenance inputs at the time of the tool call. Later user
messages cannot change the evidence for that Memorize task.

After completion, the Worker publishes one Wake event with:

```text
source: memorize
kind: completion
processing_class: tool_result_like
```

Wake delivery starts a new Main Agent turn when the current turn is idle. It
does not trigger LTM Push, Focus Detector, or another Memorize call. User
input has priority over a pending completion Wake. The task and Wake are
process-local; an interrupted process does not recover unfinished work.

The Wake is represented in the provider-compatible Main Agent turn as a
marked `role=user` message. When persisted, the current Event Stream stores
that message as a normal `raw` event with `slots.source=memorize`; the
`<runtime-wake>` envelope carries the Wake source and kind. The Event Stream
does not currently use a separate database event kind named `runtime_wake`.

## Failure Behavior

Provenance failure, retrieval failure, model failure, timeout, cancellation,
parse failure, validation failure, and Core Store failure return a stable
failure or rejection result. Memorize does not enter the background Formation
retry queue and does not affect automatic Formation, Formation Buffer, Focus
Segments, or existing LTM objects.

## Observability

Memorize observations are metadata-only. They may include request count,
status, reason code, model rounds, retrieval counts, validation status, commit
status, provenance slot count, and elapsed time. They must not expose the
instruction, context snapshot, evidence text, Payload, retrieval contents, or
model reasoning.

## Related Documents

- [LTM Formationer](ltm-formation.md): automatic initial memory creation and
  historical processing.
- [LTM Core Store](ltm-core-store.md): validation and atomic commit contract.
- [Structured Retrieval](ltm-structured-retrieval.md): private read-only
  retrieval boundary.
