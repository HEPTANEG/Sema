# Testing

## Provider HTTP Connection Reuse

Run the transport and Provider regression tests with:

```powershell
python -m pytest -q tests/test_model_provider.py
```

The Provider uses a process-local standard-library Keep-Alive pool for
OpenAI-compatible endpoints. The tests cover sequential reuse, concurrent
connection isolation, idle expiry, provider-requested close, and explicit
shutdown cleanup. They do not require a live model Provider.

For a live latency comparison, compare the first request in a process with
later requests using the existing Provider exchange metadata. Focus on
`headers_ms` for Query Compilation and Rank 3; this measures the full
request-to-response-header interval and is not a pure network measurement.
The current latency diagnostics are intentionally retained for continued
observation and should not be removed as part of the current Provider change.

## Main Agent Context Timing Diagnostics

`tests/test_main_agent_context_timing.py` verifies the temporary metadata-only
Trajectory spans for synchronous Main Agent context construction and Provider
preparation. It covers Focus Detector, context pressure, STM/WM projection,
WM ingress, LTM result processing, final assembly, token counting, span order,
background exclusion, and sensitive-content isolation. Run it with:

```powershell
python -m pytest -q tests/test_main_agent_context_timing.py
```

The spans are diagnostic-only and are intentionally retained during the
current observation period. They should be removed after that period and any
follow-up latency work. They do not extend the LTM Push JSONL schema.

Rank 3 session-context coverage is included in the Memory Push regression
tests. It verifies WM exclusion, system-message exclusion, process-restart
boundaries, the 600-second user-message session gap, and preservation of all
Slots within the current session.

## Natural Forgetting and LTM Storage

Run `python -m pytest -q tests/test_ltm_forgetting.py tests/test_ltm_storage_management.py`.
These tests use isolated temporary Stores and fake vector staging. They cover
half-life math, rollout anchors, effective-use idempotency, cached recomputation,
root protection, non-cascading run-scoped batches, retained tombstones, filtered
reads, index failure/retry, project-scoped warnings, stable cleanup previews,
busy exclusion and compaction failure/retry. Never use a real memory library for
reclamation tests. Also run Store, retrieval, Push, Maintainer, Formation and
bridge regressions plus the full Python and TUI suites and TUI build.

## Formation Write-Plan Repair

`tests/test_ltm_formation.py` checks title-limit and Episode-schema prompt visibility, measured
Payload/title feedback, two independent correction requests after normal rounds
are exhausted, retrieval tool omission and rejection during repair, atomic
failure, mixed-error rejection, and valid `no_memory`. The existing summary,
title, Root Tag, and retry regressions continue to apply. These tests use mocked
providers and isolated stores. Validation-error counts must be interpreted with
repair counts and terminal status, not treated alone as final failures.

## Formation Retrieval Reminders

Run `python -m pytest -q tests/test_formation_progress.py tests/test_ltm_formation.py tests/test_ltm_retrieval.py tests/test_ltm_formation_backfill.py`.
Tests use local temporary stores and mocked model/retrieval responses. Coverage
includes material growth versus repeated projections, alternating/empty/error
results, third-round reminders and reset, per-attempt isolation, budget rounding
and crossed tiers, complete tool-message pairing, plan repair after a reminder,
unchanged hard-limit failures, WorkItem diagnostic events, and Event Stream
isolation. No live model or embedding service is required.

## Maintainer Retrieval Caller Integration

`tests/test_maintainer_retrieval_integration.py` covers concentrated results,
profile-specific format correction, isolated state, no usage accounting, close
waking a waiter, pre-commit cleanup, nonfatal technical results, and explicit-root
source reads. Use fake providers and temporary stores. Run existing Retriever,
asynchronous Wake, Maintainer, and trajectory suites too. Raven gateway tests
verify retrieval technical-error display without terminating maintenance. Scripted
examples establish wiring, not general semantic duplicate-detection quality.

## Maintainer Explicit Completion Contract

