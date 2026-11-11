from dataclasses import dataclass

def _string_property(description):
    return {
        "type": "string",
        "description": description,
    }


def _integer_property(description):
    return {
        "type": "integer",
        "description": description,
    }


def _boolean_property(description):
    return {
        "type": "boolean",
        "description": description,
    }


def _object_property(description):
    return {
        "type": "object",
        "description": description,
        "additionalProperties": True,
    }


def _string_array_property(description):
    return {
        "type": "array",
        "items": {"type": "string"},
        "description": description,
    }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    required: list
    risk: str
    executor: str = ""
    path_arg_keys: tuple = ()
    external_path_action: str = ""
    expose_to_model: bool = True

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


def _tool(name, description, parameters, required, risk, path_arg_keys=(), external_path_action="", executor="", expose_to_model=True):
    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        required=required,
        risk=risk,
        executor=executor or name,
        path_arg_keys=tuple(path_arg_keys),
        external_path_action=external_path_action,
        expose_to_model=expose_to_model,
    )



TOOL_DEFINITIONS = [
    _tool(
        "read_file",
        "Read a text file from the current workspace. Supports line or character ranges for large files. If a full read is too large, use a range or get_overview first. An exact absolute path outside the workspace requires user confirmation.",
        {
            "path": _string_property("Workspace-relative path or exact absolute external file path."),
            "start_line": _integer_property("Optional 1-based start line for a line-range read. Use with line_count. Do not combine with start_char/char_count."),
            "line_count": _integer_property("Optional number of lines to read. Defaults to 100 when start_line is provided."),
            "start_char": _integer_property("Optional 0-based character offset for a character-range read. Use for minified or single-line files. Do not combine with start_line/line_count."),
            "char_count": _integer_property("Optional number of characters to read. Defaults to the safe read limit when start_char is provided."),
            "max_chars": _integer_property("Optional maximum returned body characters. All reads are capped by this value or the default safe limit."),
            "full_read": _boolean_property("Optional. Set true only when a warn_large file must be read in full; too_large files still require ranged reading or get_overview."),
        },
        ["path"],
        "read_file",
        path_arg_keys=("args", "args.path"),
        external_path_action="read external file",
    ),
    _tool(
        "get_overview",
        "Get a bounded overview of a text file for navigation and triage. This is not source text and must not replace reading relevant raw ranges with read_file. Use instruction to describe what matters for the current task. An exact absolute path outside the workspace requires user confirmation.",
        {
            "path": _string_property("Workspace-relative path or exact absolute external file path."),
            "instruction": _string_property("Optional task-specific guidance for what the overview should help locate."),
            "max_chars": _integer_property("Optional maximum overview body characters."),
        },
        ["path"],
        "read_file",
        path_arg_keys=("args.path",),
        external_path_action="read external file",
    ),
    _tool(
        "list_files",
        "List files in a workspace directory, or in an exact absolute external directory after user confirmation.",
        {"path": _string_property("Workspace-relative directory or exact absolute external directory path. External paths require user confirmation.")},
        ["path"],
        "read_file",
        path_arg_keys=("args",),
        external_path_action="list external directory",
        expose_to_model=False,
    ),
    _tool(
        "list_images",
        "List supported image files in a workspace directory, or in an exact absolute external directory after user confirmation.",
        {
            "path": _string_property("Workspace-relative directory or exact absolute external directory path. External paths require user confirmation."),
            "recursive": _boolean_property("Optional. Whether to include images in nested subdirectories."),
        },
        ["path"],
        "read_file",
        path_arg_keys=("args.path",),
        external_path_action="read image directory",
        expose_to_model=False,
    ),
    _tool(
        "vision_analyze_images",
        "Analyze one batch of local images with the current vision-capable provider. Use this after list_images for screenshot-to-Markdown or image understanding tasks.",
        {
            "paths": _string_array_property("Workspace-relative image paths or exact absolute external image paths. External paths require user confirmation. Use small batches, ideally 3 to 5 images."),
            "instruction": _string_property("Specific instruction for how to analyze or convert the images."),
        },
        ["paths", "instruction"],
        "read_file",
        path_arg_keys=("args.paths",),
        external_path_action="read image files",
        expose_to_model=False,
    ),
    _tool(
        "run_command",
        "Run a finite command in the current workspace and wait for it to exit. Use start_background_command for long-running services or watchers. Do not use this tool to undo Sema-created edits; use undo_edit.",
        {"command": _string_property("A command expected to finish, such as a test, build, typecheck, or file listing.")},
        ["command"],
        "run_command",
    ),
    _tool(
        "start_background_command",
        "Start a long-running background command without blocking the current turn. Use this for development servers, watchers, HTTP servers, and similar services. After starting, use read_background_command_output to inspect it and stop_background_command when it is no longer needed.",
        {
            "command": _string_property("A long-running command to start, such as npm run dev."),
            "cwd": _string_property("Optional workspace-relative working directory."),
        },
        ["command"],
        "run_command",
    ),
    _tool("list_background_commands", "List background commands started by Sema in the current process.", {}, [], "safe_read"),
    _tool(
        "read_background_command_output",
        "Read the current status and recent output of a background command started by Sema.",
        {
            "command_id": _string_property("ID returned by start_background_command or list_background_commands."),
            "max_chars": _integer_property("Optional maximum output size."),
        },
        ["command_id"],
        "safe_read",
    ),
    _tool(
        "stop_background_command",
        "Stop a background command started by Sema.",
        {"command_id": _string_property("ID returned by start_background_command or list_background_commands.")},
        ["command_id"],
        "runtime_state",
    ),
    _tool(
        "write_file",
        "Replace a workspace file with complete new content. An exact absolute external path requires user confirmation.",
        {
            "path": _string_property("Workspace-relative path or exact absolute external file path."),
            "content": _string_property("Complete replacement file content."),
        },
        ["path", "content"],
        "edit_file",
        path_arg_keys=("args",),
        external_path_action="write external file",
    ),
    _tool(
        "insert_after",
        "Insert new content after one exact existing line in a workspace file. An exact absolute external path requires user confirmation.",
        {
            "path": _string_property("Workspace-relative path or exact absolute external file path."),
            "anchor": _string_property("Exact existing line to insert after."),
            "content": _string_property("New lines to insert after the anchor."),
            "anchor_context": _string_property("Optional line after the anchor used to disambiguate duplicate anchors."),
        },
        ["path", "anchor", "content"],
        "edit_file",
        path_arg_keys=("args",),
        external_path_action="edit external file",
    ),
    _tool(
        "replace_block",
        "Replace one exact block of text in a workspace file. An exact absolute external path requires user confirmation.",
        {
            "path": _string_property("Workspace-relative path or exact absolute external file path."),
            "old": _string_property("Exact existing text block to replace."),
            "new": _string_property("Replacement text block. Use an empty string to delete."),
        },
        ["path", "old", "new"],
        "edit_file",
        path_arg_keys=("args",),
        external_path_action="edit external file",
    ),
    _tool(
        "web_search",
        "Search the web for current or external information.",
        {
            "query": _string_property("Search query."),
            "max_results": _integer_property("Optional number of results."),
        },
        ["query"],
        "network_read",
    ),
    _tool(
        "web_fetch",
        "Read one specific web page.",
        {
            "url": _string_property("Absolute URL to read."),
        },
        ["url"],
        "network_read",
    ),
    _tool("show_undo_records", "Show recent file edits created by Sema that can be undone.", {"limit": _integer_property("Optional number of recent undo records to show.")}, [], "safe_read"),
    _tool("undo_edit", "Undo a Sema-created file edit after user confirmation.", {"record_id": _string_property("Optional ID from show_undo_records. Omit to undo the latest edit.")}, [], "undo"),
    _tool(
        "show_memory_state",
        "Show a metadata-only Memory Engine status snapshot when the user explicitly asks about memory state. This tool cannot retrieve or verify memory content and must not accompany ordinary recall.",
        {},
        [],
        "safe_read",
        expose_to_model=False,
    ),
    _tool(
        "get_object",
        "Use this tool for simple, direct Long-Term Memory recall when the target is clear and likely to be found quickly. If the memory is uncertain, broad, requires connecting multiple memories, or needs continued exploration, use retrieve_memory instead.",
        {
            "object_id": _string_property("One exact MemoryObject ID."),
            "object_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 50, "description": "Exact MemoryObject IDs; omit object_id when using this array."},
            "title": _string_property("Literal case-insensitive title substring."),
            "query": _string_property("Plain-text keyword query across indexed object text; this is lexical, not embedding search."),
            "min_strength": {"type": "number", "minimum": 0, "maximum": 1, "description": "Stored derived-strength lower bound."},
            "max_strength": {"type": "number", "minimum": 0, "maximum": 1, "description": "Stored derived-strength upper bound."},
            "linked_to_object_id": _string_property("Return objects connected to this object by an active Link."),
            "tag_object_ids": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 50, "description": "Objects tagged with any of these tag IDs."},
            "source_slot_after": {"type": "integer", "minimum": 0, "description": "Inclusive source-slot overlap lower bound."},
            "source_slot_before": {"type": "integer", "minimum": 0, "description": "Inclusive source-slot overlap upper bound."},
            "projection": {"type": "string", "enum": ["title_summary", "core", "payload", "full", "bundle"], "description": "Default core; use payload/full to verify body or structured data."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum objects; payload/full are bounded more tightly and bundle returns one object."},
        },
        [],
        "safe_read",
    ),
    _tool(
        "retrieve_memory",
        "Use this tool when recalling a memory is uncertain, complex, requires connecting multiple memories, or needs continued exploration. The tool may return running while retrieval continues in the background. Do not poll by immediately calling it again; later results are delivered automatically. If it requests a clue or context, continue with continuation_id and message, or use include_context=true. To close a Session, call it with continuation_id and close=true only. Do not include query, message, or include_context in a close call.",
        {
            "query": _string_property("New immutable memory search query. Omit when continuing an existing Session."),
            "continuation_id": _string_property("Opaque continuation handle returned by a previous Retrieval checkpoint."),
            "message": _string_property("Optional natural-language clue, correction, search guidance, or feedback. After a technical checkpoint, send a new message to continue; an identical request replays its cached result."),
            "include_context": _boolean_property("Optional. Capture the current provider Context Snapshot for this request."),
            "close": _boolean_property("Optional. Close an existing Retrieval Session; requires continuation_id. Set true only for an explicit close call; omit message and include_context."),
        },
        [],
        "safe_read",
    ),
    _tool(
        "memorize",
        "Create a new durable long-term memory only when the user explicitly asks Sema to remember or save information.",
        {
            "instruction": _string_property("The user's explicit memory-formation instruction."),
        },
        ["instruction"],
        "memory_write",
    ),
    _tool("sleep_for_maintenance", "Pause this task for LTM maintenance. Available only from pressure 0.5. Provide a wake_prompt containing the goal, exact progress, and next step. This must be the response's only tool call.", {"wake_prompt": _string_property("Required task continuation pointer: goal, exact progress, and next action after maintenance.")}, ["wake_prompt"], "runtime_state", expose_to_model=False),
    _tool("retry_ltm_maintenance", "Retry the suspended LTM maintenance run, reusing its wake_prompt and run ID.", {}, [], "runtime_state", expose_to_model=False),
    _tool("return_maintenance_failure", "Explain the maintenance failure to the user and keep the original task paused.", {"message": _string_property("Concise user-facing explanation of the maintenance failure and next options.")}, ["message"], "runtime_state", expose_to_model=False),
]


TOOL_DEFINITION_BY_NAME = {definition.name: definition for definition in TOOL_DEFINITIONS}
def provider_tool_definitions():
    return [definition for definition in TOOL_DEFINITIONS if definition.expose_to_model]


TOOL_SCHEMAS = [definition.schema() for definition in provider_tool_definitions()]


def tool_schema_names():
    return [definition.name for definition in provider_tool_definitions()]


def get_tool_definition(name):
    return TOOL_DEFINITION_BY_NAME.get(name)


def registered_tool_names():
    return set(TOOL_DEFINITION_BY_NAME)
