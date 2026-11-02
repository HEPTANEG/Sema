from pathlib import Path

SEMANTIC_SCORE_THRESHOLD = 6
MAX_SCORE = 10.0


def grade_eval(task, result):
    checks = []
    warnings = []
    expected = task.get("expected") or {}

    checks.extend(_grade_final_answer_contains(expected, result))
    checks.extend(_grade_required_tools(expected, result))
    checks.extend(_grade_forbidden_tools(expected, result))
    checks.extend(_grade_hard_max_tool_calls(expected, result))
    checks.extend(_grade_file_contains(expected, result))
    checks.extend(_grade_file_not_contains(expected, result))
    checks.extend(_grade_objective_commands(expected, result))
    checks.extend(_grade_required_events(expected, result))
    checks.extend(_grade_raw_conversations(expected, result))
    checks.extend(_grade_memory_engine(expected, result))
    checks.extend(_grade_semantic_assertions(expected, result))
    checks.extend(_grade_scenario_judge(task, result))
    warnings.extend(_grade_max_tool_calls_warning(expected, result))

    inconclusive = any(_check_is_inconclusive(check) for check in checks)
    passed = (not inconclusive) and all(check["passed"] for check in checks)
    scores = _grade_scores(checks, warnings, result)
    return {
        "passed": passed,
        "inconclusive": inconclusive,
        "checks": checks,
        "warnings": warnings,
        "scores": scores,
    }


def _check(name, passed, detail="", category="product_issue"):
    return {
        "name": name,
        "passed": bool(passed),
        "detail": detail,
        "category": category,
    }


def _check_is_inconclusive(check):
    if check.get("passed"):
        return False
    if check.get("category") in {"judge_parse_error", "judge_reasoning_exhausted"}:
        return True
    return check.get("name") in {"semantic_judge", "semantic:scenario_judge"} and check.get("category") == "environment_issue"


def _warning(name, detail=""):
    return {
        "name": name,
        "detail": detail,
    }


def _final_answer(result):
    for message in reversed(result.get("messages") or []):
        if message.get("role") == "assistant":
            return message.get("content") or ""
    return ""


def _tool_steps(result):
    return result.get("tool_steps") or []


def _tool_names(result):
    return [step.get("tool") for step in _tool_steps(result)]


def _grade_final_answer_contains(expected, result):
    fragments = expected.get("final_answer_contains") or []
    if not fragments:
        return []

    answer = _final_answer(result)
    checks = []
    for fragment in fragments:
        checks.append(
            _check(
                f"final_answer_contains:{fragment}",
                fragment.lower() in answer.lower(),
                "fragment missing from final answer",
                category="eval_issue",
            )
        )
    return checks


def _grade_required_tools(expected, result):
    required = expected.get("required_tools") or []
    if not required:
        return []

    names = _tool_names(result)
    checks = []
    if isinstance(required, dict):
        checks.extend(_grade_required_any_of(required.get("any_of") or [], names))
        checks.extend(_grade_required_sequence(required.get("sequence_contains") or [], names))
        required = required.get("all_of") or []

    checks.extend(
        _check(
            f"required_tool:{tool}",
            tool in names,
            f"tool calls: {names}",
        )
        for tool in required
    )
    return checks


def _grade_required_any_of(groups, names):
    checks = []
    for index, group in enumerate(groups, start=1):
        checks.append(
            _check(
                f"required_tools.any_of:{index}",
                any(tool in names for tool in group),
                f"expected one of {group}; tool calls: {names}",
            )
        )
    return checks


def _grade_required_sequence(sequences, names):
    checks = []
    for index, sequence in enumerate(sequences, start=1):
        checks.append(
            _check(
                f"required_tools.sequence_contains:{index}",
                _contains_ordered_subsequence(names, sequence),
                f"expected sequence {sequence}; tool calls: {names}",
            )
        )
    return checks


def _contains_ordered_subsequence(names, sequence):
    position = 0
    for name in names:
        if position < len(sequence) and name == sequence[position]:
            position += 1
    return position == len(sequence)


def _grade_forbidden_tools(expected, result):
    forbidden = expected.get("forbidden_tools") or []
    if not forbidden:
        return []

    names = _tool_names(result)
    return [
        _check(
            f"forbidden_tool:{tool}",
            tool not in names,
            f"tool calls: {names}",
        )
        for tool in forbidden
    ]


