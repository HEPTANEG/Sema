"""Call-scoped snapshots of the provider context visible to the Main Agent."""

import copy


def capture_context_snapshot(provider_messages):
    """Return an isolated copy without adding identity or persistence metadata."""
    if not isinstance(provider_messages, list):
        raise RuntimeError("retrieval_context_unavailable: provider context is unavailable.")
    return tuple(copy.deepcopy(provider_messages))


def context_snapshot_for_agent(snapshot):
    """Apply the fixed V5 Retrieval Agent context filter."""
    return [copy.deepcopy(message) for message in (snapshot or ()) if message.get("role") != "system"]
