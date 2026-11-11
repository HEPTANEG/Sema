# Tool Calling

Sema uses provider-native structured tool calls. `my_agent/tools/tool_registry.py` is the metadata source of truth, and `my_agent/tools/tool_schemas.py` converts provider calls into internal requests.

## Execution Flow

```text
provider tool call
-> tool_schemas.tool_call_to_request()
-> runtime.agent.run_requested_tool()
-> tool_runtime.run_tool_request()
-> tool_executor.execute_registered_tool()
```

Tool schemas describe callable interfaces only. Risk classification, confirmation, external-path handling, and execution remain in Python.

## Runtime Routing

Ordinary user turns enter the Main Agent structured tool-calling loop. Python
owns tool dispatch, permission classification, confirmation, and execution.

## Registry Rules

- Every provider-visible tool has a schema, risk metadata, and executor handler.
- Unknown tool calls fail through the standard structured-tool error path.
- When LTM is available, `get_object` and `retrieve_memory` provide explicit
  Long-Term Memory recall, and `memorize` provides the durable memory-write
  interface.
- Runtime maintenance-control tools are converted by the same adapter as
  ordinary structured tools. `sleep_for_maintenance` requires `wake_prompt`,
  `retry_ltm_maintenance` takes an empty argument object, and
  `return_maintenance_failure` requires a user-facing `message`.
- Maintainer `abstract` supports two contracts: `tagged_with` creates a Tag
  from any live MemoryObject sources; `supports` creates an assertion from
  assertion or episode sources. Episodes are created with `create_object` and
  linked to Event Stream slots using `sourced_from`.

## Security and Confirmation

`my_agent/security/security_policy.py` classifies requests before execution. File edits, commands, undo, and external paths follow their configured confirmation policy. The TUI presents decisions made by Python and returns the user's choice. `/unattended` is the explicit user-controlled bypass for confirmation prompts.

## Model Providers

DeepSeek, Kimi, and custom providers use the OpenAI-compatible `/v1/chat/completions` path. Ollama Native uses `/api/chat` instead, while keeping the same Sema model-call and tool-call result contract. The model configuration editor accepts the provider name, protocol, base URL, model, API key environment, and thinking setting directly. Ollama Native V1 supports non-streaming text and tool calls, but not streaming or vision. Built-in profiles remain available only through compatibility commands; they are not part of the primary configuration UI.

## Slash Commands and Bridge Events

Python is the single semantic source for slash commands. `my_agent/commands/command_registry.py` owns names and help metadata, `my_agent/commands/dispatch.py` owns command-family routing, and `my_agent/commands/structured_commands.py` owns execution and structured results. The TUI obtains metadata through `slash.list`, performs autocomplete/input normalization, and sends commands through `slash.execute`; it does not independently implement `/trajectory`, `/last-run`, `/permissions`, or other command behavior.

The bridge event envelope remains `{event_id, turn_id, type, payload}`. `my_agent/bridge/event_schema.py` is the Python event inventory, `tui/src/protocol/types.ts` contains matching TypeScript types, and `docs/bridge-event-schema.md` is the human-readable contract. V1 centralizes names and metadata without runtime payload validation; unknown event types remain safe to ignore in the TUI.

## Unified Settings Entry

`/settings` opens a four-group TUI settings panel without calling the model:
Model, Permissions, Interface, and Developer. It aggregates provider
configuration and process-local permission and visibility state. Memory Engine
diagnostics are not part of Settings.

Each Settings category presents read-only information in a `Status` section
and editable settings or independent operations in an `Actions` section. The
TUI keeps unsubmitted changes locally and uses a fixed `Cancel` / `Confirm`
footer. Permission grants remain read-only; they are produced by confirmation
flows. Execution mode is changed through an Action, while resetting temporary
grants is a separate confirmed operation.

`allow_run_commands` and `allow_edits` are displayed but are not directly
toggleable because they are temporary grants produced by confirmation flows.
Unattended mode has an explicit warning because it bypasses confirmations.
Session scope lasts until the current Sema session ends. Project scope is
stored in `.sema/project.json` and restored on later launches in that project;
choosing Interactive disables both scopes. Memory status and maintenance
actions remain the existing Memory Engine commands, not a second settings
implementation.

The existing `/config`, `/notify`, `/permissions`, and `/unattended` commands
remain hidden Developer entry points. `/run`, `/verbose`, and `/trajectory`
slash wrappers are removed; structured tools and Trajectory APIs remain.
Diagnostic commands such as
`/time`, `/last-run`, `/undo`, and `/status` remain outside
Settings.
