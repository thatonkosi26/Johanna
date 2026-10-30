"""Read-only market-data adapter contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class MarketDataAdapter(Protocol):
    """Minimum interface required by Johanna's market-data consumers."""

    def initialize(self) -> None: ...

    def connection_status(self) -> ConnectionStatus: ...

    def is_connected(self) -> bool: ...

    def list_symbols(self) -> list[str]: ...

    def select_symbol(self, symbol: str) -> None: ...

    def get_tick(self, symbol: str) -> dict[str, Any]: ...

    def get_bars(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]: ...

    def shutdown(self) -> None: ...


class MT5AdapterError(RuntimeError):
    """Safe, actionable error from MetaTrader 5 access."""


@dataclass(frozen=True)
class ConnectionStatus:
    """Terminal and account status without exposing account identifiers."""

    terminal_connected: bool
    account_connected: bool

    @property
    def connected(self) -> bool:
        return self.terminal_connected and self.account_connected
