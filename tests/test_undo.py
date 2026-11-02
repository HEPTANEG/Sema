import pytest

import tools
from persistence import undo


def test_write_file_records_undo_and_restores_previous_content(isolated_workspace):
    path = isolated_workspace / "app.py"
    path.write_text("old\n", encoding="utf-8")

    tools.write_file_from_context("app.py", "new\n")
    result = undo.apply_undo()

    assert path.read_text(encoding="utf-8") == "old\n"
    assert result["tool"] == "write_file"
    assert result["files"] == ["app.py"]


def test_undo_created_file_deletes_it(isolated_workspace):
    tools.write_file_from_context("created.txt", "new\n")

    undo.apply_undo()

    assert not (isolated_workspace / "created.txt").exists()


def test_undo_refuses_when_file_changed_after_edit(isolated_workspace):
    path = isolated_workspace / "app.py"
    path.write_text("old\n", encoding="utf-8")
    tools.write_file_from_context("app.py", "new\n")
    path.write_text("manual change\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="file changed after edit: app.py"):
        undo.apply_undo()

    assert path.read_text(encoding="utf-8") == "manual change\n"


def test_undo_list_formats_recent_records(isolated_workspace):
    tools.write_file_from_context("app.py", "new\n")

    output = undo.format_undo_records(undo.load_undo_records())

    assert "Recent undo records:" in output
    assert "write_file" in output
    assert "app.py" in output


def test_insert_after_records_undo(isolated_workspace):
    path = isolated_workspace / "app.py"
    path.write_text("anchor\nend\n", encoding="utf-8")

    tools.insert_after_from_context("app.py", "anchor", "inserted")
    undo.apply_undo()

    assert path.read_text(encoding="utf-8") == "anchor\nend\n"


def test_apply_patch_records_undo(isolated_workspace):
    path = isolated_workspace / "app.py"
    path.write_text("old\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-old
+new
"""

    tools.apply_patch_from_context(patch)
    undo.apply_undo()

    assert path.read_text(encoding="utf-8") == "old\n"
