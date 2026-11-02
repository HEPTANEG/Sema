import asyncio
from types import SimpleNamespace

import pytest

from remote.gateway import (
    BUSY_MESSAGE,
    GENERIC_UNAUTHORIZED,
    TELEGRAM_MESSAGE_LIMIT,
    TelegramGateway,
    _combine_telegram_prompts,
    _progress_text,
    _telegram_message_chunks,
    main,
)


def gateway_for_auth():
    gateway = object.__new__(TelegramGateway)
    gateway.user_id = 101
    gateway.chat_id = 202
    return gateway


def update(user_id=101, chat_id=202, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
    )


def test_gateway_accepts_only_configured_private_identity():
    gateway = gateway_for_auth()

    assert gateway._authorized(update())
    assert not gateway._authorized(update(user_id=999))
    assert not gateway._authorized(update(chat_id=999))
    assert not gateway._authorized(update(chat_type="group"))


def test_progress_text_is_coarse_and_bounded():
    assert _progress_text("assistant_stage", {"label": "Inspecting files"}) == "Inspecting files"
    assert _progress_text("tool_started", {"tool": "shell"}) == "Running shell..."
    assert _progress_text("tool_finished", {"tool": "shell"}) == "Finished shell."
    assert len(_progress_text("model_thinking", {})) < 4096


def test_gateway_status_messages_are_stable():
    assert "currently working" in BUSY_MESSAGE
    assert GENERIC_UNAUTHORIZED == "This bot is not authorized for this account."


def test_progress_reply_failure_still_submits_the_request(tmp_path):
    calls = []
    gateway = object.__new__(TelegramGateway)
    gateway.user_id = 101
    gateway.chat_id = 202
    gateway.telegram_request_active = False
    gateway.progress_message_id = None
    gateway.active_request_id = None
    gateway.text_admission_lock = asyncio.Lock()
    gateway.batch_flush_task = None
    from remote.state import GatewayState
    gateway.state = GatewayState(tmp_path)
    gateway._health = lambda **_changes: None
    gateway._reconcile_request = lambda _request_id: _async_none()
    gateway._backend_busy = lambda: False
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "accepted"}
    gateway.backend = SimpleNamespace(request=request)

    class FailingMessage:
        text = "hello"
        message_id = 12

        async def reply_text(self, _text):
            raise TimeoutError("Telegram API timed out")

    current_update = update()
    current_update.update_id = 22
    current_update.effective_message = FailingMessage()

    async def run_request():
        await gateway.handle_text(current_update, None)
        await gateway.batch_flush_task

    asyncio.run(run_request())

    assert gateway.telegram_request_active is True
    assert gateway.progress_message_id is None
    assert calls and calls[0][0][0] == "message.send"
    assert calls[0][0][1]["request_id"] == "telegram-batch-22-22"


def test_backend_submission_starts_before_slow_progress_reply(tmp_path):
    order = []
    gateway = object.__new__(TelegramGateway)
    gateway.user_id = 101
    gateway.chat_id = 202
    gateway.telegram_request_active = False
    gateway.active_request_id = None
    gateway.progress_message_id = None
    gateway.text_admission_lock = asyncio.Lock()
    gateway.batch_flush_task = None
    from remote.state import GatewayState
    gateway.state = GatewayState(tmp_path)
    gateway._health = lambda **_changes: None
    gateway._backend_busy = lambda: False

    def request(*_args, **_kwargs):
        order.append("backend")
        return {"status": "accepted"}
    gateway.backend = SimpleNamespace(request=request)

    async def slow_reply(_message, _text):
        order.append("progress-start")
        await asyncio.sleep(0)
        order.append("progress-end")
        return None
    gateway._safe_reply = slow_reply
    gateway._reconcile_request = lambda _request_id: _async_none()

    message = SimpleNamespace(text="hello", message_id=12)
    current_update = update()
    current_update.update_id = 23
    current_update.effective_message = message
    async def run_request():
        await gateway.handle_text(current_update, None)
        await gateway.batch_flush_task

    asyncio.run(run_request())

    assert order.index("backend") < order.index("progress-end")


def test_long_telegram_replies_are_split_within_platform_limit():
    text = ("first section\n" + "x" * 3000 + "\n") * 3

    chunks = _telegram_message_chunks(text)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.strip().replace("\n", "")


