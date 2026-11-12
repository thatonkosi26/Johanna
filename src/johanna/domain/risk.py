"""Risk-management boundary (contract only; no risk calculations in Phase 1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from johanna.domain import UnimplementedService
from johanna.domain.execution import OrderIntent


@dataclass(frozen=True)
class RiskAssessment:
    approved: bool
    reasons: tuple[str, ...]
    max_volume: Decimal | None = None


@runtime_checkable
class RiskService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def assess(self, intent: OrderIntent) -> RiskAssessment: ...


class UnimplementedRiskService(UnimplementedService):
    contract = "johanna.domain.risk.RiskService"

    def __init__(self) -> None:
        super().__init__("risk_management", enabled=False)

    def assess(self, intent: OrderIntent) -> RiskAssessment:
        self._refuse("assess")
