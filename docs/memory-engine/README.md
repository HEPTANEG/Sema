# Memory Engine Documentation

This folder is the canonical documentation home for Sema's current Memory
Engine. The subsystem documents separate implemented contracts by concern so
readers and contributors can follow the runtime without relying on private
design history.

## Reading Order

1. [Implementation Status](implementation-status.md) - what exists now, boundaries, and migration notes.
2. [Design Principles](design-principles.md) - global constraints, performance model, and high-level design rules.
3. [Event Stream](event-stream.md) - factual source, storage model, slots/events/artifacts, and projections.
4. [Short-Term Memory](stm.md) - STM Projection, interaction blocks, one-way WM ingress, evidence, and observability.
5. [Working Memory](wm.md) - `memory_engine.wm` package API, RAW/ref to SUM maintenance, candidate selection, evidence retry, preflight recovery, and downstream handoffs.
6. [Context Builder and Context Budget](stm-wm-context.md) - provider-context assembly, Dynamic Context Budget, Budget Stabilizer, and design validation.
7. [Focus Segment](focus.md) - WM attention scheduling, Detector, Segment Ingest, and Full Maintainer concepts.
8. [Runtime Wake](runtime-wake.md) - process-local background-to-Main-Agent wake delivery, scheduling, provenance, and provider projection.
9. [LTM Core Store](ltm/ltm-core-store.md) - implemented storage, contracts, derived strength, writes, reads, indexes, and Observer boundary.
10. [LTM Formationer](ltm/ltm-formation.md) - stable formation architecture and Core Store handoff.
11. [LTM Memorizer](ltm/ltm-memorize.md) - explicit memory-write path with Runtime Wake support.
12. [LTM Structured Retrieval](ltm/ltm-structured-retrieval.md) - read-only Store-facing retrieval tools and profiles.
13. [LTM Retriever](ltm/ltm-memory-retrieval.md) - explicit query-driven evidence retrieval contract.
14. [LTM Retriever Runtime](ltm/ltm-memory-retrieval-runtime.md) - Session, Worker, and recovery behavior.
15. [LTM Push](ltm/ltm-memory-push.md) - online LTM context injection pipeline.
16. [LTM Push Runtime](ltm/ltm-memory-push-runtime.md) - measured bottlenecks, routing, and incremental reuse.
17. [LTM Memory Maintainer](ltm/ltm-memory-maintainer.md) - offline sleep-like maintenance runner, tools, compiler, coverage, pressure, and UI.
18. [LTM Memory Maintainer Optimization](ltm/ltm-memory-maintainer-runtime.md) - semantic digest cache and deferred performance/runtime strategy.
19. [LTM Debug Visualization](ltm/ltm-debug-visualization.md) - local read-only graph snapshots, static HTML rendering, inspector behavior, and debug boundaries.

## First-Run LTM Availability

The Windows Backend owns one setup capability state. Explicitly continuing
without configured local memory services keeps Event Stream, STM, WM, and
Focus, but disables LTM Push (including cached injection), Formation,
historical discovery, LTM maintenance, and the three LTM tools. Execution
guards also reject tool calls from older contexts and Developer commands.

`my_agent/runtime/setup_capabilities.py` records observed Formation-window slots in
`setup_ltm_window`, the first newly paused slot in `setup_ltm_pause`, and
expired slots in `setup_ltm_skips` in the project Event
Stream database. Empty or failed projections do not imply expiration.
The slot set and pause boundary persist across restart; new slots crossed by
a window jump are included. Restoration defers Formation and historical work
until a valid non-empty window is available. It compares the pending window
before historical scheduling, excludes expired ranges from discovery and
pending work, and preserves still-eligible parts of mixed work items.

Focus retirement accepts explicit skips as a separate disposition. Consistency
reports expose `formation_explicitly_skipped_slot_count` separately from actual
captured slots. This never deletes raw Event Stream records or rewrites
previously completed LTM. Temporary service failures do not create skip records.
See [installation](../installation.md) for setup and recovery acceptance.
