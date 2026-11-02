"""Telegram-only remote Gateway with durable admission and self-healing polling."""
import asyncio
import logging
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

from remote.client import BackendClient, BackendRpcError
from remote.instance_lock import GatewayInstanceLock
from remote.state import GatewayState
from remote.transport import PollingLiveness, ResilientHTTPXRequest

BUSY_MESSAGE = "Sema is currently working. Please wait until the current request finishes."
GENERIC_UNAUTHORIZED = "This bot is not authorized for this account."
INTERRUPTED_MESSAGE = "Sema stopped before this request completed. Please send a new message."
TELEGRAM_MESSAGE_LIMIT = 4096
POLLING_LIVENESS_SECONDS = 120
WATCHDOG_INTERVAL_SECONDS = 15
BACKLOG_BATCH_WINDOW_SECONDS = 1.5
WORKER_STARTUP_GRACE_SECONDS = 180
SUPERVISOR_CHECK_INTERVAL_SECONDS = 5
logger = logging.getLogger(__name__)

class TelegramGateway:
    def __init__(self, token, user_id, chat_id, workspace=None):
        self.token, self.user_id, self.chat_id = token, int(user_id), int(chat_id)
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.loop = self.application = None
        self.backend = BackendClient(self.workspace, self._on_backend_event)
        self.state = GatewayState(self.workspace)
        self.telegram_request_active = False
        self.active_request_id = self.progress_message_id = None
        self.progress_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.text_admission_lock = None
        self.batch_flush_task = None
        self.polling_liveness = PollingLiveness()
        self.watchdog_task = None

    def build_application(self):
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
        except ImportError as error:
            raise RuntimeError("python-telegram-bot is required for `sema remote`.") from error
        self._InlineKeyboardButton, self._InlineKeyboardMarkup = InlineKeyboardButton, InlineKeyboardMarkup
        outbound = ResilientHTTPXRequest.create(on_success=lambda: self._health(last_delivery_success=_utc_now(), last_recoverable_error=""), on_failure=lambda e: self._health(last_recoverable_error=str(e)), retries=3, connect_timeout=20, read_timeout=45, write_timeout=45, pool_timeout=20)
        polling = ResilientHTTPXRequest.create(on_success=self._polling_success, on_failure=lambda e: self._health(last_recoverable_error=str(e)), retries=1, connect_timeout=20, read_timeout=90, write_timeout=30, pool_timeout=20)
        self.application = Application.builder().token(self.token).request(outbound).get_updates_request(polling).post_init(self.post_init).post_shutdown(self.post_shutdown).build()
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
        self.application.add_error_handler(self.handle_error)
        return self.application

    async def post_init(self, application):
        self.loop, self.text_admission_lock = asyncio.get_running_loop(), asyncio.Lock()
        self.watchdog_task = asyncio.create_task(self._polling_watchdog())
        await self._recover_pending()

    async def post_shutdown(self, application):
        if self.batch_flush_task:
            self.batch_flush_task.cancel()
            try: await self.batch_flush_task
            except asyncio.CancelledError: pass
        if self.watchdog_task:
            self.watchdog_task.cancel()
            try: await self.watchdog_task
            except asyncio.CancelledError: pass
        from model.provider import close_provider_connections

        close_provider_connections()

    def _authorized(self, update):
        user, chat = update.effective_user, update.effective_chat
        return bool(user and chat and int(user.id) == self.user_id and int(chat.id) == self.chat_id and chat.type == "private")

    async def _reject_if_unauthorized(self, update):
        if self._authorized(update): return False
        if getattr(update, "callback_query", None):
            try: await update.callback_query.answer(GENERIC_UNAUTHORIZED, show_alert=True)
            except Exception as error: logger.warning("Unauthorized callback response failed: %s", error)
        elif getattr(update, "effective_message", None): await self._safe_reply(update.effective_message, GENERIC_UNAUTHORIZED)
        return True

    async def handle_text(self, update, context):
        if await self._reject_if_unauthorized(update): return
        message, prompt = update.effective_message, (update.effective_message.text or "").strip()
        if not prompt: return
        busy = False
        async with self.text_admission_lock:
            if self.telegram_request_active or self._backend_busy():
                busy = True
            else:
                self.state.record_inbound(update.update_id, self.chat_id, message.message_id, prompt)
                if self.batch_flush_task is None or self.batch_flush_task.done():
                    self.batch_flush_task = asyncio.create_task(self._flush_backlog(message))
        if busy:
            await self._safe_reply(message, BUSY_MESSAGE)

    async def _flush_backlog(self, first_message):
        try:
            await asyncio.sleep(BACKLOG_BATCH_WINDOW_SECONDS)
            entries = self.state.pending_ready()
            if entries:
                await self._submit_batch(entries, first_message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._health(last_recoverable_error=str(error))
            logger.exception("Telegram backlog batch failed: %s", error)
        finally:
            if self.batch_flush_task is asyncio.current_task():
                self.batch_flush_task = None

    async def _submit_batch(self, entries, message=None):
        if not entries:
            return
        entries = sorted(entries, key=lambda item: (item.get("update_id", 0), item.get("message_id", 0)))
        first_id, last_id = entries[0]["update_id"], entries[-1]["update_id"]
        request_id = entries[0].get("batch_id") or f"telegram-batch-{first_id}-{last_id}"
        if self.telegram_request_active and self.active_request_id != request_id: return
        self.state.assign_batch([entry["request_id"] for entry in entries], request_id)
        self._activate(request_id)
        try:
            prompt = _combine_telegram_prompts(entries)



            submission = asyncio.create_task(asyncio.to_thread(
                self.backend.request,
                "message.send",
                {"prompt": prompt, "source": "telegram", "request_id": request_id},
            ))
            if message is not None:
                progress = await self._safe_reply(message, "Sema is working...")
                if progress is not None: self.progress_message_id = progress.message_id
            result = await submission
            self.state.update_batch(request_id, status=str(result.get("status") or "accepted"))
            await self._reconcile_request(request_id)
        except BackendRpcError as error:
            if error.code == -32010:
                self.state.discard_batch(request_id); await self._safe_reply(message, BUSY_MESSAGE)
            else:
                self.state.update_batch(request_id, status="failed", error=str(error)); await self._deliver_and_complete(request_id, f"Sema could not process the request: {error}")
        except Exception as error:
            self.state.update_batch(request_id, status="batched", error=str(error)); self._health(last_recoverable_error=str(error)); self._deactivate()

    async def _recover_pending(self):
        groups = {}
        for entry in self.state.pending():
            groups.setdefault(entry.get("batch_id") or "", []).append(entry)
        for batch_id, entries in groups.items():
            if not batch_id:
                await self._submit_batch(entries)
                continue
            try: status = await asyncio.to_thread(self.backend.request, "remote.request.status", {"request_id": batch_id})
            except Exception as error: self._health(last_recoverable_error=str(error)); return
            if not status.get("found"): await self._submit_batch(entries)
            else: await self._handle_status(batch_id, status)

    async def _reconcile_request(self, request_id):
        await self._handle_status(request_id, await asyncio.to_thread(self.backend.request, "remote.request.status", {"request_id": request_id}))

    async def _handle_status(self, request_id, status):


        if not self.state.has_request(request_id):
            return
        if not status.get("found") or status.get("status") in {"received", "accepted"}: return
        if status.get("status") == "running":
            self._activate(request_id)
            return
        if status.get("status") == "completed": await self._deliver_and_complete(request_id, str(status.get("final_content") or "Sema finished."))
        elif status.get("status") == "interrupted": await self._deliver_and_complete(request_id, INTERRUPTED_MESSAGE)
        elif status.get("status") == "failed": await self._deliver_and_complete(request_id, str(status.get("error") or "Sema could not complete the request."))

    async def _deliver_and_complete(self, request_id, content):
        if not request_id:
            return



        async with self.delivery_lock:
            if not self.state.has_request(request_id):
                return
            for chunk in _telegram_message_chunks(content):
                if await self._safe_bot_send_message(chat_id=self.chat_id, text=chunk) is None:
                    self.state.update_batch(request_id, status="completed", final_content=content)
                    return
            if self.state.has_batch(request_id):
                self.state.complete_batch(request_id)
            else:
                self.state.complete_delivery(request_id)
            if self.active_request_id == request_id:
                self._deactivate()

    def _activate(self, request_id): self.telegram_request_active, self.active_request_id = True, request_id
    def _deactivate(self): self.telegram_request_active, self.active_request_id, self.progress_message_id = False, None, None

    async def _safe_reply(self, message, text):
        if message is None: return None
        try: return await message.reply_text(text[:TELEGRAM_MESSAGE_LIMIT])
        except Exception as error: self._health(last_recoverable_error=str(error)); logger.warning("Telegram reply failed: %s", error); return None

    async def handle_error(self, update, context):
        error = getattr(context, "error", context); self._health(last_recoverable_error=str(error)); logger.error("Telegram update handling failed: %s", error, exc_info=error)

    def _backend_busy(self):
        try: return self.backend.request("session.state", timeout=10).get("turn_status") == "running"
        except Exception: return False

    async def handle_callback(self, update, context):
        if await self._reject_if_unauthorized(update): return
        query = update.callback_query; await query.answer(); parts = str(query.data or "").split(":", 2)
        if len(parts) != 3 or parts[0] != "confirm": await query.edit_message_text("This confirmation is invalid."); return
        try:
            await asyncio.to_thread(self.backend.request, "confirmation.respond", {"confirmation_id": parts[1], "choice": parts[2]}); await query.edit_message_text("Confirmation recorded.")
        except Exception as error: await query.edit_message_text(f"Confirmation was not accepted: {error}")

    def _on_backend_event(self, event):
        if self.loop is not None: asyncio.run_coroutine_threadsafe(self._project_event(event), self.loop)

    async def _project_event(self, event):
        if not self.telegram_request_active or self.application is None: return
        event_type, payload = event.get("type"), event.get("payload") or {}
        if event_type == "confirmation_requested": await self._send_confirmation(payload)
        elif event_type in {"assistant_stage", "tool_started", "tool_finished", "model_thinking"}: await self._edit_progress(_progress_text(event_type, payload))
        elif event_type == "final_answer":
            await self._edit_progress("Sema finished."); await self._deliver_and_complete(self.active_request_id, str(payload.get("content") or "Sema finished."))
        elif event_type in {"agent_error", "error", "turn_cancelled", "agent_stopped"}: await self._deliver_and_complete(self.active_request_id, str(payload.get("error") or payload.get("message") or "Sema could not complete the request."))

    async def _edit_progress(self, text):
        if self.progress_message_id is None or self.application is None: return
        try: await self.application.bot.edit_message_text(chat_id=self.chat_id, message_id=self.progress_message_id, text=text[:TELEGRAM_MESSAGE_LIMIT])
        except Exception as error: self._health(last_recoverable_error=str(error))

    async def _safe_bot_send_message(self, **kwargs):
        try: return await self.application.bot.send_message(**kwargs)
        except Exception as error: self._health(last_recoverable_error=str(error)); logger.warning("Telegram bot message failed: %s", error); return None

    async def _send_confirmation(self, payload):
        choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
        buttons = [[self._InlineKeyboardButton(str(c.get("label") or c.get("id")), callback_data=f"confirm:{payload.get('confirmation_id')}:{c.get('id')}")] for c in choices if c.get("id") and not c.get("local_preview") and c.get("id") != "preview"]
        text = str(payload.get("details") or payload.get("title") or "Confirmation required")
        preview = str(payload.get("preview") or "").strip()
        if preview:
            text = f"{text}\n\nPreview:\n{preview[:1000]}"
        await self._safe_bot_send_message(chat_id=self.chat_id, text=text[:TELEGRAM_MESSAGE_LIMIT], reply_markup=self._InlineKeyboardMarkup(buttons) if buttons else None)

    async def _polling_watchdog(self):
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
            if self.polling_liveness.seconds_since_success() > POLLING_LIVENESS_SECONDS:
                self._health(last_recoverable_error="Telegram polling liveness watchdog recycled the application."); logger.warning("Telegram polling stalled; recycling Gateway application."); self.application.stop_running(); return

    def _polling_success(self): self.polling_liveness.mark_success(); self._health(last_polling_success=_utc_now(), last_recoverable_error="")
    def _health(self, **changes): self.state.health(**changes)

    def run(self):
        self.build_application()
        try: self.application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False, timeout=30)
        finally: self.backend.close()

