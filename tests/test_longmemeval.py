import json


def test_longmemeval_dataset_selection_is_deterministic(tmp_path):
    from sema_eval.benchmarks.longmemeval import load_dataset, select_entries

    path = tmp_path / "oracle.json"
    path.write_text(
        json.dumps(
            [
                {"question_id": "q1", "question": "one"},
                {"question_id": "q2", "question": "two"},
                {"question_id": "q3", "question": "three"},
            ]
        ),
        encoding="utf-8",
    )

    entries = load_dataset(path)
    assert [entry["question_id"] for entry in select_entries(entries, 2)] == ["q1", "q2"]
    assert [entry["question_id"] for entry in select_entries(entries, 2, "q3")] == ["q3"]


def test_history_import_strips_benchmark_fields_and_preserves_event_time(isolated_workspace):
    from eval.eval_bridge import _append_history_session
    from memory_engine import event_stream
    from runtime import agent
    from persistence import session

    messages = agent.create_initial_messages()
    session.ensure_project()
    session.save_last_session(messages)
    seeded = _append_history_session(
        messages,
        [{"role": "user", "content": "remember this", "has_answer": True}],
        "2023-01-10T12:00:00+00:00",
    )

    assert seeded[-1] == {"role": "user", "content": "remember this"}
    assert "has_answer" not in event_stream.load_message(1)
    assert event_stream.load_slot(1)["created_at"] == "2023-01-10T12:00:00+00:00"


def test_longmemeval_metrics_include_type_and_abstention():
    from sema_eval.benchmarks.longmemeval import calculate_metrics

    metrics = calculate_metrics(
        [
            {"question_id": "q1", "question_type": "multi-session", "status": "SCORED", "label": True},
            {"question_id": "q2_abs", "question_type": "single-session-user", "status": "SCORED", "label": False},
            {"question_id": "q3", "question_type": "multi-session", "status": "SCORED", "label": False},
            {"question_id": "q4", "question_type": "temporal-reasoning", "status": "INCONCLUSIVE"},
        ]
    )

    assert metrics["overall_accuracy"] == 1 / 3
    assert metrics["abstention_accuracy"] == 0.0
    assert metrics["by_question_type"]["multi-session"] == 0.5


def test_longmemeval_prompt_keeps_question_and_answer_on_evaluator_side():
    from sema_eval.benchmarks.longmemeval import build_official_answer_check_prompt

    prompt = build_official_answer_check_prompt(
        "knowledge-update",
        "What changed?",
        "The answer changed to B.",
        "It changed to B.",
    )

    assert "What changed?" in prompt
    assert "The answer changed to B." in prompt
    assert "It changed to B." in prompt


def test_empty_longmemeval_judge_response_is_inconclusive(monkeypatch):
    from sema_eval import runner
    from sema_eval.benchmarks.longmemeval import evaluate_predictions

    monkeypatch.setenv("EVAL_JUDGE_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_BASE_URL", "https://judge.example/v1")
    monkeypatch.setattr(
        runner,
        "_request_openai_compatible_judge",
        lambda *args, **kwargs: {"status": "ok", "text": "", "raw_response": {"choices": []}},
    )

    evaluation = evaluate_predictions(
        [{"question_id": "q1", "hypothesis": "answer"}],
        [{"question_id": "q1", "question": "question", "answer": "answer", "question_type": "temporal-reasoning"}],
    )

    item = evaluation["items"][0]
    assert item["status"] == "INCONCLUSIVE"
    assert item["category"] == "judge_parse_error"
    assert evaluation["inconclusive_count"] == 1
    assert evaluation["metrics"]["overall_accuracy"] is None


def test_longmemeval_judge_default_budget_is_raised(monkeypatch):
    from sema_eval import runner
    from sema_eval.benchmarks.longmemeval import evaluate_predictions

    monkeypatch.setenv("EVAL_JUDGE_API_KEY", "test-key")
    monkeypatch.setenv("EVAL_JUDGE_BASE_URL", "https://judge.example/v1")
    monkeypatch.delenv("EVAL_JUDGE_MAX_TOKENS", raising=False)
    captured = {}

    def fake_judge(*args, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return {"status": "ok", "text": "yes", "raw_response": {}}

    monkeypatch.setattr(runner, "_request_openai_compatible_judge", fake_judge)
    evaluate_predictions(
        [{"question_id": "q1", "hypothesis": "answer"}],
        [{"question_id": "q1", "question": "question", "answer": "answer", "question_type": "temporal-reasoning"}],
    )

    assert captured["max_tokens"] == 4096
