import subprocess
import textwrap

import pytest

import tools


def test_list_directory_for_context_lists_workspace_files(isolated_workspace):
    (isolated_workspace / "b.txt").write_text("b", encoding="utf-8")
    (isolated_workspace / "folder").mkdir()
    (isolated_workspace / "a.txt").write_text("a", encoding="utf-8")

    context = tools.list_directory_for_context(".")

    assert context.startswith("--- directory: . ---")
    assert "folder/" in context
    assert "a.txt" in context
    assert "b.txt" in context
    assert context.endswith("--- end ---")


def test_list_directory_rejects_workspace_parent(isolated_workspace):
    with pytest.raises(RuntimeError, match="outside the current working directory"):
        tools.list_directory_for_context("..")


def test_list_directory_rejects_sema_dir(isolated_workspace):
    (isolated_workspace / ".sema").mkdir()

    with pytest.raises(RuntimeError, match="sensitive Sema files"):
        tools.list_directory_for_context(".sema")


def test_read_file_for_context_reads_workspace_file(isolated_workspace):
    (isolated_workspace / "note.txt").write_text("hello", encoding="utf-8")

    display_path, context = tools.read_file_for_context("note.txt")

    assert str(display_path) == "note.txt"
    assert "--- file: note.txt ---" in context
    assert "status: ok" in context
    assert "total_lines: 1" in context
    assert "hello" in context
    assert context.endswith("--- end (ok) ---")


def test_read_file_for_context_reads_line_range(isolated_workspace):
    (isolated_workspace / "note.txt").write_text("one\ntwo\nthree\nfour", encoding="utf-8")

    _display_path, context = tools.read_file_for_context(
        {"path": "note.txt", "start_line": 2, "line_count": 2}
    )

    assert "status: ok" in context
    assert "range: line" in context
    assert "returned_lines: 2..3" in context
    assert "\ntwo\nthree\n" in context
    assert "one" not in context
    assert "four" not in context


def test_read_file_for_context_reads_char_range_for_single_line_files(isolated_workspace):
    (isolated_workspace / "data.json").write_text('{"items":[1,2,3,4,5]}', encoding="utf-8")

    _display_path, context = tools.read_file_for_context(
        {"path": "data.json", "start_char": 10, "char_count": 7}
    )

    assert "status: ok" in context
    assert "range: char" in context
    assert "returned_char_range: 10..17" in context
    assert "1,2,3,4" in context


