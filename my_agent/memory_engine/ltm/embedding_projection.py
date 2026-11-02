"""Deterministic text projections for MemoryObject embeddings."""

import json
from collections.abc import Mapping


EMBEDDING_PROJECTION_VERSION = "memory_object_v1"


def _text(value, field_name):
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value.strip()


def build_embedding_projection(memory_object):
    """Build one stable semantic text projection for a MemoryObject.

    The function is intentionally storage- and service-free. Lifecycle filtering
    belongs to the caller that decides which objects enter a vector index.
    """
    if not isinstance(memory_object, Mapping):
        raise ValueError("memory_object must be a mapping")

    title = _text(memory_object.get("title"), "title")
    if not title:
        raise ValueError("title must be non-empty")

    parts = [title]
    summary = memory_object.get("summary")
    if summary is not None:
        summary = _text(summary, "summary")
        if summary:
            parts.append(summary)

    payload = memory_object.get("payload")
    if payload is not None:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        body = payload.get("body")
        if body is not None:
            body = _text(body, "payload.body")
            if body:
                parts.append(body)

        structured_data = payload.get("structured_data")
        if structured_data is not None:
            if not isinstance(structured_data, Mapping):
                raise ValueError("payload.structured_data must be an object")
            if structured_data:
                parts.append(
                    json.dumps(
                        structured_data,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )

    return "\n\n".join(parts)
