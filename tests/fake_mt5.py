"""In-memory stand-in for the ``MetaTrader5`` package (test-only).

Implements only the read-only calls Johanna uses. Any order/position function is absent, so
an accidental call fails loudly with AttributeError. ``calls`` records every API call.
Prices are synthetic and do not represent market observations.
"""

from __future__ import annotations

import fnmatch
import time
from collections import namedtuple
from types import SimpleNamespace

Tick = namedtuple("Tick", "time bid ask last volume time_msc flags volume_real")
Rate = namedtuple("Rate", "time open high low close tick_volume spread real_volume")
READ_ONLY_CALLS = {
    "initialize",
    "shutdown",
    "last_error",
    "terminal_info",
    "account_info",
    "symbol_info",
    "symbols_get",
    "symbol_select",
    "symbol_info_tick",
    "copy_rates_from_pos",
}
_SECONDS = {1: 60, 5: 300, 15: 900, 30: 1800, 16385: 3600, 16388: 14400, 16408: 86400}


class FakeMT5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(
        self,
        *,
        symbols: dict[str, dict] | None = None,
        server: str = "Exness-MT5Trial9",
        company: str = "Exness Technologies Ltd",
        server_offset_seconds: float = 0.0,
        tick_age_seconds: float = 0.5,
        now=time.time,
    ) -> None:
        self.symbols = symbols or {
            "EURUSD": {"bid": 1.08512, "ask": 1.08519, "digits": 5, "point": 0.00001},
            "USDJPY": {"bid": 151.234, "ask": 151.249, "digits": 3, "point": 0.001},
        }
        for spec in self.symbols.values():
            spec.setdefault("visible", True)
            spec.setdefault("base", spec["bid"])  # completed bars are built from this price
        self.server = server
        self.company = company
        self.server_offset_seconds = server_offset_seconds
        self.tick_age_seconds = tick_age_seconds
        self.now = now
        self.running = True
        self.terminal_connected = True
        self.delay = 0.0
        self.spin_seconds = 0.0  # CPU-bound delay that holds the GIL, like the real package
        # Optional hook (symbol, rows) -> rows to inject anomalies into copy_rates_from_pos
        # (duplicates, disorder, invalid prices, empty answers). Test-only.
        self.rates_hook = None
        self.calls: list[str] = []
        self.initialize_paths: list[str | None] = []

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.delay:
            time.sleep(self.delay)
        if self.spin_seconds and name == "symbol_info_tick":
            deadline = time.perf_counter() + self.spin_seconds
            while time.perf_counter() < deadline:
                pass
        if not self.running and name not in {"initialize", "shutdown", "last_error"}:
            raise RuntimeError("IPC: terminal closed")

    def _server_epoch(self, real_epoch: float) -> float:
        """MT5 encodes the server's wall clock as epoch seconds."""
        return real_epoch + self.server_offset_seconds

    # -- connection
    def initialize(self, path=None):
        self._call("initialize")
        self.initialize_paths.append(path)
        return self.running

    def shutdown(self):
        self._call("shutdown")
        return True

    def last_error(self):
        self.calls.append("last_error")
        return (-10003, "fake: IPC initialize failed")

    def terminal_info(self):
        self._call("terminal_info")
        return SimpleNamespace(connected=self.terminal_connected, company="MetaQuotes Ltd.")

    def account_info(self):
        self._call("account_info")
        return SimpleNamespace(
            login=12345678,
            name="Private Person",
            balance=1000.0,
            equity=1000.0,
            company=self.company,
            server=self.server,
            trade_mode=0,
        )

    # -- symbols
    def symbol_info(self, name):
        self._call("symbol_info")
        spec = self.symbols.get(name)
        if spec is None:
            return None
        if spec.get("metadata_missing"):  # a broker record without usable digits/point
            return SimpleNamespace(name=name, digits=None, point=None, visible=spec["visible"])
        return SimpleNamespace(
            name=name, digits=spec["digits"], point=spec["point"], visible=spec["visible"]
        )

    def symbols_get(self, group=None):
        self._call("symbols_get")
        names = [n for n in self.symbols if group is None or fnmatch.fnmatch(n, group)]
        return tuple(SimpleNamespace(name=name) for name in names)

    def symbol_select(self, name, enable=True):
        self._call("symbol_select")
        if name not in self.symbols:
            return False
        self.symbols[name]["visible"] = bool(enable)
        return True

    # -- data
    def symbol_info_tick(self, name):
        self._call("symbol_info_tick")
        spec = self.symbols.get(name)
        if spec is None or not spec["visible"]:
            return None
        if spec.get("loading"):
            # Real MT5 returns an all-zero tick for a symbol just added to Market Watch.
            return Tick(0, 0.0, 0.0, 0.0, 0, 0, 0, 0.0)
        epoch = self._server_epoch(self.now() - self.tick_age_seconds)
        return Tick(int(epoch), spec["bid"], spec["ask"], 0.0, 0, int(epoch * 1000), 6, 0.0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self._call("copy_rates_from_pos")
        spec = self.symbols.get(symbol)
        if spec is None:
            return None
        step = _SECONDS[timeframe]
        current_open = (int(self._server_epoch(self.now())) // step) * step
        newest = current_open - start * step
        rows = []
        for index in range(count):
            t = newest - (count - 1 - index) * step
            base = spec["base"] + ((t // step) % 7 - 3) * spec["point"] * 10
            high, low = base + spec["point"] * 25, base - spec["point"] * 20
            close = base + spec["point"] * (5 if (t // step) % 2 else -5)
            if t == current_open:  # the forming bar follows the live bid, like real MT5
                close = spec["bid"]
                high, low = max(high, close), min(low, close)
            rows.append(Rate(t, base, high, low, close, 100 + index, 7, 0))
        if self.rates_hook is not None:
            return self.rates_hook(symbol, rows)
        return rows
