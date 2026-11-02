from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_python_agent_tree_has_no_legacy_rich_cli_imports():
    forbidden = (
        "import rendering",
        "from rendering",
        "import interaction",
        "from interaction",
        "import questionary",
        "from rich",
        "handle_local_command",
        "read_user_prompt",
        "choose_action",
        "print_banner",
    )
    offenders = []
    for path in (ROOT / "my_agent").glob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        for marker in forbidden:
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    assert offenders == []


def test_root_requirements_do_not_reintroduce_questionary():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8-sig").splitlines()
    offenders = [
        line
        for line in requirements
        if line.strip()
        and not line.lstrip().startswith("#")
        and line.split("#", 1)[0].strip().lower().startswith("questionary")
    ]
    assert offenders == []
