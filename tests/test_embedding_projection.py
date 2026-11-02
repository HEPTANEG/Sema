import pytest

from memory_engine.ltm.embedding_projection import (
    EMBEDDING_PROJECTION_VERSION,
    build_embedding_projection,
)


def test_projection_has_stable_content_order():
    memory_object = {
        "object_id": "mem_ignored",
        "object_type": "episode",
        "status": "active",
        "title": "  Episode title  ",
        "summary": "Episode summary",
        "payload": {
            "body": "Episode body",
            "structured_data": {"time_scope": {"end": "2026-01-02", "start": "2026-01-01"}},
        },
    }

    assert build_embedding_projection(memory_object) == (
        "Episode title\n\nEpisode summary\n\nEpisode body\n\n"
        '{"time_scope":{"end":"2026-01-02","start":"2026-01-01"}}'
    )
    assert EMBEDDING_PROJECTION_VERSION == "memory_object_v1"


def test_projection_skips_missing_optional_content():
    assert build_embedding_projection({"title": "Tag title"}) == "Tag title"
    assert build_embedding_projection(
        {"title": "Tag title", "summary": "  ", "payload": {"body": ""}}
    ) == "Tag title"


def test_projection_is_independent_of_metadata_and_does_not_mutate_input():
    memory_object = {
        "object_id": "mem_original",
        "status": "active",
        "title": "Title",
        "payload": {"body": "Body", "structured_data": {"b": 2, "a": 1}},
    }
    original = {**memory_object, "payload": dict(memory_object["payload"])}

    assert build_embedding_projection(memory_object) == 'Title\n\nBody\n\n{"a":1,"b":2}'
    assert memory_object == original


@pytest.mark.parametrize(
    "memory_object, message",
    [
        ({}, "title must be a string"),
        ({"title": ""}, "title must be non-empty"),
        ({"title": "Title", "payload": {"structured_data": []}}, "payload.structured_data must be an object"),
    ],
)
def test_projection_rejects_invalid_content(memory_object, message):
    with pytest.raises(ValueError, match=message):
        build_embedding_projection(memory_object)