The Maintainer runner/observation and bridge schema suites verify explicit
`finish_maintenance`, coverage-before-health, unsupported response termination,
three consecutive protocol violations, nonexecuted mixed/truncated writes,
sparse parameter schemas, and rejection before database access. Fixtures now
return explicit finish tool calls and real finish reasons instead of treating
text-only replies as success. Raven gateway tests cover Runtime notices and busy
release. Use mocked providers and temporary stores; no production memory is used.

## Maintainer Staged Execution and Completion Wake

`tests/test_ltm_maintenance_staged.py` verifies the ordered semantic stages,
relevant-read evidence gates, exclusive stage reports, the 500-token prompt
target with a 1,000-token Runtime tolerance, legacy-summary exclusion,
mechanical summary composition, and completion Wake payloads. Stage summaries
are tested with mock providers and temporary stores;
the tests do not start model, embedding, or reranker services. The Wake is
asserted as `source=maintainer`, `kind=maintenance_completed`, and
`processing_class=tool_result_like`.

## Maintainer Plan Repair and Partial Commit

Maintainer plan tests verify that title, summary, and Episode structured-data
errors report the field path and measured limits; the first rejection creates
an invocation-local draft; and `repair_maintenance_plan` can change only
Runtime-marked fields. A corrected draft is recompiled and committed as one
complete plan without truncating or guessing fields. A second supported failure may
commit independent valid units while skipping the invalid object and its
dependent links, preserving a repair draft for the skipped units. Tests also
verify intent atomicity and that malformed JSON, top-level schema errors,
duplicate temp IDs, idempotency conflicts, oversized payloads, and unsupported
errors do not enter
partial-commit mode. Use mocked providers and temporary stores; do not run
these cases against a production memory library.

## Maintainer Plan Commit Diagnostics and Restart Recovery

The Maintainer Store tests verify Runtime-only commit phase events, bounded
embedding timeout results and cleanup, schema migration of the maintenance
owner/heartbeat fields, and replacement of a `running` run whose owner process
has exited. Tests also verify that committed-plan checkpoints are retained and
that the interrupted trajectory is marked failed before the replacement run is
started. These tests use temporary Stores, mocked vector staging and mocked
Embedding Service behavior; they do not start a live model or embedding
process.

`tests/test_vector_index.py` checks isolated staging, abnormal worker exit,
timeout, preservation of the installed index and normal staged installation.

## Main-Agent Maintenance Sleep and Resume

Run `python -m pytest -q tests/test_agent_loop.py tests/test_maintenance_sleep_gate.py tests/test_ui_bridge.py tests/test_context_builder.py tests/test_retrieval_async_wake.py tests/test_ltm_maintenance_runner.py`.
These tests cover pressure-based tool availability, input admission at the hard
threshold, turn-lock release before maintenance starts, continuation failure
attempt metadata, same-continuation chained maintenance cycles, and existing
Wake behavior. Add or update mocked-provider cases when changing the suspended
task or retry/handoff contract. No live model or production Store is used.

## Maintainer Formation-Buffer Boundary

The Maintainer boundary tests use the same dynamic Formation Buffer calculation
as Formation. They verify that STM/newer WM provenance and coverage are rejected,
older WM overlap is accepted, existing-object-only plans remain allowed, and
pressure counts only the maintainable backlog. Formation tests continue to
verify that its own WM-overlap writes are unchanged. Use temporary Event Stream
and Store roots with mocked providers and fake embeddings.

## Maintainer Keyword Search and Context Repetition Detection

Run `python -m pytest -q tests/test_context_repetition.py tests/test_ltm_maintenance_observation.py tests/test_ltm_maintenance_runner.py tests/test_bridge_event_schema.py` with mock providers and temporary stores. Coverage includes literal OR queries, distinct-term ranking before limits, short terms, stable ties, exact and near-duplicate model outputs, the ten-output window, warnings at rounds three, five, and eight, stage boundaries, Tool-result exclusion, diagnostic persistence, and the absence of automatic failure. Run Raven gateway event tests for isolated warning rendering and terminal busy release, followed by the TUI suite and build. No live model or embedding services are required.

