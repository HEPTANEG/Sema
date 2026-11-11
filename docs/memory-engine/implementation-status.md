# Memory Engine Implementation Status

## Current Runtime

`my_agent/memory_engine/` is the canonical memory implementation. The runtime uses one project-scoped Event Stream with STM Projection, Working Memory, Focus Segments, Context Builder, dynamic budgets, WM ingress, evidence retrieval, LTM Core Store, meta-memory, formation, structured retrieval, root-tag linking, Memory Retrieval/formation, LTM Push, and manual LTM Maintenance.

## Runtime Boundaries

- Provider context is built by Context Builder.
- `show_memory_state` reads Memory Engine observer metadata only.
- `retrieve_memory` is the current Memory Retrieval entry point. It is an
  explicit, read-only, query-driven, resumable Retrieval Agent with one
  process-local Session. The retired `recall_memory` high-level flow is no
  longer an active tool. The stable contract is documented in
  `ltm/ltm-memory-retrieval.md`; Session, Worker, Wake, and recovery behavior
  are documented in `ltm/ltm-memory-retrieval-runtime.md`.
- `my_agent/memory_engine/ltm/candidate_generation.py` is the neutral lexical/embedding
  Candidate Pool core shared by LTM Push and Memory Retrieval. Push keeps
  its existing three-channel orchestration; Retrieval owns Session-local
  search hypotheses and iterative observation.
- `memorize` is explicit and evidence-bound. Wake-enabled Runtime entries run it as a background Worker and notify Main Agent on completion; Headless/Eval entries without Wake support use the synchronous fallback. All writes still pass through LTM Core Store validation.
- Local BGE-M3 Embedding Service V1 and the usearch-backed local HNSW Vector Index V1 are available through isolated adapters. LTM Push Candidate Pool V1 collects candidate contributions from three end-to-end retrieval channels; each channel contains its own context projection, Query Compiler, and Semantic Search or lexical/FTS retrieval policy. Candidate integration is deterministic. The ranking pipeline uses Rank 1 for deterministic compression to at most 400 items, Rank 2 with local `Qwen/Qwen3-Reranker-0.6B` compression to at most 80, and Rank 3 with the Main Agent provider for final selection of at most 40. Rank 2 uses STM-only input capped at 1,024 content tokens and adaptive GPU micro-batching (maximum batch size 8, with CUDA OOM backoff). The reranker process remains isolated from the default Python environment.
- During an online LTM Push, the existing `assistant_stage` status surface reports `Generating memory retrieval queries`, `Searching long-term memory`, `Rank 2: Ranking long-term memory`, and `Rank 3: Selecting long-term memory`. These are observability-only stages and do not add transcript messages or alter the LTM Push pipeline.
- Online STM/WM/Focus maintenance runs after final answers and does not block
  response generation. LTM Maintenance uses a separate offline mode started
  explicitly or through the Main Agent's pressure-aware `sleep_for_maintenance`
  tool; successful Main-Agent-initiated runs resume their process-local task by
  Wake, while failed runs return retry or handoff controls.
- `read_file` is STM-safe for large file ingress. It supports line ranges, character ranges, returned character caps, explicit full-read requests for warning-sized files, observable `ok | warn_large | too_large` statuses, bounded first-100-line previews for large files, and `get_overview` for navigation and triage before exact raw range reads.
- STM projects a continuous newest suffix of ordinary message slots and atomic
  assistant-tool-result bundles. Interaction blocks remain available for
  Focus and Evidence semantics but no longer determine STM capacity.
- Capacity Planner combines Dynamic Context Budget with structural STM and WM
  requirements. It persists one shared continuous-session budget-failure
  update and both 80/10 Stabilizer results per real provider Context Build.
  Read-only surfaces preview the same plan without advancing state.
- Current-turn WM pins preserve an external user instruction from its
  immutable original event after it leaves STM, share the WM budget, and clear
  on the next external user message. Runtime-originated user-role messages do
  not affect the pin.
