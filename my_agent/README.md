# Sema

Sema is a local coding agent with a TypeScript/React/Ink TUI and a Python backend. The TUI is the chat interface; Python owns the model loop, tools, persistence, and Memory Engine.

## Start

```powershell
sema
sema tui
```

Both commands start the TUI in the current working directory.

## Bridge Commands

```powershell
sema ui-bridge
sema backend
sema remote
sema eval-bridge
```

`backend` starts the shared local Backend/Core for the current workspace.
`remote` starts the foreground Telegram Long Polling Gateway and connects it to
the shared Backend/Core through the workspace Windows Named Pipe. It requires
`SEMA_TELEGRAM_BOT_TOKEN`, `SEMA_TELEGRAM_USER_ID`, and
`SEMA_TELEGRAM_CHAT_ID`. Both the TUI and Gateway ensure the Backend/Core is
running, but starting the TUI does not automatically start Telegram. `ui-bridge`
remains the legacy stdio JSON-RPC backend for compatibility; `eval-bridge`
provides the JSONL bridge for evaluation tooling.

## Project Layout

- `main.py`: CLI dispatch.
- `bridge/`: JSON-RPC, TUI launch, headless runtime, and UI events.
- `runtime/`: model/tool loop, structured loop, directives, and background maintenance.
- `commands/`: slash metadata and execution.
- `tools/`: provider schemas, tool runtime, executors, and workspace helpers.
- `persistence/`: Event Stream-backed session persistence, trace records, and undo data.
- `memory_engine/`: Event Stream, STM, Working Memory, Focus Segments, Context Builder, LTM, formation, retrieval, and observer APIs.

## Validation

```powershell
python -m pytest -q
npm test --prefix tui
npm run build --prefix tui
```