def _grade_max_tool_calls_warning(expected, result):
    limit = expected.get("max_tool_calls")
    if limit is None:
        return []

    count = len(_tool_steps(result))
    if count <= int(limit):
        return []
    return [_warning("max_tool_calls", f"{count} > {limit}")]


def _grade_hard_max_tool_calls(expected, result):
    limit = expected.get("hard_max_tool_calls")
    if limit is None:
        return []

    count = len(_tool_steps(result))
    return [_check("hard_max_tool_calls", count <= int(limit), f"{count} > {limit}")]


def _grade_file_contains(expected, result):
    checks = []
    workspace = Path(result["workspace"])
    for assertion in expected.get("file_contains") or []:
        path = workspace / assertion["path"]
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        fragment = assertion["text"]
        case_sensitive = assertion.get("case_sensitive", True)
        haystack = content if case_sensitive else content.lower()
        needle = fragment if case_sensitive else fragment.lower()
        checks.append(
            _check(
                f"file_contains:{assertion['path']}",
                needle in haystack,
                "fragment missing from file",
            )
        )
    return checks


def _grade_file_not_contains(expected, result):
    checks = []
    workspace = Path(result["workspace"])
    for assertion in expected.get("file_not_contains") or []:
        path = workspace / assertion["path"]
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        fragment = assertion["text"]
        checks.append(
            _check(
                f"file_not_contains:{assertion['path']}",
                fragment not in content,
                "forbidden fragment found in file",
            )
        )
    return checks


def _grade_objective_commands(expected, result):
    commands = expected.get("objective_commands") or []
    if not commands:
        return []

    command_results = result.get("objective_commands") or []
    checks = []
    for index, command in enumerate(commands):
        command_result = command_results[index] if index < len(command_results) else {}
        expected_exit = int(_command_field(command, "expect_exit_code", 0))
        exit_code = command_result.get("exit_code")
        label = _command_field(command, "name", _command_field(command, "command", f"command_{index + 1}"))
        checks.append(
            _check(
                f"objective_command:{label}",
                exit_code == expected_exit,
                f"exit_code={exit_code}, expected={expected_exit}, stdout={_clip(command_result.get('stdout'), 180)}, stderr={_clip(command_result.get('stderr'), 180)}",
            )
        )
        stdout = command_result.get("stdout") or ""
        stderr = command_result.get("stderr") or ""
        combined = stdout + "\n" + stderr
        for fragment in _command_field(command, "stdout_contains", []) or []:
            checks.append(
                _check(
                    f"objective_command.stdout_contains:{label}",
                    fragment in stdout,
                    f"missing stdout fragment: {fragment}",
                )
            )
        for fragment in _command_field(command, "output_contains", []) or []:
            checks.append(
                _check(
                    f"objective_command.output_contains:{label}",
                    fragment in combined,
                    f"missing output fragment: {fragment}",
                )
            )
    return checks


def _grade_required_events(expected, result):
    assertions = expected.get("required_events") or []
    if not assertions:
        return []

    events = result.get("events") or []
    checks = []
    for index, assertion in enumerate(assertions, start=1):
        matched = any(_event_matches(event, assertion) for event in events)
        checks.append(
            _check(
                f"required_event:{assertion.get('event', index)}",
                matched,
                f"expected event {assertion}; events={_clip(events, 400)}",
            )
        )
    return checks


def _event_matches(event, assertion):
    for key in ("type", "event", "kind", "decision"):
        expected_value = assertion.get(key)
        if expected_value is not None and event.get(key) != expected_value:
            return False
    for key in ("reason_contains", "target_contains"):
        expected_fragment = assertion.get(key)
        if expected_fragment is None:
            continue
        field = key.replace("_contains", "")
        if str(expected_fragment).lower() not in str(event.get(field, "")).lower():
            return False
    return True


def _grade_raw_conversations(expected, result):
    checks = []
    state = result.get("fixture_state") or {}
    snapshots = state.get("raw_conversation_snapshots") or {}
    workspace = Path(result["workspace"])
    for conversation_id in expected.get("raw_conversation_unchanged") or []:
        path = workspace / ".sema" / "conversations" / f"{conversation_id}.jsonl"
        before = snapshots.get(conversation_id)
        after = path.read_text(encoding="utf-8-sig", errors="replace") if path.exists() else None
        checks.append(
            _check(
                f"raw_conversation_unchanged:{conversation_id}",
                before is not None and after == before,
                f"path={path}",
            )
        )
    return checks