- Every Main Agent tool result passes a common pre-append ingress guard. An
  oversized atomic bundle stores the complete result as an Event Stream
  artifact and appends only a bounded `too_large` envelope. Oversized WM RAW or
  REF records receive one exact-slot compaction attempt followed by a bounded
  active REF when necessary.
- Exception-driven context retry, transient `context_build_failure` state, and
  interaction-block/top-three Preflight WM Recovery are removed.
- Online Formation processes the current Formation Buffer only. Formation Backfill runs one background batch at a time for older Event Stream ranges outside that buffer and resumes from persisted Formation state.
- Focus-aware WM Projection separates prompt injection from logical residency. Active segments contribute detailed records, inactive segments contribute titles and summaries, and resolved segments contribute titles only; the dynamic WM budget limits the final rendered text. Online and Formation Backfill keep using the raw WM suffix selector. The background sequence is Online Formation, Formation Backfill, one-time Residency V2 Reconciliation, read-only pre-audit, resolved-only Focus Range Retirement, and persisted final audit. Reconciliation restores valid retired active/inactive assignments and retired unassigned units created under the prior semantics. Retirement affects only complete resolved units that have left STM and are fully captured by Formation WorkItems. Segment metadata, assignment audit rows, Event Stream records, and Formation references remain intact. Status surfaces report injected and resident metrics separately. Repairable consistency findings can schedule the existing Focus Maintainer on a later background cycle with fingerprint cooldown; residency and retirement findings do not invoke model repair.
- LTM Push is part of the foreground provider-context build and has a 60-second end-to-end deadline, including cold starts of its local services. It fails open when the deadline expires: metadata-only diagnostics remain available, while the Main Agent continues with STM and Working Memory without a Memory Push placeholder.
- The Provider uses a process-local Keep-Alive pool. Metadata-only latency
  diagnostics remain available for measuring foreground context construction,
  Query Compiler, and Rank 3 behavior without recording message bodies.
- LTM Store no longer stores MemoryObject lifecycle `status`. Numeric Store-owned derived strength controls retrieval eligibility and maintenance priority, while MemoryLink lifecycle is represented by `link_state = active | inactive`.
- Manual LTM Maintenance is available through `/memory ltm maintain`. It runs an isolated five-stage Maintainer ReAct loop, exposes only Maintainer observation/write tools, writes through the Store's intent compiler and atomic commit path, displays realtime progress and the active stage in a dedicated TUI view, composes completed-run `maintenance_summary` sections for future Maintainer orientation, and emits a completion Wake without writing internal maintenance messages to Event Stream.
- Maintenance pressure is computed from the unmaintained portion of the dynamic Formation Buffer frontier with a 200-slot threshold; STM and newer WM slots do not increase the backlog. Maintainer fixes its frontier and coverage target at invocation start, may inspect newer projections as reference, but cannot create `sourced_from` provenance or advance coverage beyond that frontier. One request-scoped decision controls the Main Agent notice, offered tools, boundary checks, and sleep authorization. Soft pressure notices are injected at 0.5, 0.75, and 0.9, corresponding to 100, 150, and 180 unmaintained Slots. At pressure 1.0, the user message enters context and Runtime latches the mandatory sleep boundary until Bridge establishes a maintenance continuation; the existing Wake mechanism resumes the suspended task after maintenance.

## Persistence

Event Stream data is project-scoped under `.sema/memory/event_stream/`. LTM storage is managed separately by the Memory Engine. Existing project data is preserved when projects are opened; runtime behavior does not select memory modes from project metadata.

## Verification

The Python suite covers Event Stream persistence, context construction, WM, Focus Segments, observer isolation, LTM storage/retrieval/formation, embedding service protocol tests, LTM Push Candidate Pool and Rank 1/2/3 behavior, foreground deadline fail-open behavior, and model tool calling. TUI tests cover bridge protocol, rendering behavior, and foreground status updates. A separate opt-in embedding smoke test loads the locally installed BGE-M3 model. Foreground LTM Push has a fixed 60-second end-to-end deadline; performance is measured from real usage and optimized incrementally.
