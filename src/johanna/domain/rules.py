"""Configurable trading-rules boundary (contract only; no rule evaluation in Phase 1)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from johanna.domain import UnimplementedService
from johanna.domain.signals import Signal


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    passed: bool
    reason: str


@runtime_checkable
class RulesService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def evaluate(self, signal: Signal) -> list[RuleEvaluation]: ...


class UnimplementedRulesService(UnimplementedService):
    contract = "johanna.domain.rules.RulesService"

    def __init__(self) -> None:
        super().__init__("trading_rules", enabled=False)

    def evaluate(self, signal: Signal) -> list[RuleEvaluation]:
        self._refuse("evaluate")
