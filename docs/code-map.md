# Sema Code Map

Sema is a local coding agent with a TypeScript/React/Ink TUI and a Python backend. Python owns runtime behavior, persistence, tool execution, and the Memory Engine.

## LTM Forgetting and Storage Management

- `my_agent/memory_engine/ltm/ltm_store.py`: formula v2, migration anchors,
  live graph views and run-idempotent logical deletion.
- `my_agent/memory_engine/ltm/storage_gate.py`: activity/write exclusion,
  including process-scoped locks released on exit.
- `my_agent/memory_engine/ltm/storage_management.py`: per-library capacity
  warnings, fixed purge previews, physical cleanup and space reclamation.
- `tui/src/components/LtmStoragePanel.tsx`: Settings storage view and confirmation.

## Entry Points

- `my_agent/main.py`: CLI dispatch for the TUI, shared Backend, Remote Gateway, UI bridge, and eval bridge.
- `my_agent/bridge/backend_server.py`: Shared multi-client Backend/Core owner and Named Pipe server.
- `my_agent/bridge/backend_transport.py`: Per-workspace Windows Named Pipe transport.
- `my_agent/bridge/ui_bridge.py`: Legacy stdio JSON-RPC bridge process kept for compatibility.
- `my_agent/remote/client.py`: Python JSON-RPC client for Backend/Core requests and event notifications.
- `my_agent/remote/gateway.py`: Telegram Long Polling Gateway worker, allowlist checks, backlog batching, progress projection, inline Confirmation callbacks, final-delivery serialization, and worker supervision entry points.
- `my_agent/remote/service.py`: Windows Task Scheduler installation, status, restart, orphan cleanup, and task-command generation.
- `my_agent/remote/state.py`: non-secret durable inbound-update and Gateway-health state.
- `my_agent/remote/transport.py`: retryable Telegram HTTP transport and polling liveness tracking.
- `my_agent/remote/instance_lock.py`: per-workspace Windows mutex preventing duplicate Gateway processes.
- `tui/src/`: Ink application, protocol client, command completion, and runtime rendering.

## Runtime

- `my_agent/runtime/agent.py`: main model/tool loop integration, prompt construction, recovery, and background scheduling.
- `my_agent/runtime/model_runtime.py`: provider payload construction, streaming, retry, and model error handling.
- `my_agent/runtime/structured_loop.py`: structured tool-call loop and final-answer handling.
- `my_agent/commands/`: slash metadata, dispatch, and structured command execution.
- `my_agent/tools/`: provider tool schemas, permission-aware runtime execution, and tool handlers.
- `my_agent/security/`: confirmation and risk classification.
- `my_agent/agents/temporal_state.py`: deterministic process clock and runtime rhythm signals used by `/time` and Context Pressure.

## Persistence and Integrations

- `my_agent/persistence/session.py`: project metadata and Event Stream-backed session persistence.
- `my_agent/persistence/trajectory.py`: unified process trajectory metadata, complete gzip artifacts, retention, legacy observability migration, and debug queries.
- `my_agent/persistence/trajectory_contexts.py`: exact readable provider request JSON, per-Agent 50-request windows, migration, and context queries.
- `my_agent/persistence/observability.py`: safe lightweight argument/result summarizers used by runtime instrumentation.
- `my_agent/persistence/undo.py`: edit undo records.
- `my_agent/integrations/`: web search and image context support.
- `my_agent/model/provider.py`: OpenAI-compatible and Ollama Native provider configuration, requests, and response normalization.

The bridge event inventory is in `my_agent/bridge/event_schema.py`, with matching TypeScript types in `tui/src/protocol/types.ts` and human-readable documentation in `docs/bridge-event-schema.md`.

## Memory Engine

