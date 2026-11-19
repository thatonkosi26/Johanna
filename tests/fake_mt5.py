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
    # Phase 4: read-only exposure reads and terminal-side calculators (no order is sent)
    "positions_get",
    "orders_get",
    "history_deals_get",
    "order_calc_profit",
    "order_calc_margin",
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
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1

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
        # Account record (Phase 3). Synthetic values shaped like an Exness Standard Cent
        # account (currency USC). ``login`` and ``name`` must never leave the adapter.
        self.account = {
            "login": 12345678,
            "name": "Private Person",
            "currency": "USC",
            "currency_digits": 2,
            "balance": 1000.0,
            "equity": 1000.0,
            "profit": 0.0,
            "credit": 0.0,
            "margin": 0.0,
            "margin_free": 1000.0,
            "margin_level": 0.0,
            "margin_mode": 2,
            "margin_so_mode": 0,
            "margin_so_call": 60.0,
            "margin_so_so": 0.0,
            "leverage": 2000,
            "limit_orders": 200,
            "trade_allowed": True,
            "trade_expert": True,
            "trade_mode": 0,
        }
        self.account_missing: set[str] = set()  # account fields the "broker" does not supply
        self.terminal = {"trade_allowed": True, "tradeapi_disabled": False, "build": 5000}
        # Phase 4 exposure (lists of dicts) and test hooks.
        self.positions: list[dict] = []
        self.orders: list[dict] = []
        self.deals: list[dict] = []
        self.failing_reads: set[str] = set()  # e.g. {"positions_get"}: that read returns None
        self.deal_ranges: list[tuple] = []
        self.calc_hook = None  # (name, args, value) -> value, to inject calculator disagreement

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
        return SimpleNamespace(
            connected=self.terminal_connected, company="MetaQuotes Ltd.", **self.terminal
        )

    def account_info(self):
        self._call("account_info")
        values = {**self.account, "company": self.company, "server": self.server}
        return SimpleNamespace(
            **{key: value for key, value in values.items() if key not in self.account_missing}
        )

    # -- symbols
    def symbol_info(self, name):
        self._call("symbol_info")
        spec = self.symbols.get(name)
        if spec is None:
            return None
        if spec.get("metadata_missing"):  # a broker record without usable digits/point
            return SimpleNamespace(name=name, digits=None, point=None, visible=spec["visible"])
        values = {**_specification(name, spec), **spec.get("info", {})}
        for key in spec.get("info_missing", ()):
            values.pop(key, None)
        return SimpleNamespace(**values)

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

    # -- Phase 4: exposure and calculators (read-only)
    def positions_get(self):
        self._call("positions_get")
        if "positions_get" in self.failing_reads:
            return None
        return tuple(
            SimpleNamespace(**{"ticket": 900001, "comment": "private", "magic": 0, **p})
            for p in self.positions
        )

    def orders_get(self):
        self._call("orders_get")
        if "orders_get" in self.failing_reads:
            return None
        return tuple(
            SimpleNamespace(**{"ticket": 900002, "comment": "private", **o}) for o in self.orders
        )

    def history_deals_get(self, date_from, date_to):
        self._call("history_deals_get")
        self.deal_ranges.append((date_from, date_to))
        if "history_deals_get" in self.failing_reads:
            return None
        return tuple(
            SimpleNamespace(
                **{
                    "ticket": 900003,
                    "order": 900004,
                    "position_id": 900005,
                    "comment": "private",
                    **d,  # P6.3 tests give each deal its own ticket / position
                }
            )
            for d in self.deals
            if date_from <= d["time"] <= date_to
        )

    def _conversion(self, symbol, *, loss):
        """USC per unit of the symbol's profit currency (synthetic cent account)."""
        spec = self.symbols[symbol]
        if symbol[3:6] == "USD":
            return 100.0
        return 100.0 / (spec["bid"] if loss else spec["ask"])

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        self._call("order_calc_profit")
        if symbol not in self.symbols:
            return None
        sign = 1 if action == self.ORDER_TYPE_BUY else -1
        move = (price_close - price_open) * sign
        value = move * 1000.0 * volume * self._conversion(symbol, loss=move < 0)
        if self.calc_hook is not None:
            value = self.calc_hook(
                "profit", (action, symbol, volume, price_open, price_close), value
            )
        return value

    def order_calc_margin(self, action, symbol, volume, price):
        self._call("order_calc_margin")
        if symbol not in self.symbols:
            return None
        rate = 100.0 if symbol[:3] == "USD" else price * 100.0
        value = 1000.0 * volume * rate / self.account["leverage"]
        if self.calc_hook is not None:
            value = self.calc_hook("margin", (action, symbol, volume, price), value)
        return value

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


def _specification(name: str, spec: dict) -> dict:
    """Synthetic symbol properties shaped like an Exness Standard Cent forex symbol: contract
    size 1000 and tick values in US cents (USC), consistent with the synthetic prices."""
    base, profit = name[:3], name[3:6]
    tick, contract = spec["point"], 1000.0
    if profit == "USD":
        value_profit = value_loss = tick * contract * 100
    else:  # USD base: converted through the symbol's own price
        value_profit, value_loss = (
            tick * contract * 100 / spec["ask"],
            tick * contract * 100 / spec["bid"],
        )
    return {
        "name": name,
        "path": f"Cent\\Forex\\{name}",
        "description": f"{base} vs {profit} (synthetic)",
        "visible": spec["visible"],
        "currency_base": base,
        "currency_profit": profit,
        "currency_margin": base,
        "trade_contract_size": contract,
        "digits": spec["digits"],
        "point": spec["point"],
        "trade_tick_size": tick,
        "trade_tick_value": value_profit,
        "trade_tick_value_profit": value_profit,
        "trade_tick_value_loss": value_loss,
        "volume_min": 0.01,
        "volume_max": 200.0,
        "volume_step": 0.01,
        "volume_limit": 0.0,
        "trade_calc_mode": 0,
        "trade_exemode": 2,
        "order_mode": 127,
        "filling_mode": 3,
        "expiration_mode": 15,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "trade_mode": 4,
        "swap_mode": 1,
        # Phase 1 (M-10): zero by default, so results never depend on the wall-clock rollover
        # schedule; swap tests set rates explicitly (risk_fixtures.negative_swap).
        "swap_long": 0.0,
        "swap_short": 0.0,
        "swap_rollover3days": 3,
        "spread": round((spec["ask"] - spec["bid"]) / spec["point"]),
        "spread_float": True,
        "time": 0,
    }
