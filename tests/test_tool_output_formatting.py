import pytest

from tools import tool_output_formatting as formatting


@pytest.mark.parametrize(
    ("path_text", "lexer"),
    [
        ("agent.py", "python"),
        ("README.md", "markdown"),
        ("config.json", "json"),
        ("install.ps1", "powershell"),
        ("sema.cmd", "batch"),
        ("script.bat", "batch"),
        ("notes.txt", "text"),
        ("Makefile", "text"),
    ],
)
def test_guess_lexer_returns_stable_mapping(path_text, lexer):
    assert formatting._guess_lexer(path_text) == lexer


def test_summarize_read_file_hides_file_body():
    context = "--- app.py ---\nprint('secret')\n--- end ---"

    summary = formatting.summarize_tool_output("read_file", "app.py", context)

    assert "Read app.py" in summary
    assert "captured for context" in summary
    assert "print('secret')" not in summary


def test_format_read_file_label_with_range_args():
    label = formatting.format_tool_label(
        "read_file",
        {"path": "large.txt", "start_line": 50, "line_count": 25},
    )

    assert label == "read_file large.txt (line 50+25)"


def test_summarize_get_overview_hides_file_body():
    context = "--- overview: app.py ---\nsecret details\n--- end overview ---"

    summary = formatting.summarize_tool_output(
        "get_overview",
        {"path": "app.py", "instruction": "Find imports."},
        context,
    )

    assert "Generated overview for app.py" in summary
    assert "captured for context" in summary
    assert "secret details" not in summary


def test_summarize_run_command_hides_command_output():
    context = "--- command: python app.py ---\nexit code: 0\nlong output\n--- end ---"

    summary = formatting.summarize_tool_output("run_command", "python app.py", context)

    assert "Ran command: python app.py" in summary
    assert "exit code 0" in summary
    assert "long output" not in summary


def test_summarize_vision_analysis_hides_model_output():
    context = "--- vision analysis ---\nresult:\nsecret OCR text\n--- end ---"

    summary = formatting.summarize_tool_output(
        "vision_analyze_images",
        {"paths": ["one.png", "two.jpg"], "instruction": "Convert."},
        context,
    )

    assert "Analyzed 2 image(s)" in summary
    assert "captured for context" in summary
    assert "secret OCR text" not in summary


def test_format_vision_tool_label_is_compact():
    label = formatting.format_tool_label(
        "vision_analyze_images",
        {
            "paths": ["one.png", "two.jpg", "three.jpg"],
            "instruction": "Extract date/time, full text, and interaction stats from each Kehua app blog screenshot.",
        },
    )

    assert label == "vision_analyze_images 3 image(s); Extract date/time, full text, and interaction stats from each Kehua app blog ..."
    assert "one.png" not in label


def test_summarize_tool_error_keeps_error_detail():
    summary = formatting.summarize_tool_output("read_file", "missing.txt", "Tool error: File not found")

    assert summary == "Tool error: File not found"


def test_tool_progress_line_marks_cancelled_tool_without_success():
    line = formatting.tool_progress_line(
        "vision_analyze_images",
        {"paths": ["one.png"], "instruction": "OCR"},
        "External image access cancelled.\nReason: not yet",
    )

    assert line.startswith("x vision_analyze_images")
    assert not line.startswith("ok ")


def test_diff_stats_ignores_headers():
    diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-old
+new
 context
"""

    assert formatting._diff_stats(diff) == (1, 1)


def test_text_diff_stats_counts_replacement_lines():
    assert formatting.text_diff_stats("old\nkeep\n", "new\nkeep\nadded\n") == (2, 1)


def test_format_last_run_returns_recorded_details():
    output = formatting.format_last_run(
        [
            {
                "tool": "read_file",
                "args": "app.py",
                "result": "file content",
            }
        ]
    )

    assert "1. read_file app.py" in output
    assert "file content" in output
