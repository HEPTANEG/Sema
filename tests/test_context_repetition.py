from my_agent.runtime.context_repetition import ContextRepetitionDetector, canonicalize_model_output


def output(query="same", text="Let me check"):
    return {
        "content": text,
        "tool_calls": [{"id": "call-1", "function": {"name": "get_object", "arguments": {"query": query}}}],
    }


def distinct_output():
    return {
        "content": "I will inspect the link topology before deciding whether the stage is complete.",
        "tool_calls": [{
            "id": "call-2",
            "function": {
                "name": "get_links",
                "arguments": {"endpoint_object_ids": ["mem-a", "mem-b"], "include_inactive": True, "limit": 50},
            },
        }],
    }


def test_canonicalization_normalizes_unicode_whitespace_and_object_order():
    left = {"content": "A\r\n  B", "values": {"b": 2, "a": 1}}
    right = {"values": {"a": 1, "b": 2}, "content": "A B"}

    assert canonicalize_model_output(left) == canonicalize_model_output(right)


def test_detector_warns_at_three_five_and_eight_and_not_again_in_cluster():
    detector = ContextRepetitionDetector()
    signals = [detector.observe(output(), index, stage="recent_review") for index in range(1, 11)]

    assert [(signal.threshold, signal.repeated_rounds) for signal in signals if signal] == [(3, 3), (5, 5), (8, 8)]
    assert detector.cluster_rounds == tuple(range(1, 11))


def test_detector_keeps_only_the_latest_ten_rounds():
    detector = ContextRepetitionDetector()
    for index in range(1, 13):
        detector.observe(output(), index, stage="recent_review")

    assert [item.model_round for item in detector.window] == list(range(3, 13))
    assert detector.cluster_rounds == tuple(range(3, 13))


def test_detector_resets_cluster_on_different_output_and_allows_new_thresholds():
    detector = ContextRepetitionDetector()
    for index in range(1, 4):
        detector.observe(output(), index, stage="recent_review")
    assert detector.observe(distinct_output(), 4, stage="recent_review") is None
    assert detector.observe(distinct_output(), 5, stage="recent_review") is None
    signal = detector.observe(distinct_output(), 6, stage="recent_review")

    assert signal is not None
    assert signal.threshold == 3
    assert signal.matched_rounds == (4, 5, 6)


def test_detector_does_not_combine_rounds_across_stage_boundary():
    detector = ContextRepetitionDetector()
    detector.observe(output(), 1, stage="recent_review")
    detector.observe(output(), 2, stage="recent_review")
    detector.mark_stage_boundary("global_duplicate_review")

    assert detector.observe(output(), 3, stage="global_duplicate_review") is None
    assert detector.observe(output(), 4, stage="global_duplicate_review") is None
    signal = detector.observe(output(), 5, stage="global_duplicate_review")

    assert signal is not None
    assert signal.threshold == 3
    assert signal.matched_rounds == (3, 4, 5)


def test_detector_handles_small_argument_rewrite_as_near_duplicate():
    detector = ContextRepetitionDetector(minhash_similarity=0.6)
    detector.observe(output("same"), 1, stage="recent_review")
    signal = detector.observe(output("same", text="Let me check again"), 2, stage="recent_review")

    assert signal is None
    assert detector.cluster_rounds == (1, 2)


def test_detector_treats_array_reordering_as_a_near_duplicate():
    left = output()
    left["tool_calls"][0]["function"]["arguments"] = {"object_ids": ["mem-a", "mem-b", "mem-c"]}
    right = output()
    right["tool_calls"][0]["function"]["arguments"] = {"object_ids": ["mem-c", "mem-b", "mem-a"]}

    detector = ContextRepetitionDetector()
    detector.observe(left, 1, stage="recent_review")
    detector.observe(right, 2, stage="recent_review")

    assert detector.cluster_rounds == (1, 2)


def test_detector_does_not_combine_alternating_outputs():
    detector = ContextRepetitionDetector()
    for index in range(1, 7):
        signal = detector.observe(output() if index % 2 else distinct_output(), index, stage="recent_review")

    assert signal is None
    assert detector.cluster_rounds == (6,)
