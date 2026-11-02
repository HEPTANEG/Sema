import io

import pytest

from bridge.ui_bridge_server import BridgeServer, JsonRpcError
from runtime.directives import messages_with_runtime_directives


def directive_contents(messages):
    return [message["content"] for message in messages if message.get("role") == "system"]


def test_telegram_source_adds_only_a_temporary_plain_text_directive():
    messages = [{"role": "system", "content": "base prompt"}, {"role": "user", "content": "hello"}]

    result = messages_with_runtime_directives(messages, {"interaction_source": "telegram"})

    assert any("This turn comes from Telegram Remote." in content for content in directive_contents(result))
    assert any("avoid Markdown formatting" in content for content in directive_contents(result))
    assert messages == [{"role": "system", "content": "base prompt"}, {"role": "user", "content": "hello"}]


def test_tui_source_does_not_add_remote_directive():
    result = messages_with_runtime_directives(
        [{"role": "system", "content": "base prompt"}],
        {"interaction_source": "tui"},
    )

    assert not any("Telegram Remote" in content for content in directive_contents(result))


def test_message_source_is_turn_local_and_not_persisted():
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    server.messages = [{"role": "system", "content": "base prompt"}]
    server._preflight_provider = lambda: None
    observed = []
    server._run_message_turn = lambda prompt, turn_id: observed.append(
        server.runtime_state.get("interaction_source")
    ) or {"ok": True}

    server.send_message({"prompt": "remote question", "source": "telegram"})

    assert observed == ["telegram"]
    assert "interaction_source" not in server.runtime_state
    assert all("Telegram Remote" not in str(message) for message in server.messages)


def test_unknown_message_source_is_rejected():
    server = BridgeServer(input_stream=io.StringIO(), output_stream=io.StringIO())

    with pytest.raises(JsonRpcError, match="unsupported interaction source"):
        server.send_message({"prompt": "hello", "source": "unknown"})
