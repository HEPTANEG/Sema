# Context Repetition Detection

Sema provides a low-latency runtime detector for suspicious repetition in
append-only model/tool loops. The first integration is the staged LTM
Maintainer. The detector is independent of Memory Engine schemas and can be
reused by other iterative Agents later.

## Detection Input

One detection unit is one accepted assistant response containing its natural
language content and all tool calls generated in that response. Multiple tool
calls in one response are one unit.

Tool results are not part of the similarity input. They are Runtime feedback
and may be large. Runtime uses them only to complete the round and retain
references to the existing tool spans and artifacts.

Protocol errors, JSON correction responses, Runtime notices, provider errors,
and cancelled responses do not enter the repetition cluster.

## Canonicalization and Similarity

The canonicalizer is schema-independent. It normalizes Unicode, line endings,
whitespace, JSON object-key order, and primitive type boundaries while
preserving arrays, fields, and values. It does not remove IDs, timestamps,
cursors, or tool-specific fields.

The detector applies exact SHA-256 fingerprints first and then a lightweight
token-shingle MinHash for near duplicates. It does not call an LLM or Embedding
service. Embedding is not used in the synchronous path because the local
BGE-M3 benchmark was approximately 177 ms per warm CPU request before service
overhead, and the available GPU path could not load the model within the
current memory budget.

## Maintainer Policy

Each Maintainer invocation owns one detector with a ten-output sliding window.
The detector tracks a consecutive single-output repetition cluster:

- Three similar outputs produce a light reminder.
- Five similar outputs produce a clear warning.
- Eight similar outputs produce a strong warning asking the Maintainer to
  decide whether continuing is necessary.

These thresholds are warnings only. They do not fail or stop maintenance. A
materially different output ends the current cluster. Compound and alternating
patterns are outside the current scope.

Stage transitions are soft boundaries. The invocation keeps one detector and a
bounded recent buffer, but a cluster cannot combine outputs from before and
after a stage boundary. A new invocation or resume creates a new detector.

At the eight-output threshold, Runtime reuses the current Maintainer model in
the next normal request. It does not start a separate Judge Agent or Embedding
request.

## Diagnostics

Each threshold is recorded immediately in the current Trajectory, so useful
evidence survives when possible even if the process later stops unexpectedly.
The warning records the invocation, stage, model round, threshold, pattern
type, matched rounds, similarity summary, tool names, tool spans, and existing
artifact references. It does not duplicate Tool results or write Event Stream
content.

Each threshold is emitted at most once in one repetition cluster. Trajectory
diagnostic persistence is fail-open and cannot turn a successful or running
Maintainer into a failed run.

The existing `ltm_maintenance_repetition_warning` UI event is reused. Runtime
notices are internal maintenance messages and are excluded from subsequent
detector input.

## Scope

The current implementation integrates the detector with the staged LTM
Maintainer only. The legacy Maintainer path is handled separately. Formation,
Retriever, Main Agent, Focus Maintainer, and Memorizer remain candidates for
future integrations and do not change behavior through this document.