class GatewaySupervisor:
    def __init__(self, token, user_id, chat_id, workspace): self.values, self.workspace = (token, user_id, chat_id), Path(workspace).resolve()
    def run(self):
        instance_lock = GatewayInstanceLock(self.workspace)
        if not instance_lock.acquire():
            print(f"Sema Telegram Gateway is already running for {self.workspace}.", file=sys.stderr)
            return 1
        delay = 1
        worker_entry = Path(__file__).resolve().parents[1] / "main.py"
        try:
            while True:
                worker = None
                try:
                    worker = subprocess.Popen(
                        [sys.executable, str(worker_entry), "remote", "worker", "--workspace", str(self.workspace)],
                        cwd=str(self.workspace),
                        env=dict(os.environ),
                        stdin=subprocess.DEVNULL,
                    )
                    self._monitor_worker(worker)
                    logger.warning("Telegram Gateway worker stopped; restarting it.")
                except KeyboardInterrupt: return 0
                except Exception as error: logger.exception("Telegram Gateway stopped unexpectedly: %s", error)
                finally:
                    if worker is not None and worker.poll() is None:
                        _terminate_process(worker)
                threading.Event().wait(delay); delay = min(delay * 2, 60)
        finally:
            instance_lock.close()

    def _monitor_worker(self, worker):
        started_at = time.time()
        while worker.poll() is None:
            if _polling_heartbeat_stale(self.workspace, started_at):
                logger.warning("Telegram Gateway worker heartbeat stalled; recycling worker.")
                _terminate_process(worker)
                return
            time.sleep(SUPERVISOR_CHECK_INTERVAL_SECONDS)