The default TUI configuration excludes Raven tests. From `tui`, run them with
`node --input-type=module -e "import {startVitest} from 'vitest/node'; const ctx=await startVitest('test',['src/raven/__tests__/createGatewayEventHandler.test.ts','src/raven/__tests__/turnController'],{config:false,watch:false}); await ctx.close();"`.

## Readable Provider Contexts

Run `python -m pytest -q tests/test_trajectory_contexts.py tests/test_trajectory.py tests/test_model_provider.py tests/test_slash_dispatch.py` for exact request-body snapshots, independent 50-request Agent windows, legacy migration, storage failure isolation, read-only request queries, and context commands. Providers and streams are mocked; no live services are used.

## Memory Push Focus Routing

The Focus-aware path is covered by
`tests/test_memory_push_focus_routing.py`. Run the focused regression with:

```powershell
python -m pytest tests/test_memory_push_focus_routing.py tests/test_context_builder.py tests/test_memory_push_rank_1.py tests/test_memory_push_ranking.py -q
```

The tests cover full, incremental, and Focus Shift routing, baseline reuse when
incremental retrieval returns no items, and the five-entry developer cache.

## Memorize Runtime Wake

Run the focused Memorize and Wake suites with:

```powershell
python -m pytest tests/test_ltm_memorize.py tests/test_retrieval_async_wake.py tests/test_agent_loop.py tests/test_trajectory_process_integration.py -q
```

The tests cover immediate `running` results, call-time message and context
snapshots, terminal completion Wake payloads, stable task and Wake IDs,
`tool_result_like` delivery, metadata-only observer output, and synchronous
fallback behavior when no Wake callback is available.

Run the full project checks before shipping backend or TUI changes:

```powershell
python -m pytest -q
npm test --prefix tui
npm run build --prefix tui
```

## Python Coverage

- Runtime, bridge, command, tool, persistence, skill, and provider suites verify user-visible agent behavior.
- Deterministic runtime clock and rhythm tests verify process timing, continuous-session boundaries, assistant finish markers, `/time`, and current-clock provider injection.
- Memory Engine suites cover Event Stream persistence, STM, Working Memory, Focus Segments, Context Builder, dynamic budgets, observer metadata, LTM storage, retrieval, automatic Formation, Memorize, Memory Push Candidate Pool, Rank 1/2/3, per-turn Context Builder injection, and foreground Memory Push deadline fail-open behavior.
- Embedding Service tests use a fake model for normal regression. The real BGE-M3 process smoke test is opt-in with `SEMA_EMBEDDING_SMOKE=1` and requires the user-level model cache. Vector Index tests use the installed `usearch` adapter and temporary project roots; they cover persistence, stable ordering, removal, rebuild, dirty-index handling, and Core Store synchronization. Memory Push Candidate Pool tests cover the three channel input boundaries, parallel query compilation, strict query validation, the independent `memory_push` Retrieval Profile, semantic and lexical adapters, Root Tag exclusion, raw score preservation, and deterministic object-id integration.
- Tests import canonical package paths such as `runtime.agent`, `persistence.session`, `tools.tool_schemas`, and `bridge.ui_bridge_server`.
- Unified Trajectory tests use temporary project roots and mocked provider exchanges. They cover SQLite schema and ordering, nested and resumable process roots, complete gzip artifact round trips and reuse, retention, legacy trace/context migration, Provider finish reasons and interrupted streams, process instrumentation, and `/trajectory` commands. They must not call live model, embedding, or reranker services.
- Read-only Trajectory inspection must not create a database, initialize a schema, migrate legacy sources, trim request files, or run artifact GC. Write entry points remain responsible for initialization and automatic migration.
- Remote-control tests cover workspace pipe naming, Backend/Core event broadcast, Telegram identity checks, coarse progress formatting, credential validation, request receipt idempotency, durable inbound state, polling liveness, final-delivery serialization, singleton mutex ownership, Scheduled Task command generation, and restart teardown ordering.

