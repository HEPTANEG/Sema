# Sema TUI

The Sema TUI is Sema's primary local chat interface. This document describes
Sema's user-facing behavior, protocol boundary, and ownership rules. It does
not document the internal architecture of the vendored UI implementation.

Start the TUI with either command:

```powershell
sema
sema tui
```

Both commands start the same interface and preserve the current working
directory. Use `/new` inside the TUI to start a new conversation.

## Backend Boundary

### Developer Command Visibility

Developer Mode is process-local and controls command discovery, not permissions.
After confirming a Settings action, the TUI applies the complete `settings.update`
response, including `slash_commands`. This keeps autocomplete synchronized with
backend help when Developer Mode is enabled or disabled. A subsequent `/settings`
response must not replace this update: it does not include the command list.

The TUI is a client of the shared Sema Backend/Core. The normal startup flow
is:

1. Python dispatches to `my_agent/bridge/ui_bridge_launch.py`.
2. The TUI ensures that the workspace Backend/Core is available.
3. The TUI connects to the Backend/Core through the per-workspace Windows
   Named Pipe.
4. The Backend/Core initializes project, session, provider, permission, and
   slash-command metadata.
5. User input and UI choices are sent to the Backend/Core through JSON-RPC.
6. Runtime events are delivered back to the TUI through the bridge event
   channel.

Python remains the authority for runtime behavior. The TUI must not duplicate
agent semantics, provider behavior, tool execution, persistence, memory
operations, permission policy, or slash-command execution.

The legacy stdio bridge writes JSON-RPC protocol messages to stdout. The
normal TUI path uses the shared Backend/Core Named Pipe and does not depend on
backend stdout for rendering.

### Slash Command Visibility

Python owns slash-command metadata and filters discoverability by the current
process's Developer Mode. Public commands appear in `/help`, `slash.list`, and
autocomplete:

- `/help`, `/settings`, `/status`, `/memory`, `/image`, `/undo`, `/undo list`,
  and `/exit`.

Developer commands remain executable when entered explicitly, whether or not
Developer Mode is enabled. The mode controls visibility only. When enabled,
these command families also appear in help and autocomplete:

- Workspace inspection: `/pwd`, `/ls`, `/read`, `/time`, `/last-run`.
- Model and permissions: `/config` and its subcommands, `/permissions` and
  `/permissions reset`, `/notify` and its subcommands, and `/unattended` and
  its subcommands.
- Memory diagnostics and maintenance: `/memory status` and the event-stream,
  context, budget, STM, WM, Focus, and LTM diagnostic or maintenance
  subcommands.

The direct `/run`, `/verbose`, and `/trajectory` slash command families are
not registered. Their separate underlying command execution, output behavior,
and Trajectory recording or Bridge APIs are not removed by removing those
slash entry points. `/mcp` and `/skills` are not Settings categories or
registered slash commands.

## Ownership

The Backend/Core owns:

- agent and model execution;
- provider and tool-call behavior;
- tool safety and confirmation policy;
- session and conversation persistence;
- Event Stream, STM, WM, Focus, and LTM behavior;
- slash-command names, semantics, and results;
- foreground and background runtime events.

The TUI owns:

- text input, IME commits, multiline paste, and editing;
- slash-command autocomplete and input normalization;
- transcript presentation and scrolling;
- tool, confirmation, config, settings, trace, and command-output panels;
- selection and copy behavior;
- status-bar presentation;
- sending user choices and cancellation requests to the Backend/Core.

The TUI may format and combine structured backend data for presentation, but
it must not infer or recreate backend decisions from rendered text.

## Message Flow

The visible transcript is an ordered projection of backend turn events.

- A user message is shown once and remains in turn order.
- Assistant streaming deltas update the current assistant item.
- A tool start creates a running tool item identified by its stable item id.
- A tool finish updates that same item instead of appending a duplicate.
- Assistant text before and after a tool call remains associated with the
  correct model phase.
- A final answer completes the current assistant item once.
- Duplicate events must not create duplicate transcript entries.
- Backend `sequence` is the preferred ordering signal. Older events without
  a sequence use bridge arrival order.

Streaming assistant text is displayed as plain text while it is incomplete.
Completed assistant content is rendered as Markdown. Incomplete Markdown must
not be parsed as if it were final content, because doing so can leave stale
bold, heading, or code-block styles in the terminal.

Startup information and stage labels, and
background activity are not transcript messages.

## Status Bar

The status bar has a stable persistent row and a variable process row.

The persistent row may contain:

- STM and WM usage;
- the current active Focus title;
- code changes;
- the last response duration.

The variable row contains the current foreground or background activity. When
nothing is active it displays `Nothing is happening`, preserving a stable UI
height.

Focus Detector state follows this lifecycle:

```text
Focus Detector is working
        -> Focus shifted: <title>
        -> current background activity or Nothing is happening
```

`Focus Detector is working` is shown only while a real detector call is in
progress. `Focus shifted` is shown briefly only when the active focus changes.
The current active focus remains in the persistent row. Focus, Retriever,
Working Memory, Segment Maintainer, and LTM Maintainer statuses are temporary
UI state and must not be written into the transcript.

