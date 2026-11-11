# Architecture

Sema is a TUI-first application with a shared local Backend/Core. The TypeScript/React/Ink frontend and the Python Telegram Gateway connect to the Backend/Core; Python owns model execution, tool safety, persistence, and the Memory Engine.

## Process Model

1. `sema` or `sema tui` starts the Ink frontend.
2. The TUI ensures the workspace Backend/Core is running and connects through a Windows Named Pipe.
3. `sema remote` ensures the same Backend/Core is running and starts the Telegram Long Polling Gateway. `sema remote service` is an outer supervisor used by the optional per-user Scheduled Task; it launches a separate `remote worker` process and monitors its polling heartbeat.
4. Backend/Core exchanges JSON-RPC 2.0 messages with multiple local clients and broadcasts structured runtime events.
5. Python emits structured turn, tool, confirmation, configuration, and background events.
6. Only one Backend/Core instance owns a given workspace endpoint; the Backend exits after its idle grace period when no clients remain.

LTM staged vector-index mutations run in a bounded Python worker process so a
native index failure is reported to the Backend instead of terminating it.

The legacy `sema ui-bridge` stdio entry point remains available for compatibility
and evaluation seams. Human-facing rendering belongs in the clients; the
Backend/Core does not render terminal or Telegram output.

## Python Boundaries

- `main.py` dispatches TUI, shared Backend, Remote Gateway, bridge, and eval bridge entry points.
- `bridge/` owns JSON-RPC, Named Pipe transport, Backend/Core lifecycle, launch behavior, headless runtime seams, and UI events.
- `remote/` owns Telegram authentication, Long Polling, callback buttons, Backend client connections, durable authenticated-update admission and backlog batching, retryable Telegram delivery, in-process and external polling liveness, singleton Gateway ownership, Scheduled Task lifecycle helpers, and remote presentation of Backend events. It does not own chat history or Agent execution.
- `runtime/` owns model payloads, the structured loop, directives, model/provider recovery, and background maintenance.
- `runtime/context_repetition.py` owns schema-independent bounded repetition detection for iterative model/tool contexts; it does not call models or persist memory.
- `commands/` owns slash metadata, dispatch, and command execution.
- `tools/` owns provider schemas, request adaptation, security-aware execution, and handlers.
- `persistence/` owns project/session metadata, traces, and undo records.
- `memory_engine/` owns Event Stream storage and all memory processing.

The bridge event inventory is centralized in `my_agent/bridge/event_schema.py`, with matching TypeScript event types in `tui/src/protocol/types.ts` and a human-readable contract in [bridge-event-schema.md](bridge-event-schema.md). The V1 registry documents event ownership without rejecting or transforming payloads at runtime; the TUI safely ignores unknown event types.

## Memory Engine

Provider context is built through Context Builder from Event Stream, STM
Projection, Working Memory, Focus Segment state, and the automatic LTM Push
projection. `get_object` and LTM Retriever provide explicit read-only recall;
Formation and Memorize perform separate evidence-bound writes through the
Memory Engine.

STM uses ordinary slots and atomic assistant-tool-result bundles as its
continuous-suffix selection units. Capacity Planner combines Dynamic Context
Budget with structural STM and WM requirements, writes one shared failure and
Stabilizer update per real provider build, and exposes a side-effect-free
preview to Observer and status APIs. Current-turn WM pins preserve an omitted
external user instruction by immutable original-event reference. Tool Ingress
Guard stores oversized Main Agent results as artifacts before appending a
bounded active envelope.

Background maintenance runs after final answers. It commits the WM ingress
watermark and advances Working Memory, Focus Segments, and LTM Formation
without blocking the next user input. LTM Maintainer runs in its separate
offline mode when explicitly requested or when Main Agent calls its
Runtime-controlled `sleep_for_maintenance` entry point. Maintenance pressure
opens that tool at `0.5` and forces its use at the next model decision boundary
at `1.0`. One request-scoped pressure decision controls the notice, tool schema,
boundary validation, and tool authorization. Reaching `1.0` also creates a
process-local mandatory latch that remains active even if the dynamic frontier
later retreats. A process-local continuation carries the Main-Agent task
through the Maintainer run and the existing Wake scheduler. After a successful
Wake, a resumed long task may request another maintenance run using the same
continuation ID; an already-running continuation remains non-reentrant. Failed
runs return to Main Agent with retry or user-handoff controls.

## Frontend Boundaries

The TUI renders structured Python state and returns user choices. It does not duplicate command semantics, permission policy, persistence, provider behavior, or Memory Engine behavior.

`runtime.ts` is the public TUI store facade. Its atom definitions and formatting helpers live in `runtime_state.ts` and `runtime_formatting.ts`; the facade remains the stable import path for existing components and tests.
The TUI also provides a grouped `/settings` entry with Model, Permissions &
Safety, Interface, and Developer sections. It aggregates existing persistent
configuration plus process-local settings without replacing the underlying
command, Bridge, or policy boundaries. Memory Engine diagnostics remain outside
Settings and diagnostics remain separate commands.