## Regression Rules

- Provider context must be built by Context Builder and must not mutate observer state.
- Memory Engine observer output is metadata-only and must not expose message bodies or payload bodies.
- LTM retrieval is read-only and bounded; LTM formation requires validated evidence and durable writes remain separate from Event Stream storage.
- Background maintenance must not block the online answer path.
- Formation Backfill must exclude the current Formation Buffer, process at most one batch per background invocation, persist its fixed end boundary and cursor, and resume failed or interrupted WorkItems without overlapping online Formation.
- Tool schemas, security classification, and executor handlers must remain synchronized.
- The TUI must render structured bridge responses rather than reconstructing backend semantics.
- TUI and Telegram must attach to the same Backend/Core; Gateway-generated busy responses must not enter chat history.
- Telegram Confirmation callbacks must be processed while the originating Agent turn is waiting for approval.
- A transient Telegram status or progress delivery failure must not prevent Backend submission. Gateway restart must not start a second Agent turn for the same Telegram update.
- Backend submission must begin before a potentially slow initial Telegram progress reply completes.
- A managed task must directly own its Python Gateway process. Restart must not leave an orphaned `remote service` process or race `IgnoreNew` into losing the replacement process.
- The outer `remote service` supervisor must recycle a worker whose persisted polling heartbeat is stale even when the worker process itself remains alive.
- Reconnected Telegram text updates must be combined in update order into one Backend request and one final answer; a message received during an active turn must not be appended to it.
- The Embedding Service must remain isolated from LTM/Event Stream storage; V1 tests must not require Vector Index or Memory Push integration.
- Vector Index tests must verify that Root Tags keep their configured exclusion behavior, `strength <= 0` objects are filtered by Store reads, inactive Links do not affect derived strength, and Store/Vector synchronization failures do not leave visible partial LTM writes. Observer tests may expose index metadata only, never vectors or Payload text.
- Candidate Pool tests must verify that retrievable-object results are metadata-only, duplicate object IDs are integrated before ranking, and failed queries do not prevent successful channels from producing candidates. Rank 1 tests must verify Query-level Evidence identity, source-local best-rank fusion, deterministic tie breakers, malformed-evidence degradation, and the 400-item cap. Rank 1 must not read Payload, use MemoryLink, or mutate LTM, Vector Index, or Event Stream.
- LTM Push retrieval tests also cover batched semantic object hydration and
  ordered multi-object Core Store reads, including Payload reconstruction and
  preservation of per-query result order.
- LTM Push provider diagnostics cover buffered streaming for Query Compiler and
  Rank 3, including header arrival, first content delta, stream completion, and
  JSON validation boundaries. These timing fields are temporary and should be
  removed only after the observation period and follow-up latency work.
- Rank 2 and Rank 3 tests must verify the local Qwen3 service boundary, STM-only Rank 2 input capped at 1,024 content tokens, adaptive GPU micro-batching with CUDA OOM backoff, 400-to-80 compression, STM-only Query Compiler projections with Latest Semantic limited to the latest Slot quarter, post-response query truncation at 20 per channel, full Payload Rank 3 input, strict retained object-ID validation, post-response 40-item selection truncation, provenance ordering, title/Payload-only injection, per-turn reuse, foreground stage status events, and fail-open LTM placeholders. The ranking pipeline and Memory Push must not mutate Event Stream, STM, WM, LTM, or Vector Index.
- Temporary Object Reference tests must verify sequential per-scope refs, stable registration, rejection of unknown/non-integer/boolean/duplicate refs, Rank 3 `ref`/`selected_refs` conversion, and session-stable Memory Retrieval refs. Model-facing projections and observation results must not leak real MemoryObject IDs; final Runtime results and successful-use accounting must continue to use real IDs. Refs must not be persisted and must be released with the Retrieval Session.
- Focus routing tests must verify that the current-turn Detector action is recorded in Main Agent Trajectory, selects the matching Memory Push mode, and that delayed TUI Focus events from another `turn_id` are ignored.
