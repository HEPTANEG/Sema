from pathlib import Path


def test_sema_cmd_has_no_utf8_bom():

    content = Path("sema.cmd").read_bytes()

    assert content.startswith(b"@echo off")
    assert not content.startswith(b"\xef\xbb\xbf")
