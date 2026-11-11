import json

from tools.tool_registry import TOOL_SCHEMAS, tool_schema_names


def tool_call_to_request(tool_call):

    function = tool_call.get("function") or {}
    name = function.get("name")

    arguments = parse_tool_arguments(function.get("arguments", "{}"))

    if name == "read_file":
        args = {"path": require_string(arguments, "path", name)}
        for key in ("start_line", "line_count", "start_char", "char_count", "max_chars"):
            value = optional_integer(arguments, key, name)
            if value is not None:
                args[key] = value
        value = optional_boolean(arguments, "full_read", name)
        if value is not None:
            args["full_read"] = value
        return {"tool": "read_file", "args": args}

    if name == "get_overview":
        args = {
            "path": require_string(arguments, "path", name),
            "instruction": optional_string(arguments, "instruction", name),
        }
        value = optional_integer(arguments, "max_chars", name)
        if value is not None:
            args["max_chars"] = value
        return {"tool": "get_overview", "args": args}

    if name == "list_files":
        return {
            "tool": "list_files",
            "args": require_string(arguments, "path", name),
        }

    if name == "list_images":
        return {
            "tool": "list_images",
            "args": {
                "path": require_string(arguments, "path", name),
                "recursive": optional_boolean(arguments, "recursive", name),
            },
        }

    if name == "vision_analyze_images":
        return {
            "tool": "vision_analyze_images",
            "args": {
                "paths": require_string_list(arguments, "paths", name),
                "instruction": require_string(arguments, "instruction", name),
            },
        }

    if name == "run_command":
        return {
            "tool": "run_command",
            "args": require_string(arguments, "command", name),
        }

    if name == "start_background_command":
        return {
            "tool": "start_background_command",
            "args": {
                "command": require_string(arguments, "command", name),
                "cwd": optional_string(arguments, "cwd", name),
            },
        }

    if name == "list_background_commands":
        return {
            "tool": "list_background_commands",
            "args": "",
        }

    if name == "read_background_command_output":
        return {
            "tool": "read_background_command_output",
            "args": {
                "command_id": require_string(arguments, "command_id", name),
                "max_chars": optional_integer(arguments, "max_chars", name),
            },
        }

    if name == "stop_background_command":
        return {
            "tool": "stop_background_command",
            "args": require_string(arguments, "command_id", name),
        }

    if name == "write_file":
        return {
            "tool": "write_file",
            "args": require_string(arguments, "path", name),
            "content": require_string(arguments, "content", name),
            "content_closed": True,
        }

    if name == "insert_after":
        return {
            "tool": "insert_after",
            "args": require_string(arguments, "path", name),
            "anchor": require_string(arguments, "anchor", name),
            "anchor_closed": True,
            "anchor_context": optional_string(arguments, "anchor_context", name),
            "content": require_string(arguments, "content", name),
            "content_closed": True,
        }

    if name == "replace_block":
        return {
            "tool": "replace_block",
            "args": require_string(arguments, "path", name),
            "old_block": require_string(arguments, "old", name),
            "old_block_closed": True,
            "new_block": require_string(arguments, "new", name),
            "new_block_closed": True,
        }

    if name == "web_search":
        return {
            "tool": "web_search",
            "args": {
                "query": require_string(arguments, "query", name),
                "max_results": optional_integer(arguments, "max_results", name),
            },
        }

    if name == "web_fetch":
        return {
            "tool": "web_fetch",
            "args": require_string(arguments, "url", name),
        }

    if name == "show_undo_records":
        return {
            "tool": "show_undo_records",
            "args": optional_integer(arguments, "limit", name),
        }

    if name == "undo_edit":
        return {
            "tool": "undo_edit",
            "args": optional_string(arguments, "record_id", name) or "",
        }

    if name == "show_memory_state":
        return {
            "tool": "show_memory_state",
            "args": "",
        }

    if name == "get_object":
        string_keys = ("object_id", "title", "query", "linked_to_object_id")
        list_keys = ("object_ids", "tag_object_ids")
        number_keys = ("min_strength", "max_strength")
        integer_keys = ("source_slot_after", "source_slot_before", "limit")
        args = {}
        for key in string_keys:
            value = optional_string(arguments, key, name)
            if value is not None:
                args[key] = value
        for key in list_keys:
            value = optional_string_list(arguments, key, name)
            if value is not None:
                args[key] = value
        for key in number_keys:
            value = optional_number(arguments, key, name)
            if value is not None:
                args[key] = value
        value = optional_string(arguments, "projection", name)
        if value is not None:
            args["projection"] = value
        for key in integer_keys:
            value = optional_integer(arguments, key, name)
            if value is not None:
                args[key] = value
        return {"tool": "get_object", "args": args}

    if name == "retrieve_memory":
        return {
            "tool": "retrieve_memory",
            "args": {
                "query": optional_string(arguments, "query", name),
                "continuation_id": optional_string(arguments, "continuation_id", name),
                "message": optional_string(arguments, "message", name),
                "include_context": optional_boolean(arguments, "include_context", name),
                "close": optional_boolean(arguments, "close", name),
            },
        }

    if name == "memorize":
        return {
            "tool": "memorize",
            "args": {
                "instruction": require_string(arguments, "instruction", name),
            },
        }

    if name == "sleep_for_maintenance":
        return {
            "tool": "sleep_for_maintenance",
            "args": {
                "wake_prompt": require_string(arguments, "wake_prompt", name),
            },
        }

    if name == "retry_ltm_maintenance":
        return {"tool": "retry_ltm_maintenance", "args": {}}

    if name == "return_maintenance_failure":
        return {
            "tool": "return_maintenance_failure",
            "args": {
                "message": require_string(arguments, "message", name),
            },
        }

    raise RuntimeError(f"Unknown structured tool call: {name}")


def parse_tool_arguments(arguments_text):
    try:
        arguments = json.loads(arguments_text or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Invalid tool arguments JSON: {error.msg}") from error

    if not isinstance(arguments, dict):
        raise RuntimeError("Tool arguments must be a JSON object.")

    return arguments


def require_string(arguments, key, tool_name):
    value = arguments.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"{tool_name} requires string argument: {key}")
    return value


def optional_string(arguments, key, tool_name):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"{tool_name} requires string argument: {key}")
    return value


def optional_integer(arguments, key, tool_name):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise RuntimeError(f"{tool_name} requires integer argument: {key}")
    return value


def optional_number(arguments, key, tool_name):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise RuntimeError(f"{tool_name} requires number argument: {key}")
    return value


def optional_boolean(arguments, key, tool_name):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RuntimeError(f"{tool_name} requires boolean argument: {key}")
    return value


def require_string_list(arguments, key, tool_name):
    value = arguments.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{tool_name} requires string array argument: {key}")
    return value


def optional_string_list(arguments, key, tool_name):
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"{tool_name} requires string array argument: {key}")
    return value


def require_object(arguments, key, tool_name):
    value = arguments.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"{tool_name} requires object argument: {key}")
    return value
