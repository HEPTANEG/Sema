import pytest

from tools import tool_registry, tool_schemas


def _schema_by_name(name):
    for schema in tool_schemas.TOOL_SCHEMAS:
        if schema["function"]["name"] == name:
            return schema
    raise AssertionError(f"Missing schema: {name}")


def _registered_schema_by_name(name):
    definition = tool_registry.get_tool_definition(name)
    assert definition is not None
    return definition.schema()


def test_initial_tool_schema_names_are_stable():
    assert tool_schemas.tool_schema_names() == [
        "read_file",
        "get_overview",
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
        "get_object",
        "retrieve_memory",
        "memorize",
    ]


def test_retrieve_memory_schema_and_conversion_are_flat_and_high_level_only():
    schema = next(item for item in tool_schemas.TOOL_SCHEMAS if item["function"]["name"] == "retrieve_memory")
    assert schema["function"]["parameters"]["required"] == []
    assert set(schema["function"]["parameters"]["properties"]) == {"query", "continuation_id", "message", "include_context", "close"}
    description = schema["function"]["description"]
    assert "close=true only" in description
    assert "Do not include query, message, or include_context in a close call" in description
    assert not {"ltm_select_objects", "ltm_search_objects", "ltm_traverse_objects"} & set(tool_schemas.tool_schema_names())

    request = tool_schemas.tool_call_to_request(
        {
            "function": {
                "name": "retrieve_memory",
                "arguments": '{"query":"preferences"}',
            }
        }
    )
    assert request == {
        "tool": "retrieve_memory",
        "args": {"query": "preferences", "continuation_id": None, "message": None, "include_context": None, "close": None},
    }


def test_get_object_schema_and_conversion_expose_bounded_read_only_lookup():
    schema = _schema_by_name("get_object")
    properties = schema["function"]["parameters"]["properties"]
    assert {"query", "object_id", "object_ids", "projection", "limit"} <= set(properties)
    assert schema["function"]["parameters"]["required"] == []
    assert "lexical, not embedding" in properties["query"]["description"]

    request = tool_schemas.tool_call_to_request({
        "function": {
            "name": "get_object",
            "arguments": '{"query":"desktop files","projection":"payload","limit":3,"object_ids":["mem_a"]}',
        }
    })
    assert request == {
        "tool": "get_object",
        "args": {"object_ids": ["mem_a"], "query": "desktop files", "projection": "payload", "limit": 3},
    }


def test_show_memory_state_schema_excludes_recall_semantics():
    schema = _registered_schema_by_name("show_memory_state")
    description = schema["function"]["description"]

    assert "metadata-only Memory Engine status" in description
    assert "cannot retrieve or verify memory content" in description
    assert "must not accompany ordinary recall" in description


def test_unknown_tool_calls_use_the_standard_parser_error():
    with pytest.raises(RuntimeError, match="Unknown structured tool call: obsolete_tool"):
        tool_schemas.tool_call_to_request(
            {
                "type": "function",
                "function": {"name": "obsolete_tool", "arguments": "{}"},
            }
        )


def test_run_command_schema_warns_against_undo_revert():
    assert "use undo_edit" in _schema_by_name("run_command")["function"]["description"]
    assert "after user confirmation" in _schema_by_name("undo_edit")["function"]["description"]


@pytest.mark.parametrize("name", ["read_file", "get_overview", "list_files", "list_images", "vision_analyze_images", "write_file", "insert_after", "replace_block"])
def test_file_tool_schemas_explain_confirmed_external_paths(name):
    schema_text = str(_registered_schema_by_name(name))

    assert "external" in schema_text
    assert "confirmation" in schema_text


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("read_file", ["path"]),
        ("get_overview", ["path"]),
        ("list_files", ["path"]),
        ("list_images", ["path"]),
        ("vision_analyze_images", ["paths", "instruction"]),
        ("run_command", ["command"]),
        ("start_background_command", ["command"]),
        ("list_background_commands", []),
        ("read_background_command_output", ["command_id"]),
        ("stop_background_command", ["command_id"]),
        ("write_file", ["path", "content"]),
        ("insert_after", ["path", "anchor", "content"]),
        ("replace_block", ["path", "old", "new"]),
        ("web_search", ["query"]),
        ("web_fetch", ["url"]),
        ("show_undo_records", []),
        ("undo_edit", []),
        ("show_memory_state", []),
    ],
)
def test_tool_schemas_declare_required_arguments(name, required):
    schema = _registered_schema_by_name(name)

    assert schema["type"] == "function"
    assert schema["function"]["parameters"]["required"] == required
    assert schema["function"]["parameters"]["additionalProperties"] is False


