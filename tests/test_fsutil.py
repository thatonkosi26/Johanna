"""Atomic replace must survive a reader briefly holding the destination open (Windows)."""

from __future__ import annotations

import os
import threading

import pytest

from johanna.fsutil import replace_with_retry


def test_replace_waits_for_a_transient_reader(tmp_path) -> None:
    destination = tmp_path / "state.json"
    destination.write_text("old", encoding="utf-8")
    source = tmp_path / "state.json.tmp"
    source.write_text("new", encoding="utf-8")
    reader = open(destination, encoding="utf-8")  # noqa: SIM115 - deliberately held open
    timer = threading.Timer(0.2, reader.close)
    timer.start()
    try:
        replace_with_retry(source, destination)
    finally:
        timer.cancel()
        reader.close()
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


@pytest.mark.skipif(os.name != "nt", reason="POSIX allows replacing a file that is open")
def test_replace_gives_up_on_a_persistent_lock(tmp_path) -> None:
    destination = tmp_path / "state.json"
    destination.write_text("old", encoding="utf-8")
    source = tmp_path / "state.json.tmp"
    source.write_text("new", encoding="utf-8")
    with open(destination, encoding="utf-8"):
        with pytest.raises(PermissionError):
            replace_with_retry(source, destination, attempts=3, delay_seconds=0.01)
    assert destination.read_text(encoding="utf-8") == "old"
