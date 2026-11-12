"""T1: bar metadata, live OHLC validation, duplicate/out-of-order detection, closure evidence.

All MT5 access uses the in-memory FakeMT5; no terminal is involved.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fake_mt5 import READ_ONLY_CALLS, FakeMT5

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import (
    CACHED,
    LIVE,
    UNAVAILABLE,
    LiveMarketService,
    _bar_values,
    check_bars,
    ohlc_issues,
)
from johanna.market_data import ParquetBarStore

MID_BAR = datetime(2026, 11, 11, 10, 31, 30, tzinfo=UTC)  # inside a bar on every timeframe
LEGACY_BAR_KEYS = {"t", "o", "h", "l", "c", "v", "close_t", "complete"}
LEGACY_PAYLOAD_KEYS = {
    "symbol",
    "broker_symbol",
    "timeframe",
    "digits",
    "bars",
    "latest_tick_utc",
    "source",
    "time_basis",
    "status",
    "fetched_at_utc",
    "requested_at_utc",
    "reason",
    "message",
}


def _service(fake: FakeMT5, **kwargs) -> LiveMarketService:
    service = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: True,
        reconnect_interval=0,
        **kwargs,
    )
    service.start()
    return service


def _bars(fake: FakeMT5, symbol="EURUSD", timeframe="H1", count=20, **kwargs) -> dict:
    service = _service(fake, **kwargs)
    try:
        return service.bars(symbol, timeframe, count)
    finally:
        service.stop()


def _row(t: datetime, o=1.1, h=1.2, low=1.0, c=1.15, v=10, spread=7) -> dict:
    return {
        "timestamp": t,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "tick_volume": v,
        "spread_points": spread,
    }


T0 = datetime(2026, 11, 11, 8, tzinfo=UTC)
HOUR = timedelta(hours=1)


# ---------------------------------------------------------------- pure checks
def test_consistent_bars_pass_unchanged() -> None:
    rows = [_row(T0), _row(T0 + HOUR, o=1.15, h=1.16, low=1.14, c=1.155)]
    kept, rejected, summary = check_bars(rows)
    assert [issues for _, issues in kept] == [[], []]
    assert [row for row, _ in kept] == rows  # values untouched
    assert rejected == []
    assert summary == {
        "bars_received": 2,
        "bars_returned": 2,
        "invalid_bars": 0,
        "duplicates_rejected": 0,
        "out_of_order": 0,
    }


@pytest.mark.parametrize(
    ("row", "issue"),
    [
        (_row(T0, h=1.12), "high_below_open_close_or_low"),  # high below close 1.15
        (_row(T0, low=1.13), "low_above_open_close_or_high"),  # low above open 1.10
        (_row(T0, h=0.9, low=1.3), "high_below_open_close_or_low"),  # inverted range
        (_row(T0, o=0.0), "non_positive_price"),
        (_row(T0, c=-1.0), "non_positive_price"),
        (_row(T0, c=float("nan")), "non_finite_price"),
        (_row(T0, h=float("inf")), "non_finite_price"),
        (_row(T0, o=None), "non_finite_price"),
        (_row(T0, v=-5), "negative_tick_volume"),
    ],
)
def test_inconsistent_values_are_flagged_not_corrected(row, issue) -> None:
    assert issue in ohlc_issues(row)
    kept, _, summary = check_bars([row])
    ((kept_row, issues),) = kept
    assert issue in issues and summary["invalid_bars"] == 1
    assert kept_row is row  # reported, never altered or dropped


def test_non_finite_prices_are_sent_as_null_so_the_response_stays_valid_json() -> None:
    values = _bar_values(_row(T0, h=float("inf"), c=float("nan")))
    assert values["h"] is None and values["c"] is None and values["o"] == 1.1
    json.dumps(values, allow_nan=False)  # would raise on NaN/Infinity


def test_duplicate_timestamps_keep_one_bar_and_report_the_rest() -> None:
    rows = [
        _row(T0),
        _row(T0 + HOUR),
        _row(T0 + HOUR),  # identical repeat
        _row(T0 + HOUR, c=1.19),  # same time, different values
    ]
    kept, rejected, summary = check_bars(rows)
    assert [row["timestamp"] for row, _ in kept] == [T0, T0 + HOUR]
    assert "duplicate_timestamp" in kept[1][1] and kept[0][1] == []
    assert [(r["reason"], r["identical_to_kept"]) for r in rejected] == [
        ("duplicate_timestamp", True),
        ("duplicate_timestamp", False),
    ]
    assert rejected[1]["c"] == 1.19  # the rejected values are reported as received
    assert summary["duplicates_rejected"] == 2 and summary["bars_returned"] == 2


def test_out_of_order_bars_are_sorted_and_flagged() -> None:
    rows = [_row(T0), _row(T0 + 2 * HOUR), _row(T0 + HOUR), _row(T0 + 3 * HOUR)]
    kept, _, summary = check_bars(rows)
    assert [row["timestamp"] for row, _ in kept] == [T0 + n * HOUR for n in range(4)]
    assert [issues for _, issues in kept] == [[], ["out_of_order"], [], []]
    assert summary["out_of_order"] == 1


def test_no_bars_is_an_empty_result() -> None:
    assert check_bars([]) == (
        [],
        [],
        {
            "bars_received": 0,
            "bars_returned": 0,
            "invalid_bars": 0,
            "duplicates_rejected": 0,
            "out_of_order": 0,
        },
    )


# ---------------------------------------------------------------- adapter
def test_read_bars_keeps_mt5_order_duplicates_and_spread() -> None:
    fake = FakeMT5()
    fake.rates_hook = lambda symbol, rows: [rows[1], rows[0], rows[1]]
    adapter = MetaTrader5Adapter(mt5_module=fake)
    adapter.initialize()
    rows = adapter.read_bars("EURUSD", "H1", 2)
    assert [row["timestamp"] for row in rows] == [
        rows[0]["timestamp"],
        rows[1]["timestamp"],
        rows[0]["timestamp"],
    ]
    assert rows[1]["timestamp"] < rows[0]["timestamp"]
    assert {row["spread_points"] for row in rows} == {7}


def test_collector_get_bars_keeps_its_schema_and_strictness() -> None:
    fake = FakeMT5()
    adapter = MetaTrader5Adapter(mt5_module=fake)
    adapter.initialize()
    rows = adapter.get_bars("EURUSD", "H1", 3)
    assert "spread_points" not in rows[0]  # the stored bar schema is unchanged
    assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
    fake.rates_hook = lambda symbol, rows: [rows[0], rows[0]]
    with pytest.raises(ValueError, match="strictly increasing"):
        adapter.get_bars("EURUSD", "H1", 2)


# ---------------------------------------------------------------- live service
def test_anomalous_bars_are_reported_without_failing_the_request() -> None:
    fake = FakeMT5()

    def anomalies(symbol, rows):
        rows = list(rows)
        rows[3] = rows[3]._replace(high=rows[3].low - 0.001)  # inverted candle
        rows[5] = rows[5]._replace(close=float("nan"))
        rows[7], rows[8] = rows[8], rows[7]  # out of order
        return rows + [rows[10]]  # duplicate

    fake.rates_hook = anomalies
    payload = _bars(fake, count=20)
    assert payload["status"] == LIVE
    times = [bar["t"] for bar in payload["bars"]]
    assert times == sorted(times) and len(set(times)) == len(times) == 20
    invalid = {bar["t"]: bar["issues"] for bar in payload["bars"] if not bar["valid"]}
    assert len(invalid) == 4
    assert any("high_below_open_close_or_low" in i for i in invalid.values())
    assert any("non_finite_price" in i for i in invalid.values())
    assert any("out_of_order" in i for i in invalid.values())
    assert any("duplicate_timestamp" in i for i in invalid.values())
    nan_bar = next(bar for bar in payload["bars"] if "non_finite_price" in bar["issues"])
    assert nan_bar["c"] is None and nan_bar["o"] is not None
    assert len(payload["rejected_bars"]) == 1
    assert payload["rejected_bars"][0]["identical_to_kept"] is True
    assert payload["quality"] == {
        "bars_received": 21,
        "bars_returned": 20,
        "invalid_bars": 4,
        "duplicates_rejected": 1,
        "out_of_order": 2,  # the swapped bar, and the duplicate that arrived after newer bars
    }
    json.dumps(payload, allow_nan=False, default=str)


def test_closure_evidence_distinguishes_forming_tick_observed_and_clock_only() -> None:
    fake = FakeMT5(now=MID_BAR.timestamp, tick_age_seconds=10 * 60)  # last tick 10:21:30
    payload = _bars(fake, timeframe="M1", count=30, clock=lambda: MID_BAR)
    by_time = {bar["t"]: bar for bar in payload["bars"]}
    newest = payload["bars"][-1]
    assert newest["closure"] == "forming" and newest["complete"] is False
    tick = MID_BAR - timedelta(minutes=10)
    for bar in payload["bars"][:-1]:
        close = datetime.fromisoformat(bar["close_t"].replace("Z", "+00:00"))
        if close <= tick:
            expected = "tick_observed"  # a tick at/after its close was seen
        elif MID_BAR >= close + timedelta(seconds=60):
            expected = "clock_only"  # closed by time only: no later tick yet
        else:
            expected = "forming"  # inside the clock-skew grace: not yet treated as closed
        assert bar["closure"] == expected, bar
        assert bar["complete"] is (expected != "forming")
    kinds = {bar["closure"] for bar in by_time.values()}
    assert kinds == {"forming", "tick_observed", "clock_only"}


def test_point_pip_size_and_spread_are_exposed() -> None:
    fake = FakeMT5()
    eur = _bars(fake, "EURUSD")
    jpy = _bars(fake, "USDJPY")
    assert (eur["point"], eur["pip_size"]) == (0.00001, pytest.approx(0.0001))
    assert (jpy["point"], jpy["pip_size"]) == (0.001, pytest.approx(0.01))
    assert {bar["spread_points"] for bar in eur["bars"]} == {7}
    service = _service(fake)
    try:
        quotes = {q["symbol"]: q for q in service.quotes(["EURUSD", "USDJPY"])["quotes"]}
    finally:
        service.stop()
    assert quotes["EURUSD"]["point"] == 0.00001
    assert quotes["EURUSD"]["pip_size"] == pytest.approx(0.0001)
    assert quotes["USDJPY"]["pip_size"] == pytest.approx(0.01)


def test_missing_symbol_metadata_is_reported_as_null_not_guessed() -> None:
    fake = FakeMT5()
    fake.symbols["EURUSD"]["metadata_missing"] = True
    payload = _bars(fake)
    assert payload["status"] == LIVE and payload["bars"]
    assert payload["digits"] is None and payload["point"] is None
    assert payload["pip_size"] is None
    service = _service(fake)
    try:
        row = service.quotes(["EURUSD"])["quotes"][0]
    finally:
        service.stop()
    assert row["status"] == LIVE and row["bid"] == 1.08512
    assert row["point"] is None and row["pip_size"] is None and row["spread_pips"] is None


def test_empty_mt5_answer_is_an_empty_live_result() -> None:
    fake = FakeMT5()
    fake.rates_hook = lambda symbol, rows: []
    payload = _bars(fake)
    assert payload["status"] == LIVE and payload["bars"] == []
    assert payload["quality"]["bars_received"] == 0


def test_unavailable_mt5_answer_is_unavailable() -> None:
    fake = FakeMT5()
    fake.rates_hook = lambda symbol, rows: None  # MT5 returns None: retrieval failed
    payload = _bars(fake)
    assert payload["status"] == UNAVAILABLE and payload["reason"] == "mt5_error"
    assert payload["bars"] == []


def test_archived_bars_carry_unknown_closure_and_checks(tmp_path) -> None:
    ParquetBarStore(tmp_path / "collectors" / "mt5").ingest(
        [
            {
                "symbol": "EURUSD",
                "timeframe": "H1",
                "timestamp": T0,
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "tick_volume": 1,
                "real_volume": None,
                "source": "MT5",
                "source_timezone": "UTC",
                "source_timestamp_convention": "bar_open",
            }
        ],
        source="MT5",
    )
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    app = JohannaApplication(config)  # conftest: the terminal is "not running"
    payload = app.live_market.bars("EURUSD", "H1", 50)
    app.live_market.stop()
    assert payload["status"] == CACHED and payload["source"] == "parquet_archive"
    (bar,) = payload["bars"]
    assert bar["closure"] == "unknown" and bar["complete"] is True
    assert bar["valid"] is True and bar["spread_points"] is None
    assert payload["point"] is None and payload["quality"]["bars_returned"] == 1


def test_api_response_stays_backward_compatible_and_strict_json(tmp_path) -> None:
    fake = FakeMT5()
    fake.rates_hook = lambda symbol, rows: [
        rows[0]._replace(close=float("inf")),
        *rows[1:],
    ]
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: True,
        reconnect_interval=0,
    )
    app = JohannaApplication(config, live_market=live)
    app.start()
    try:
        response: dict = {}
        body = b"".join(
            BackendAPI(app)(
                {
                    "PATH_INFO": "/api/market/bars",
                    "REQUEST_METHOD": "GET",
                    "QUERY_STRING": "symbol=EURUSD&timeframe=H1&count=30",
                    "wsgi.input": BytesIO(),
                },
                lambda status, headers: response.update(status=status),
            )
        )
    finally:
        app.stop()
    assert response["status"] == "200 OK"
    assert b"NaN" not in body and b"Infinity" not in body  # browsers reject those
    payload = json.loads(body)
    assert LEGACY_PAYLOAD_KEYS <= set(payload)  # every pre-T1 field is still there
    for bar in payload["bars"]:
        assert LEGACY_BAR_KEYS <= set(bar)
        assert isinstance(bar["t"], str) and isinstance(bar["complete"], bool)
        assert isinstance(bar["v"], int)
        assert bar["close_t"] is None or isinstance(bar["close_t"], str)
    assert payload["bars"][0]["c"] is None and payload["bars"][0]["valid"] is False
    assert all(
        isinstance(bar[key], float) for bar in payload["bars"][1:] for key in ("o", "h", "l", "c")
    )
    assert math.isclose(payload["bars"][1]["o"], payload["bars"][1]["o"])


def test_only_read_only_mt5_calls_are_used() -> None:
    fake = FakeMT5()
    _bars(fake)
    service = _service(fake)
    try:
        service.quotes(["EURUSD"])
    finally:
        service.stop()
    assert set(fake.calls) <= READ_ONLY_CALLS
