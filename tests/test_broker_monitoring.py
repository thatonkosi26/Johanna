"""P6.3 read-only broker monitoring and reconciliation.

Pure reconciliation rules with sanitised inputs, and the monitoring service on the fake MT5
through the real worker path. Temporary journals only; staged schema v5 is applied explicitly
where persistence is tested. Nothing modifies, closes or manages a position."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import StubAnalysis, make_fake, stack
from test_decision_journal import _app, _call
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.api import BackendAPI
from johanna.broker_monitoring import BrokerMonitoringService
from johanna.live_market import BACKGROUND_DEFERRED, MarketUnavailable
from johanna.reconciliation import reconcile
from johanna.trade_logbook import TradeLogbook

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
STAGED = js.MIGRATIONS  # Phase 2: v5 is registered
V4 = js.MIGRATIONS[:4]  # a journal still on v4 (before the v5 migration)


def obs(**change) -> dict:
    base = {
        "status": "LIVE",
        "reason": None,
        "observed_utc": NOW,
        "coverage_from_utc": NOW - timedelta(days=30),
        "coverage_to_utc": NOW,
        "positions": [],
        "orders": [],
        "deals": [],
    }
    return {**base, **change}


def trade(tid="T1", *, ref=None, time=NOW - timedelta(days=1), volume=0.1, voided=False, **kw):
    return {
        "id": tid,
        "broker_symbol": kw.get("symbol", "EURUSDc"),
        "direction": kw.get("direction", "BUY"),
        "voided": voided,
        "fills": [
            {
                "kind": "entry",
                "time_utc": time,
                "volume": volume,
                "broker_deal_ref": ref,
                "status": "RECORDED",
            }
        ],
    }


def deal(ref, pos, *, entry=0, time=NOW - timedelta(days=1), volume=0.1, type_=0, sym="EURUSDc"):
    return {
        "deal_ref": ref,
        "position_ref": pos,
        "order_ref": None,
        "entry": entry,
        "type": type_,
        "symbol": sym,
        "volume": volume,
        "price": 1.1,
        "time_utc": time,
    }


def states(results) -> dict:
    return {(r["subject"], r["trade_id"] or r["position_ref"]): r["state"] for r in results}


# ====================================================================== pure rules
@pytest.mark.parametrize(
    "observation",
    [
        None,
        obs(status="UNAVAILABLE", reason="timeout"),
        obs(observed_utc=NOW - timedelta(hours=1)),
        obs(observed_utc=NOW + timedelta(minutes=5)),
    ],
)
def test_missing_failed_stale_or_future_observations_are_unavailable(observation) -> None:
    results = reconcile([trade()], observation, NOW)
    assert [(r["state"], r["basis"]) for r in results] == [("UNAVAILABLE", "unavailable")]


def test_referenced_trade_with_open_position_is_confirmed_open() -> None:
    o = obs(
        positions=[{"position_ref": "P1", "symbol": "EURUSDc", "volume": 0.1}],
        deals=[deal("D1", "P1")],
    )
    (r,) = reconcile([trade(ref="D1")], o, NOW)
    assert (r["state"], r["basis"], r["position_ref"]) == (
        "CONFIRMED_OPEN",
        "broker_confirmed",
        "P1",
    )


def test_closure_needs_closing_deals_in_covered_history() -> None:
    closed = obs(deals=[deal("D1", "P1"), deal("D2", "P1", entry=1, time=NOW - timedelta(hours=2))])
    (r,) = reconcile([trade(ref="D1")], closed, NOW)
    assert (r["state"], r["basis"]) == ("CONFIRMED_CLOSED", "broker_confirmed")


def test_absence_from_positions_is_never_a_closure() -> None:
    (r,) = reconcile([trade(ref="D1")], obs(deals=[deal("D1", "P1")]), NOW)
    assert r["state"] == "PENDING" and r["basis"] == "inferred"
    assert "not inferred from absence" in r["detail"]["reason"]


def test_partial_closing_volume_stays_pending() -> None:
    o = obs(deals=[deal("D1", "P1", volume=0.2), deal("D2", "P1", entry=1, volume=0.1)])
    (r,) = reconcile([trade(ref="D1", volume=0.2)], o, NOW)
    assert r["state"] == "PENDING" and r["detail"]["volume_out"] == pytest.approx(0.1)


def test_incomplete_history_coverage_never_confirms() -> None:
    late = obs(
        coverage_from_utc=NOW - timedelta(hours=1),
        deals=[deal("D2", "P1", entry=1, time=NOW - timedelta(minutes=30))],
    )
    (r,) = reconcile([trade(ref="D1")], late, NOW)  # entry deal outside the queried history
    assert r["state"] == "PENDING"


def test_unreferenced_trade_single_candidate_is_never_linked() -> None:
    o = obs(deals=[deal("D1", "P1", time=NOW - timedelta(days=1, seconds=40))])
    (r,) = reconcile([trade()], o, NOW)
    assert (r["state"], r["basis"]) == ("CANDIDATE_MATCH", "inferred")
    assert "not linked until the user confirms" in r["detail"]["reason"]


def test_several_candidates_are_ambiguous() -> None:
    o = obs(deals=[deal("D1", "P1"), deal("D2", "P2", time=NOW - timedelta(days=1, seconds=30))])
    (r,) = reconcile([trade()], o, NOW)
    assert r["state"] == "AMBIGUOUS" and sorted(r["detail"]["candidates"]) == ["D1", "D2"]


def test_wrong_direction_volume_symbol_or_time_is_not_a_candidate() -> None:
    o = obs(
        deals=[
            deal("D1", "P1", type_=1),  # SELL deal for a BUY trade
            deal("D2", "P2", volume=0.2),
            deal("D3", "P3", sym="GBPUSDc"),
            deal("D4", "P4", time=NOW - timedelta(days=1, minutes=10)),
            deal("D5", "P5", entry=1),  # a closing deal is never an entry candidate
        ]
    )
    (r,) = reconcile([trade()], o, NOW)
    assert r["state"] == "UNMATCHED"


def test_unreferenced_trade_without_coverage_is_pending() -> None:
    (r,) = reconcile([trade()], obs(coverage_from_utc=NOW - timedelta(hours=1)), NOW)
    assert r["state"] == "PENDING"


def test_two_trades_claiming_one_deal_are_ambiguous() -> None:
    o = obs(deals=[deal("D1", "P1")])
    results = reconcile([trade("T1", ref="D1"), trade("T2", ref="D1")], o, NOW)
    assert {r["state"] for r in results} == {"AMBIGUOUS"}


def test_unrecorded_broker_position_and_voided_trades() -> None:
    o = obs(positions=[{"position_ref": "P9", "symbol": "USDJPYc", "volume": 0.05}])
    results = reconcile([trade("T1", voided=True)], o, NOW)
    assert states(results) == {("broker_position", "P9"): "UNRECORDED_POSITION"}
    assert results[0]["basis"] == "broker_confirmed" and results[0]["trade_id"] is None


def test_recorded_reference_missing_from_covered_history_is_unmatched() -> None:
    (r,) = reconcile([trade(ref="D404")], obs(), NOW)
    assert r["state"] == "UNMATCHED"


# ====================================================================== service
def _service(tmp_path, fake, *, migrations=None):
    live, _, _ = stack(fake, StubAnalysis())
    store = started(tmp_path, migrations=migrations or js.MIGRATIONS)
    return BrokerMonitoringService(store, live), store


def _broker(fake):
    epoch = (NOW - timedelta(days=1)).timestamp()
    fake.positions = [
        {
            "identifier": 555001,
            "symbol": "EURUSDc",
            "type": 0,
            "volume": 0.1,
            "price_open": 1.1,
            "sl": 1.09,
            "tp": 1.12,
            "price_current": 1.1,
            "profit": 0.0,
            "swap": 0.0,
            "time": int(epoch),
        }
    ]
    fake.deals = [
        {
            "ticket": 777001,
            "order": 666001,
            "position_id": 555001,
            "time": int(datetime.now(UTC).timestamp()) - 60,
            "type": 0,
            "entry": 0,
            "symbol": "EURUSDc",
            "volume": 0.1,
            "price": 1.1,
            "profit": 0.0,
            "swap": 0.0,
            "commission": 0.0,
            "fee": 0.0,
        }
    ]


def test_observation_uses_hmac_references_and_read_only_calls(tmp_path) -> None:
    fake = make_fake()
    _broker(fake)
    service, _ = _service(tmp_path, fake, migrations=V4)
    summary = service.observe()
    assert summary["status"] == "LIVE" and summary["positions"] == 1 and summary["deals"] == 1
    assert set(fake.calls) <= READ_ONLY_CALLS
    obs_ = service._observation
    text = json.dumps(obs_, default=str)
    for raw_id in ("555001", "777001", "666001", "private"):
        assert raw_id not in text  # raw broker identifiers never kept
    ref = obs_["positions"][0]["position_ref"]
    assert len(ref) == 64 and obs_["deals"][0]["position_ref"] == ref  # same HMAC reference
    assert service.installed() is False  # a v4 journal: memory only
    assert service.status()["enabled"] is False


def test_without_a_verified_key_nothing_is_read_or_stored(tmp_path) -> None:
    fake = make_fake()
    _broker(fake)
    service, store = _service(tmp_path, fake)
    store._key = None  # key unavailable: refs cannot be made
    calls = len(fake.calls)
    assert service.observe()["status"] == "UNAVAILABLE"
    assert service.status()["last_error"] == "hmac_unavailable"
    assert len(fake.calls) == calls  # no MT5 read without HMAC


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (MarketUnavailable(BACKGROUND_DEFERRED, "busy"), BACKGROUND_DEFERRED),
        (MarketUnavailable("timeout", "hung"), "timeout"),
        (RuntimeError("boom"), "monitoring_error"),
    ],
)
def test_failures_are_unavailable_and_never_raise(tmp_path, error, reason) -> None:
    service, _ = _service(tmp_path, make_fake())

    def failing(start, end):
        raise error

    service.live_market.reconciliation_read = failing
    summary = service.observe()
    assert (summary["status"], summary["reason"]) == ("UNAVAILABLE", reason)
    assert service.reconcile()["status"] == "ok"  # still answers, with UNAVAILABLE results


def test_dashboard_priority_defers_the_background_read(tmp_path) -> None:
    """T-1: the REAL worker admission (no patched runner). A dashboard (foreground) read
    makes the worker 'recently used', so the background observation is deferred -- never
    queued -- and is admitted once the quiet window has passed."""
    import time  # noqa: PLC0415

    from johanna.live_market import BACKGROUND_QUIET_SECONDS  # noqa: PLC0415

    fake = make_fake()
    _broker(fake)
    service, _ = _service(tmp_path, fake)
    service.live_market.quotes(["EURUSD"])  # a dashboard read on the same worker
    calls = len(fake.calls)
    deferred = service.observe()
    assert (deferred["status"], deferred["reason"]) == ("UNAVAILABLE", BACKGROUND_DEFERRED)
    assert len(fake.calls) == calls  # deferred before any MT5 call: nothing was queued
    time.sleep(BACKGROUND_QUIET_SECONDS + 0.3)
    admitted = service.observe()
    assert admitted["status"] == "LIVE" and admitted["positions"] == 1


def test_staged_v5_persists_append_only_observations_and_results(tmp_path) -> None:
    fake = make_fake()
    _broker(fake)
    service, store = _service(tmp_path, fake, migrations=STAGED)
    book = TradeLogbook(store, None)
    book.create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {
                "time_utc": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
        }
    )
    assert service.installed() is True
    service.observe()
    outcome = service.reconcile()
    assert outcome["counts"] == {"CANDIDATE_MATCH": 1}  # one fitting deal: never auto-linked
    latest = service.latest()
    assert latest["status"] == "installed" and latest["observation"]["status"] == "LIVE"
    assert [r["state"] for r in latest["results"]] == ["CANDIDATE_MATCH"]
    with closing(raw(tmp_path)) as conn:
        for sql in (
            "UPDATE broker_observation SET status='LIVE'",
            "DELETE FROM reconciliation_result",
            "UPDATE reconciliation_result SET state='CONFIRMED_OPEN'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        tid = conn.execute("SELECT id FROM trade").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):  # confirmed states need broker evidence
            conn.execute(
                "INSERT INTO reconciliation_result(id, observation_id, subject, subject_key, "
                "trade_id, position_ref, state, basis, detail, recorded_utc) VALUES "
                "('X1', ?, 'trade', ?, ?, NULL, 'CONFIRMED_OPEN', 'inferred', '{}', 't')",
                (latest["observation"]["id"], tid, tid),
            )
        payload = conn.execute("SELECT payload FROM broker_observation").fetchone()[0]
        assert "555001" not in payload and "777001" not in payload
        trades = conn.execute("SELECT count(*) FROM trade").fetchone()[0]
        fills = conn.execute("SELECT count(*) FROM trade_fill").fetchone()[0]
    assert (trades, fills) == (1, 1)  # reconciliation never changes the logbook
    assert store.verify_audit_chain()["valid"] is True


def test_monitoring_api_is_read_only_and_triggers_no_mt5_read(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        api = BackendAPI(app)
        runner = app.live_market._quote_runner  # T-1: count every job the worker receives
        jobs: list = []
        original = runner.run

        def counting(op, args, timeout):
            jobs.append(op)
            return original(op, args, timeout)

        runner.run = counting
        status, body, headers = _call(api, "/api/monitoring/status")
        assert status == "200 OK" and headers["Cache-Control"] == "no-store"
        assert body["enabled"] is False and body["mode"] == "read_only"
        assert body["last_observation"] is None and body["installed"] is True  # v5, inactive
        status, body, _ = _call(api, "/api/monitoring/reconciliation")
        assert status == "200 OK" and body["status"] == "installed"
        assert body["observation"] is None and body["results"] == []  # nothing observed
        assert jobs == []  # neither GET sent anything to the MT5 worker
        assert app.monitoring._observation is None  # GET observed nothing
    finally:
        app.stop()
