# Unified Process Trajectories

Sema records diagnostic trajectories for complex runtime processes. The system is observability only: trajectory data is never Event Stream input, LTM content, automatic model context, or recovery truth.

## Storage

Project-local data is stored under:

```text
.sema/observability/
├── trajectory.sqlite3
├── contexts/
│   └── <agent_type>/<utc-time>_<request-seq>_<request-id>.json
└── artifacts/
    └── <sha256-prefix>/<sha256>.<type>.gz
```

SQLite stores root trajectories, nested spans, ordered events, request snapshots, artifact metadata, references, and migration state. Provider requests are directly readable JSON files. Responses, streams, tool arguments, and results remain complete gzip artifacts, identified by the SHA-256 hash of their uncompressed bytes.

Trajectory recording is best-effort. Storage or formatting failures must not change the observed process. Provider authorization headers and API keys injected outside a request body are not recorded.

Read-only inspection uses an existing SQLite database in `mode=ro`. It does not create the observability directory, initialize the schema, run migrations, apply retention, or remove artifacts. Store status distinguishes `uninitialized`, `migration_pending`, `ready`, and `unavailable`. A missing database returns empty query results without creating one; legacy sources and incomplete migrations are reported as pending and remain untouched.

## Root Boundaries

- `main_agent`: one root for each ordinary user turn. It covers context construction, model calls and retries, tool execution, the final answer, and Memory Push as a child span.
- `ltm_maintainer`: one root for each `maintenance_run_id`. Resuming a run appends invocation spans to the same root.
- `memory_retriever`: one root for each `continuation_id`. Each call adds an invocation span and records its caller IDs.
- `formation`: one root per online or historical Formation run.
- `memorize`: one root per explicit synchronous memory-write run; `memorize_mode` identifies the path.
- `memory_push`: a standalone root only when no Main Agent trajectory is available.

Focus/WM does not produce a separate trajectory. Retrieval and Memory Engine
processes retain their own diagnostic roots where documented above.

## Provider Exchanges

The provider exchange observer records the exact serialized request body, complete raw non-stream response or SSE stream, normalized parsed response, finish reason, attempt metadata, duration, and transport or parsing failure. Partial streams are retained as failure artifacts. These observations do not alter stop semantics.

Each actual API attempt, including retries and identical requests, gets its own JSON snapshot. Its bytes are copied from the same serialized body used for HTTP `Request.data`, without indentation changes, wrappers, truncation, or reserialization. This guarantees equality with Sema's outbound body, not the provider's unobservable server-side transformations. Metadata remains in SQLite. New requests have no duplicate gzip request artifact.

The five independent request groups are `main_agent`, `ltm_maintainer`, `memory_retriever`, `memory_push`, and `formation`. Memory Push uses its own group even inside a Main Agent root. All Formation modes share one group and retain `formation_mode` metadata.

## Retention and Migration

The latest 20 terminal roots are retained independently for each process type. Running roots are never pruned. Completion triggers retention and removes unreferenced artifact records and files.

Each Agent group retains its latest 50 request JSON files, independently of root retention. Removing a root does not remove its still-retained requests; a long running root can have expired requests. Each write trims its group, with failed deletions retried at later writes or write initialization. Queries distinguish `available`, `expired`, and unexpectedly `missing` files. Request references remain lightweight after expiration. Artifact GC never scans the contexts directory. Read-only inspection never performs these cleanup operations.

Schema V2 migrates existing gzip `provider_request` artifacts by verifying their hash and writing the original bytes to JSON before switching event references. Shared artifacts are preserved until unreferenced. Migration retries failed items without duplicating completed imports. Imported legacy Main Agent snapshots retain their original file bytes (including their historical metadata/payload wrapper), carry `exact_wire_bytes=false`, and exclude `index.json`. Their original timestamps determine retention, so migration does not displace newer requests with old snapshots. Legacy audit artifacts remain intact.

OpenAI-compatible Provider request bodies are serialized as UTF-8 JSON without
escaping non-ASCII characters. Context request snapshots preserve those exact
wire bytes, so JSON source files remain directly readable while retaining the
same parsed request content.

On first write initialization, the store imports `.sema/logs/trace.jsonl` and `.sema/memory/context_cache/`. Valid records, malformed input, orphan snapshots, and complete source bytes are retained as legacy artifacts. Source hashes and counts are verified before legacy files are removed. Failed migration is fail-open and leaves source files in place for retry. Read-only inspection reports pending migration without importing or deleting these sources.

## Debug Commands

- `/trajectory`: list recent roots.
- `/trajectory current`: show the current or latest Main Agent tree.
- `/trajectory errors`: show recent failed roots, spans, and events.
- `/trajectory path`: show the database and artifact directories.
- `/trajectory <trajectory_id>`: show a formatted span/event tree.
- `/trajectory artifact <artifact_id>`: show artifact metadata and its local path without dumping the body into the TUI.
- `/trajectory context [agent_type]`: show the latest request JSON path, defaulting to Main Agent.
- `/trajectory contexts [agent_type]`: list the latest 50 request snapshots for that Agent.
- `/trajectory context request <request_id>`: locate a specific request or show its expired/missing state.

These commands are read-only inspections. When the store is not ready, they report its status instead of implicitly initializing or migrating it.

`/trace` was removed during the one-time cutover and is not an alias.

## Testing

Trajectory tests use temporary project roots, mocked providers, and local fake streams. No trajectory test may require a live model, embedding, or reranker service.

`tests/test_trajectory_contexts.py` checks byte-for-byte HTTP request equality, streaming and Ollama requests, retries, independent retention, concurrent writes, request migration, failure isolation, and the context commands.
