"""Read-only adapter for the official MetaTrader 5 Python package.

This adapter contains no order, position-modification, or close operations. It
uses an already configured local terminal account and never accepts credentials.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from johanna.adapters.base import ConnectionStatus, MT5AdapterError
from johanna.fsutil import replace_with_retry
from johanna.time import get_timezone, strictly_increasing_utc, to_utc

_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}
_TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}

_SYMBOL_SPEC_FIELDS = {
    "name": "name",
    "description": "description",
    "path": "path",
    "currency_base": "currency_base",
    "currency_profit": "currency_profit",
    "currency_margin": "currency_margin",
    "digits": "digits",
    "point": "point",
    "contract_size": "trade_contract_size",
    "tick_size": "trade_tick_size",
    "tick_value": "trade_tick_value",
    "tick_value_profit": "trade_tick_value_profit",
    "tick_value_loss": "trade_tick_value_loss",
    "minimum_volume": "volume_min",
    "maximum_volume": "volume_max",
    "volume_step": "volume_step",
    "volume_limit": "volume_limit",
    "filling_mode": "filling_mode",
    "execution_mode": "trade_exemode",
    "trade_mode": "trade_mode",
    "calculation_mode": "trade_calc_mode",
    "stop_level_points": "trade_stops_level",
    "freeze_level_points": "trade_freeze_level",
    "spread_points": "spread",
    "spread_is_floating": "spread_float",
    "order_mode": "order_mode",
    "swap_mode": "swap_mode",
}


# Account fields that may leave the MT5 worker (P3.1). An allowlist: ``login``, ``name`` and
# any field added by a future package version are never copied, so they cannot reach the API,
# the browser or the logs.
ACCOUNT_FIELDS = (
    "currency",
    "currency_digits",
    "balance",
    "equity",
    "profit",
    "credit",
    "margin",
    "margin_free",
    "margin_level",
    "margin_mode",
    "margin_so_mode",
    "margin_so_call",
    "margin_so_so",
    "leverage",
    "limit_orders",
    "trade_allowed",
    "trade_expert",
    "trade_mode",
    "company",
    "server",
)
TERMINAL_FIELDS = ("connected", "trade_allowed", "tradeapi_disabled", "build")
# Symbol properties read for broker instrument specifications (P3.2), by MT5 field name.
SYMBOL_FIELDS = (
    "name",
    "path",
    "description",
    "visible",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "trade_contract_size",
    "digits",
    "point",
    "trade_tick_size",
    "trade_tick_value",
    "trade_tick_value_profit",
    "trade_tick_value_loss",
    "volume_min",
    "volume_max",
    "volume_step",
    "volume_limit",
    "trade_calc_mode",
    "trade_exemode",
    "order_mode",
    "filling_mode",
    "expiration_mode",
    "trade_stops_level",
    "trade_freeze_level",
    "trade_mode",
    "swap_mode",
    "swap_long",
    "swap_short",
    "swap_rollover3days",
    "spread",
    "spread_float",
    "time",
)


# Phase 4 exposure reads (allowlists; no tickets, comments, magic numbers or identifiers).
POSITION_FIELDS = (
    "symbol",
    "type",
    "volume",
    "price_open",
    "sl",
    "tp",
    "price_current",
    "profit",
    "swap",
    "time",
)
# price_stoplimit: the limit price of a stop-limit order (F4: its possible execution price).
ORDER_FIELDS = (
    "symbol",
    "type",
    "volume_current",
    "price_open",
    "price_stoplimit",
    "sl",
    "tp",
    "time_setup",
)
DEAL_FIELDS = (
    "time",
    "type",
    "entry",
    "symbol",
    "volume",
    "price",
    "profit",
    "swap",
    "commission",
    "fee",
)


# P6.3 reconciliation reads: the Phase 4 allowlists plus the identifiers needed to match a
# position to its deals. Identifiers are converted to HMAC references in the backend.
RECON_POSITION_FIELDS = (*POSITION_FIELDS, "ticket", "identifier")
RECON_ORDER_FIELDS = (*ORDER_FIELDS, "ticket", "position_id")
RECON_DEAL_FIELDS = (*DEAL_FIELDS, "ticket", "order", "position_id")


def _order_type(module: Any, side: str) -> int:
    """MT5 ``ORDER_TYPE_BUY`` / ``ORDER_TYPE_SELL`` code for a calculator call (0 / 1)."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    name = "ORDER_TYPE_BUY" if side == "buy" else "ORDER_TYPE_SELL"
    code = getattr(module, name, 0 if side == "buy" else 1)
    return int(code)


