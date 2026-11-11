import json


def _assistant_call(*call_ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for call_id in call_ids
        ],
    }


def test_small_tool_result_passes_unchanged(isolated_workspace):
    from memory_engine.tool_ingress import guard_tool_result
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    content = "small result"

    result = guard_tool_result("run_command", content, "call-1", [_assistant_call("call-1")])

    assert result["status"] == "ok"
    assert result["content"] == content
    assert result["artifact_id"] is None


def test_large_file_result_becomes_artifact_and_bounded_envelope(isolated_workspace):
    import memory_engine.context_budget_override as overrides
    import memory_engine.event_stream as event_stream
    from memory_engine.tool_ingress import guard_tool_result
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    overrides.set_budget_override("stm", 2000)
    content = "large file content " * 4000

    result = guard_tool_result("read_file", content, "call-1", [_assistant_call("call-1")])
    envelope = json.loads(result["content"])
    artifact = event_stream.load_artifact(result["artifact_id"])

    assert result["status"] == "too_large"
    assert artifact["content"] == content
    assert envelope["artifact_id"] == result["artifact_id"]
    assert envelope["output_chars"] == len(content)
    assert any("start_line" in action for action in envelope["available_actions"])
    assert content not in result["content"]


def test_multi_tool_bundle_guard_uses_cumulative_results(isolated_workspace):
    import memory_engine.context_budget_override as overrides
    from memory_engine.tool_ingress import guard_tool_result
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    overrides.set_budget_override("stm", 2000)
    messages = [_assistant_call("call-1", "call-2")]
    first_content = "first " * 1100
    first = guard_tool_result("run_command", first_content, "call-1", messages)
    messages.append({"role": "tool", "tool_call_id": "call-1", "content": first["content"]})

    second = guard_tool_result("run_command", "second " * 1100, "call-2", messages)

    assert first["status"] == "ok"
    assert second["status"] == "too_large"


def test_artifact_failure_never_returns_full_oversized_body(isolated_workspace, monkeypatch):
    import memory_engine.context_budget_override as overrides
    import memory_engine.tool_ingress as tool_ingress
    from persistence import session

    session.ensure_project()
    session.save_last_session([{"role": "system", "content": "system"}])
    overrides.set_budget_override("stm", 2000)
    content = "secret oversized output " * 4000
    monkeypatch.setattr(tool_ingress.event_stream, "store_artifact", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    result = tool_ingress.guard_tool_result("run_command", content, "call-1", [_assistant_call("call-1")])

    assert result["status"] == "too_large_storage_failed"
    assert result["artifact_id"] is None
    assert content not in result["content"]
    assert "disk full" not in result["content"]
