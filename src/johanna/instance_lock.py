"""Cross-platform, crash-safe exclusive file locks.

``FileLock`` holds an OS-level lock (``msvcrt.locking`` on Windows, ``flock`` on Linux and
macOS) on a lock file for as long as the owner keeps it. The operating system releases the
lock when the owning process exits for any reason, including a crash or a kill, so a lock
file left on disk is never a lock by itself and cannot block later runs ("stale lock").
The file's content is informational only (owner PID and purpose).

Locks are per open file handle, so two threads in one process using separate ``FileLock``
objects for the same path also exclude each other.

Used for:
- ``InstanceLock``: one Johanna backend per data directory.
- ``johanna.collectors.SourceLock``: one collector per source (``mt5``/``calendar``),
  shared by the standalone collector CLI and the backend monitoring workers.
"""

from __future__ import annotations

import os
from pathlib import Path


class LockBusyError(RuntimeError):
    """The lock is held by another live owner."""


class InstanceLockError(LockBusyError):
    """Another Johanna backend already owns this data directory."""


class FileLock:
    busy_error: type[LockBusyError] = LockBusyError

    def __init__(self, path: str | Path, *, purpose: str = "") -> None:
        self.path = Path(path)
        self.purpose = purpose
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - held open for the lock lifetime
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise self.busy_error(self._busy_message()) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} {self.purpose}".strip().encode("utf-8"))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def _busy_message(self) -> str:
        return f"Lock is held by another process: {self.path}"


class InstanceLock(FileLock):
    busy_error = InstanceLockError

    def __init__(self, path: str | Path) -> None:
        super().__init__(path, purpose="johanna backend")

    def _busy_message(self) -> str:
        return f"Another Johanna backend is already using {self.path.parent}"


if os.name == "nt":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