def test_backlog_prompts_are_combined_in_update_order():
    entries = [
        {"update_id": 11, "prompt": "first"},
        {"update_id": 12, "prompt": "second"},
    ]

    combined = _combine_telegram_prompts(entries)

    assert combined.index("first") < combined.index("second")
    assert "one consolidated reply" in combined


def test_messages_arriving_during_batch_window_share_one_submission(tmp_path, monkeypatch):
    from remote.state import GatewayState
    import remote.gateway as gateway_module

    submitted = []
    gateway = object.__new__(TelegramGateway)
    gateway.user_id = 101
    gateway.chat_id = 202
    gateway.telegram_request_active = False
    gateway.active_request_id = None
    gateway.progress_message_id = None
    gateway.text_admission_lock = asyncio.Lock()
    gateway.batch_flush_task = None
    gateway.state = GatewayState(tmp_path)
    gateway._health = lambda **_changes: None
    gateway._backend_busy = lambda: False
    gateway._safe_reply = lambda *_args, **_kwargs: _async_none()

    async def submit(entries, _message=None):
        submitted.append([entry["prompt"] for entry in entries])

    gateway._submit_batch = submit
    monkeypatch.setattr(gateway_module, "BACKLOG_BATCH_WINDOW_SECONDS", 0.01)

    first = update()
    first.update_id = 30
    first.effective_message = SimpleNamespace(text="first", message_id=1)
    second = update()
    second.update_id = 31
    second.effective_message = SimpleNamespace(text="second", message_id=2)

    async def run_requests():
        await asyncio.gather(
            gateway.handle_text(first, None),
            gateway.handle_text(second, None),
        )
        await gateway.batch_flush_task

    asyncio.run(run_requests())

    assert submitted == [["first", "second"]]


def test_concurrent_terminal_paths_send_one_final_reply(tmp_path):
    sent = []
    gateway = object.__new__(TelegramGateway)
    from remote.state import GatewayState
    gateway.state = GatewayState(tmp_path)
    gateway.delivery_lock = asyncio.Lock()
    gateway.chat_id = 202
    gateway.active_request_id = "telegram-update-22"
    gateway.telegram_request_active = True
    gateway.progress_message_id = None
    gateway._safe_bot_send_message = lambda **kwargs: _record_message(sent, kwargs)
    gateway.state.record_inbound(22, 202, 12, "hello")

    async def deliver_twice():
        await asyncio.gather(
            gateway._deliver_and_complete("telegram-update-22", "done"),
            gateway._deliver_and_complete("telegram-update-22", "done"),
        )

    asyncio.run(deliver_twice())

    assert [message["text"] for message in sent] == ["done"]


def test_remote_confirmation_embeds_preview_without_a_dead_preview_button():
    sent = []
    gateway = object.__new__(TelegramGateway)
    gateway.chat_id = 202
    gateway.application = SimpleNamespace(
        bot=SimpleNamespace(send_message=lambda **kwargs: _record_async(sent, kwargs))
    )
    gateway._InlineKeyboardButton = lambda label, callback_data: (label, callback_data)
    gateway._InlineKeyboardMarkup = lambda buttons: buttons

    asyncio.run(
        gateway._send_confirmation({
            "confirmation_id": "abc",
            "details": "Write file?",
            "preview": "new content",
            "choices": [
                {"id": "yes", "label": "Yes"},
                {"id": "preview", "label": "Preview modification", "local_preview": True},
                {"id": "no", "label": "No"},
            ],
        })
    )

    assert "Preview:\nnew content" in sent[0]["text"]
    assert sent[0]["reply_markup"] == [
        [("Yes", "confirm:abc:yes")],
        [("No", "confirm:abc:no")],
    ]


def test_gateway_requires_credentials(monkeypatch):
    for name in ("SEMA_TELEGRAM_BOT_TOKEN", "SEMA_TELEGRAM_USER_ID", "SEMA_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="SEMA_TELEGRAM_BOT_TOKEN"):
        main()


async def _record_async(target, value):
    target.append(value)


async def _record_message(target, value):
    target.append(value)
    return SimpleNamespace(message_id=len(target))


async def _async_none():
    return None
