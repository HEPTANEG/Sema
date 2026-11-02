# Telegram Remote Control

Sema provides a Telegram Gateway that connects to the same workspace Backend/Core as the TUI. Both clients use the same chat history and runtime; Telegram does not create a separate Agent or memory store.

## Architecture

```text
Telegram
   -> Telegram Gateway
   -> Workspace Backend/Core
   -> Sema runtime

TUI --------------------> Workspace Backend/Core
```

The Backend/Core owns chat history, Event Stream, Agent runtime, active turns, tool execution, permissions, confirmations, and runtime state. The Gateway owns Telegram polling, access checks, message conversion, and projecting selected runtime events to Telegram. Local clients communicate with the Backend/Core through the workspace bridge.

## Access And Interaction

- Access is restricted to one configured numeric Telegram user ID and one private chat ID. Group chats, channels, topics, and dynamic pairing are not supported.
- Unauthorized updates receive a generic rejection and do not expose project, runtime, chat, or confirmation details.
- Telegram text messages enter the shared chat when the Backend/Core is idle. While a turn is active, new prompts are rejected as busy; they are not queued or appended to the active turn.
- Telegram receives concise progress, final answers, actionable errors, and permission confirmation prompts. Raw tool output, token-level assistant deltas, and the full transcript are not forwarded by default.
- Tool permissions remain enforced by the Backend/Core. Telegram presents confirmation choices and returns the selected response; it cannot grant permissions directly or enable unattended mode.
- Telegram model requests receive a temporary mobile-output instruction. It is not persisted in chat history or Event Stream.

## Reliability

- Each authenticated Telegram update is recorded before Backend submission. Its update ID is used as the request ID, so redelivery returns the existing receipt instead of starting the same Agent turn again.
- After reconnect, queued text updates are collected for a short admission window, kept in update order, and submitted as one message. Updates received during an active turn use the busy behavior instead.
- Completed final answers can be delivered again after a Gateway restart. An ambiguous Telegram send failure can rarely produce a duplicate final answer.
- A Backend/Core restart interrupts unfinished work. Model calls and tools are not resumed or retried, and pending confirmations and temporary grants are cleared. Persisted chat history remains available.
- Only one Gateway instance may serve a workspace at a time. A duplicate foreground or managed instance exits rather than forwarding the same Backend events twice.
- Telegram disconnection does not stop the Backend/Core or create a separate conversation. Reconnection uses current state and persisted results; it does not replay the complete transcript.

## Operations

Run `sema remote` to start the Gateway in the foreground. The optional Windows Scheduled Task commands are:

```text
sema remote install
sema remote status
sema remote restart
sema remote uninstall
```

The Gateway reads `SEMA_TELEGRAM_BOT_TOKEN`, `SEMA_TELEGRAM_USER_ID`, and `SEMA_TELEGRAM_CHAT_ID` from their effective Remote settings; environment variables override saved user-level settings. The bot token is secret and is not included in status output. Installing the Scheduled Task additionally requires all three values to exist as persistent Windows User environment variables. The task starts at user logon and cannot operate while the PC is off or asleep.

`sema remote restart` restarts the managed Gateway and may interrupt an active remote turn. The Backend/Core marks interrupted work as unfinished and does not replay it automatically.

## Limits

Telegram currently supports text, progress and final-answer messages, and confirmation callbacks. It does not provide file, image, or voice exchange; workspace or chat switching; remote configuration or permission management; remote TUI control; Trace browsing; queued or parallel turns; or cross-client cancellation. The Gateway uses Telegram Long Polling and does not open a public listening port.
