# Memory Engine Design Principles

## Source Of Truth

- Event Stream is the factual source for user interaction, external evidence,
  tool output, files, and UI events.
- Markdown, HTML, TUI, and GUI views are projections of Event Stream data, not
  separate memory stores.
- STM Projection, Working Memory, Focus Segments, and LTM are layers built on
  top of Event Stream.
- Provider context is assembled by Context Builder from bounded current state
  and evidence. It is not a persisted transcript projection.

## Runtime Boundaries

- `my_agent/memory_engine/` is the canonical memory package; LTM-specific implementation lives under `my_agent/memory_engine/ltm/`.
- The online path remains light: append events, build bounded context, and
  make the model request.
- Slower formation, index rebuilds, and maintenance run in the background or
  during idle time. The current LTM Push candidate and Rank 1/2/3 stages
  are bounded foreground work because their result is needed for the current
  provider request.
- `show_memory_state` exposes observer metadata only; it does not inject or
  disclose full memory content.
- Higher-level code uses stable Event Stream and Memory Engine APIs instead of
  reading storage files directly.

## Performance Model

Current performance is measured from real usage rather than a fixed
end-to-end latency target. The implementation keeps technical timeouts where
needed and optimizes the dominant stages incrementally. Foreground resource
use and disk I/O remain secondary constraints.

The online path avoids full-database scans, expensive graph cleanup, index
rebuilds, and unbounded retrieval. It prefers STM state, Working Memory, Focus
Segment scores, promoted LTM objects, and bounded candidate ranking.

Longer-term storage separates hot, warm, and cold data. Hot state supports
interactive context construction; warm data is indexed for retrieval; cold
artifacts are loaded only for bounded recall, audit, export, or background
work.

## Storage Direction

The conceptual model is storage-engine independent. The current implementation
uses project-local Event Stream persistence under:

```text
project/.sema/memory/event_stream/
  stream.db
  artifacts/
```

SQLite stores structured metadata and short content. Immutable artifacts hold
larger payloads. Callers use Event Stream APIs rather than deciding whether a
value is inline or artifact-backed.

The engine should remain language-replaceable. Python currently orchestrates
agents and policies while storage, indexing, ranking, and graph traversal may
move to specialized implementations later.
