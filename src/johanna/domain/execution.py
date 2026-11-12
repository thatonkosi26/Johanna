"""Broker-execution boundary (contract only; execution is unavailable in Phase 1).

This module deliberately imports no broker adapter. The only implementation,
``DisabledExecutionService``, refuses every request. MT5 stays read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from johanna.domain import ServiceNotImplementedError, UnimplementedService

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop"]
Origin = Literal["manual", "autonomous"]


class ExecutionUnavailableError(ServiceNotImplementedError):
    """Broker execution is not available; no order was sent."""


@dataclass(frozen=True)
class OrderIntent:
    """A request to trade. Creating one never contacts a broker."""

    symbol: str
    side: Side
    volume: Decimal
    order_type: OrderType
    origin: Origin
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    message: str
    broker_reference: str | None = None


@runtime_checkable
class ExecutionService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def submit(self, intent: OrderIntent) -> ExecutionResult: ...


class DisabledExecutionService(UnimplementedService):
    contract = "johanna.domain.execution.ExecutionService"

    def __init__(self) -> None:
        super().__init__("broker_execution", enabled=False)

    def submit(self, intent: OrderIntent) -> ExecutionResult:
        raise ExecutionUnavailableError(
            "Broker execution is not available in this phase; no order was sent"
        )
