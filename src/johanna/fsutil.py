"""Filesystem helpers shared by the atomic writers."""

from __future__ import annotations

import time
from pathlib import Path


def replace_with_retry(
    source: Path, destination: Path, *, attempts: int = 40, delay_seconds: float = 0.025
) -> None:
    """Atomically move ``source`` over ``destination``, tolerating transient Windows locks.

    On Windows, replacing a file fails with ``PermissionError`` while any other handle has
    the destination open (a dashboard status read, antivirus or an editor). Those readers
    hold the file for milliseconds, so the move is retried for up to ~1 second before the
    error is raised. On Linux and macOS the first attempt succeeds even with open readers.
    """
    for attempt in range(attempts):
        try:
            Path(source).replace(destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)
