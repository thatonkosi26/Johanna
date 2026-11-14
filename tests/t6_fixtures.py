"""Shared T6 test fixtures (not a test module).

- ``trend_bars``: deterministic drift + oscillation H1 histories on the real T3 schedule, which
  produce genuine T4 -> T5 -> T6 results (found once by search; parameters frozen here).
- ``base_features``: a hand-written bullish feature set with explicit numbers (the §8.3 design
  example), so condition and setup tests do not depend on the feature implementation.
- ``reflect``: the price reflection p -> 2M - p, written independently of T6.
"""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime, timedelta

from test_window_validation import BASE, candle, snapshot

from johanna.analysis.engine import analyse_window
from johanna.analysis.sessions import EXNESS_T0_PROVISIONAL
from johanna.analysis.window import session_candle_check, validate_window

END = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)
LAST = "2026-11-11T12:00:00Z"
AS_OF = "2026-11-11T13:00:00Z"
# Explicit evaluation time: 1.0 s after assembly, 0.8 s after the bars were retrieved (T2: <= 5 s).
FRESH = BASE + timedelta(seconds=1)


def schedule(count: int, end: datetime = END) -> list[datetime]:
    """``count`` H1 open times before ``end`` that the T3 schedule expects (closures skipped)."""
    out, moment = [], end
    while len(out) < count:
        moment -= timedelta(hours=1)
        if not session_candle_check([moment], 3600, EXNESS_T0_PROVISIONAL)["unexpected"]:
            out.append(moment)
    return out[::-1]


def trend_bars(
    count: int = 700,
    *,
    drift: float = 0.0002,
    amp: float = 0.002,
    period: int = 16,
    sign: int = 1,
    end: datetime = END,
) -> list[dict]:
    """Rising (sign=1) or falling (sign=-1) oscillating path; each open = previous close."""
    bars, prev = [], None
    for i, t in enumerate(schedule(count, end)):
        mid = 1.1 + sign * (drift * i + amp * math.sin(2 * math.pi * i / period))
        close = round(mid, 5)
        open_ = prev if prev is not None else close
        high = round(max(open_, close) + 0.0002, 5)
        low = round(min(open_, close) - 0.0002, 5)
        bars.append(candle(t, o=round(open_, 5), h=high, low=low, c=close))
        prev = close
    return bars


def pipeline(bars, required=None, analysed_at=FRESH, **snap_kwargs):
    """Real T2 snapshot -> T4 (freshness judged at ``analysed_at``) -> T5 for ``bars``."""
    snap = snapshot(bars, **snap_kwargs)
    validation = validate_window(
        snap, required_count=required or len(bars), analysed_at=analysed_at
    )
    return snap, validation, analyse_window(snap, validation)


def reflect_bars(bars, mirror: float = 1.1) -> list[dict]:
    """p -> 2M - p for every price; highs and lows swap roles."""
    out = []
    for b in bars:
        out.append(
            candle(
                b["t"],
                o=round(2 * mirror - b["o"], 5),
                h=round(2 * mirror - b["l"], 5),
                low=round(2 * mirror - b["h"], 5),
                c=round(2 * mirror - b["c"], 5),
            )
        )
    return out


# ---------------------------------------------------------------- hand-written features
def rec(value, *, usable=True, reason=None, status="available", t=LAST):
    return {
        "name": None,
        "value": value,
        "t": t,
        "status": status,
        "usable": usable,
        "reason": reason,
    }


def row(t, low, high, close, ema20, ema50, atr):
    return {
        "t": t,
        "low": low,
        "high": high,
        "close": close,
        "ema20": ema20,
        "ema50": ema50,
        "atr": atr,
        "d_bullish": abs(low - ema20) / atr,
        "d_bearish": abs(high - ema20) / atr,
    }


def level(low, high, strength, first="2026-11-05T10:00:00Z"):
    return {
        "level": (low + high) / 2,
        "low": low,
        "high": high,
        "strength": strength,
        "first_confirmed_at_utc": first,
        "latest_confirmed_at_utc": first,
        "pivots": [{"price": low, "pivot_t": first, "confirmed_at_utc": first}] * strength,
    }


def pivot(role, kind, price, t, confirmed):
    return {"role": role, "type": kind, "price": price, "pivot_t": t, "confirmed_at_utc": confirmed}