## Slash Commands

Slash command names and help text come from
`my_agent/commands/command_registry.py`. Python execution comes from
`my_agent/commands/structured_commands.py` through
`my_agent/commands/dispatch.py`.

The TUI obtains filtered command metadata through `slash.list`, performs autocomplete
and input normalization, and sends the selected command through
`slash.execute`. It does not independently implement command semantics.

`slash.execute` returns structured fields such as `view`, `title`, `content`,
`transcript_items`, `project`, `permissions`, `config`, `session`, and
`should_exit`. Backend errors and unknown commands must be rendered as
user-visible command errors rather than silently disappearing.

Bridge event names and payload ownership are documented in
[bridge-event-schema.md](bridge-event-schema.md). The Python event registry
and matching TypeScript event types define the protocol boundary. The TUI
must safely ignore event types it does not understand.

## Confirmations

Python owns risk classification and permission semantics. The TUI renders the
choices supplied by Python and sends the selected response back to the bridge.

Typical choices include `Yes`, `Preview modification`, `No and explain why`,
`Hold on and discuss`, and `Allow all edits for this session`. The default
Python confirmation policy raises for risky actions when no TUI or evaluation
policy is injected.

## Panels and Settings

`/config` opens the TUI model configuration panel. The editor uses a local
draft and requires an explicit Confirm or Cancel. Provider name, protocol, base
URL, model, API key environment variable, and thinking are edited in one
LLM Provider section. Protocol and thinking use Left/Right selection; text fields
use Enter. Provider capability information is read-only and belongs to the
editor's read-only Status section. Model now groups LLM, Embedding, and Reranker.
Selecting LLM opens this editor; `/config` remains a direct entry. Configuration changes go through the backend
protocol; the TUI does not maintain a competing configuration store.

## First-Run Preparation

Follow [Windows installation](installation.md) before starting the TUI.
Preparation performs lightweight checks without loading models. Model service
tests are explicit actions; LLM testing also checks tokenizer usability.
The setup views expose Missing, Not tested, Checking, Ready, Failed, and
Restart required. Ready local services have passed an actual inference test.

LLM secrets use masked input, blank keeps the existing key, and a separate
clear action takes effect only on Confirm. Search and Remote expose optional
Tavily and Telegram credentials using the same policy. Status never returns
credential values. Environment overrides are shown; saved changes do not
override environment variables. User configuration is stored in plaintext in
the user-level config file, not the current project.

Local model paths require a Backend restart after testing. Preparation offers
a confirmed restart, rejected while project work is active. Connected clients
reconnect and initialize without replaying requests. Other project Backends
and the Telegram Gateway require their own restart.

An explicitly selected `Continue without LTM` mode requires a tested LLM.
It keeps Event Stream, STM, and WM while disabling LTM tools and processing.
The status bar shows `LTM unavailable`. Input leaving the Formation window
during this mode is permanently excluded from automatic LTM formation;
raw conversation history is retained. Optional services never block chatting.

`/settings` opens the grouped settings panel. It is a navigation and
presentation layer over existing backend commands and configuration state. It
does not independently implement permissions, safety, notifications, memory,
or provider behavior.

Settings categories use a consistent layout: a read-only `Status` section at
the top, an `Actions` section for editable settings and independent operations,
and a fixed `Cancel` / `Confirm` footer. Permission state is read-only because
command and file-edit grants are produced by the relevant confirmation flow.
`Change execution mode` is an editable action; `Reset temporary grants` is an
independent action with its own confirmation page. Resetting temporary grants
does not disable a persisted project-level unattended setting.

Diagnostic, last-run, memory, trajectory, and command-output results are shown
in their respective panels or overlays. Diagnostic output must not remain in
the normal transcript after the command view is closed or a new conversation
turn begins.

## Notifications

`/notify`, `/notify on`, and `/notify off` control system notifications for
the TUI. Notifications are enabled by default and saved in the user-level
Sema config.

The TUI sends notifications only when the terminal is not focused. V1 uses
terminal focus in/out events when supported by the host terminal. Notifications
are limited to final assistant responses, direct assistant content,
confirmations, errors, stopped turns, and cancelled turns. Tool progress,
streaming deltas, and background maintenance do not notify.

## Compatibility Boundary

The current frontend implementation is replaceable. Sema's durable contract
is the Backend/Core JSON-RPC interface, the bridge event envelope, and the
documented ownership rules above. A future TUI, GUI, remote client, or Agent
plugin may consume the same backend capabilities without reproducing the
current terminal presentation.

UI-framework-specific details belong in source comments, vendored-source
notices, or frontend development notes rather than in this Sema behavior
document.

## Validation

From the repository root:

```powershell
python -m pytest -q
```

From `tui/`:

```powershell
npm test -- --run
npm run build
```

Manual validation should cover normal multi-turn chat, streaming, tool calls,
confirmation, slash-command panels, Chinese input and multiline paste,
scrolling and copy, resize, cancellation, and concurrent background status.
