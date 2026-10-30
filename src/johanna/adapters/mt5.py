"""Read-only adapter for the official MetaTrader 5 Python package.

This adapter contains no order, position-modification, or close operations. It
uses an already configured local terminal account and never accepts credentials.
"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from johanna.adapters.base import ConnectionStatus, MT5AdapterError
from johanna.time import strictly_increasing_utc

_TIMEFRAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
}

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


class MetaTrader5Adapter:
    """Read ticks, bars, symbols, and symbol specifications from local MT5."""

    def __init__(self, terminal_path: str | Path | None = None, *, mt5_module: Any = None):
        self.terminal_path = str(terminal_path) if terminal_path is not None else None
        self._mt5 = mt5_module
        self._initialized = False

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
            result["time_utc"] = datetime.fromtimestamp(float(seconds), UTC)
        return result

    def get_bars(self, symbol: str, timeframe: str = "M5", count: int = 5) -> list[dict[str, Any]]:
        """Return completed bars, using MT5's documented UTC bar-open timestamps."""
        module = self._require_connection()
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe!r}; use {', '.join(_TIMEFRAMES)}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        timeframe_code = getattr(module, _TIMEFRAMES[timeframe], None)
        if timeframe_code is None:
            raise MT5AdapterError(f"Installed MT5 package does not expose {timeframe}")
        try:
            rates = module.copy_rates_from_pos(symbol, timeframe_code, 1, count)
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
            timestamp = datetime.fromtimestamp(float(bar_time), UTC)
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
                    "source": "MT5",
                    "source_timezone": "UTC",
                    "source_timestamp_convention": "bar_open",
                }
            )
        result.sort(key=lambda row: row["timestamp"])
        strictly_increasing_utc([row["timestamp"] for row in result])
        return result

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
            temp_path.replace(path)
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
