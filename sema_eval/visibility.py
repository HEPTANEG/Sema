HIDDEN_FILENAMES = {"questions.json", "expected.json", "answers.json", "rubric.json"}


def build_visibility_audit(task, workspace=None):
    fixture = task.get("fixture") or {}
    audit = {
        "policy": "default",
        "sut_visible": [],
        "grader_hidden": [],
        "report_only": [],
        "violations": [],
    }

    if fixture:
        for relative_path in sorted((fixture.get("files") or {}).keys()):
            audit["sut_visible"].append(f"workspace:{relative_path}")

    if workspace:
        from pathlib import Path

        audit["violations"].extend(_workspace_hidden_file_violations(Path(workspace)))
    return audit


def validate_no_hidden_workspace_files(audit):
    if audit.get("violations"):
        names = ", ".join(audit["violations"])
        raise RuntimeError(f"Eval visibility violation: hidden grader data entered SUT workspace: {names}")


def _workspace_hidden_file_violations(workspace):
    if not workspace.exists():
        return []
    violations = []
    for path in workspace.rglob("*"):
        if path.is_file() and path.name in HIDDEN_FILENAMES:
            violations.append(str(path.relative_to(workspace)))
    return violations
