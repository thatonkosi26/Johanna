"""Trading-signal boundary (contract only; no signal generation in Phase 1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from johanna.domain import UnimplementedService

Direction = Literal["long", "short", "flat"]


@dataclass(frozen=True)
class Signal:
    symbol: str
    timeframe: str
    direction: Direction
    generated_at_utc: datetime
    source: str
    rationale: str
    strength: float | None = None


@runtime_checkable
class SignalService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def generate(
        self, symbol: str, timeframe: str, bars: Sequence[Mapping[str, Any]]
    ) -> list[Signal]: ...


class UnimplementedSignalService(UnimplementedService):
    contract = "johanna.domain.signals.SignalService"

    def __init__(self) -> None:
        super().__init__("trading_signals", enabled=False)

    def generate(
        self, symbol: str, timeframe: str, bars: Sequence[Mapping[str, Any]]
    ) -> list[Signal]:
        self._refuse("generate")
