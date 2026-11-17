"""Account and broker intelligence (Phase 3): read-only, validated, provenance-stamped.

Reads the connected MT5 account and the broker's specifications for the supported pairs through
the existing live-market worker (``LiveMarketService.broker_read``), validates every field and
reports it with its MT5 source field and one of these statuses:

- ``verified``: supplied by MT5, passes its validation rule and every applicable consistency
  check, and broker provenance for the read is established;
- ``unavailable``: MT5 did not supply the field (absent or null);
- ``invalid``: supplied but fails validation, a consistency check, or provenance;
- ``not_applicable``: the field has no meaning in the current state (for example the margin
  level while no margin is in use, which MT5 reports as 0);
- ``unverified``: supplied and well-formed, but no independent check exists for it (for
  example a tick value whose profit currency has no conversion path to the account currency).

No defaults are ever substituted. Nothing here places, modifies or closes orders, and this
module is independent of the T5/T6 market analysis.

**Exness Cent accounts** (observed live on the connected Exness server, 17 Nov 2026): the
account currency is ``USC`` -- US cents, a broker unit equal to 1/100 USD, not an ISO 4217
code -- symbols carry a ``c`` suffix (``EURUSDc``), the contract size is 1000 units, and tick
values are quoted in USC. The cent interpretation is not assumed: it is confirmed per read by
reconciling each symbol's tick value with its tick size, contract size and price
(``currency_unit`` check).

The account login, holder name and other identifiers never reach this module: the adapter
copies only allowlisted fields (``johanna.adapters.mt5.ACCOUNT_FIELDS``).
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.live_market import MarketUnavailable

LOGGER = logging.getLogger("johanna.account_intelligence")

VERIFIED = "verified"
UNAVAILABLE = "unavailable"
INVALID = "invalid"
NOT_APPLICABLE = "not_applicable"
UNVERIFIED = "unverified"

SUPPORTED_PAIRS = ("EURUSD", "GBPUSD", "USDJPY")
_CODE = re.compile(r"[A-Z]{3}")
# ISO 4217 codes accepted as an account or instrument currency (common trading currencies).
ISO_CURRENCIES = frozenset(
    "AED AUD BGN BRL CAD CHF CLP CNH CNY COP CZK DKK EUR GBP HKD HUF IDR ILS INR ISK JPY KES "
    "KRW KWD MXN MYR NGN NOK NZD PHP PLN QAR RON RUB SAR SEK SGD THB TRY TWD USD ZAR".split()
)
# Broker cent units: 1 unit = 1/100 of the major currency (Exness Standard Cent).
CENT_UNITS = {"USC": "USD", "EUC": "EUR"}
# Price movement allowed between the specification read and the tick read when a tick value
# is reconciled through the symbol's own price.
PRICE_RECONCILE_TOLERANCE = 0.002

_ACCOUNT_TRADE_MODES = {0: "demo", 1: "contest", 2: "real"}
_MARGIN_MODES = {0: "retail_netting", 1: "exchange", 2: "retail_hedging"}
_STOPOUT_MODES = {0: "percent", 1: "money"}
_CALC_MODES = {
    0: "forex",
    1: "futures",
    2: "cfd",
    3: "cfd_index",
    4: "cfd_leverage",
    5: "forex_no_leverage",
    32: "exchange_stocks",
    33: "exchange_futures",
    34: "exchange_futures_forts",
    35: "exchange_bonds",
    36: "exchange_stocks_moex",
    37: "exchange_bonds_moex",
    38: "service_collateral",
}
_EXECUTION_MODES = {0: "request", 1: "instant", 2: "market", 3: "exchange"}
_SYMBOL_TRADE_MODES = {0: "disabled", 1: "long_only", 2: "short_only", 3: "close_only", 4: "full"}
_SWAP_MODES = {
    0: "disabled",
    1: "points",
    2: "symbol_base_currency",
    3: "margin_currency",
    4: "deposit_currency",
    5: "interest_current_price",
    6: "interest_open_price",
    7: "reopen_current_price",
    8: "reopen_bid_price",
}
_WEEKDAYS = {
    0: "sunday",
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
}
# Documented MQL5 flag values (the Python package does not expose all of these constants).
_ORDER_FLAGS = (
    (1, "market"),
    (2, "limit"),
    (4, "stop"),
    (8, "stop_limit"),
    (16, "stop_loss"),
    (32, "take_profit"),
    (64, "close_by"),
)
_FILLING_FLAGS = ((1, "fok"), (2, "ioc"), (4, "boc"))

ACCOUNT_REQUIRED = ("currency", "balance", "equity", "margin", "margin_free", "leverage")
INSTRUMENT_REQUIRED = (
    "broker_symbol",
    "currency_base",
    "currency_profit",
    "contract_size",
    "digits",
    "point",
    "tick_size",
    "tick_value",
    "volume_min",
    "volume_max",
    "volume_step",
    "trade_mode",
)
_MISSING = object()


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


# ====================================================================== field reports
def _field(
    value: Any, status: str, source: str, reason: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {"value": value, "status": status, "source": source, "reason": reason, **extra}


def _missing(source: str) -> dict[str, Any]:
    return _field(None, UNAVAILABLE, source, "MT5 did not supply this field")


def _number(
    raw: dict[str, Any],
    key: str,
    source: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
    integer: bool = False,
    unit: str | None = None,
) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return {**_missing(source), "unit": unit}
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _field(None, INVALID, source, f"not a number ({type(value).__name__})", unit=unit)
    if not math.isfinite(value):
        # Non-finite values are never echoed: they are not valid JSON.
        return _field(None, INVALID, source, f"not a finite number ({value!r})", unit=unit)
    if integer and value != int(value):
        return _field(value, INVALID, source, "not a whole number", unit=unit)
    if positive and value <= 0:
        return _field(value, INVALID, source, "must be positive", unit=unit)
    if non_negative and value < 0:
        return _field(value, INVALID, source, "must not be negative", unit=unit)
    return _field(int(value) if integer else value, VERIFIED, source, unit=unit)


def _flag(raw: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return _missing(source)
    if not isinstance(value, bool):
        return _field(None, INVALID, source, f"not a boolean ({type(value).__name__})")
    return _field(value, VERIFIED, source)


def _enum(raw: dict[str, Any], key: str, source: str, names: dict[int, str]) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return _missing(source)
    if isinstance(value, bool) or not isinstance(value, int):
        return _field(None, INVALID, source, f"not an integer code ({type(value).__name__})")
    if value not in names:
        return _field(None, INVALID, source, f"unknown code {value}", raw=value)
    return _field(names[value], VERIFIED, source, raw=value)


def _flags(raw: dict[str, Any], key: str, source: str, flags: tuple) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return _missing(source)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return _field(None, INVALID, source, "not a non-negative integer bit mask")
    known = sum(bit for bit, _ in flags)
    if value & ~known:
        return _field(None, INVALID, source, f"unknown flag bits in {value}", raw=value)
    return _field([name for bit, name in flags if value & bit], VERIFIED, source, raw=value)


def _currency(raw: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return _missing(source)
    if not isinstance(value, str) or not value:
        return _field(None, INVALID, source, "empty or not text")
    if not _CODE.fullmatch(value):
        return _field(value, INVALID, source, "not a three-letter upper-case currency code")
    if value in CENT_UNITS:
        return _field(
            value,
            VERIFIED,
            source,
            unit={"kind": "cent", "major": CENT_UNITS[value], "units_per_major": 100},
        )
    if value not in ISO_CURRENCIES:
        return _field(value, INVALID, source, "not a recognised currency code")
    return _field(value, VERIFIED, source, unit={"kind": "iso", "major": value})


def _text(raw: dict[str, Any], key: str, source: str) -> dict[str, Any]:
    value = raw.get(key, _MISSING)
    if value is _MISSING or value is None:
        return _missing(source)
    if not isinstance(value, str) or not value.strip():
        return _field(None, INVALID, source, "empty or not text")
    return _field(value, VERIFIED, source)


def _ok(*reports: dict[str, Any]) -> bool:
    return all(report["status"] == VERIFIED for report in reports)


def _invalidate(report: dict[str, Any], reason: str) -> None:
    if report["status"] in (VERIFIED, UNVERIFIED, NOT_APPLICABLE):
        report["status"] = INVALID
        report["reason"] = reason


def _close(a: float, b: float, *, absolute: float = 0.0, relative: float = 1e-9) -> bool:
    return abs(a - b) <= max(absolute, relative * max(abs(a), abs(b)))


def _check(name: str, status: str, detail: str, **values: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **values}


# ====================================================================== provenance
def _provenance(payload: dict[str, Any]) -> dict[str, Any]:
    """Broker provenance of one read: who answered, and did it stay the same server."""
    before = payload.get("identity") or {}
    value = payload.get("value") or {}
    after = value.get("identity_after") or {}
    account = value.get("account") or {}
    terminal = value.get("terminal") or {}
    keys = ("company", "server", "account_type")
    problems = []
    if not isinstance(before.get("server"), str) or not before.get("server"):
        problems.append("trade server name not supplied")
    if not isinstance(before.get("company"), str) or not before.get("company"):
        problems.append("broker company not supplied")
    if any(before.get(key) != after.get(key) for key in keys):
        problems.append("broker identity changed during the read")
    if account.get("server") != before.get("server") or account.get("company") != before.get(
        "company"
    ):
        problems.append("account record names a different broker or server")
    return {
        "status": "established" if not problems else "not_established",
        "reason": "; ".join(problems) or None,
        "source": "MetaTrader5 Python package (read-only account_info, symbol_info, ticks)",
        "company": before.get("company"),
        "server": before.get("server"),
        "account_type": before.get("account_type"),
        "terminal_build": terminal.get("build"),
        "identity_rechecked_after_read": all(before.get(key) == after.get(key) for key in keys),
    }


# ====================================================================== account (P3.1)
def evaluate_account(raw: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
    """Validated account fields (no provenance gate; see ``evaluate_snapshot``)."""
    currency = _currency(raw, "currency", "account_info.currency")
    money = currency["value"] if currency["status"] == VERIFIED else None
    fields: dict[str, dict[str, Any]] = {
        "currency": currency,
        "currency_digits": _number(
            raw, "currency_digits", "account_info.currency_digits", integer=True, non_negative=True
        ),
        "balance": _number(raw, "balance", "account_info.balance", unit=money),
        "equity": _number(raw, "equity", "account_info.equity", unit=money),
        "profit": _number(raw, "profit", "account_info.profit", unit=money),
        "credit": _number(raw, "credit", "account_info.credit", unit=money),
        "margin": _number(raw, "margin", "account_info.margin", non_negative=True, unit=money),
        "margin_free": _number(raw, "margin_free", "account_info.margin_free", unit=money),
        "margin_level": _number(
            raw, "margin_level", "account_info.margin_level", non_negative=True, unit="%"
        ),
        "leverage": _number(
            raw, "leverage", "account_info.leverage", integer=True, positive=True, unit="1:x"
        ),
        "trade_allowed": _flag(raw, "trade_allowed", "account_info.trade_allowed"),
        "trade_expert": _flag(raw, "trade_expert", "account_info.trade_expert"),
        "terminal_trade_allowed": _flag(terminal, "trade_allowed", "terminal_info.trade_allowed"),
        "terminal_tradeapi_disabled": _flag(
            terminal, "tradeapi_disabled", "terminal_info.tradeapi_disabled"
        ),
        "limit_orders": _number(
            raw, "limit_orders", "account_info.limit_orders", integer=True, non_negative=True
        ),
        "trade_mode": _enum(raw, "trade_mode", "account_info.trade_mode", _ACCOUNT_TRADE_MODES),
        "margin_mode": _enum(raw, "margin_mode", "account_info.margin_mode", _MARGIN_MODES),
        "margin_so_mode": _enum(
            raw, "margin_so_mode", "account_info.margin_so_mode", _STOPOUT_MODES
        ),
    }
    so_unit = {"percent": "%", "money": money}.get(fields["margin_so_mode"]["value"])
    fields["margin_so_call"] = _number(
        raw, "margin_so_call", "account_info.margin_so_call", non_negative=True, unit=so_unit
    )
    fields["margin_so_so"] = _number(
        raw, "margin_so_so", "account_info.margin_so_so", non_negative=True, unit=so_unit
    )
    checks = _account_checks(fields)
    return {"fields": fields, "checks": checks}


def _account_checks(f: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    digits = f["currency_digits"]["value"] if f["currency_digits"]["status"] == VERIFIED else 2
    money_tol = 10.0 ** -min(int(digits), 8)
    checks = []
    bal, eq, credit, profit = f["balance"], f["equity"], f["credit"], f["profit"]
    if _ok(bal, eq, credit, profit):
        expected = bal["value"] + credit["value"] + profit["value"]
        if _close(eq["value"], expected, absolute=money_tol):
            checks.append(_check("equity", "consistent", "equity = balance + credit + profit"))
        else:
            reason = "inconsistent: equity differs from balance + credit + profit"
            for report in (bal, eq, credit, profit):
                _invalidate(report, reason)
            checks.append(
                _check("equity", "discrepancy", reason, equity=eq["value"], expected=expected)
            )
    else:
        checks.append(_check("equity", "not_checkable", "a required value is not verified"))
    margin, free = f["margin"], f["margin_free"]
    if _ok(eq, margin, free):
        expected = eq["value"] - margin["value"]
        if _close(free["value"], expected, absolute=money_tol):
            checks.append(_check("free_margin", "consistent", "free margin = equity - margin"))
        else:
            reason = "inconsistent: free margin differs from equity - margin"
            for report in (eq, margin, free):
                _invalidate(report, reason)
            checks.append(
                _check(
                    "free_margin",
                    "discrepancy",
                    reason,
                    free_margin=free["value"],
                    expected=expected,
                )
            )
    else:
        checks.append(_check("free_margin", "not_checkable", "a required value is not verified"))
    level = f["margin_level"]
    if margin["status"] == VERIFIED and margin["value"] == 0:
        if level["status"] in (VERIFIED, INVALID) and level["value"] not in (0, 0.0, None):
            _invalidate(level, "inconsistent: a margin level is reported while no margin is used")
            checks.append(_check("margin_level", "discrepancy", level["reason"]))
        else:
            level.update(
                status=NOT_APPLICABLE,
                reason="no margin in use; MT5 reports 0 for an undefined margin level",
            )
            checks.append(_check("margin_level", "not_applicable", "no margin in use"))
    elif _ok(eq, margin, level):
        expected = eq["value"] / margin["value"] * 100
        if _close(level["value"], expected, absolute=0.01, relative=1e-4):
            checks.append(_check("margin_level", "consistent", "margin level = equity / margin"))
        else:
            reason = "inconsistent: margin level differs from equity / margin x 100"
            _invalidate(level, reason)
            checks.append(
                _check(
                    "margin_level",
                    "discrepancy",
                    reason,
                    reported=level["value"],
                    expected=expected,
                )
            )
    else:
        checks.append(_check("margin_level", "not_checkable", "a required value is not verified"))
    return checks


# ====================================================================== instruments (P3.2)
def evaluate_instrument(
    pair: str,
    entry: dict[str, Any],
    account_currency: dict[str, Any],
    *,
    now: datetime,
    stale_after: timedelta,
) -> dict[str, Any]:
    """Validated specification of one supported pair (no provenance gate)."""
    if "error" in entry:
        return {
            "symbol": pair,
            "broker_symbol": None,
            "validation": UNAVAILABLE,
            "reason": entry["error"],
            "retrieved_at_utc": None,
            "fields": {},
            "checks": [],
            "quote": None,
        }
    broker = entry["broker_symbol"]
    spec = entry.get("spec") or {}
    src = f"symbol_info({broker})"
    money = account_currency["value"] if account_currency["status"] == VERIFIED else None
    f: dict[str, dict[str, Any]] = {
        "broker_symbol": _text(spec, "name", f"{src}.name"),
        "path": _text(spec, "path", f"{src}.path"),
        "description": _text(spec, "description", f"{src}.description"),
        "currency_base": _currency(spec, "currency_base", f"{src}.currency_base"),
        "currency_profit": _currency(spec, "currency_profit", f"{src}.currency_profit"),
        "currency_margin": _currency(spec, "currency_margin", f"{src}.currency_margin"),
        "contract_size": _number(
            spec, "trade_contract_size", f"{src}.trade_contract_size", positive=True, unit="units"
        ),
        "digits": _number(spec, "digits", f"{src}.digits", integer=True, non_negative=True),
        "point": _number(spec, "point", f"{src}.point", positive=True),
        "tick_size": _number(spec, "trade_tick_size", f"{src}.trade_tick_size", positive=True),
        "tick_value": _number(
            spec, "trade_tick_value", f"{src}.trade_tick_value", positive=True, unit=money
        ),
        "tick_value_profit": _number(
            spec,
            "trade_tick_value_profit",
            f"{src}.trade_tick_value_profit",
            positive=True,
            unit=money,
        ),
        "tick_value_loss": _number(
            spec, "trade_tick_value_loss", f"{src}.trade_tick_value_loss", positive=True, unit=money
        ),
        "volume_min": _number(spec, "volume_min", f"{src}.volume_min", positive=True, unit="lots"),
        "volume_max": _number(spec, "volume_max", f"{src}.volume_max", positive=True, unit="lots"),
        "volume_step": _number(
            spec, "volume_step", f"{src}.volume_step", positive=True, unit="lots"
        ),
        "volume_limit": _number(
            spec, "volume_limit", f"{src}.volume_limit", non_negative=True, unit="lots"
        ),
        "calculation_mode": _enum(spec, "trade_calc_mode", f"{src}.trade_calc_mode", _CALC_MODES),
        "execution_mode": _enum(spec, "trade_exemode", f"{src}.trade_exemode", _EXECUTION_MODES),
        "order_types": _flags(spec, "order_mode", f"{src}.order_mode", _ORDER_FLAGS),
        "filling_modes": _flags(spec, "filling_mode", f"{src}.filling_mode", _FILLING_FLAGS),
        "stops_level": _number(
            spec,
            "trade_stops_level",
            f"{src}.trade_stops_level",
            integer=True,
            non_negative=True,
            unit="points",
        ),
        "freeze_level": _number(
            spec,
            "trade_freeze_level",
            f"{src}.trade_freeze_level",
            integer=True,
            non_negative=True,
            unit="points",
        ),
        "trade_mode": _enum(spec, "trade_mode", f"{src}.trade_mode", _SYMBOL_TRADE_MODES),
        "swap_mode": _enum(spec, "swap_mode", f"{src}.swap_mode", _SWAP_MODES),
        "swap_rollover3days": _enum(
            spec, "swap_rollover3days", f"{src}.swap_rollover3days", _WEEKDAYS
        ),
        "spread_float": _flag(spec, "spread_float", f"{src}.spread_float"),
    }
    swap_unit = {"points": "points", "deposit_currency": money}.get(f["swap_mode"]["value"])
    f["swap_long"] = _number(spec, "swap_long", f"{src}.swap_long", unit=swap_unit)
    f["swap_short"] = _number(spec, "swap_short", f"{src}.swap_short", unit=swap_unit)
    if f["filling_modes"]["status"] == VERIFIED and not f["filling_modes"]["value"]:
        f["filling_modes"]["reason"] = "no FOK/IOC/BOC flag set by the broker"
    if f["volume_limit"]["status"] == VERIFIED and f["volume_limit"]["value"] == 0:
        f["volume_limit"].update(
            status=NOT_APPLICABLE, reason="0 means the broker sets no aggregate volume limit"
        )
    if f["swap_mode"]["value"] == "disabled":
        for key in ("swap_long", "swap_short"):
            f[key].update(status=NOT_APPLICABLE, reason="swaps are disabled for this symbol")
    quote = _quote(entry.get("tick") or {}, spec, f, src, now=now, stale_after=stale_after)
    checks = _instrument_checks(pair, broker, f, quote, account_currency)
    return {
        "symbol": pair,
        "broker_symbol": broker,
        "retrieved_at_utc": _iso(entry.get("retrieved_at")),
        "fields": f,
        "checks": checks,
        "quote": quote,
    }


def _quote(
    tick: dict[str, Any],
    spec: dict[str, Any],
    f: dict[str, dict[str, Any]],
    src: str,
    *,
    now: datetime,
    stale_after: timedelta,
) -> dict[str, Any]:
    tick_src = src.replace("symbol_info", "symbol_info_tick")
    bid = _number(tick, "bid", f"{tick_src}.bid", positive=True)
    ask = _number(tick, "ask", f"{tick_src}.ask", positive=True)
    time_utc = tick.get("time_utc")
    reported = _number(spec, "spread", f"{src}.spread", integer=True, non_negative=True)
    reported["unit"] = "points"
    spread = _field(None, UNAVAILABLE, "derived: (ask - bid) / point", "needs bid, ask and point")
    spread["unit"] = "points"
    if _ok(bid, ask):
        if bid["value"] > ask["value"]:
            for report in (bid, ask):
                _invalidate(report, "inconsistent: bid above ask")
        elif f["point"]["status"] == VERIFIED:
            points = (ask["value"] - bid["value"]) / f["point"]["value"]
            spread.update(value=round(points, 1), status=VERIFIED, reason=None)
            if spread["value"] == 0:
                spread["reason"] = "zero spread at retrieval (a legitimate value)"
    if time_utc is None or not _ok(bid, ask):
        freshness, age = UNAVAILABLE.upper(), None
    else:
        age = round((now - time_utc).total_seconds(), 1)
        freshness = "LIVE" if age <= stale_after.total_seconds() else "STALE"
    return {
        "bid": bid,
        "ask": ask,
        "spread_points": spread,
        "spread_reported_points": reported,
        "quote_time_utc": _iso(time_utc),
        "quote_age_seconds": age,
        "freshness": freshness,
    }


def _instrument_checks(
    pair: str,
    broker: str,
    f: dict[str, dict[str, Any]],
    quote: dict[str, Any],
    account_currency: dict[str, Any],
) -> list[dict[str, Any]]:
    checks = []
    name = f["broker_symbol"]
    if name["status"] == VERIFIED:
        if name["value"] != broker or not name["value"].upper().startswith(pair):
            _invalidate(name, f"resolved symbol {broker!r} does not match {name['value']!r}")
            checks.append(_check("symbol_resolution", "discrepancy", name["reason"]))
        else:
            checks.append(
                _check(
                    "symbol_resolution", "consistent", f"{pair} resolves to {broker} on this broker"
                )
            )
    base, profit = f["currency_base"], f["currency_profit"]
    if _ok(base, profit):
        if base["value"] + profit["value"] == pair:
            checks.append(
                _check("currencies", "consistent", "base + profit currency match the pair")
            )
        else:
            reason = f"inconsistent: {base['value']}/{profit['value']} is not {pair}"
            for report in (base, profit):
                _invalidate(report, reason)
            checks.append(_check("currencies", "discrepancy", reason))
    digits, point, tick = f["digits"], f["point"], f["tick_size"]
    if _ok(digits, point):
        if _close(point["value"], 10.0 ** -digits["value"], relative=1e-9):
            checks.append(_check("point", "consistent", "point = 10^-digits"))
        else:
            reason = "inconsistent: point is not 10^-digits"
            for report in (digits, point):
                _invalidate(report, reason)
            checks.append(_check("point", "discrepancy", reason))
    if _ok(point, tick):
        ratio = tick["value"] / point["value"]
        if ratio >= 1 - 1e-9 and _close(ratio, round(ratio), absolute=1e-6):
            checks.append(
                _check("tick_size", "consistent", "tick size is a whole number of points")
            )
        else:
            _invalidate(tick, "inconsistent: tick size is not a whole number of points")
            checks.append(_check("tick_size", "discrepancy", tick["reason"]))
    vmin, vmax, step = f["volume_min"], f["volume_max"], f["volume_step"]
    if _ok(vmin, vmax) and vmax["value"] < vmin["value"]:
        _invalidate(vmax, "maximum volume is smaller than the minimum")
        checks.append(_check("volume_range", "discrepancy", vmax["reason"]))
    elif _ok(vmin, vmax):
        checks.append(_check("volume_range", "consistent", "minimum <= maximum"))
    if _ok(vmin, step):
        multiple = vmin["value"] / step["value"]
        if multiple >= 1 - 1e-9 and _close(multiple, round(multiple), absolute=1e-6):
            checks.append(_check("volume_step", "consistent", "minimum is a whole number of steps"))
        else:
            reason = "inconsistent: minimum volume is not a whole number of volume steps"
            for report in (vmin, step):
                _invalidate(report, reason)
            checks.append(_check("volume_step", "discrepancy", reason))
    checks.append(_tick_value_check(pair, f, quote, account_currency))
    return checks


def _tick_value_check(
    pair: str, f: dict[str, dict[str, Any]], quote: dict[str, Any], account: dict[str, Any]
) -> dict[str, Any]:
    """Reconcile the tick values with tick size x contract size in the account currency.

    This both validates the tick values and confirms the account currency unit (for a cent
    account, 100 units per major-currency unit). Profit currency = account major: exact. Base
    currency = account major: through the symbol's own price (profit uses the ask, loss the
    bid), within ``PRICE_RECONCILE_TOLERANCE`` for price movement between reads. Otherwise there
    is no conversion path here and the tick values stay ``unverified``.
    """
    values = [f["tick_value"], f["tick_value_profit"], f["tick_value_loss"]]
    present = [v for v in values if v["status"] == VERIFIED]
    needed = (f["tick_size"], f["contract_size"], f["currency_base"], f["currency_profit"])
    if not present or not _ok(*needed) or account["status"] != VERIFIED:
        return _check("tick_value", "not_checkable", "a required value is not verified")
    factor = 100 if account["unit"]["kind"] == "cent" else 1
    major = account["unit"]["major"]
    per_tick = f["tick_size"]["value"] * f["contract_size"]["value"] * factor
    if f["currency_profit"]["value"] == major:
        expected = {key: per_tick for key in ("tick_value", "tick_value_profit", "tick_value_loss")}
        basis, tolerance = f"profit currency is {major}: tick size x contract size x {factor}", 1e-6
    elif f["currency_base"]["value"] == major and _ok(quote["bid"], quote["ask"]):
        bid, ask = quote["bid"]["value"], quote["ask"]["value"]
        expected = {
            "tick_value": per_tick / ask,
            "tick_value_profit": per_tick / ask,
            "tick_value_loss": per_tick / bid,
        }
        basis = f"base currency is {major}: tick size x contract size x {factor} / price"
        tolerance = PRICE_RECONCILE_TOLERANCE
    else:
        for v in present:
            v.update(status=UNVERIFIED, reason="no conversion path to reconcile this tick value")
        return _check(
            "tick_value", "not_checkable", f"no conversion path from {pair} to {account['value']}"
        )
    mismatched = []
    for key, value in zip(
        ("tick_value", "tick_value_profit", "tick_value_loss"), values, strict=True
    ):
        if value["status"] == VERIFIED and not _close(
            value["value"], expected[key], relative=tolerance
        ):
            mismatched.append(key)
    if mismatched:
        reason = f"inconsistent with {basis}"
        for key in mismatched:
            _invalidate(f[key], reason)
        return _check(
            "tick_value",
            "discrepancy",
            reason,
            mismatched=mismatched,
            expected={key: expected[key] for key in mismatched},
        )
    return _check("tick_value", "consistent", basis, account_units_per_major=factor)


# ====================================================================== snapshot
def evaluate_snapshot(
    payload: dict[str, Any],
    pairs: Sequence[str],
    *,
    now: datetime,
    stale_after: timedelta,
) -> dict[str, Any]:
    """Validate one ``broker_read`` result; the result is JSON-safe."""
    value = payload["value"]
    provenance = _provenance(payload)
    account = evaluate_account(value.get("account") or {}, value.get("terminal") or {})
    currency = account["fields"]["currency"]
    symbols = value.get("symbols") or {}
    instruments = [
        evaluate_instrument(
            pair,
            symbols.get(pair, {"error": "not_read"}),
            currency,
            now=now,
            stale_after=stale_after,
        )
        for pair in pairs
    ]
    unit_checks = [
        check for item in instruments for check in item["checks"] if check["check"] == "tick_value"
    ]
    _currency_unit(currency, unit_checks, account["checks"])
    if provenance["status"] != "established":
        reason = f"broker provenance not established: {provenance['reason']}"
        for report in _all_reports(account, instruments):
            _invalidate(report, reason)
    account["validation"] = _validation(account["fields"], ACCOUNT_REQUIRED, provenance)
    for item in instruments:
        if item["fields"]:
            item["validation"] = _validation(item["fields"], INSTRUMENT_REQUIRED, provenance)
            item["provenance"] = {
                "company": provenance["company"],
                "server": provenance["server"],
                "status": provenance["status"],
            }
    return {
        "retrieved_at_utc": _iso(value.get("account_retrieved_at")),
        "provenance": provenance,
        "account": account,
        "instruments": instruments,
        # Account and symbols are read one after another on one connection, not atomically.
        "atomic": False,
    }


def _currency_unit(
    currency: dict[str, Any], unit_checks: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> None:
    """Confirm the account currency unit from the instruments' tick-value reconciliations."""
    if currency["status"] != VERIFIED:
        checks.append(_check("currency_unit", "not_checkable", "account currency not verified"))
        return
    states = {check["status"] for check in unit_checks}
    if "discrepancy" in states:
        status, detail = "discrepancy", "tick values contradict the account currency unit"
    elif "consistent" in states:
        status, detail = "consistent", "tick values agree with the account currency unit"
    else:
        status, detail = "not_checkable", "no instrument could confirm the account currency unit"
    currency["unit"] = {**currency["unit"], "confirmation": status}
    checks.append(_check("currency_unit", status, detail))


