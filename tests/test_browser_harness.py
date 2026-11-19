"""The browser harness reads Chrome's DevToolsActivePort file robustly during start-up."""

from __future__ import annotations

from pathlib import Path

from browser_harness import _read_devtools_port


def test_missing_file_is_not_ready(tmp_path) -> None:
    assert _read_devtools_port(tmp_path / "DevToolsActivePort") is None


def test_locked_file_is_not_ready(tmp_path, monkeypatch) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/x\n")

    def locked(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", locked)
    assert _read_devtools_port(port_file) is None  # Chrome still holds it: retry later


def test_partially_written_file_is_not_ready(tmp_path) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    for text in ("", "  \n", "92a", "0\n"):
        port_file.write_text(text)
        assert _read_devtools_port(port_file) is None


def test_complete_file_gives_the_port(tmp_path) -> None:
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("51234\n/devtools/browser/abc\n")
    assert _read_devtools_port(port_file) == 51234