def test_read_file_for_context_rejects_mixed_ranges(isolated_workspace):
    (isolated_workspace / "note.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(RuntimeError, match="either line range or character range"):
        tools.read_file_for_context({"path": "note.txt", "start_line": 1, "start_char": 0})


def test_read_file_for_context_warns_large_file_with_preview(isolated_workspace):
    content = "\n".join(f"line {index}" for index in range(1, 7000))
    (isolated_workspace / "large.txt").write_text(content, encoding="utf-8")

    _display_path, context = tools.read_file_for_context("large.txt")

    assert "status: warn_large" in context
    assert "file_chars:" in context
    assert "preview_lines: 1..100" in context
    assert "Large file preview only" in context
    assert "line 1" in context
    assert "line 100" in context
    assert "line 101" not in context
    assert "get_overview" in context


def test_read_file_for_context_too_large_keeps_small_preview(isolated_workspace):
    line = "x" * 100 + "\n"
    (isolated_workspace / "huge.txt").write_text(line * 2500, encoding="utf-8")

    _display_path, context = tools.read_file_for_context("huge.txt")

    assert "status: too_large" in context
    assert "File is too large for full raw reading" in context
    assert "preview_lines: 1..100" in context
    assert len(context) < 20000
    assert "get_overview" in context


def test_read_file_for_context_full_read_warn_large_under_hard_limit(isolated_workspace):
    content = "a" * (tools.MAX_FILE_CHARS + 10)
    (isolated_workspace / "large.txt").write_text(content, encoding="utf-8")

    _display_path, context = tools.read_file_for_context({"path": "large.txt", "full_read": True, "max_chars": len(content)})

    assert "status: ok" in context
    assert f"returned_chars: {len(content)}" in context


def test_read_file_for_context_range_is_capped_by_max_chars(isolated_workspace):
    (isolated_workspace / "single.txt").write_text("x" * 5000, encoding="utf-8")

    _display_path, context = tools.read_file_for_context({"path": "single.txt", "start_line": 1, "line_count": 1, "max_chars": 100})

    assert "status: warn_large" in context
    assert "line_too_long: True" in context
    assert "returned_chars: 100" in context


def test_get_file_overview_for_context_is_navigation_only(isolated_workspace):
    content = "\n".join(f"line {index}" for index in range(1, 130))
    (isolated_workspace / "large.txt").write_text(content, encoding="utf-8")

    context = tools.get_file_overview_for_context({"path": "large.txt", "instruction": "Find config sections."})

    assert context.startswith("--- overview: large.txt ---")
    assert "not source text" in context
    assert "instruction: Find config sections." in context
    assert "covered_ranges:" in context
    assert "Use read_file with start_line/line_count" in context


def test_read_file_rejects_env_file(isolated_workspace):
    (isolated_workspace / ".env").write_text("SECRET=value", encoding="utf-8")

    with pytest.raises(RuntimeError, match="sensitive Sema files"):
        tools.read_file_for_context(".env")


def test_read_file_rejects_sema_file(isolated_workspace):
    (isolated_workspace / ".sema").mkdir()
    (isolated_workspace / ".sema" / "project.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="sensitive Sema files"):
        tools.read_file_for_context(".sema/project.json")


def test_read_file_rejects_workspace_parent_file(isolated_workspace):
    outside = isolated_workspace.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(RuntimeError, match="outside the current working directory"):
        tools.read_file_for_context("../outside.txt")


def test_write_file_from_context_writes_workspace_file(isolated_workspace):
    result = tools.write_file_from_context("out/result.txt", "hello")

    assert result == "Wrote file: out\\result.txt" or result == "Wrote file: out/result.txt"
    assert (isolated_workspace / "out" / "result.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_from_context_can_write_confirmed_external_file(isolated_workspace, tmp_path):
    external = tmp_path / "external" / "result.txt"

    with pytest.raises(RuntimeError, match="outside the current working directory"):
        tools.write_file_from_context(str(external), "hello")

    result = tools.write_file_from_context(str(external), "hello", allow_external=True)

    assert result == f"Wrote file: {external}"
    assert external.read_text(encoding="utf-8") == "hello"


def test_write_file_rejects_env_file(isolated_workspace):
    with pytest.raises(RuntimeError, match="sensitive Sema files"):
        tools.write_file_from_context(".env", "SECRET=value")


def test_write_file_rejects_sema_file(isolated_workspace):
    with pytest.raises(RuntimeError, match="sensitive Sema files"):
        tools.write_file_from_context(".sema/project.json", "{}")


def test_write_file_rejects_workspace_parent_file(isolated_workspace):
    with pytest.raises(RuntimeError, match="outside the current working directory"):
        tools.write_file_from_context("../outside.txt", "outside")


def test_insert_after_from_context_inserts_after_unique_anchor(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text(
        textwrap.dedent(
            """\
            class Car:
                def update(self):
                    terrain_angle = terrain.get_angle(self.x)
                    accel = self.vx - self.prev_vx
            """
        ),
        encoding="utf-8",
    )

    result = tools.insert_after_from_context(
        "car.py",
        "terrain_angle = terrain.get_angle(self.x)",
        "slope_gravity = math.sin(terrain_angle) * 0.3\nself.vx += slope_gravity",
    )

    assert result == "Inserted content after anchor in: car.py"
    assert target.read_text(encoding="utf-8") == textwrap.dedent(
        """\
        class Car:
            def update(self):
                terrain_angle = terrain.get_angle(self.x)
                slope_gravity = math.sin(terrain_angle) * 0.3
                self.vx += slope_gravity
                accel = self.vx - self.prev_vx
        """
    )


def test_insert_after_from_context_rejects_duplicate_anchor(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text("anchor\nanchor\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Anchor is not unique"):
        tools.insert_after_from_context("car.py", "anchor", "inserted")


def test_insert_after_from_context_uses_anchor_context_to_choose_duplicate_anchor(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text(
        textwrap.dedent(
            """\
            class Car:
                def update(self):
                    self.vy = 0
                    self.on_ground = True

                    self.vx *= (1 - FRICTION)

                def reset(self):
                    self.vy = 0
                    self.angle = 0
            """
        ),
        encoding="utf-8",
    )

    result = tools.insert_after_from_context(
        "car.py",
        "self.vy = 0",
        "ground_resolved = True",
        "self.vx *= (1 - FRICTION)",
    )

    assert result == "Inserted content after anchor in: car.py"
    assert target.read_text(encoding="utf-8") == textwrap.dedent(
        """\
        class Car:
            def update(self):
                self.vy = 0
                ground_resolved = True
                self.on_ground = True

                self.vx *= (1 - FRICTION)

            def reset(self):
                self.vy = 0
                self.angle = 0
        """
    )


def test_replace_block_from_context_replaces_unique_block(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text(
        textwrap.dedent(
            """\
            def update(self):
                if self.on_ground:
                    self.vy = 0

                self.x += self.vx
                self.y += self.vy
            """
        ),
        encoding="utf-8",
    )

    result = tools.replace_block_from_context(
        "car.py",
        "    self.x += self.vx\n    self.y += self.vy",
        "    if not self.on_ground:\n        self.x += self.vx\n        self.y += self.vy",
    )

    assert result == "Replaced block in: car.py"
    assert target.read_text(encoding="utf-8") == textwrap.dedent(
        """\
        def update(self):
            if self.on_ground:
                self.vy = 0

            if not self.on_ground:
                self.x += self.vx
                self.y += self.vy
        """
    )


def test_replace_block_from_context_deletes_block_with_empty_new(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")

    result = tools.replace_block_from_context("car.py", "b = 2", "")

    assert result == "Replaced block in: car.py"
    assert target.read_text(encoding="utf-8") == "a = 1\nc = 3\n"


def test_replace_block_from_context_rejects_duplicate_old_block(isolated_workspace):
    target = isolated_workspace / "car.py"
    target.write_text("anchor\nanchor\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="OLD block is not unique"):
        tools.replace_block_from_context("car.py", "anchor", "changed")


def test_run_command_for_context_captures_stdout(isolated_workspace):
    returncode, context = tools.run_command_for_context("python -c \"print('hello')\"")

    assert returncode == 0
    assert "exit code: 0" in context
    assert "hello" in context


def test_run_command_for_context_captures_stderr(isolated_workspace):
    command = "python -c \"import sys; sys.stderr.write('warn')\""

    returncode, context = tools.run_command_for_context(command)

    assert returncode == 0
    assert "warn" in context


def test_run_command_for_context_uses_replace_decode_errors(isolated_workspace, monkeypatch):
    captured = {}

    class Completed:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def fake_run(*_args, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    _returncode, context = tools.run_command_for_context("echo ok")

    assert captured["errors"] == "replace"
    assert captured["stdin"] is subprocess.DEVNULL
    assert "ok" in context


def test_run_command_for_context_rejects_empty_command(isolated_workspace):
    with pytest.raises(RuntimeError, match="Usage: /run <command>"):
        tools.run_command_for_context("   ")


def test_run_command_for_context_propagates_timeout(isolated_workspace, monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("slow", 60)

    monkeypatch.setattr(tools.subprocess, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        tools.run_command_for_context("slow")


def test_run_configured_test_uses_workspace_config(isolated_workspace):
    config_dir = isolated_workspace / ".sema"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        '{"test_command": "python -c \\"print(123)\\""}',
        encoding="utf-8",
    )

    context = tools.run_configured_test_for_context()

    assert "123" in context


def test_apply_patch_rejects_empty_patch(isolated_workspace):
    with pytest.raises(RuntimeError, match="apply_patch requires PATCH content"):
        tools.apply_patch_from_context("   ")


def test_apply_patch_from_context_applies_valid_patch(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    target = isolated_workspace / "app.py"
    target.write_text('print("old")\n', encoding="utf-8")
    patch = textwrap.dedent(
        """\
        diff --git a/app.py b/app.py
        --- a/app.py
        +++ b/app.py
        @@ -1 +1 @@
        -print("old")
        +print("new")
        """
    )

    result = tools.apply_patch_from_context(patch)

    assert result == "Patch applied."
    assert target.read_text(encoding="utf-8") == 'print("new")\n'


def test_apply_patch_from_context_resolves_unique_basename_patch(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    source_dir = isolated_workspace / "src"
    source_dir.mkdir()
    target = source_dir / "car.py"
    target.write_text("speed = 1\n", encoding="utf-8")
    patch = textwrap.dedent(
        """\
        --- a/car.py
        +++ b/car.py
        @@ -1 +1 @@
        -speed = 1
        +speed = 2
        """
    )

    result = tools.apply_patch_from_context(patch)

    assert result == "Patch applied."
    assert target.read_text(encoding="utf-8") == "speed = 2\n"


def test_apply_patch_from_context_adds_git_prefixes_to_unprefixed_paths(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    source_dir = isolated_workspace / "src"
    source_dir.mkdir()
    target = source_dir / "car.py"
    target.write_text("speed = 1\n", encoding="utf-8")
    patch = textwrap.dedent(
        """\
        --- car.py
        +++ car.py
        @@ -1 +1 @@
        -speed = 1
        +speed = 3
        """
    )

    result = tools.apply_patch_from_context(patch)

    assert result == "Patch applied."
    assert target.read_text(encoding="utf-8") == "speed = 3\n"


def test_normalize_patch_paths_rewrites_headers_only(isolated_workspace):
    (isolated_workspace / "src").mkdir()
    (isolated_workspace / "src" / "car.py").write_text("speed = 1\n", encoding="utf-8")
    patch = textwrap.dedent(
        """\
        diff --git car.py car.py
        --- car.py
        +++ car.py
        @@ -1 +1 @@
        -path mention: car.py
        +path mention: car.py
        """
    )

    normalized = tools.normalize_patch_paths(patch)

    assert "diff --git a/src/car.py b/src/car.py" in normalized
    assert "--- a/src/car.py" in normalized
    assert "+++ b/src/car.py" in normalized
    assert "+path mention: car.py" in normalized


def test_apply_patch_from_context_recounts_model_hunk_lengths(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    target = isolated_workspace / "car.py"
    target.write_text(
        textwrap.dedent(
            """\
            class Car:
                def update(self):
                    self.vx *= 0.95

                    terrain_angle = terrain.get_angle(self.x)

                    accel = self.vx - self.prev_vx
            """
        ),
        encoding="utf-8",
    )
    patch = (
        "--- a/car.py\n"
        "+++ b/car.py\n"
        "@@ -1,2 +1,5 @@\n"
        " class Car:\n"
        "     def update(self):\n"
        "         self.vx *= 0.95\n"
        " \n"
        "         terrain_angle = terrain.get_angle(self.x)\n"
        "+        slope_gravity = math.sin(terrain_angle) * 0.3\n"
        "+        self.vx += slope_gravity\n"
        " \n"
        "         accel = self.vx - self.prev_vx\n"
    )

    result = tools.apply_patch_from_context(patch)

    assert result == "Patch applied."
    assert "slope_gravity = math.sin(terrain_angle) * 0.3" in target.read_text(encoding="utf-8")


def test_apply_patch_from_context_falls_back_for_insert_only_context_drift(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    source_dir = isolated_workspace / "MY_RacingGame"
    source_dir.mkdir()
    target = source_dir / "car.py"
    target.write_text(
        textwrap.dedent(
            """\
            class Car:
                def update(self, terrain):
                    self.vx *= 0.95

                    # Drifted context not present in the model patch.
                    terrain_angle = terrain.get_angle(self.x)

                    accel = self.vx - self.prev_vx
            """
        ),
        encoding="utf-8",
    )
    patch = (
        "--- a/MY_RacingGame/car.py\n"
        "+++ b/MY_RacingGame/car.py\n"
        "@@ -49,6 +49,9 @@\n"
        "         self.vx *= (1 - FRICTION)\n"
        " \n"
        "         terrain_angle = terrain.get_angle(self.x)\n"
        "+        slope_gravity = math.sin(terrain_angle) * 0.3\n"
        "+        self.vx += slope_gravity\n"
        " \n"
        "         accel = self.vx - self.prev_vx\n"
    )

    result = tools.apply_patch_from_context(patch)

    assert result == "Patch applied with insert fallback: MY_RacingGame/car.py"
    content = target.read_text(encoding="utf-8")
    assert "slope_gravity = math.sin(terrain_angle) * 0.3" in content
    assert "self.vx += slope_gravity" in content


def test_apply_patch_from_context_rejects_invalid_patch_without_modifying_file(isolated_workspace):
    subprocess.run(["git", "init"], cwd=isolated_workspace, check=True, capture_output=True, text=True)
    target = isolated_workspace / "app.py"
    target.write_text('print("old")\n', encoding="utf-8")
    patch = textwrap.dedent(
        """\
        diff --git a/app.py b/app.py
        --- a/app.py
        +++ b/app.py
        @@ -1 +1 @@
        -print("does not exist")
        +print("new")
        """
    )

    with pytest.raises(RuntimeError):
        tools.apply_patch_from_context(patch)

    assert target.read_text(encoding="utf-8") == 'print("old")\n'


def test_background_command_lifecycle(isolated_workspace):
    from tools import background_commands
    import sys

    background_commands.reset_background_commands_for_tests()
    command = subprocess.list2cmdline(
        [
            sys.executable,
            "-c",
            "import time; print('ready', flush=True); time.sleep(30)",
        ]
    )

    try:
        started = background_commands.start_background_command(command)
        command_id = _extract_background_field(started, "id")

        assert "status: running" in started
        assert command_id
        assert "ready" in background_commands.read_background_command_output(command_id)
        assert command_id in background_commands.list_background_commands()

        stopped = background_commands.stop_background_command(command_id)
        assert "status: exited" in stopped
    finally:
        background_commands.reset_background_commands_for_tests()


def _extract_background_field(text, field):
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""
