import json

import pytest

from integrations import image_context


def test_validate_image_paths_requires_vision_provider(monkeypatch, tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))

    with pytest.raises(RuntimeError, match="does not support image input"):
        image_context.validate_image_paths([str(image)])


def test_validate_image_paths_accepts_kimi_profile(monkeypatch, tmp_path):
    from model import provider as model_provider

    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi"]))

    records = image_context.validate_image_paths([str(image)])

    assert records == [
        {
            "path": str(image.resolve()),
            "filename": "one.png",
            "mime_type": "image/png",
        }
    ]


def test_build_image_context_message_uses_lightweight_reference(tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    record = {
        "path": str(image),
        "filename": image.name,
        "mime_type": "image/png",
    }

    message = image_context.build_image_context_message([record])
    serialized = json.dumps(message, ensure_ascii=False)

    assert message["role"] == "user"
    assert message["content"][1]["type"] == "sema_image"
    assert message["content"][1]["path"] == str(image)
    assert "data:image" not in serialized


def test_expire_image_context_removes_active_image_refs(tmp_path):
    image = tmp_path / "one.png"
    image.write_bytes(b"png")
    message = image_context.build_image_context_message(
        [
            {
                "path": str(image),
                "filename": image.name,
                "mime_type": "image/png",
            }
        ]
    )
    messages = [{"role": "system", "content": "system"}, message]

    expired_count = image_context.expire_image_context(messages)

    assert expired_count == 1
    assert messages[1]["content"] == [
        {
            "type": "text",
            "text": "Expired image context, not sent again: one.png.",
        }
    ]
    assert not image_context.has_image_parts(messages)


def test_validate_image_paths_rejects_more_than_ten(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMA_HOME", str(tmp_path / "home"))
    paths = [f"{index}.png" for index in range(11)]

    with pytest.raises(RuntimeError, match="at most 10"):
        image_context.validate_image_paths(paths)


def test_list_images_for_context_lists_supported_images(isolated_workspace):
    (isolated_workspace / "shots").mkdir()
    (isolated_workspace / "shots" / "one.png").write_bytes(b"png")
    (isolated_workspace / "shots" / "two.webp").write_bytes(b"webp")
    (isolated_workspace / "shots" / "notes.txt").write_text("no", encoding="utf-8")

    result = image_context.list_images_for_context("shots")

    assert "--- images: shots ---" in result
    assert "count: 2" in result
    assert "shots\\one.png" in result or "shots/one.png" in result
    assert "notes.txt" not in result


def test_list_images_for_context_can_use_confirmed_external_directory(isolated_workspace, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "one.jpg").write_bytes(b"jpg")

    with pytest.raises(RuntimeError, match="outside the current working directory"):
        image_context.list_images_for_context(str(external))

    result = image_context.list_images_for_context(str(external), allow_external=True)

    assert f"--- images: {external} ---" in result
    assert "count: 1" in result
    assert str(external / "one.jpg") in result


def test_analyze_images_for_context_calls_vision_provider(monkeypatch, isolated_workspace):
    from model import provider as model_provider

    image = isolated_workspace / "one.png"
    image.write_bytes(b"png")
    model_provider.save_global_model_config(dict(model_provider.PROVIDER_PROFILES["kimi"]))
    captured = {}

    def fake_call(messages, tools=None, temperature=None, timeout_seconds=120, purpose="model"):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["temperature"] = temperature
        captured["purpose"] = purpose
        return {"choices": [{"message": {"content": "## Result\nImage text."}}]}

    monkeypatch.setattr(image_context, "call_chat_completion", fake_call)

    result = image_context.analyze_images_for_context(["one.png"], "Convert to Markdown.")

    assert captured["purpose"] == "vision"
    assert captured["tools"] is None
    assert captured["temperature"] is None
    assert captured["messages"][1]["content"][1]["type"] == "sema_image"
    assert "## Result" in result
    assert "--- vision analysis ---" in result