def _all_reports(account: dict[str, Any], instruments: list[dict[str, Any]]):
    yield from account["fields"].values()
    for item in instruments:
        yield from item["fields"].values()
        if item["quote"]:
            for key in ("bid", "ask", "spread_points", "spread_reported_points"):
                yield item["quote"][key]


def _validation(
    fields: dict[str, dict[str, Any]], required: Sequence[str], provenance: dict[str, Any]
) -> str:
    """``verified`` | ``partial`` (required fields verified, others not) | ``incomplete``
    (a required field unavailable) | ``invalid`` (a required field invalid, or provenance)."""
    if provenance["status"] != "established":
        return INVALID
    statuses = [fields[key]["status"] for key in required]
    if INVALID in statuses or UNVERIFIED in statuses:
        return INVALID
    if UNAVAILABLE in statuses:
        return "incomplete"
    others = [r["status"] for k, r in fields.items() if k not in required]
    return "partial" if INVALID in others or UNAVAILABLE in others else VERIFIED


# ====================================================================== service
class AccountIntelligenceService:
    """Serves validated account and instrument snapshots (P3.4). Read-only.

    One MT5 read serves account and instruments (both endpoints) for ``ttl``; concurrent
    requests share it (single flight), so the dashboard never causes parallel MT5 calls.
    When MT5 is unavailable the last validated snapshot is served as ``CACHED`` with its
    original retrieval time -- never as live -- or ``UNAVAILABLE`` when there is none.
    """

    name = "account_intelligence"

    def __init__(
        self,
        live_market: Any,
        *,
        pairs: Sequence[str] = SUPPORTED_PAIRS,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(seconds=5),
        stale_after: timedelta = timedelta(seconds=60),
    ) -> None:
        self.live_market = live_market
        self.pairs = tuple(pairs)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.ttl = ttl
        self.stale_after = stale_after
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] | None = None
        self._fetched_at: datetime | None = None
        self.reads = 0  # MT5 reads performed (diagnostics and tests)

    def _current(self) -> tuple[dict[str, Any] | None, str, MarketUnavailable | None, bool]:
        with self._lock:
            now = self._clock()
            if self._snapshot is not None and now - self._fetched_at < self.ttl:
                return self._snapshot, "LIVE", None, True
            try:
                self.reads += 1
                payload = self.live_market.broker_read(self.pairs)
            except MarketUnavailable as exc:
                LOGGER.info("Account and broker information unavailable (%s)", exc.code)
                if self._snapshot is not None:
                    return self._snapshot, "CACHED", exc, True
                return None, "UNAVAILABLE", exc, False
            snapshot = evaluate_snapshot(
                payload, self.pairs, now=self._clock(), stale_after=self.stale_after
            )
            self._snapshot, self._fetched_at = snapshot, now
            return snapshot, "LIVE", None, False

    def _envelope(
        self,
        snapshot: dict[str, Any] | None,
        status: str,
        error: MarketUnavailable | None,
        from_cache: bool,
    ) -> dict[str, Any]:
        now = self._clock()
        retrieved = snapshot["retrieved_at_utc"] if snapshot else None
        age = None
        if snapshot is not None and self._fetched_at is not None:
            age = round((now - self._fetched_at).total_seconds(), 1)
        return {
            "status": status,
            "requested_at_utc": _iso(now),
            "retrieved_at_utc": retrieved,
            "age_seconds": age,
            "served_from_cache": from_cache,
            "atomic": False,
            "reason": error.code if error else None,
            "message": error.message if error else None,
            "provenance": snapshot["provenance"] if snapshot else None,
        }

    def summary(self) -> dict[str, Any]:
        snapshot, status, error, cached = self._current()
        envelope = self._envelope(snapshot, status, error, cached)
        if snapshot is None:
            return {**envelope, "validation": UNAVAILABLE, "account": None, "checks": []}
        account = snapshot["account"]
        return {
            **envelope,
            "validation": account["validation"],
            "account": account["fields"],
            "checks": account["checks"],
        }

    def instruments(self) -> dict[str, Any]:
        snapshot, status, error, cached = self._current()
        envelope = self._envelope(snapshot, status, error, cached)
        if snapshot is None:
            return {**envelope, "account_currency": None, "instruments": []}
        return {
            **envelope,
            "account_currency": snapshot["account"]["fields"]["currency"],
            "instruments": snapshot["instruments"],
        }

    def supported_pair(self, symbol: str) -> str | None:
        """The supported pair a requested symbol names (``EURUSD`` or a broker form such as
        ``EURUSDc``), or ``None``. Decided without contacting MT5."""
        wanted = symbol.strip().upper()
        for pair in self.pairs:
            if wanted == pair or (wanted.startswith(pair) and len(wanted) <= len(pair) + 4):
                return pair
        return None

    def instrument(self, symbol: str) -> dict[str, Any] | None:
        """One instrument, or ``None`` when ``symbol`` is not a supported pair or names a
        broker symbol other than the one this broker actually uses for the pair."""
        pair = self.supported_pair(symbol)
        if pair is None:
            return None
        payload = self.instruments()
        item = next((i for i in payload.pop("instruments") if i["symbol"] == pair), None)
        wanted = symbol.strip().upper()
        if item is not None and wanted != pair:
            broker = item.get("broker_symbol")
            if broker is None or broker.upper() != wanted:
                return None
        return {**payload, "instrument": item}
