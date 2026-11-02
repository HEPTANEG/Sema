"""Compatibility filename retained while the old Recall tests are migrated."""

import json


def test_retrieve_observer_snapshot_is_metadata_only():
    from memory_engine.ltm import memory_retrieval
    from memory_engine import observer

    runtime_state = {"memory_engine_retrieval_observer": {"retrieval_request_count": 1, "last_result": {"kind": "checkpoint", "query": "secret"}}}
    snapshot = observer.memory_engine_snapshot(runtime_state=runtime_state)
    rendered = json.dumps(snapshot["retrieval"])
    assert snapshot["retrieval"]["retrieval_request_count"] == 1
    assert "secret" not in rendered
    assert memory_retrieval.retrieval_observer_snapshot(runtime_state)["retrieval_request_count"] == 1


def test_retrieve_trace_argument_summary_redacts_query():
    from persistence.observability import summarize_args

    summary = summarize_args("retrieve_memory", {"query": "private preference", "include_context": True})
    rendered = json.dumps(summary)
    assert summary["has_query"] is True
    assert summary["include_context"] is True
    assert "private preference" not in rendered
