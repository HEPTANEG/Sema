# Bridge Event Schema

This document defines the current Python-to-client runtime event contract. Sema
uses JSON-RPC request/response messages for commands and JSON-RPC
notifications with the `event.<type>` method prefix for runtime progress. The
TUI and Telegram Gateway receive the same Backend/Core broadcasts, then apply
different presentation policies.

## LTM Storage Settings RPC

These user-initiated interfaces do not create model context or Event Stream slots:

- `ltm.storage.status({})`: live/deleted counts, library path, database/vector
  bytes, optional `warning_mib`, and `over_warning`.
- `ltm.storage.warning({warning_mib})`: positive MiB value or null to disable;
  warning only, never a write limit.
- `ltm.storage.preview({})`: immutable `preview_token`, count, path and an
  irreversible-cleanup warning.
- `ltm.storage.purge({preview_token, confirmed: true})`: purge only that preview,
  or return `busy`. `compaction_failed` explicitly means rows were removed but
  file reclamation needs a retry with the same token.

`ltm_maintenance_completed` may include a lightweight `storage_status` snapshot.
Mechanical cleanup progress reuses `ltm_maintenance_runtime_notice`; it does not
introduce another semantic stage or a separate notification stream.

## Envelope

Agent display names do not rename lifecycle event keys. The existing
`retriever_*`, `memory_push_*`, `ltm_formation_*`, `memorize_*`, and
`ltm_maintenance_*` events represent LTM Retriever, LTM Push, LTM Formationer,
LTM Memorizer, and LTM Maintainer respectively. `formation_backfill_*` is a
mode-specific activity of LTM Formationer, not a separate Agent.

Every bridge event uses this shape:

```json
{
  "jsonrpc": "2.0",
  "method": "event.final_answer",
  "params": {
    "event_id": "opaque-event-id",
    "turn_id": "opaque-turn-id",
    "type": "final_answer",
    "payload": {}
  }
}
```

`event_id` identifies one notification. `turn_id` groups events belonging to
one user turn. `type` selects the event contract. `payload` contains event
specific fields. The V1 bridge remains permissive and does not reject unknown
payload fields or unknown event types.

## Event Registry

| Event | Category | Producer | Typical TUI responsibility |
| --- | --- | --- | --- |
| `turn_started`, `user_message` | turn | bridge | Start a visible turn and append the user message |
| `assistant_stage`, `assistant_delta`, `assistant_content`, `final_answer` | assistant | runtime/bridge | Render staged, streamed, and committed assistant content |
| `model_thinking`, `model_finished`, `model_error` | model | model runtime | Update waiting, completion, and retry status |
| `tool_started`, `tool_finished` | tool | structured loop | Show tool lifecycle and results |
| `confirmation_requested` | confirmation | bridge confirmation | Open the confirmation view |
| `context_pressure` | context | runtime | Update context telemetry |
| `working_memory_preflight_started`, `working_memory_preflight_finished` | background | runtime | Keep background maintenance out of the transcript |
| `focus_detector_started`, `focus_detector_finished`, `focus_shifted` | background | context builder | Show Focus Detector and focus transition status |
| `segment_ingest_started`, `segment_ingest_finished`, `full_segment_maintenance_started`, `full_segment_maintenance_finished` | background | background tasks | Show Focus Ingester and Focus Maintainer status |
| `retriever_started`, `retriever_finished`, `memory_push_started`, `memory_push_finished` | background | memory engine | Show Retriever and LTM Push status |
| `ltm_formation_started`, `ltm_formation_finished`, `formation_backfill_started`, `formation_backfill_finished`, `memorize_started`, `memorize_finished` | background | memory engine/runtime | Show LTM Formationer, Formation Backfill, and Memorize status |
| `ltm_maintenance_started`, `ltm_maintenance_completed`, `ltm_maintenance_failed` | maintenance | maintenance runner | Show the aggregate LTM Maintainer status |
| `wm_ingress_watermark_status`, `working_memory_status`, `focus_segment_status`, `ltm_formation_status` | background | background tasks | Update background status telemetry when supported |
| `cancel_requested`, `turn_cancelled`, `agent_stopped` | turn | bridge | Show cancellation or stopped-turn results |
| `agent_error`, `error` | error | bridge | Surface an actionable failure |

The Python registry is in `my_agent/bridge/event_schema.py`. TypeScript
protocol types are in `tui/src/protocol/types.ts`. These are deliberately
lightweight implementation indexes backed by this contract; V1 does not add a
code generator or runtime payload validator.

## Lifecycle Rules

- Events are notifications and do not replace JSON-RPC responses for commands.
- Events may be emitted from background work after the main assistant answer.
- The TUI must safely ignore events it does not understand.
- Tool arguments and results remain summaries intended for the UI; the model's
  full tool messages stay in the Python runtime/session path.
- Runtime status events are not transcript messages unless the TUI explicitly
  materializes them as user-visible content.
- Telegram does not receive every event. The Gateway projects selected stages,
  tool progress, Confirmation requests, final answers, and actionable errors.
- V1 does not provide event replay or resume cursors; clients use current-state
  queries and persisted history after reconnecting.

## Ownership Boundaries

Python owns command semantics, risk decisions, tool execution, and event
production. The TUI owns input, autocomplete, rendering, focus/notification
behavior, and confirmation choice presentation. `runtime.ts` is a state facade
and reducer entry point, not the definition of agent behavior.