def _grade_memory_engine(expected, result):
    engine_expected = expected.get("memory_engine") or {}
    assertions = engine_expected.get("snapshot") or []
    operation_assertions = engine_expected.get("operations") or []
    if not assertions and not operation_assertions:
        return []

    engine_result = result.get("memory_engine") or {}
    default_snapshot = engine_result.get("snapshot") or {}
    snapshots = engine_result.get("snapshots") or {}
    checks = []
    for index, assertion in enumerate(assertions, start=1):
        path = assertion.get("path")
        snapshot_name = assertion.get("snapshot")
        snapshot = snapshots.get(snapshot_name) if snapshot_name else default_snapshot
        if snapshot is None:
            snapshot = {}
        actual = _path_get(snapshot, path)
        passed, detail = _memory_engine_assertion_passed(actual, assertion)
        checks.append(
            _check(
                f"memory_engine.snapshot:{snapshot_name + ':' if snapshot_name else ''}{path or index}",
                passed,
                detail,
                category="product_issue",
            )
        )
    operations = {item.get("name"): item for item in engine_result.get("operations") or []}
    for index, assertion in enumerate(operation_assertions, start=1):
        name = assertion.get("name")
        path = assertion.get("path")
        operation = operations.get(name) or {}
        actual = _path_get(operation, path)
        passed, detail = _memory_engine_assertion_passed(actual, assertion)
        checks.append(
            _check(
                f"memory_engine.operation:{name or index}:{path or ''}",
                passed,
                detail,
                category="product_issue",
            )
        )
    return checks


def _memory_engine_assertion_passed(actual, assertion):
    if "equals" in assertion:
        expected = assertion["equals"]
        return actual == expected, f"actual={_clip(actual, 220)}, expected={_clip(expected, 220)}"
    if "gte" in assertion:
        expected = assertion["gte"]
        return _number(actual) >= _number(expected), f"actual={actual}, expected>={expected}"
    if "lte" in assertion:
        expected = assertion["lte"]
        return _number(actual) <= _number(expected), f"actual={actual}, expected<={expected}"
    if "contains" in assertion:
        expected = assertion["contains"]
        return _contains(actual, expected), f"actual={_clip(actual, 220)}, expected contains={_clip(expected, 220)}"
    if "contains_all" in assertion:
        expected = assertion["contains_all"] or []
        missing = [item for item in expected if not _contains(actual, item)]
        return not missing, f"actual={_clip(actual, 220)}, missing={_clip(missing, 220)}"
    if "contains_object" in assertion:
        expected = assertion["contains_object"] or {}
        return _contains_object(actual, expected), f"actual={_clip(actual, 220)}, expected object={_clip(expected, 220)}"
    if "one_of" in assertion:
        expected = assertion["one_of"] or []
        return actual in expected, f"actual={_clip(actual, 220)}, expected one of={_clip(expected, 220)}"
    if "not_equals" in assertion:
        expected = assertion["not_equals"]
        return actual != expected, f"actual={_clip(actual, 220)}, forbidden={_clip(expected, 220)}"
    return False, "unsupported memory_engine assertion"


def _path_get(value, path):
    current = value
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
            continue
        return None
    return current


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _contains(actual, expected):
    if isinstance(actual, dict):
        return expected in actual or expected in actual.values()
    if isinstance(actual, list):
        return expected in actual
    return str(expected) in str(actual or "")


def _contains_object(actual, expected):
    if not isinstance(actual, list):
        return False
    for item in actual:
        if isinstance(item, dict) and all(item.get(key) == value for key, value in expected.items()):
            return True
    return False