def _field(record: Any, name: str) -> Any:
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _as_mapping(record: Any) -> dict[str, Any]:
    if hasattr(record, "_asdict"):
        values = record._asdict()
    elif isinstance(record, dict):
        values = record
    else:
        values = {}
    return {key: value.item() if hasattr(value, "item") else value for key, value in values.items()}


_EPOCH = datetime(1970, 1, 1)


class MT5TimeBaseError(MT5AdapterError):
    """MT5 timestamps are inconsistent with UTC under the configured server clock."""


@dataclass(frozen=True)
class ServerClock:
    """How MT5 epoch values map to real UTC.

    MT5 ``time``/``time_msc`` fields are the trade server's *wall clock* encoded as seconds
    since 1970 -- not true UTC. A server clock is described as an IANA ``zone`` plus a
    whole-hour ``shift_hours``: server wall time = wall time in ``zone`` + ``shift_hours``.
    DST comes from the zone database, never from a fixed offset.

    Examples: a server on true UTC is ``ServerClock()``; a "New York close" server that
    opens every week at Monday 00:00 (UTC+2 in US winter, UTC+3 in US summer) is
    ``ServerClock("America/New_York", 7)``; a server documented as Athens time is
    ``ServerClock("Europe/Athens")``.
    """

    zone: str = "UTC"
    shift_hours: int = 0

    def __post_init__(self) -> None:
        get_timezone(self.zone)  # raises for an unknown IANA name
        if isinstance(self.shift_hours, bool) or not isinstance(self.shift_hours, int):
            raise ValueError("server time shift must be a whole number of hours")
        if not -12 <= self.shift_hours <= 14:
            raise ValueError("server time shift must be between -12 and 14 hours")

    @property
    def is_utc(self) -> bool:
        return self.zone == "UTC" and self.shift_hours == 0

    def describe(self) -> str:
        return self.zone if not self.shift_hours else f"{self.zone}{self.shift_hours:+d}h"

    def to_utc(self, epoch_seconds: float) -> datetime:
        """Convert an MT5 epoch (server wall clock) to an aware UTC datetime."""
        wall = _EPOCH + timedelta(seconds=float(epoch_seconds))
        if self.is_utc:
            return wall.replace(tzinfo=UTC)
        reference = wall - timedelta(hours=self.shift_hours)
        try:
            return to_utc(reference, self.zone)
        except ValueError as exc:  # DST gap or repeated hour in the reference zone
            raise MT5AdapterError(
                f"MT5 server time {wall:%Y-%m-%d %H:%M:%S} cannot be converted "
                f"unambiguously with server clock {self.describe()}: {exc}"
            ) from None

    def from_utc(self, value: datetime) -> float:
        """Inverse of ``to_utc``: an aware UTC instant as an MT5 epoch (server wall clock)."""
        instant = value.astimezone(UTC)
        if self.is_utc:
            wall = instant.replace(tzinfo=None)
        else:
            local = instant.astimezone(get_timezone(self.zone)).replace(tzinfo=None)
            wall = local + timedelta(hours=self.shift_hours)
        return (wall - _EPOCH).total_seconds()

    def bar_close_utc(self, open_utc: datetime, seconds: int) -> datetime:
        """UTC instant at which a bar of ``seconds`` length, opened at ``open_utc``, closes.

        MT5 bars span fixed lengths of *server* wall time, so the close is computed on the
        server clock (relevant for DST-observing server zones; identical under UTC).
        """
        return self.to_utc(self.from_utc(open_utc) + seconds)


