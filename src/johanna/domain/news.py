"""News boundary (contract only; no news ingestion in Phase 1)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from johanna.domain import UnimplementedService


@dataclass(frozen=True)
class Headline:
    headline_id: str
    title: str
    source: str
    published_at_utc: datetime
    url: str | None = None
    currencies: tuple[str, ...] = ()


@runtime_checkable
class NewsService(Protocol):
    name: str

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def status(self) -> dict[str, object]: ...

    def latest(self, *, limit: int = 20, currencies: Sequence[str] = ()) -> list[Headline]: ...


class UnimplementedNewsService(UnimplementedService):
    contract = "johanna.domain.news.NewsService"

    def __init__(self, *, enabled: bool = False) -> None:
        super().__init__("news", enabled=enabled)

    def latest(self, *, limit: int = 20, currencies: Sequence[str] = ()) -> list[Headline]:
        self._refuse("latest")
