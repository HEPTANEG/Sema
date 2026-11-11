import json
from pathlib import Path

import pytest

from model import provider
from runtime import setup_config


def test_reranker_default_prefers_root_and_accepts_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_config, "__file__", str(tmp_path / "my_agent/runtime/setup_config.py"))
    root = tmp_path / ".venv-reranker/Scripts/python.exe"
    legacy = tmp_path / "my_agent/.venv-reranker/Scripts/python.exe"
    assert Path(setup_config.defaults("reranker")["python_path"]) == root
    legacy.parent.mkdir(parents=True)
    legacy.touch()
    assert Path(setup_config.defaults("reranker")["python_path"]) == legacy
    root.parent.mkdir(parents=True)
    root.touch()
    assert Path(setup_config.defaults("reranker")["python_path"]) == root


def test_reranker_saved_and_environment_paths_override_default(isolated_workspace, tmp_path, monkeypatch):
    saved = str(tmp_path / "saved-python.exe")
    override = str(tmp_path / "env-python.exe")
    setup_config.save("reranker", {"python_path": saved})
    assert setup_config.value("reranker", "python_path") == saved
    monkeypatch.setenv("SEMA_RERANKER_PYTHON", override)
    assert setup_config.value("reranker", "python_path") == override


def test_missing_detail_does_not_repeat_status(isolated_workspace, tmp_path):
    setup_config.save("reranker", {"model_path": str(tmp_path), "python_path": str(tmp_path / "absent.exe")})
    state = setup_config.status()["components"]["reranker"]
    assert state["status"] == "Missing"
    assert state["detail"] == "python path"


@pytest.fixture(autouse=True)
def isolated_setup_environment(monkeypatch):
    for fields in setup_config.FIELDS.values():
        for name in fields.values():
            monkeypatch.setenv(name, "")


def test_setup_secret_precedence_and_redaction(isolated_workspace, monkeypatch):
    setup_config.save("search", {"api_key": "saved-secret"})
    monkeypatch.setenv("TAVILY_API_KEY", "env-secret")
    assert setup_config.value("search", "api_key") == "env-secret"
    snapshot = setup_config.status()
    assert "saved-secret" not in json.dumps(snapshot)
    assert "env-secret" not in json.dumps(snapshot)
    assert snapshot["components"]["search"]["sources"]["api_key"].startswith("environment")
    setup_config.save("search", {"api_key": ""})
    monkeypatch.delenv("TAVILY_API_KEY")
    assert setup_config.value("search", "api_key") == "saved-secret"
    setup_config.save("search", {"clear_api_key": True})
    assert setup_config.value("search", "api_key") == ""


def test_setup_test_fingerprint_and_failure_redaction(isolated_workspace, monkeypatch):
    setup_config.save("search", {"api_key": "secret"})
    monkeypatch.setattr(setup_config, "_test", lambda _: None)
    assert setup_config.test("search")["status"] == "Ready"
    assert setup_config.status()["components"]["search"]["status"] == "Ready"
    setup_config.save("search", {"api_key": "replacement"})
    assert setup_config.status()["components"]["search"]["status"] == "Not tested"
    def fail(_):
        raise RuntimeError("https://secret-token")
    monkeypatch.setattr(setup_config, "_test", fail)
    assert "secret-token" not in str(setup_config.test("search"))


def test_llm_setup_test_bounds_provider_output(monkeypatch):
    calls = []

    def fake_call(_messages, **kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(provider, "call_chat_completion", fake_call)

    assert setup_config._test("llm") == {}
    assert calls[0]["max_tokens"] == 8


def test_atomic_config_preserves_sections_on_failure(isolated_workspace, monkeypatch):
    provider.save_global_notification_config({"enabled": False})
    before = provider.global_config_path().read_bytes()
    def fail(*_):
        raise OSError("disk failure")
    monkeypatch.setattr(provider.os, "replace", fail)
    with pytest.raises(OSError):
        setup_config.save("search", {"api_key": "new"})
    assert provider.global_config_path().read_bytes() == before


def test_setup_rejects_unknown_fields(isolated_workspace):
    with pytest.raises(ValueError):
        setup_config.save("remote", {"allow_all": True})
    with pytest.raises(ValueError):
        setup_config.save("remote", {"user_id": "abc"})


def test_model_queries_never_return_secret_fragments(isolated_workspace, monkeypatch):
    from commands.structured_commands import format_model_config
    provider.save_global_model_config({"api_key": "prefix-private-suffix"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-private-key")
    result = json.dumps(setup_config.public_model()) + format_model_config()
    for secret in ('prefix', 'suffix', 'env-private-key'):
        assert secret not in result
    assert setup_config.public_model()['api_key_source'] == 'DEEPSEEK_API_KEY'
