"""Typed contracts for domain services that are NOT implemented in Phase 1.

Each module defines the data types a boundary exchanges, a ``Protocol`` describing the
service, and an ``Unimplemented*`` stub. The stubs take part in the application lifecycle
(they report ``not_implemented``/``disabled`` status) and raise
``ServiceNotImplementedError`` from every domain method. Nothing here performs news
ingestion, model calls, signal generation, rule evaluation, risk calculation or broker
execution, and nothing imports a broker or provider adapter.

Later phases replace a stub with a real implementation of the same Protocol and register it
in ``johanna.application._services``.
"""

from __future__ import annotations

from typing import NoReturn

from johanna.services import ManagedService


class ServiceNotImplementedError(NotImplementedError):
    """The domain service exists only as a Phase 1 contract."""


class UnimplementedService(ManagedService):
    """Lifecycle-compatible stub whose domain methods always refuse."""

    contract: str = ""

    def status(self) -> dict[str, object]:
        return {**super().status(), "implemented": False, "contract": self.contract}

    def _refuse(self, operation: str) -> NoReturn:
        raise ServiceNotImplementedError(
            f"{self.name}.{operation} is not implemented in this phase ({self.contract})"
        )