def _terminate_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _polling_heartbeat_stale(workspace, started_at, now=None):
    """Return true when a worker has no fresh polling success."""
    now = time.time() if now is None else now
    try:
        health = GatewayState(workspace).snapshot().get("health") or {}
        value = health.get("last_polling_success")
        if not value:
            return now - started_at > WORKER_STARTUP_GRACE_SECONDS
        from datetime import datetime
        heartbeat = datetime.fromisoformat(value).timestamp()
        if heartbeat < started_at:
            return now - started_at > WORKER_STARTUP_GRACE_SECONDS
        return now - heartbeat > POLLING_LIVENESS_SECONDS
    except (OSError, ValueError, TypeError):
        return now - started_at > WORKER_STARTUP_GRACE_SECONDS

def _progress_text(event_type, payload):
    if event_type == "assistant_stage": return str(payload.get("label") or "Sema is working...")[:TELEGRAM_MESSAGE_LIMIT]
    if event_type == "tool_started": return f"Running {payload.get('tool') or 'a tool'}..."[:TELEGRAM_MESSAGE_LIMIT]
    if event_type == "tool_finished": return f"Finished {payload.get('tool') or 'a tool'}."[:TELEGRAM_MESSAGE_LIMIT]
    return "Sema is thinking..."


def _combine_telegram_prompts(entries):
    if len(entries) == 1:
        return entries[0]["prompt"]
    parts = [
        "Please handle the following messages together and give one consolidated reply. "
        "They were sent in this order:",
    ]
    for index, entry in enumerate(entries, 1):
        parts.append(f"\n[Message {index}]\n{entry['prompt']}")
    return "\n".join(parts)

