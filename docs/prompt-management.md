# Prompt Management

Prompt files live under `my_agent/prompts/` and are loaded by `shared.prompt_loader.load_prompt()`.

The Main Agent system prompt is the minimal `my_agent/prompts/main_agent.md` file. It contains only identity and cross-tool interaction rules. The runtime does not select prompt packs from project metadata.

Tool-specific usage guidance belongs in `my_agent/tools/tool_registry.py`, in each exposed tool's schema description and parameter descriptions. The registry is sent with every Main Agent provider request, so the model can compare tools before choosing one. Hidden tools remain registered for runtime compatibility but are not included in the provider tool payload.

Specialized components load their own prompt files by stable names in `PROMPT_FILES`, including temporal processing, Working Memory, Focus Segments, formation, recall, and vision analysis.

The system prompt must not duplicate tool-specific instructions, expose retired tools, or describe internal Memory Engine implementation details. `memorize`'s explicit-user-request condition is model guidance in its tool schema; the runtime does not independently infer user intent.