def _command_field(command, name, default=None):
    if isinstance(command, str):
        return command if name == "command" else default
    return command.get(name, default)


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _grade_semantic_assertions(expected, result):
    assertions = expected.get("natural_language_assertions") or []
    if not assertions:
        return []

    judge = result.get("semantic_judge") or {}
    if judge.get("status") == "skipped":
        return [
            _check(
                "semantic_judge",
                False,
                judge.get("reason", "semantic judge was skipped"),
                category="environment_issue",
            )
        ]
    if judge.get("status") == "error":
        return [
            _check(
                "semantic_judge",
                False,
                judge.get("error", "semantic judge failed"),
                category=judge.get("category", "environment_issue"),
            )
        ]

    assertion_results = judge.get("results") or judge.get("assertions") or []
    threshold = int(expected.get("semantic_score_threshold", SEMANTIC_SCORE_THRESHOLD))
    checks = []
    by_assertion = {item.get("assertion"): item for item in assertion_results}
    for index, assertion in enumerate(assertions):
        item = None
        for candidate in assertion_results:
            if candidate.get("index") == index:
                item = candidate
                break
        if item is None:
            item = by_assertion.get(assertion)
        if item is None and index < len(assertion_results):
            item = assertion_results[index]
        if item is None:
            item = {}
        if judge.get("parse_error"):
            checks.append(
                _check(
                    f"semantic:{assertion}",
                    False,
                    judge.get("improvement_note") or "judge response could not be parsed",
                    category=judge.get("category", "judge_parse_error"),
                )
            )
            continue
        score = _score_value(item.get("score"))
        checks.append(
            _check(
                f"semantic:{assertion}",
                score >= threshold,
                f"score={score}, threshold={threshold}, reason={item.get('reason', '')}",
                category=item.get("category", "product_issue"),
            )
        )
    return checks


def _grade_scenario_judge(task, result):
    if task.get("type", "single") != "scenario":
        return []

    judge = result.get("scenario_judge") or {}
    threshold = int((task.get("expected") or {}).get("semantic_score_threshold", SEMANTIC_SCORE_THRESHOLD))
    if judge.get("status") == "skipped":
        return [
            _check(
                "semantic:scenario_judge",
                False,
                judge.get("reason", "scenario judge was skipped"),
                category="environment_issue",
            )
        ]
    if judge.get("status") == "error":
        return [
            _check(
                "semantic:scenario_judge",
                False,
                judge.get("error", "scenario judge failed"),
                category=judge.get("category", "environment_issue"),
            )
        ]
    if judge.get("parse_error"):
        return [
            _check(
                "semantic:scenario_judge",
                False,
                judge.get("improvement_note") or "scenario judge response could not be parsed",
                category=judge.get("category", "judge_parse_error"),
            )
        ]

    score = _score_value(judge.get("score"))
    reason = judge.get("reason") or judge.get("improvement_note") or ""
    return [
        _check(
            "semantic:scenario_judge",
            score >= threshold,
            f"score={score}, threshold={threshold}, reason={reason}",
            category=judge.get("category", "product_issue"),
        )
    ]


def _score_value(value):
    if isinstance(value, int):
        return max(0, min(10, value))
    if isinstance(value, float):
        return max(0, min(10, value))
    text = str(value or "")
    digits = ""
    for char in text:
        if char.isdigit() or (char == "." and digits):
            digits += char
        elif digits:
            break
    if digits:
        try:
            return max(0, min(10, float(digits)))
        except ValueError:
            return 0
    return 0


def _grade_scores(checks, warnings, result):
    objective_checks = [check for check in checks if not check.get("name", "").startswith("semantic")]
    semantic_checks = [check for check in checks if check.get("name", "").startswith("semantic")]
    objective_score = _checks_score(objective_checks, warnings, result)
    semantic_score = _semantic_score(semantic_checks)
    components = [objective_score]
    if semantic_score is not None:
        components.append(semantic_score)
    task_score = min(components) if components else MAX_SCORE
    return {
        "task": round(task_score, 1),
        "objective": round(objective_score, 1),
        "semantic": round(semantic_score, 1) if semantic_score is not None else None,
    }


def _checks_score(checks, warnings, result):
    if result.get("error"):
        return 0.0
    if not checks:
        base = MAX_SCORE
    else:
        passed_count = sum(1 for check in checks if check.get("passed"))
        base = MAX_SCORE * passed_count / len(checks)
    penalty = min(2.0, 0.5 * len(warnings))
    return max(0.0, base - penalty)


def _semantic_score(checks):
    if not checks:
        return None
    scores = []
    for check in checks:
        if check.get("passed"):
            scores.append(_detail_score(check.get("detail")))
        else:
            scores.append(_detail_score(check.get("detail")))
    return sum(scores) / len(scores)


def _detail_score(detail):
    text = str(detail or "")
    marker = "score="
    if marker not in text:
        return 0.0
    return float(_score_value(text.split(marker, 1)[1]))