def _telegram_message_chunks(text, limit=TELEGRAM_MESSAGE_LIMIT):
    remaining, chunks = str(text or "").strip(), []
    while remaining:
        if len(remaining) <= limit: return chunks + [remaining]
        split_at = remaining.rfind("\n", 0, limit + 1); split_at = split_at if split_at > 0 else limit
        chunks.append(remaining[:split_at].rstrip()); remaining = remaining[split_at:].lstrip("\n")
    return chunks

def credentials_from_environment():
    from runtime.setup_config import value
    values = tuple(value("remote", key).strip() for key in ("bot_token", "user_id", "chat_id"))
    if not all(values): raise RuntimeError("SEMA_TELEGRAM_BOT_TOKEN, SEMA_TELEGRAM_USER_ID, and SEMA_TELEGRAM_CHAT_ID are required.")
    return values

def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

def main(argv=None):
    from remote.service import main as service_main
    args = list(argv or [])
    if args and args[0] in {"install", "status", "restart", "uninstall"}: return service_main(args)
    service, worker, workspace = args[:1] == ["service"], args[:1] == ["worker"], Path.cwd()
    if service or worker:
        args = args[1:]
        if args[:1] == ["--workspace"] and len(args) == 2: workspace = Path(args[1])
        elif args: raise RuntimeError("Usage: sema remote service --workspace C:\\path\\to\\workspace")
    if worker:
        TelegramGateway(*credentials_from_environment(), workspace=workspace).run()
        return 0
    return GatewaySupervisor(*credentials_from_environment(), workspace=workspace).run()
