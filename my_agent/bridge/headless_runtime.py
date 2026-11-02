import contextlib
import threading

from runtime import agent
from model import provider as model_provider


def hard_timeout_call_chat_completion(call_chat_completion, timeout_seconds, label="TUI"):
    timeout_limit = int(timeout_seconds)

    def wrapped(messages, tools=None, temperature=None, timeout_seconds=None, purpose="model", **kwargs):
        effective_timeout = min(int(timeout_seconds or timeout_limit), timeout_limit)
        result = {}
        finished = threading.Event()

        def worker():
            try:
                result["data"] = call_chat_completion(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    timeout_seconds=effective_timeout,
                    purpose=purpose,
                    **kwargs,
                )
            except Exception as error:
                result["error"] = error
            finally:
                finished.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if not finished.wait(effective_timeout):
            raise RuntimeError(f"{label} {purpose} request timed out after {effective_timeout} seconds.")
        if "error" in result:
            raise result["error"]
        return result.get("data")

    return wrapped


def _confirm_persistent(policy, action, details, preview=""):
    try:
        return policy.confirm_persistent_action(action, details, preview)
    except TypeError:
        return policy.confirm_persistent_action(action, details)


@contextlib.contextmanager
def patch_agent_for_headless(
    policy,
    timeout_seconds=None,
    max_model_retries=None,
):
    originals = {
        "agent_call_chat_completion": agent.call_chat_completion,
        "provider_call_chat_completion": model_provider.call_chat_completion,
        "MODEL_TIMEOUT_SECONDS": agent.MODEL_TIMEOUT_SECONDS,
        "MODEL_TIMEOUT_SECONDS": agent.MODEL_TIMEOUT_SECONDS,
        "MAX_MODEL_RETRIES_PER_FAILURE_POINT": agent.MAX_MODEL_RETRIES_PER_FAILURE_POINT,
        "confirm_write": agent.confirm_write,
        "confirm_patch": agent.confirm_patch,
        "confirm_insert": agent.confirm_insert,
        "confirm_replace": agent.confirm_replace,
        "confirm_run_command": agent.confirm_run_command,
        "confirm_file_mutating_command": agent.confirm_file_mutating_command,
        "confirm_external_path_access": agent.confirm_external_path_access,
        "confirm_session_control": agent.confirm_session_control,
        "confirm_undo_edit": agent.confirm_undo_edit,
        "confirm_tool_checkpoint": agent.confirm_tool_checkpoint,
    }
    try:
        if timeout_seconds is not None:
            hard_timeout_call = hard_timeout_call_chat_completion(agent.call_chat_completion, timeout_seconds)
            agent.call_chat_completion = hard_timeout_call
            model_provider.call_chat_completion = hard_timeout_call_chat_completion(
                model_provider.call_chat_completion, timeout_seconds
            )
            agent.MODEL_TIMEOUT_SECONDS = int(timeout_seconds)
            agent.MODEL_TIMEOUT_SECONDS = int(timeout_seconds)
        if max_model_retries is not None:
            agent.MAX_MODEL_RETRIES_PER_FAILURE_POINT = int(max_model_retries)

        agent.confirm_write = policy.confirm_write
        agent.confirm_patch = policy.confirm_patch
        agent.confirm_insert = policy.confirm_insert
        agent.confirm_replace = policy.confirm_replace
        agent.confirm_run_command = policy.confirm_run_command
        agent.confirm_file_mutating_command = policy.confirm_file_mutating_command
        agent.confirm_external_path_access = policy.confirm_external_path_access
        agent.confirm_session_control = policy.confirm_session_control
        agent.confirm_undo_edit = policy.confirm_undo_edit
        agent.confirm_tool_checkpoint = policy.confirm_tool_checkpoint
        yield
    finally:
        agent.call_chat_completion = originals["agent_call_chat_completion"]
        model_provider.call_chat_completion = originals["provider_call_chat_completion"]
        for name, value in originals.items():
            if name.endswith("_call_chat_completion"):
                continue
            setattr(agent, name, value)