def base_features() -> dict:
    """Bullish §8.3 example: every check passes with the default parameters."""
    rows = [
        row("2026-11-11T08:00:00Z", 1.08520, 1.08600, 1.08560, 1.08440, 1.08280, 0.00110),
        row("2026-11-11T09:00:00Z", 1.08500, 1.08580, 1.08510, 1.08450, 1.08290, 0.00109),
        row("2026-11-11T10:00:00Z", 1.08470, 1.08540, 1.08450, 1.08460, 1.08300, 0.00108),
        row("2026-11-11T11:00:00Z", 1.08510, 1.08560, 1.08530, 1.08480, 1.08305, 0.00109),
        row(LAST, 1.08530, 1.08570, 1.08540, 1.08495, 1.08310, 0.00110),
    ]
    # d_bullish by hand: 0.727, 0.459, 0.0926 (the only touch, 10:00), 0.275, 0.318
    return {
        "features_version": 1,
        "diagnostic": False,
        "count": 700,
        "last_t": LAST,
        "as_of_utc": AS_OF,
        "price": {"source": "last_closed_candle_close", "t": LAST, "value": 1.0854},
        "latest": {
            "EMA20": rec(1.08495),
            "EMA50": rec(1.0831),
            "EMA200": rec(1.0772),
            "ATR14": rec(0.0011),
            "RSI14": rec(58.2),
            "ADX14": {**rec(29.4), "plus_di": 27.1, "minus_di": 14.8},
        },
        "slope50": {"value": 0.73, "lookback": 5, "usable": True, "reason": None},
        "pullback": {"lookback": 5, "usable": True, "reason": None, "candles": rows},
        "structure_break": {"usable": True, "reason": None},
        "swings": {
            "state": "higher_highs_higher_lows",
            "usable": True,
            "reason": None,
            "pending_ignored": 0,
            "pivots": [
                pivot("H_prev", "high", 1.0842, "2026-11-09T15:00:00Z", "2026-11-09T18:00:00Z"),
                pivot("H_last", "high", 1.0861, "2026-11-10T14:00:00Z", "2026-11-10T17:00:00Z"),
                pivot("L_prev", "low", 1.0815, "2026-11-10T02:00:00Z", "2026-11-10T05:00:00Z"),
                pivot("L_last", "low", 1.0830, "2026-11-11T06:00:00Z", "2026-11-11T09:00:00Z"),
            ],
        },
        "levels": {
            "usable": True,
            "reason": None,
            "t5_status": "available",
            "t5_trusted": True,
            "t5_reason": None,
            "support_reason": None,
            "resistance_reason": None,
            "tolerance": 0.000275,
            "support": [level(1.0828, 1.0832, 3)],
            "resistance": [level(1.0876, 1.0879, 2)],
        },
        "volatility": {
            "usable": True,
            "reason": None,
            "regime": "normal",
            "percentile": 52.0,
            "current_t": LAST,
        },
    }


def reflect_features(f: dict, mirror: float = 1.1) -> dict:
    """Independent reflection of a feature set: bullish evidence becomes bearish."""
    g = copy.deepcopy(f)
    m2 = 2 * mirror

    def r(x):
        return m2 - x

    g["price"]["value"] = r(f["price"]["value"])
    for name in ("EMA20", "EMA50", "EMA200"):
        g["latest"][name]["value"] = r(f["latest"][name]["value"])
    g["latest"]["RSI14"]["value"] = 100 - f["latest"]["RSI14"]["value"]
    adx = g["latest"]["ADX14"]
    adx["plus_di"], adx["minus_di"] = (
        f["latest"]["ADX14"]["minus_di"],
        f["latest"]["ADX14"]["plus_di"],
    )
    g["slope50"]["value"] = -f["slope50"]["value"]
    g["pullback"]["candles"] = [
        {
            **x,
            "low": r(x["high"]),
            "high": r(x["low"]),
            "close": r(x["close"]),
            "ema20": r(x["ema20"]),
            "ema50": r(x["ema50"]),
            "d_bullish": x["d_bearish"],
            "d_bearish": x["d_bullish"],
        }
        for x in f["pullback"]["candles"]
    ]
    state = f["swings"]["state"]
    g["swings"]["state"] = {
        "higher_highs_higher_lows": "lower_highs_lower_lows",
        "lower_highs_lower_lows": "higher_highs_higher_lows",
    }.get(state, state)

    def flip(lvl):
        return {**lvl, "low": r(lvl["high"]), "high": r(lvl["low"]), "level": r(lvl["level"])}

    lv = f["levels"]
    g["levels"]["support"] = [flip(x) for x in lv["resistance"]]
    g["levels"]["resistance"] = [flip(x) for x in lv["support"]]
    g["levels"]["support_reason"], g["levels"]["resistance_reason"] = (
        lv["resistance_reason"],
        lv["support_reason"],
    )
    return g