`my_agent/memory_engine/` is the single memory implementation. Cross-layer Event Stream, STM Projection, Working Memory, Focus Segments, Context Builder, dynamic budgets, evidence retrieval, and observer APIs remain in the parent package. LTM Core Store, formation, structured retrieval, Memory Push, query-driven Memory Retrieval, candidate generation, maintenance, and their model-backed services live under `my_agent/memory_engine/ltm/`.

`my_agent/memory_engine/ltm/retrieval_context_compiler.py` owns the Query Compiler stage for the three Memory Push channels and its strict output validation. `my_agent/memory_engine/ltm/candidate_generation.py` owns neutral lexical/embedding Candidate Pool generation; `my_agent/memory_engine/ltm/memory_push.py` is the Push orchestration wrapper. `my_agent/memory_engine/ltm/memory_retrieval.py` owns the process-local Retrieval Session, iterative Agent loop, candidate-pool tools, source Evidence validation, and continuation protocol. `my_agent/memory_engine/ltm/ltm_retrieval.py` owns profile-bound semantic and structured retrieval. `my_agent/memory_engine/ltm/memory_push_rank_1.py` owns shared Rank 1. `my_agent/memory_engine/ltm/memory_push_ranking.py` orchestrates Push Rank 1/2/3, while `my_agent/memory_engine/ltm/reranker_adapter.py` also exposes the caller-neutral Retrieval Rank 2 entry point. `my_agent/memory_engine/context_builder.py` injects the final temporary Memory Push projection into provider context; it does not persist the result.

Provider context is constructed by `my_agent/memory_engine/context_builder.py`.
Background maintenance in `my_agent/runtime/background_tasks.py` advances WM
ingress, Working Memory, Focus Segments, and LTM Formation after final answers.

`tui/src/stores/runtime.ts` is the compatibility facade for the public runtime store API. `runtime_state.ts` owns atoms and public state types, `runtime_event_reducer.ts` owns ordered bridge-event transitions, and `runtime_formatting.ts` owns pure tool/trajectory/permission/notification formatting helpers.

## Tests

`tests/` imports canonical package paths directly. Python tests cover runtime behavior and Memory Engine invariants; `tui/src/**/*.test.ts*` covers the Ink client. Run `python -m pytest -q`, `npm test --prefix tui`, and `npm run build --prefix tui` before shipping changes.

## Main Agent Runtime

Ordinary turns enter the Main Agent structured tool loop. Context Builder
assembles the provider context from the current Event Stream and Memory Engine
projections; tool requests pass through Python permission classification and
execution.

### Unified settings entry

`/settings` is implemented by the Python command layer as a structured settings aggregate consumed by `tui/src/components/SettingsPanel.tsx`. The panel uses draft values and the allow-listed `settings.update` Bridge method for notification, unattended, permission-reset, and Developer Mode changes. Each category separates a read-only `Status` section from editable or independent `Actions`, with a fixed Cancel/Confirm footer. Model edits continue through existing config persistence. It is a UI entry point, not a new persistence, memory, or policy layer.

The command registry distinguishes Public and Developer commands. Developer
Mode is process-local and controls only help, `slash.list`, and autocomplete.
The `/run`, `/verbose`, and `/trajectory` slash wrappers are removed while the
structured command tool and Trajectory APIs remain.
## First-Run Setup Entry Points

- `my_agent/runtime/setup_config.py`: user-level setup configuration, redacted
  snapshots, environment precedence, fingerprints, and explicit isolated tests.
- `my_agent/runtime/setup_capabilities.py`: Backend capability gates, active
  service paths, background-work tracking, and explicit Formation skip ranges.
- `my_agent/bridge/backend_server.py`: setup initialization and controlled
  project restart; `ui_bridge_server.py`: setup/settings RPC allowlists.
- `tui/src/components/SetupPanel.tsx`: Preparation, local model configuration,
  optional services, and restart confirmation. `ConfigPanel.tsx`: LLM secrets
  and connection tests. `protocol/client.ts`: reconnect without request replay.
- `docs/installation.md`: fixed Windows prerequisites and second-PC acceptance.
