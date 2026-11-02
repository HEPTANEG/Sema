from runtime import agent


STALE_MAIN_PROMPT_TERMS = {
    "Memory system: Event Stream.",
    "show_memory_state",
    "list_images",
    "vision_analyze_images",
    "Runtime Wake",
    "You are Sema, the assistant inside the Sema CLI project.",
    "TOOL:",
}


def test_system_prompt_is_minimal_and_uses_structured_tools():
    prompt = agent.create_initial_messages()[0]["content"]

    assert prompt == """# Identity

You are Sema.

# Tool Use

- Use available structured tools when needed. Do not write tool calls as plain text.
- Before invoking a tool, briefly state what you are about to inspect, run, or change.
- If a tool fails, retry the same failure point at most two times, then stop and explain what blocked the task."""


def test_system_prompt_does_not_include_retired_or_tool_specific_rules():
    prompt = agent.create_initial_messages()[0]["content"]

    for term in STALE_MAIN_PROMPT_TERMS:
        assert term not in prompt
