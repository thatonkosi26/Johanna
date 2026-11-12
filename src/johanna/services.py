"""Application service contracts and lifecycle coordination."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

LOGGER = logging.getLogger("johanna.services")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


@runtime_checkable
class Service(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...


class ManagedService:
    """Lifecycle placeholder for a domain service that has not been configured yet."""

    def __init__(self, name: str, *, enabled: bool = False) -> None:
        self.name = name
        self.enabled = enabled
        self._started_at: datetime | None = None

    def start(self) -> None:
        if self.enabled:
            self._started_at = datetime.now(UTC)

    def stop(self) -> None:
        self._started_at = None

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "state": "not_implemented" if self.enabled else "disabled",
            "started_at_utc": self._started_at.isoformat() if self._started_at else None,
        }


class BackgroundService:
    """Runs one stoppable background task independently of the HTTP frontend.

    A task that raises is logged with its traceback and the service enters the ``error``
    state, which stays visible (including after ``stop``) until the next ``start``. A task
    that returns before a stop was requested is reported as ``exited`` so an unexpectedly
    finished worker is never shown as running. Failed workers are not restarted
    automatically; restart is an explicit ``start`` call.
    """

    def __init__(self, name: str, task: Callable[[threading.Event], None]) -> None:
        self.name = name
        self._task = task
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._exited_unexpectedly = False
        self._started_at: datetime | None = None
        self._failed_at: datetime | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._error = None
            self._failed_at = None
            self._exited_unexpectedly = False
            self._started_at = datetime.now(UTC)

            def run() -> None:
                try:
                    self._task(stop_event)
                except Exception as exc:
                    self._error = type(exc).__name__
                    self._failed_at = datetime.now(UTC)
                    LOGGER.exception("Background service %s failed", self.name)
                    return
                if not stop_event.is_set():
                    self._exited_unexpectedly = True
                    LOGGER.warning("Background service %s exited without a stop request", self.name)

            self._thread = threading.Thread(target=run, name=f"johanna-{self.name}", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            thread.join(timeout=30)
            if thread.is_alive():
                raise RuntimeError(f"Background service {self.name} did not stop within 30 seconds")
            self._thread = None

    def status(self) -> dict[str, object]:
        thread = self._thread
        if self._error:
            state = "error"
        elif thread and thread.is_alive():
            state = "running"
        elif self._exited_unexpectedly:
            state = "exited"
        else:
            state = "stopped"
        return {
            "name": self.name,
            "state": state,
            "error": self._error,
            "started_at_utc": _iso(self._started_at),
            "failed_at_utc": _iso(self._failed_at),
        }


class ServiceManager:
    """Starts services once, rolls back partial startup, and stops in reverse order."""

    def __init__(self, services: list[Service] | tuple[Service, ...] = ()) -> None:
        unique: dict[str, Service] = {}
        for service in services:
            unique.setdefault(service.name, service)
        self.services = tuple(unique.values())
        self._started: list[Service] = []
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            try:
                for service in self.services:
                    self._started.append(service)
                    service.start()
            except Exception:
                self._stop_started()
                raise

    def stop(self) -> None:
        with self._lock:
            self._stop_started()

    def _stop_started(self) -> None:
        errors: list[Exception] = []
        while self._started:
            service = self._started.pop()
            try:
                service.stop()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError(f"{len(errors)} service(s) failed to stop") from errors[0]

    def status(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {service.name: service.status() for service in self.services}