def test_read_file_tool_call_converts_to_internal_request():
    request = tool_schemas.tool_call_to_request(
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"app.py","start_line":10,"line_count":20,"max_chars":1000}',
            },
        }
    )

    assert request == {
        "tool": "read_file",
        "args": {"path": "app.py", "start_line": 10, "line_count": 20, "max_chars": 1000},
    }


def test_replace_block_tool_call_converts_to_internal_request():
    request = tool_schemas.tool_call_to_request(
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "replace_block",
                "arguments": '{"path":"hill_climb.html","old":"old code","new":"new code"}',
            },
        }
    )

    assert request == {
        "tool": "replace_block",
        "args": "hill_climb.html",
        "old_block": "old code",
        "old_block_closed": True,
        "new_block": "new code",
        "new_block_closed": True,
    }


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("list_files", '{"path":"src"}', {"tool": "list_files", "args": "src"}),
        (
            "read_file",
            '{"path":"data.json","start_char":100,"char_count":200,"full_read":false}',
            {"tool": "read_file", "args": {"path": "data.json", "start_char": 100, "char_count": 200, "full_read": False}},
        ),
        (
            "get_overview",
            '{"path":"large.txt","instruction":"Find the schema definitions.","max_chars":5000}',
            {"tool": "get_overview", "args": {"path": "large.txt", "instruction": "Find the schema definitions.", "max_chars": 5000}},
        ),
        (
            "list_images",
            '{"path":"screens","recursive":true}',
            {"tool": "list_images", "args": {"path": "screens", "recursive": True}},
        ),
        (
            "vision_analyze_images",
            '{"paths":["screens/1.png","screens/2.jpg"],"instruction":"Convert to Markdown."}',
            {
                "tool": "vision_analyze_images",
                "args": {
                    "paths": ["screens/1.png", "screens/2.jpg"],
                    "instruction": "Convert to Markdown.",
                },
            },
        ),
        ("run_command", '{"command":"python -m pytest"}', {"tool": "run_command", "args": "python -m pytest"}),
        (
            "start_background_command",
            '{"command":"npm run dev","cwd":"app"}',
            {"tool": "start_background_command", "args": {"command": "npm run dev", "cwd": "app"}},
        ),
        ("list_background_commands", '{}', {"tool": "list_background_commands", "args": ""}),
        (
            "read_background_command_output",
            '{"command_id":"abc123","max_chars":500}',
            {"tool": "read_background_command_output", "args": {"command_id": "abc123", "max_chars": 500}},
        ),
        ("stop_background_command", '{"command_id":"abc123"}', {"tool": "stop_background_command", "args": "abc123"}),
        (
            "write_file",
            '{"path":"app.py","content":"print(1)"}',
            {"tool": "write_file", "args": "app.py", "content": "print(1)", "content_closed": True},
        ),
        (
            "insert_after",
            '{"path":"app.py","anchor":"x = 1","content":"y = 2","anchor_context":"z = 3"}',
            {
                "tool": "insert_after",
                "args": "app.py",
                "anchor": "x = 1",
                "anchor_closed": True,
                "anchor_context": "z = 3",
                "content": "y = 2",
                "content_closed": True,
            },
        ),
        (
            "web_search",
            '{"query":"latest DeepSeek API","max_results":3}',
            {"tool": "web_search", "args": {"query": "latest DeepSeek API", "max_results": 3}},
        ),
        (
            "web_fetch",
            '{"url":"https://example.com/docs"}',
            {"tool": "web_fetch", "args": "https://example.com/docs"},
        ),
        ("show_undo_records", '{"limit":5}', {"tool": "show_undo_records", "args": 5}),
        ("undo_edit", '{}', {"tool": "undo_edit", "args": ""}),
        ("undo_edit", '{"record_id":"20260516-120000-abcd1234"}', {"tool": "undo_edit", "args": "20260516-120000-abcd1234"}),
        ("show_memory_state", '{}', {"tool": "show_memory_state", "args": ""}),
        ("memorize", '{"instruction":"Remember that I prefer concise answers."}', {"tool": "memorize", "args": {"instruction": "Remember that I prefer concise answers."}}),
    ],
)
def test_tool_calls_convert_to_internal_requests(name, arguments, expected):
    request = tool_schemas.tool_call_to_request(
        {
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments,
            },
        }
    )

    assert request == expected


def test_invalid_tool_call_json_raises_clear_error():
    with pytest.raises(RuntimeError, match="Invalid tool arguments JSON"):
        tool_schemas.tool_call_to_request(
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": "{bad json",
                },
            }
        )


def test_missing_required_argument_raises_clear_error():
    with pytest.raises(RuntimeError, match="replace_block requires string argument: new"):
        tool_schemas.tool_call_to_request(
            {
                "type": "function",
                "function": {
                    "name": "replace_block",
                    "arguments": '{"path":"app.py","old":"old"}',
                },
            }
        )
