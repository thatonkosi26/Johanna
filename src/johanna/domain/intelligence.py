"""Market/chart intelligence boundary, e.g. Gemini (contract only; no model calls)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from johanna.domain import UnimplementedService


@dataclass(frozen=True)
class AnalysisRequest:
    symbol: str
    timeframe: str
    question: str
    requested_at_utc: datetime
    bars: tuple[Mapping[str, Any], ...] = ()
    chart_image_png: bytes | None = None


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    model: str
    created_at_utc: datetime
    confidence: float | None = None


@runtime_checkable
class IntelligenceService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def analyze(self, request: AnalysisRequest) -> AnalysisResult: ...


class UnimplementedIntelligenceService(UnimplementedService):
    contract = "johanna.domain.intelligence.IntelligenceService"

    def __init__(self, *, enabled: bool = False) -> None:
        super().__init__("gemini_intelligence", enabled=enabled)

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        self._refuse("analyze")
