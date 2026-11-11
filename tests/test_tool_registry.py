from security import security_policy
from tools import tool_executor
from tools import tool_registry
from tools import tool_schemas


EXPECTED_REGISTERED_TOOL_NAMES = [
    "read_file",
    "get_overview",
    "list_files",
    "list_images",
    "vision_analyze_images",
    "run_command",
    "start_background_command",
    "list_background_commands",
    "read_background_command_output",
    "stop_background_command",
    "write_file",
    "insert_after",
    "replace_block",
    "web_search",
    "web_fetch",
    "show_undo_records",
    "undo_edit",
    "show_memory_state",
    "get_object",
    "retrieve_memory",
    "memorize",
    "sleep_for_maintenance",
    "retry_ltm_maintenance",
    "return_maintenance_failure",
]

HIDDEN_FROM_MODEL = {
    "list_files",
    "list_images",
    "vision_analyze_images",
    "show_memory_state",
    "sleep_for_maintenance",
    "retry_ltm_maintenance",
    "return_maintenance_failure",
}

EXPECTED_PROVIDER_TOOL_NAMES = [name for name in EXPECTED_REGISTERED_TOOL_NAMES if name not in HIDDEN_FROM_MODEL]


def test_tool_registry_names_are_stable():
    assert [definition.name for definition in tool_registry.TOOL_DEFINITIONS] == EXPECTED_REGISTERED_TOOL_NAMES
    assert tool_registry.tool_schema_names() == EXPECTED_PROVIDER_TOOL_NAMES
    assert tool_schemas.tool_schema_names() == EXPECTED_PROVIDER_TOOL_NAMES
    assert len(tool_registry.TOOL_DEFINITIONS) == 24


def test_tool_names_are_current_registry_names():
    names = [definition.name for definition in tool_registry.TOOL_DEFINITIONS]

    assert names == [
        "read_file",
        "get_overview",
        "list_files",
        "list_images",
        "vision_analyze_images",
        "run_command",
        "start_background_command",
        "list_background_commands",
        "read_background_command_output",
        "stop_background_command",
        "write_file",
        "insert_after",
        "replace_block",
        "web_search",
        "web_fetch",
        "show_undo_records",
        "undo_edit",
            "show_memory_state",
            "get_object",
            "retrieve_memory",
        "memorize",
        "sleep_for_maintenance",
        "retry_ltm_maintenance",
        "return_maintenance_failure",
    ]


def test_tool_names_include_memory_engine_and_core_tools():
    names = tool_registry.registered_tool_names()
    assert {"show_undo_records", "read_file", "get_overview", "run_command"} <= names
    assert names.isdisjoint({"show_skills", "show_skill_content", "mcp"})
    assert "retrieve_memory" in names
    assert "recall_memory" not in names
    assert names.isdisjoint({"ltm_select_objects", "ltm_search_objects", "ltm_traverse_objects"})


def test_selected_tools_remain_registered_but_are_not_exposed_to_the_model():
    assert HIDDEN_FROM_MODEL <= tool_registry.registered_tool_names()
    assert HIDDEN_FROM_MODEL.isdisjoint(tool_registry.tool_schema_names())
    assert all(
        tool_registry.get_tool_definition(name).expose_to_model is False
        for name in HIDDEN_FROM_MODEL
    )


def test_tool_registry_definitions_are_complete():
    for definition in tool_registry.TOOL_DEFINITIONS:
        assert definition.name
        assert definition.description
        assert isinstance(definition.parameters, dict)
        assert isinstance(definition.required, list)
        assert definition.risk
        assert definition.executor


def test_tool_registry_generates_openai_compatible_schemas():
    for definition in tool_registry.TOOL_DEFINITIONS:
        schema = definition.schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == definition.name
        assert schema["function"]["description"] == definition.description
        assert schema["function"]["parameters"] == {
            "type": "object",
            "properties": definition.parameters,
            "required": definition.required,
            "additionalProperties": False,
        }


def test_tool_schemas_are_generated_from_registry():
    assert tool_schemas.TOOL_SCHEMAS == tool_registry.TOOL_SCHEMAS


def test_exposed_tool_descriptions_carry_tool_specific_guidance():
    assert "start_background_command" in tool_registry.get_tool_definition("run_command").description
    assert "read_background_command_output" in tool_registry.get_tool_definition("start_background_command").description
    assert "stop_background_command" in tool_registry.get_tool_definition("start_background_command").description
    assert "simple, direct Long-Term Memory recall" in tool_registry.get_tool_definition("get_object").description
    assert "Do not poll by immediately calling it again" in tool_registry.get_tool_definition("retrieve_memory").description
    assert tool_registry.get_tool_definition("memorize").description == (
        "Create a new durable long-term memory only when the user explicitly asks Sema to remember or save information."
    )


def test_registered_tools_have_explicit_security_policy_classification():
    for definition in tool_registry.TOOL_DEFINITIONS:
        decision = security_policy.classify_tool_request({"tool": definition.name, "args": ""})

        assert decision.risk != "unknown", definition.name
        assert decision.blocked is False, definition.name


def test_registered_tools_have_executor_and_security_policy():
    for definition in tool_registry.TOOL_DEFINITIONS:
        decision = security_policy.classify_tool_request({"tool": definition.name, "args": ""})

        assert decision.risk != "unknown", definition.name
        assert decision.blocked is False, definition.name
        assert definition.executor in tool_executor.TOOL_EXECUTORS, definition.name


def test_registered_tools_have_executor_handlers():
    for definition in tool_registry.TOOL_DEFINITIONS:
        assert definition.executor in tool_executor.TOOL_EXECUTORS, definition.name
    assert tool_executor.missing_registered_executors() == []


def test_apply_patch_is_legacy_executor_not_provider_schema():
    assert "apply_patch" in tool_executor.TOOL_EXECUTORS
    assert "apply_patch" not in tool_registry.tool_schema_names()
    assert "apply_patch" not in tool_schemas.tool_schema_names()
