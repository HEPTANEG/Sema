def blocked_git_undo_message(command):
    return (
        f"Command blocked: {command}\n"
        "Reason: model-requested git checkout/restore/reset/clean can overwrite files. "
        "For undo/revert requests, use undo_edit first. If the user explicitly wants this Git command, "
        "ask them to run it with /run."
    )


def cancellation_message(message, reason):
    if reason:
        return f"{message}\nReason: {reason}"
    return message


def build_structured_tool_result_message(tool_result, is_write_success):
    if is_write_success:
        return (
            f"{tool_result}\n\n"
            "Instruction: the edit/write tool succeeded. Unless the original user request "
            "explicitly requires more tool work, give the final answer now and do not request "
            "another tool."
        )
    return tool_result


def format_tool_error(tool_request, error):

    message = f"Tool error: {error}"
    if tool_request.get("tool") == "apply_patch" and "patch does not apply" in str(error):
        message += (
            "\n\nRecovery hint: read the current file and retry with a structured edit. "
            "Use insert_after for pure insertions, or replace_block for local replacements, "
            "deletions, and moving nearby lines."
        )
    return message
