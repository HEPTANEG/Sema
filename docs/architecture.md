# Architecture

Sema is a TUI-first application with a shared local Backend/Core. The TypeScript/React/Ink frontend and the Python Telegram Gateway connect to the Backend/Core; Python owns model execution, tool safety, persistence, and the Memory Engine.

## Process Model

1. `sema` or `sema tui` starts the Ink frontend.
2. The TUI ensures the workspace Backend/Core is running and connects through a Windows Named Pipe.
3. `sema remote` ensures the same Backend/Core is running and starts the Telegram Long Polling Gateway. `sema remote service` is an outer supervisor used by the optional per-user Scheduled Task; it launches a separate `remote worker` process and monitors its polling heartbeat.
4. Backend/Core exchanges JSON-RPC 2.0 messages with multiple local clients and broadcasts structured runtime events.
5. Python emits structured turn, tool, confirmation, configuration, and background events.
6. Only one Backend/Core instance owns a given workspace endpoint; the Backend exits after its idle grace period when no clients remain.

The legacy `sema ui-bridge` stdio entry point remains available for compatibility
and evaluation seams. Human-facing rendering belongs in the clients; the
Backend/Core does not render terminal or Telegram output.

## Python Boundaries

- `main.py` dispatches TUI, shared Backend, Remote Gateway, bridge, and eval bridge entry points.
- `bridge/` owns JSON-RPC, Named Pipe transport, Backend/Core lifecycle, launch behavior, headless runtime seams, and UI events.
- `remote/` owns Telegram authentication, Long Polling, callback buttons, Backend client connections, durable authenticated-update admission and backlog batching, retryable Telegram delivery, in-process and external polling liveness, singleton Gateway ownership, Scheduled Task lifecycle helpers, and remote presentation of Backend events. It does not own chat history or Agent execution.
- `runtime/` owns model payloads, the structured loop, directives, recovery, and background maintenance.
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

Background maintenance runs after final answers. It commits the WM ingress
watermark and advances Working Memory, Focus Segments, and LTM Formation
without blocking the next user input. LTM Maintainer runs in its separate
offline mode when explicitly requested or when the maintenance-pressure gate
requires it.

## Frontend Boundaries

The TUI renders structured Python state and returns user choices. It does not duplicate command semantics, permission policy, persistence, provider behavior, or Memory Engine behavior.

`runtime.ts` is the public TUI store facade. Its atom definitions and formatting helpers live in `runtime_state.ts` and `runtime_formatting.ts`; the facade remains the stable import path for existing components and tests.
The TUI also provides a grouped `/settings` entry with Model, Permissions &
Safety, Interface, and Developer sections. It aggregates existing persistent
configuration plus process-local settings without replacing the underlying
command, Bridge, or policy boundaries. Memory Engine diagnostics remain outside
Settings and diagnostics remain separate commands.