class MetaTrader5Adapter:
    """Read ticks, bars, symbols, and symbol specifications from local MT5."""

    # A real tick or completed bar can never be later than "now"; allow for clock skew.
    FUTURE_TOLERANCE = timedelta(seconds=60)

    def __init__(
        self,
        terminal_path: str | Path | None = None,
        *,
        mt5_module: Any = None,
        server_clock: ServerClock | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.terminal_path = str(terminal_path) if terminal_path is not None else None
        self._mt5 = mt5_module
        self._initialized = False
        self.server_clock = server_clock or ServerClock()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _reject_future(self, timestamp: datetime, what: str) -> None:
        """Fail closed instead of storing timestamps shifted by an unconfigured server zone."""
        ahead = timestamp - self._clock()
        if ahead > self.FUTURE_TOLERANCE:
            hours = ahead.total_seconds() / 3600
            raise MT5TimeBaseError(
                f"MT5 {what} time {timestamp.isoformat()} is {hours:.2f} h ahead of this "
                f"computer's UTC clock. MT5 reports trade-server time; configure "
                f"mt5.server_time_zone and mt5.server_time_shift_hours (currently "
                f"{self.server_clock.describe()}) or correct the system clock."
            )

    def _module(self) -> Any:
        if self._mt5 is None:
            try:
                self._mt5 = importlib.import_module("MetaTrader5")
            except ImportError:
                raise MT5AdapterError(
                    "MetaTrader5 Python package is unavailable. On Windows install "
                    'the optional dependency with: python -m pip install -e ".[mt5]"'
                ) from None
        return self._mt5

    def _error_code(self) -> int | str | None:
        try:
            error = self._module().last_error()
        except Exception:
            return None
        if isinstance(error, tuple | list) and error:
            code = error[0]
            if isinstance(code, int | str):
                return code
        return None

    def _failure(self, operation: str) -> MT5AdapterError:
        code = self._error_code()
        detail = f" (MT5 error code {code})" if code is not None else ""
        return MT5AdapterError(
            f"MetaTrader 5 {operation} failed{detail}. Confirm the terminal is installed, "
            "open, logged in, and its data is available. Credentials are not displayed."
        )

    def _require_initialized(self) -> Any:
        module = self._module()
        if not self._initialized:
            raise MT5AdapterError(
                "MetaTrader 5 adapter is not initialized; call initialize() first"
            )
        return module

    def initialize(self) -> None:
        """Connect to the terminal's current account without accepting credentials."""
        module = self._module()
        try:
            ok = (
                module.initialize(self.terminal_path) if self.terminal_path else module.initialize()
            )
        except Exception:
            error = self._failure("initialization")
            try:
                module.shutdown()
            except Exception:
                pass
            raise error from None
        if not ok:
            error = self._failure("initialization")
            try:
                module.shutdown()
            except Exception:
                pass
            raise error
        self._initialized = True

    def connection_status(self) -> ConnectionStatus:
        """Return terminal and account connectivity separately."""
        module = self._require_initialized()
        try:
            terminal = module.terminal_info()
            account = module.account_info()
        except Exception:
            return ConnectionStatus(False, False)
        return ConnectionStatus(
            terminal_connected=bool(_field(terminal, "connected")),
            account_connected=account is not None,
        )

    def is_connected(self) -> bool:
        """Return true only when both the terminal and an account are connected."""
        return self.connection_status().connected

    def _require_connection(self) -> Any:
        module = self._require_initialized()
        if not self.is_connected():
            raise MT5AdapterError("MetaTrader 5 terminal or account is not connected")
        return module

    def server_identity(self) -> dict[str, Any]:
        """Broker company, trade-server name and account type only.

        Deliberately excludes the account login, holder name, balance and every other
        account identifier or value.
        """
        module = self._require_initialized()
        try:
            terminal = module.terminal_info()
            account = module.account_info()
        except Exception:
            raise self._failure("server identity") from None
        return {
            "company": _field(account, "company") if account is not None else None,
            "server": _field(account, "server") if account is not None else None,
            "account_type": _TRADE_MODES.get(_field(account, "trade_mode"))
            if account is not None
            else None,
            "terminal_connected": bool(_field(terminal, "connected")) if terminal else False,
        }

    def resolve_symbol(self, base: str) -> str | None:
        """Exact broker symbol for a pair such as ``EURUSD`` (e.g. ``EURUSD`` or ``EURUSDm``).

        Exact name first; otherwise the shortest broker symbol that starts with ``base``
        (account-type suffixes). Returns ``None`` when the broker has no such symbol.
        """
        module = self._require_connection()
        try:
            if module.symbol_info(base) is not None:
                return base
            candidates = module.symbols_get(group=f"{base}*") or ()
        except Exception:
            raise self._failure(f"symbol lookup for {base!r}") from None
        names = sorted(
            (str(_field(item, "name")) for item in candidates if _field(item, "name")),
            key=lambda name: (len(name), name),
        )
        names = [name for name in names if name.startswith(base) and len(name) <= len(base) + 4]
        return names[0] if names else None

    def price_format(self, symbol: str) -> dict[str, Any]:
        """Digits, point size and Market Watch visibility for an exact broker symbol."""
        module = self._require_connection()
        try:
            info = module.symbol_info(symbol)
        except Exception:
            raise self._failure(f"symbol information for {symbol!r}") from None
        if info is None:
            raise self._failure(f"symbol information for {symbol!r}")
        return {
            "digits": _optional_int(_field(info, "digits")),
            "point": _optional_positive_float(_field(info, "point")),
            "visible": bool(_field(info, "visible")),
        }

    def list_symbols(self) -> list[str]:
        """Return exact symbol names exposed by the connected broker terminal."""
        module = self._require_connection()
        try:
            symbols = module.symbols_get()
        except Exception:
            raise self._failure("symbol listing") from None
        if symbols is None:
            raise self._failure("symbol listing")
        return sorted(str(_field(symbol, "name")) for symbol in symbols if _field(symbol, "name"))

    def select_symbol(self, symbol: str) -> None:
        """Select the exact broker symbol in Market Watch for data access."""
        module = self._require_connection()
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty exact broker symbol name")
        try:
            ok = module.symbol_select(symbol, True)
        except Exception:
            raise self._failure("symbol selection") from None
        if not ok:
            raise self._failure(f"selection of symbol {symbol!r}")

    def get_tick(self, symbol: str) -> dict[str, Any]:
        """Return the latest tick with its MT5 epoch time converted to UTC."""
        module = self._require_connection()
        try:
            record = module.symbol_info_tick(symbol)
        except Exception:
            raise self._failure(f"tick retrieval for {symbol!r}") from None
        if record is None:
            raise self._failure(f"tick retrieval for {symbol!r}")
        result = _as_mapping(record)
        time_msc = result.get("time_msc")
        seconds = time_msc / 1000 if time_msc is not None else result.get("time")
        if seconds is not None:
            result["time_utc"] = self.server_clock.to_utc(float(seconds))
            self._reject_future(result["time_utc"], f"{symbol} tick")
        return result

    def get_bars(
        self, symbol: str, timeframe: str = "M5", count: int = 5, *, include_current: bool = False
    ) -> list[dict[str, Any]]:
        """Return bars with server-clock epochs converted to UTC bar-open times.

        Completed bars only by default; ``include_current=True`` also returns the bar that is
        still forming (its values change until it closes). Sorted, and strict: duplicate or
        regressing timestamps raise (the collector stores these rows as they are). The live
        workspace uses ``read_bars`` instead, which reports such anomalies per bar.
        """
        rows = self.read_bars(symbol, timeframe, count, include_current=include_current)
        rows.sort(key=lambda row: row["timestamp"])
        strictly_increasing_utc([row["timestamp"] for row in rows])
        for row in rows:
            del row["spread_points"]  # not part of the stored bar schema
        return rows

    def read_bars(
        self, symbol: str, timeframe: str = "M5", count: int = 5, *, include_current: bool = False
    ) -> list[dict[str, Any]]:
        """Bars exactly as MT5 returned them (its order, duplicates included), UTC open times.

        Like ``get_bars`` plus ``spread_points`` (MT5's per-bar spread in points, ``None``
        when the record has none), but without sorting or rejecting duplicate/out-of-order
        timestamps, so a caller can report such bars individually. Values are not validated.
        A timestamp ahead of UTC still fails the whole read: that is a server-clock problem,
        not a property of one bar.
        """
        module = self._require_connection()
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use {', '.join(_TIMEFRAMES)}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        timeframe_code = getattr(module, _TIMEFRAMES[timeframe], None)
        if timeframe_code is None:
            raise MT5AdapterError(f"Installed MT5 package does not expose {timeframe}")
        try:
            rates = module.copy_rates_from_pos(
                symbol, timeframe_code, 0 if include_current else 1, count
            )
        except Exception:
            raise self._failure(f"{timeframe} bar retrieval for {symbol!r}") from None
        if rates is None:
            raise self._failure(f"{timeframe} bar retrieval for {symbol!r}")

        names = getattr(getattr(rates, "dtype", None), "names", None)
        result: list[dict[str, Any]] = []
        for rate in rates:
            if names:
                item = {
                    name: (rate[name].item() if hasattr(rate[name], "item") else rate[name])
                    for name in names
                }
            elif hasattr(rate, "_asdict"):
                item = _as_mapping(rate)
            else:
                raise MT5AdapterError("MT5 returned bars in an unsupported record format")
            bar_time = item.get("time")
            if bar_time is None:
                raise MT5AdapterError("MT5 bar record has no time field")
            timestamp = self.server_clock.to_utc(float(bar_time))
            result.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "tick_volume": int(item["tick_volume"]) if "tick_volume" in item else None,
                    "real_volume": int(item["real_volume"]) if "real_volume" in item else None,
                    "spread_points": _optional_int(item.get("spread")),
                    "source": "MT5",
                    # IANA zone of the server clock; "UTC" also marks legacy rows that were
                    # stored before server-time conversion existed (see migration tool).
                    "source_timezone": self.server_clock.zone,
                    "source_timestamp_convention": "bar_open",
                }
            )
        if result:
            newest = max(row["timestamp"] for row in result)
            self._reject_future(newest, f"{symbol} {timeframe} bar")
        return result

    def account_snapshot(self) -> dict[str, Any]:
        """Allowlisted account and terminal properties, as MT5 returned them (read-only).

        A field the installed package or broker does not supply is *absent* from the result
        (not ``None``), so callers can tell "not supplied" from a supplied value. Never
        contains the login, holder name or any other identifier outside ``ACCOUNT_FIELDS``.
        """
        module = self._require_connection()
        try:
            account = module.account_info()
            terminal = module.terminal_info()
        except Exception:
            raise self._failure("account information") from None
        if account is None:
            raise self._failure("account information")
        return {
            "account": _present(account, ACCOUNT_FIELDS),
            "terminal": _present(terminal, TERMINAL_FIELDS) if terminal is not None else {},
        }

    def symbol_snapshot(self, symbol: str) -> dict[str, Any]:
        """Allowlisted ``symbol_info`` properties of an exact broker symbol (read-only).

        Absent fields mean MT5 did not supply them. Values are not validated here.
        """
        module = self._require_connection()
        try:
            info = module.symbol_info(symbol)
        except Exception:
            raise self._failure(f"specification retrieval for {symbol!r}") from None
        if info is None:
            raise self._failure(f"specification retrieval for {symbol!r}")
        return _present(info, SYMBOL_FIELDS)

    # ------------------------------------------------------------ Phase 4 (read-only)
    def exposure_snapshot(self, from_epoch: float, to_epoch: float) -> dict[str, Any]:
        """Open positions, pending orders and the deals in ``[from_epoch, to_epoch]``.

        Read-only (``positions_get``, ``orders_get``, ``history_deals_get``). Epochs are MT5
        server-clock seconds. Only allowlisted fields are copied: tickets, comments, magic
        numbers, external ids and every account identifier are left behind. A ``None`` answer
        from MT5 means the read failed (an empty tuple means "none").
        """
        module = self._require_connection()
        try:
            positions = module.positions_get()
            orders = module.orders_get()
            deals = module.history_deals_get(int(from_epoch), int(to_epoch))
        except Exception:
            raise self._failure("exposure read") from None
        for name, value in (("positions", positions), ("orders", orders), ("deals", deals)):
            if value is None:
                raise self._failure(f"{name} read")
        return {
            "positions": [_present(item, POSITION_FIELDS) for item in positions],
            "orders": [_present(item, ORDER_FIELDS) for item in orders],
            "deals": [_present(item, DEAL_FIELDS) for item in deals],
        }

    # ------------------------------------------------------------ P6.3 (read-only)
    def reconciliation_snapshot(self, from_epoch: float, to_epoch: float) -> dict[str, Any]:
        """Open positions, pending orders and the deals in ``[from_epoch, to_epoch]`` WITH their
        broker identifiers, for reconciliation only.

        Read-only (``positions_get``, ``orders_get``, ``history_deals_get``). The identifiers
        leave the worker only to be replaced by HMAC references in the backend
        (``JournalStore.broker_ref``) before anything is stored, logged or shown; comments,
        magic numbers, external ids and account identifiers are never copied.
        """
        module = self._require_connection()
        try:
            positions = module.positions_get()
            orders = module.orders_get()
            deals = module.history_deals_get(int(from_epoch), int(to_epoch))
        except Exception:
            raise self._failure("reconciliation read") from None
        for name, value in (("positions", positions), ("orders", orders), ("deals", deals)):
            if value is None:
                raise self._failure(f"{name} read")
        return {
            "positions": [_present(i, RECON_POSITION_FIELDS) for i in positions],
            "orders": [_present(i, RECON_ORDER_FIELDS) for i in orders],
            "deals": [_present(i, RECON_DEAL_FIELDS) for i in deals],
        }

    def calc_profit(
        self, side: str, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float:
        """MT5's terminal-side profit calculator (``order_calc_profit``), in account currency.

        A calculation only: nothing is sent to the trade server and no order is created.
        """
        module = self._require_connection()
        try:
            value = module.order_calc_profit(
                _order_type(module, side),
                symbol,
                float(volume),
                float(price_open),
                float(price_close),
            )
        except Exception:
            raise self._failure(f"profit calculation for {symbol!r}") from None
        if value is None:
            raise self._failure(f"profit calculation for {symbol!r}")
        return float(value)

    def calc_margin(self, side: str, symbol: str, volume: float, price: float) -> float:
        """MT5's terminal-side margin calculator (``order_calc_margin``), in account currency.

        A calculation only: nothing is sent to the trade server and no order is created.
        """
        module = self._require_connection()
        try:
            value = module.order_calc_margin(
                _order_type(module, side), symbol, float(volume), float(price)
            )
        except Exception:
            raise self._failure(f"margin calculation for {symbol!r}") from None
        if value is None:
            raise self._failure(f"margin calculation for {symbol!r}")
        return float(value)

    def capture_symbol_metadata(self, symbol: str) -> dict[str, Any]:
        """Capture known symbol specifications; unavailable fields remain null."""
        module = self._require_connection()
        try:
            info = module.symbol_info(symbol)
        except Exception:
            raise self._failure(f"specification retrieval for {symbol!r}") from None
        if info is None:
            raise self._failure(f"specification retrieval for {symbol!r}")

        properties = {
            output_name: _field(info, attribute)
            for output_name, attribute in _SYMBOL_SPEC_FIELDS.items()
        }
        properties["name"] = _field(info, "name") or symbol
        properties["filling_mode_names"] = _filling_mode_names(module, properties["filling_mode"])
        properties["execution_mode_name"] = _constant_name(
            module,
            properties["execution_mode"],
            (
                "SYMBOL_TRADE_EXECUTION_REQUEST",
                "SYMBOL_TRADE_EXECUTION_INSTANT",
                "SYMBOL_TRADE_EXECUTION_MARKET",
                "SYMBOL_TRADE_EXECUTION_EXCHANGE",
            ),
        )
        terminal_version = None
        try:
            terminal_version = module.version()
        except Exception:
            pass
        try:
            account = module.account_info()
        except Exception:
            account = None
        server_name = _field(account, "server")
        return {
            "captured_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "provenance": {
                "source": "MetaTrader5 Python package",
                "exact_symbol_name": symbol,
                "terminal_version": list(terminal_version) if terminal_version else None,
                "broker_server_name": server_name,
            },
            "symbol_specification": properties,
            "server_time": {
                "status": "unknown",
                "value": None,
                "reason": (
                    "The terminal data used here does not establish the broker server timezone "
                    "or UTC offset; no machine-timezone assumption was made."
                ),
            },
        }

    @staticmethod
    def save_metadata(metadata: dict[str, Any], destination: str | Path) -> Path:
        """Write metadata JSON atomically without printing account details."""
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(metadata, stream, indent=2, sort_keys=True, default=_json_default)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            replace_with_retry(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return path

    def shutdown(self) -> None:
        """Close the terminal connection, safe to call repeatedly."""
        if self._mt5 is not None and self._initialized:
            try:
                self._mt5.shutdown()
            finally:
                self._initialized = False

    def __enter__(self) -> MetaTrader5Adapter:
        self.initialize()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.shutdown()


_MISSING = object()


def _present(record: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """The listed fields that ``record`` actually has (numpy scalars made plain)."""
    values: dict[str, Any] = {}
    for name in names:
        if isinstance(record, dict):
            value = record.get(name, _MISSING)
        else:
            value = getattr(record, name, _MISSING)
        if value is _MISSING:
            continue
        values[name] = value.item() if hasattr(value, "item") else value
    return values


def _optional_int(value: Any) -> int | None:
    """An integer field, or ``None`` when MT5 did not provide a usable one."""
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _optional_positive_float(value: Any) -> float | None:
    """A positive finite number, or ``None`` (missing, zero or invalid metadata)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize metadata value of type {type(value).__name__}")


def _constant_name(module: Any, value: Any, names: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    for name in names:
        if getattr(module, name, object()) == value:
            return name
    return None


def _filling_mode_names(module: Any, value: Any) -> list[str] | None:
    if not isinstance(value, int):
        return None
    flags = (
        "SYMBOL_FILLING_FOK",
        "SYMBOL_FILLING_IOC",
    )
    found = [name for name in flags if isinstance(getattr(module, name, None), int)]
    if not found:
        return None
    return [name for name in found if value & getattr(module, name)]


def metadata_path(directory: str | Path, symbol: str) -> Path:
    """Build a safe filename while preserving the exact symbol inside the JSON."""
    return Path(directory) / f"{quote(symbol, safe='')}.json"
