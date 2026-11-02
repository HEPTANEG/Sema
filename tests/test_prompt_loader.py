import pytest

from shared.prompt_loader import PROMPT_FILES, compose_main_agent_prompt, load_prompt


def test_all_registered_prompt_files_load_and_are_non_empty():
    for name in PROMPT_FILES:
        assert load_prompt(name).strip(), name


def test_missing_prompt_name_raises_clear_error():
    with pytest.raises(RuntimeError, match="Unknown prompt"):
        load_prompt("missing_prompt")


def test_missing_prompt_file_raises_clear_error(monkeypatch):
    monkeypatch.setitem(PROMPT_FILES, "missing_file", "missing-file.md")
    load_prompt.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="Prompt file not found"):
            load_prompt("missing_file")
    finally:
        PROMPT_FILES.pop("missing_file", None)
        load_prompt.cache_clear()


def test_key_prompt_name_mapping_is_stable():
    assert PROMPT_FILES["main_agent"] == "main_agent.md"


def test_main_agent_prompt_composer_uses_the_minimal_prompt():
    prompt = compose_main_agent_prompt()

    assert prompt == load_prompt("main_agent")
    assert prompt.count("# Identity") == 1
    assert prompt.count("# Tool Use") == 1
