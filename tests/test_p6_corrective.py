"""Phase 1 corrective remediation of the overnight P6.2b.1 -> P6.R work (review findings H-1,
H-2, M-1 .. M-5) and the corrected, still UNREGISTERED schema v5.

Offline only: temporary journals, the in-process fake MT5, deterministic clocks. The production
journal and ``data/`` are never touched; schema v5 is applied only to temporary journals."""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import (
    BUY_LEVEL,
    BUY_SYMBOLS,
    SELL_LEVEL,
    SELL_SYMBOLS,
    StubAnalysis,
    envelope,
    fixture_broker_terms,
    make_fake,
    negative_swap,
    propose,
    stack,
    synthetic_terms,
)
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.broker_monitoring import (
    MAX_DETAIL_CHARS,
    MAX_DETAIL_ITEMS,
    BrokerMonitoringService,
    bounded_detail,
)
from johanna.cost_evidence import (
    CostComponent,
    CostTerms,
    assess_component,
    commission_round_trip_usc,
    holding_window,
    rollover_charges,
)
from johanna.decision_journal import DecisionJournal
from johanna.reconciliation import reconcile
from johanna.risk_engine import APPROVED, UNVERIFIED
from johanna.trade_logbook import TradeLogbook

STAGED = js.MIGRATIONS  # Phase 2: v5 is registered
V4 = js.MIGRATIONS[:4]  # a journal still on v4 (before the v5 migration)
V5 = js.MIGRATIONS[:5]  # the v4 -> v5 step these tests cover (v6 registered later)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
WED = 2


def u(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ====================================================================== H-1
def _trade(tid, *, ref=None, volume=0.1, time=NOW - timedelta(days=1), symbol="EURUSDc"):
    return {
        "id": tid,
        "broker_symbol": symbol,
        "direction": "BUY",
        "voided": False,
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


def _deal(ref, pos, *, volume=0.1, time=NOW - timedelta(days=1), symbol="EURUSDc"):
    return {
        "deal_ref": ref,
        "position_ref": pos,
        "order_ref": None,
        "entry": 0,
        "type": 0,
        "symbol": symbol,
        "volume": volume,
        "price": 1.1,
        "time_utc": time,
    }


def _obs(deals, positions=()):
    return {
        "status": "LIVE",
        "reason": None,
        "observed_utc": NOW,
        "coverage_from_utc": NOW - timedelta(days=30),
        "coverage_to_utc": NOW,
        "positions": list(positions),
        "orders": [],
        "deals": list(deals),
    }


def _states(results):
    return {r["trade_id"]: r for r in results if r["subject"] == "trade"}


def test_h1_two_trades_fitting_one_deal_are_both_ambiguous() -> None:
    results = _states(reconcile([_trade("T1"), _trade("T2")], _obs([_deal("D1", "P1")]), NOW))
    assert results["T1"]["state"] == results["T2"]["state"] == "AMBIGUOUS"
    assert results["T1"]["detail"]["competing_trades"] == ["T2"]
    assert results["T2"]["detail"]["competing_trades"] == ["T1"]


def test_h1_one_trade_one_deal_is_a_unique_candidate_never_linked() -> None:
    (r,) = reconcile([_trade("T1")], _obs([_deal("D1", "P1")]), NOW)
    assert (r["state"], r["detail"]["candidate"]) == ("CANDIDATE_MATCH", "D1")
    assert "not linked until the user confirms" in r["detail"]["reason"]


def test_h1_two_trades_fitting_different_deals_keep_their_unique_candidates() -> None:
    t2_time = NOW - timedelta(hours=5)
    results = _states(
        reconcile(
            [_trade("T1"), _trade("T2", time=t2_time)],
            _obs([_deal("D1", "P1"), _deal("D2", "P2", time=t2_time)]),
            NOW,
        )
    )
    assert results["T1"]["detail"]["candidate"] == "D1"
    assert results["T2"]["detail"]["candidate"] == "D2"


def test_h1_a_deal_claimed_by_a_recorded_reference_is_never_a_candidate() -> None:
    results = _states(
        reconcile(
            [_trade("T1", ref="D1"), _trade("T2")],
            _obs([_deal("D1", "P1")], [{"position_ref": "P1", "symbol": "EURUSDc"}]),
            NOW,
        )
    )
    assert results["T1"]["state"] == "CONFIRMED_OPEN"
    assert results["T2"]["state"] == "UNMATCHED"  # D1 belongs to T1


def test_h1_several_deals_fitting_one_trade_are_ambiguous() -> None:
    o = _obs([_deal("D1", "P1"), _deal("D2", "P2", time=NOW - timedelta(days=1, seconds=40))])
    (r,) = reconcile([_trade("T1")], o, NOW)
    assert r["state"] == "AMBIGUOUS" and sorted(r["detail"]["candidates"]) == ["D1", "D2"]


@pytest.mark.parametrize(
    "fill",
    [
        {"time_utc": None, "volume": 0.1},
        {"time_utc": datetime(2026, 11, 17, 12, 0), "volume": 0.1},  # naive time
        {"time_utc": NOW - timedelta(days=1), "volume": None},
        {"time_utc": NOW - timedelta(days=1), "volume": 0},
        {"time_utc": NOW - timedelta(days=1), "volume": True},
    ],
)
def test_h1_incomplete_matching_evidence_is_pending_not_matched(fill) -> None:
    trade = _trade("T1")
    trade["fills"][0].update(fill)
    results = _states(reconcile([trade, _trade("T2")], _obs([_deal("D1", "P1")]), NOW))
    assert results["T1"]["state"] == "PENDING"
    assert "incomplete logbook evidence" in results["T1"]["detail"]["reason"]
    assert results["T2"]["state"] == "CANDIDATE_MATCH"  # the incomplete trade claims nothing


def test_h1_no_deal_is_ever_a_unique_candidate_for_two_trades() -> None:
    trades = [_trade(f"T{i}", time=NOW - timedelta(days=1, seconds=10 * i)) for i in range(5)]
    deals = [_deal(f"D{i}", f"P{i}", time=NOW - timedelta(days=1, seconds=7 * i)) for i in range(3)]
    unique = [
        r["detail"]["candidate"]
        for r in reconcile(trades, _obs(deals), NOW)
        if r["state"] == "CANDIDATE_MATCH"
    ]
    assert len(unique) == len(set(unique))


# ====================================================================== H-2
@pytest.mark.parametrize(
    ("timeframe", "evaluated", "as_of", "hours", "end"),
    [
        ("H1", u(2026, 7, 14, 12, 0, 30), u(2026, 7, 14, 12), 24, u(2026, 7, 15, 13)),
        ("H4", u(2026, 7, 14, 12, 0, 30), u(2026, 7, 14, 12), 120, u(2026, 7, 19, 16)),
        ("D1", u(2026, 7, 14, 0, 0, 30), u(2026, 7, 14), 480, u(2026, 8, 4)),
    ],
)
def test_h2_window_runs_to_entry_expiry_plus_holding(timeframe, evaluated, as_of, hours, end):
    seconds = {"H1": 3600, "H4": 14400, "D1": 86400}[timeframe]
    start, stop = holding_window(evaluated, as_of + timedelta(seconds=seconds), hours)
    assert (start, stop) == (evaluated, end)


def test_h2_d1_entry_validity_across_the_wednesday_triple() -> None:
    evaluated, valid_until = u(2026, 7, 14, 0, 0, 30), u(2026, 7, 15)  # Tuesday D1 candle
    old = rollover_charges(evaluated, evaluated + timedelta(hours=24), WED)["units"]
    new = rollover_charges(*holding_window(evaluated, valid_until, 24), WED)["units"]
    assert old == 1  # the defect: only Tuesday's single rollover
    assert new == 4  # Tuesday 1 + Wednesday 3: a late entry is still held over Wednesday


@pytest.mark.parametrize(
    ("evaluated", "valid_until", "hours"),
    [
        (u(2026, 7, 14, 0, 0, 30), u(2026, 7, 15), 24),  # D1 across the triple
        (u(2026, 7, 14, 20, 0, 5), u(2026, 7, 15, 0), 4),  # H4 across a rollover
        (u(2026, 3, 6, 20, 0, 5), u(2026, 3, 7), 72),  # across the US DST start
        (u(2026, 3, 10, 21, 0, 5), u(2026, 3, 10, 22), 1),  # US/UK gap week
        (u(2026, 10, 30, 0, 0, 30), u(2026, 10, 31), 96),  # US DST end weekend
    ],
)
def test_h2_window_is_conservative_for_every_permitted_entry(evaluated, valid_until, hours):
    window = rollover_charges(*holding_window(evaluated, valid_until, hours), WED)["units"]
    entry = evaluated
    while entry <= valid_until:  # every entry the proposal permits, every 15 minutes
        actual = rollover_charges(entry, entry + timedelta(hours=hours), WED)["units"]
        assert actual <= window, entry
        entry += timedelta(minutes=15)


def test_h2_exact_boundaries_are_counted() -> None:
    start, end = holding_window(u(2026, 7, 14, 21), u(2026, 7, 14, 21), 24)
    events = rollover_charges(start, end, WED)["events"]
    assert [e["at_utc"] for e in events] == ["2026-07-14T21:00:00Z", "2026-07-15T21:00:00Z"]


@pytest.mark.parametrize(
    ("evaluated", "valid_until", "hours"),
    [
        (datetime(2026, 7, 14, 12), u(2026, 7, 14, 13), 24),
        (u(2026, 7, 14, 12), datetime(2026, 7, 14, 13), 24),
        (u(2026, 7, 14, 12), u(2026, 7, 14, 13), 0),
        (u(2026, 7, 14, 12), u(2026, 7, 14, 13), float("nan")),
        (u(2026, 7, 14, 12), u(2026, 7, 14, 13), True),
    ],
)
def test_h2_invalid_window_inputs_fail(evaluated, valid_until, hours) -> None:
    with pytest.raises(ValueError):
        holding_window(evaluated, valid_until, hours)


def test_h2_expired_entry_validity_never_shortens_the_window() -> None:
    start, end = holding_window(u(2026, 7, 14, 12), u(2026, 7, 14, 11), 24)
    assert end == u(2026, 7, 15, 12)


def _pinned(at: datetime, symbols=None):
    fake = negative_swap(make_fake(symbols or BUY_SYMBOLS, now=lambda: at.timestamp()))
    return fake, (lambda: at)


def test_h2_engine_counts_the_d1_entry_window_deterministically() -> None:
    at = u(2026, 7, 14, 0, 0, 30)  # Tuesday, just after the D1 close
    fake, clock = _pinned(at)
    env = envelope(level=BUY_LEVEL, evaluated_at=at)
    terms = synthetic_terms(holding_hours=24.0, when=at - timedelta(hours=1))
    result = propose(fake, env, timeframe="D1", costs=terms, clock=clock)
    swap = result["costs"]["swap"]
    assert swap["entry_valid_until_utc"] == "2026-07-15T00:00:00Z"
    assert swap["charge_units"] == 4  # was 1 before the correction
    assert swap["cost_per_lot"] == pytest.approx(5.7 * 4)


def test_h2_missing_entry_validity_fails_closed() -> None:
    env = envelope(level=BUY_LEVEL)
    del env["result"]["as_of_utc"]
    result = propose(negative_swap(make_fake()), env, costs=synthetic_terms(holding_hours=24.0))
    assert result["costs"]["swap"]["status"] == "unverified"
    assert "entry validity" in result["costs"]["swap"]["reason"]
    assert result["status"] != APPROVED


# ====================================================================== M-1
def test_m1_each_instrument_matches_only_its_own_fingerprint() -> None:
    terms = fixture_broker_terms()
    assert len(set(terms.values())) == 3  # symbol is part of the fingerprint
    c = synthetic_terms().components[0]
    for symbol in c.instruments:
        view = assess_component(
            c,
            "commission",
            datetime.now(UTC),
            symbol,
            live_terms=terms[symbol],
            contract_size=1000.0,
        )
        assert view["status"] == "VERIFIED", symbol
    swapped = assess_component(
        c,
        "commission",
        datetime.now(UTC),
        "GBPUSDc",
        live_terms=terms["EURUSDc"],
        contract_size=1000.0,
    )
    assert swapped["status"] == "BROKER_TERMS_CHANGED"  # EUR's fingerprint never validates GBP


def test_m1_one_instrument_changing_leaves_the_others_usable() -> None:
    terms = fixture_broker_terms()
    c = synthetic_terms().components[0]
    live = {**terms, "GBPUSDc": "sha256:" + "f" * 64}
    states = {
        s: assess_component(
            c, "commission", datetime.now(UTC), s, live_terms=live[s], contract_size=1000.0
        )["status"]
        for s in c.instruments
    }
    assert states == {
        "EURUSDc": "VERIFIED",
        "GBPUSDc": "BROKER_TERMS_CHANGED",
        "USDJPYc": "VERIFIED",
    }
    unknown = assess_component(
        c, "commission", datetime.now(UTC), "EURUSDc", live_terms=None, contract_size=1000.0
    )
    assert unknown["status"] == "BROKER_TERMS_UNKNOWN"


def test_m1_three_instrument_record_works_for_every_instrument_in_the_engine() -> None:
    terms = synthetic_terms()
    eur = propose(costs=terms)
    gbp = propose(
        make_fake(SELL_SYMBOLS), envelope("bearish", level=SELL_LEVEL), symbol="GBPUSD", costs=terms
    )
    jpy = propose(
        env=envelope(level=(161.000, 161.100), pivot=158.300, atr=0.32, symbol="USDJPY"),
        symbol="USDJPY",
        costs=terms,
    )
    for result in (eur, gbp, jpy):
        assert result["costs"]["commission"]["evidence"]["status"] == "VERIFIED"
        assert result["costs"]["slippage"]["evidence"]["status"] == "VERIFIED"


@pytest.mark.parametrize(
    "broker_terms",
    [
        None,  # VERIFIED evidence without fingerprints
        {"EURUSDc": "sha256:" + "a" * 64},  # missing two covered instruments
        {"EURUSDc": "sha256:" + "a" * 64, "GBPUSDc": "x", "USDJPYc": "sha256:" + "a" * 64},
        {"EURUSDc": "a", "GBPUSDc": "b", "USDJPYc": "c", "XAUUSDc": "d"},
    ],
)
def test_m1_missing_or_malformed_fingerprints_are_refused(broker_terms) -> None:
    c = synthetic_terms().components[0]
    with pytest.raises(ValueError):
        CostComponent(**{**c.__dict__, "broker_terms": broker_terms})


def test_m1_expired_and_out_of_scope_evidence_stay_unusable() -> None:
    old = synthetic_terms(when=datetime.now(UTC) - timedelta(days=31)).components[0]
    terms = fixture_broker_terms()
    expired = assess_component(
        old,
        "commission",
        datetime.now(UTC),
        "EURUSDc",
        live_terms=terms["EURUSDc"],
        contract_size=1000.0,
    )
    assert expired["status"] == "EXPIRED"
    scoped = synthetic_terms(symbols=("GBPUSDc",)).components[0]
    view = assess_component(
        scoped,
        "commission",
        datetime.now(UTC),
        "EURUSDc",
        live_terms=terms["EURUSDc"],
        contract_size=1000.0,
    )
    assert view["status"] == "OUT_OF_SCOPE"


# ====================================================================== M-2
def _monitor(tmp_path, positions=(), deals=(), migrations=None):
    fake = make_fake()
    fake.positions, fake.deals = list(positions), list(deals)
    live, _, _ = stack(fake, StubAnalysis())
    store = started(tmp_path, migrations=migrations or js.MIGRATIONS)
    return BrokerMonitoringService(store, live), store, fake


_POS = {
    "symbol": "EURUSDc",
    "type": 0,
    "volume": 0.1,
    "price_open": 1.1,
    "sl": 1.09,
    "tp": 1.12,
    "price_current": 1.1,
    "profit": 0.0,
    "swap": 0.0,
    "time": 1_700_000_000,
}


def _deal_row(**change):
    base = {
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
    return {**base, **change}


@pytest.mark.parametrize(
    ("positions", "deals"),
    [
        ([{**_POS, "identifier": None, "ticket": None}], []),  # position without id or ticket
        ([], [_deal_row(ticket=None)]),  # deal without ticket
        ([], [_deal_row(position_id=0)]),  # a buy deal without its position
        ([{**_POS, "identifier": None, "ticket": None}] * 2, []),  # several missing
        ([{**_POS, "identifier": "555001", "ticket": None}], []),  # wrong type
        ([], [_deal_row(ticket=1.5)]),
        ([], [_deal_row(ticket=True)]),
        ([{**_POS, "identifier": -5, "ticket": None}], []),
    ],
)
def test_m2_missing_or_invalid_identifiers_fail_closed(tmp_path, positions, deals) -> None:
    service, store, _ = _monitor(tmp_path, positions, deals, migrations=STAGED)
    summary = service.observe()
    assert (summary["status"], summary["reason"]) == ("UNAVAILABLE", "broker_identifier_missing")
    with closing(raw(tmp_path)) as conn:
        rows = conn.execute("SELECT status, payload FROM broker_observation").fetchall()
    assert [r[0] for r in rows] == ["UNAVAILABLE"]  # nothing that could merge objects is kept
    assert json.loads(rows[0][1]) == {"deals": [], "orders": [], "positions": []}


def test_m2_valid_identifiers_and_non_trade_deals(tmp_path) -> None:
    balance = _deal_row(ticket=777002, type=2, position_id=0, order=0)  # a deposit: no position
    service, _, _ = _monitor(tmp_path, [{**_POS, "identifier": 555001}], [_deal_row(), balance])
    summary = service.observe()
    assert summary["status"] == "LIVE" and summary["deals"] == 2
    deal_refs = {d["deal_ref"] for d in service._observation["deals"]}
    assert len(deal_refs) == 2  # distinct objects keep distinct references
    assert service._observation["deals"][1]["position_ref"] is None


def test_m2_missing_hmac_key_fails_closed(tmp_path) -> None:
    service, store, fake = _monitor(tmp_path, [{**_POS, "identifier": 555001}])
    store._key = None
    calls = len(fake.calls)
    assert service.observe()["reason"] == "hmac_unavailable" and len(fake.calls) == calls


@pytest.mark.parametrize("raw_id", [None, 0, -1, "", "123", 1.0, True])
def test_m2_broker_ref_refuses_missing_identifiers(tmp_path, raw_id) -> None:
    store = started(tmp_path)
    with pytest.raises(ValueError):
        store.broker_ref("position", raw_id)
    assert len(store.broker_ref("position", 123)) == 64


# ====================================================================== M-3
def _reconciled(tmp_path, n_trades=1):
    service, store, fake = _monitor(tmp_path, migrations=STAGED)
    book = TradeLogbook(store, None)
    for i in range(n_trades):
        book.create_trade(
            {
                "request_id": str(uuid.uuid4()),
                "symbol": "EURUSDc",
                "direction": "BUY",
                "entry": {
                    "time_utc": (datetime.now(UTC) - timedelta(minutes=5 + i)).isoformat(),
                    "price": 1.1,
                    "volume": 0.1,
                },
            }
        )
    service.observe()
    return service, store


def test_m3_reconciling_an_observation_is_idempotent(tmp_path) -> None:
    service, _ = _reconciled(tmp_path, n_trades=2)
    first = service.reconcile()["persistence"]
    second = service.reconcile()["persistence"]
    assert first == {"status": "persisted", "rows": 2}
    assert second == {"status": "already_reconciled", "rows": 2}
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM reconciliation_result").fetchone()[0] == 2


def _insert(conn, **change):
    obs = conn.execute("SELECT id FROM broker_observation").fetchone()[0]
    tid = conn.execute("SELECT id FROM trade").fetchone()[0]
    row = {
        "id": str(uuid.uuid4()),
        "observation_id": obs,
        "subject": "trade",
        "subject_key": tid,
        "trade_id": tid,
        "position_ref": None,
        "state": "UNMATCHED",
        "basis": "inferred",
        "detail": "{}",
        "recorded_utc": "t",
        **change,
    }
    conn.execute(
        f"INSERT INTO reconciliation_result({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )


@pytest.mark.parametrize(
    "change",
    [
        {},  # a duplicate logical result for the same observation and trade
        {"state": "CONFIRMED_OPEN", "basis": "inferred"},
        {"subject_key": None},
        {"subject_key": "another-trade"},
        {"state": "MAYBE"},
        {"basis": None},
        {"subject": "broker_position", "trade_id": None, "position_ref": None},
    ],
)
def test_m3_database_refuses_duplicates_and_invalid_rows(tmp_path, change) -> None:
    service, _ = _reconciled(tmp_path)
    service.reconcile()
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, **change)


def test_m3_persistence_errors_are_reported_and_rolled_back(tmp_path, monkeypatch) -> None:
    service, _ = _reconciled(tmp_path, n_trades=2)
    import johanna.broker_monitoring as bm

    real = bm.reconcile

    def corrupt(trades, observation, now, **kwargs):  # P6.2b.4: accepts ``links=``
        results = real(trades, observation, now, **kwargs)
        results[1] = {**results[1], "state": "CONFIRMED_OPEN", "basis": "inferred"}
        return results

    monkeypatch.setattr(bm, "reconcile", corrupt)
    outcome = service.reconcile()
    assert outcome["persistence"]["status"] == "failed"
    assert outcome["persistence"]["reason"] == "integrity_error"
    assert service.status()["last_error"] == "reconciliation_persist_failed"
    with closing(raw(tmp_path)) as conn:  # the valid first row was rolled back too
        assert conn.execute("SELECT count(*) FROM reconciliation_result").fetchone()[0] == 0


def test_m3_no_silent_conflict_resolution_remains_in_the_code() -> None:
    from pathlib import Path

    text = Path(bm_file()).read_text(encoding="utf-8")
    assert "OR IGNORE" not in text.upper()


def bm_file():
    import johanna.broker_monitoring as bm

    return bm.__file__


# ====================================================================== M-4
@pytest.mark.parametrize(
    "detail",
    [
        {"reason": "small"},
        {"candidates": [f"ref{i:061d}" for i in range(MAX_DETAIL_ITEMS)]},  # at the limit
        {"candidates": [f"ref{i:061d}" for i in range(MAX_DETAIL_ITEMS + 1)]},
        {"candidates": ["a" * 64] * 45, "reason": "x"},
        {"reason": "y" * 5000},
        {"candidates": ["z" * 5000] * 30},
        "not a mapping",
        {"when": datetime(2026, 1, 1), "set": {1, 2}, "nested": {"a": [1]}},
        {f"k{i}": "v" * 200 for i in range(40)},
    ],
)
def test_m4_details_are_always_valid_bounded_json(detail) -> None:
    text = bounded_detail(detail)
    parsed = json.loads(text)
    assert len(text) <= MAX_DETAIL_CHARS
    assert isinstance(parsed, dict)


def test_m4_truncation_is_explicit() -> None:
    at_limit = json.loads(bounded_detail({"candidates": ["a" * 64] * MAX_DETAIL_ITEMS}))
    assert "truncated" not in at_limit and len(at_limit["candidates"]) == MAX_DETAIL_ITEMS
    over = json.loads(bounded_detail({"candidates": ["a" * 64] * 45}))
    assert over["truncated"] is True and over["candidates_omitted"] == 45 - MAX_DETAIL_ITEMS
    assert len(over["candidates"]) == MAX_DETAIL_ITEMS


def test_m4_forty_candidates_persist_and_parse(tmp_path) -> None:
    deals = [
        _deal_row(
            ticket=800000 + i,
            position_id=900000 + i,
            time=int(datetime.now(UTC).timestamp()) - 60 - i,
        )
        for i in range(45)
    ]
    service, store, _ = _monitor(tmp_path, deals=deals, migrations=STAGED)
    TradeLogbook(store, None).create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {
                "time_utc": (datetime.now(UTC) - timedelta(seconds=90)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
        }
    )
    service.observe()
    assert service.reconcile()["persistence"]["status"] == "persisted"
    latest = service.latest()  # parses every stored detail
    trade = next(r for r in latest["results"] if r["subject"] == "trade")
    assert trade["state"] == "AMBIGUOUS"
    assert trade["detail"]["truncated"] is True and trade["detail"]["candidates_omitted"] > 0
    with closing(raw(tmp_path)) as conn:
        for (detail,) in conn.execute("SELECT detail FROM reconciliation_result"):
            json.loads(detail)


# ====================================================================== M-5
@pytest.mark.parametrize("lot_units", [None, 0, -1000.0, float("nan"), float("inf"), True])
def test_m5_per_lot_evidence_needs_a_valid_lot_size(lot_units) -> None:
    c = synthetic_terms().components[0]
    with pytest.raises(ValueError):
        CostComponent(**{**c.__dict__, "lot_units": lot_units})


def test_m5_lot_size_only_for_per_lot_amounts() -> None:
    slippage = synthetic_terms().components[1]
    with pytest.raises(ValueError):
        CostComponent(**{**slippage.__dict__, "lot_units": 1000.0})


@pytest.mark.parametrize(
    ("lot_units", "contract", "status"),
    [
        (1000.0, 1000.0, "VERIFIED"),  # Exness cent lot
        (100000.0, 1000.0, "LOT_SIZE_MISMATCH"),  # a standard-lot figure on a cent account
        (1000.0, 100000.0, "LOT_SIZE_MISMATCH"),
        (100000.0, 100000.0, "VERIFIED"),
        (1000.0, None, "LOT_SIZE_UNKNOWN"),
        (1000.0, 0, "LOT_SIZE_UNKNOWN"),
    ],
)
def test_m5_lot_size_is_checked_against_the_contract(lot_units, contract, status) -> None:
    c = CostComponent(**{**synthetic_terms().components[0].__dict__, "lot_units": lot_units})
    view = assess_component(
        c,
        "commission",
        datetime.now(UTC),
        "EURUSDc",
        live_terms=fixture_broker_terms()["EURUSDc"],
        contract_size=contract,
    )
    assert view["status"] == status


def test_m5_standard_lot_figure_never_becomes_actionable() -> None:
    result = propose(costs=synthetic_terms(commission=7.0, lot_units=100000.0))
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    assert result["costs"]["commission"]["evidence"]["status"] == "LOT_SIZE_MISMATCH"


@pytest.mark.parametrize(("currency", "value", "usc"), [("USC", 7.0, 7.0), ("USD", 0.07, 7.0)])
def test_m5_usc_and_usd_conversions_record_the_lot_basis(currency, value, usc) -> None:
    c = CostComponent(
        **{**synthetic_terms().components[0].__dict__, "currency": currency, "value": value}
    )
    converted = commission_round_trip_usc(c)
    assert converted["usc_per_lot_round_trip"] == pytest.approx(usc)
    assert converted["usc_per_0_01_lot_round_trip"] == pytest.approx(usc / 100)
    assert "per lot of 1000 units" in converted["steps"][0]


def test_m5_commission_volume_basis_and_rounding() -> None:
    result = propose(costs=synthetic_terms(commission=7.0))
    sizing = result["sizing"]
    assert result["costs"]["commission"]["per_lot_round_trip"] == 7.0
    assert sizing["loss_per_lot"]["commission"] == 7.0  # per 1000-unit broker lot
    volume = sizing["volume"]
    assert math.isclose(volume * 100, round(volume * 100))  # on the 0.01 step
    assert sizing["estimated_loss"] <= sizing["risk_budget"] + 1e-9


def test_m5_registry_refuses_commission_without_lot_units(tmp_path) -> None:
    started(tmp_path, migrations=STAGED)
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cost_policy_record(id, kind, timeframe, value, currency, unit, basis, "
                "instruments, status, record_json, record_fingerprint, broker_terms, lot_units, "
                "supersedes_id, actor, recorded_utc) VALUES ('R1','commission',NULL,1,'USC',"
                "'amount_per_lot','round_trip','[]','UNVERIFIED','{}','fp1',NULL,NULL,NULL,"
                "'user','t')"
            )


# ====================================================================== schema v5 migration
V4_TABLES = None


def _v4_journal(tmp_path) -> dict:
    store = started(tmp_path, migrations=V4, clock=lambda: NOW)  # a v4 journal
    journal = DecisionJournal(store, clock=lambda: NOW)
    env = envelope(evaluated_at=NOW)
    decision = journal.record_analysis(env, "analysis_request")
    journal.record_risk(propose(env=envelope(), costs=synthetic_terms()), decision)
    TradeLogbook(store, None, clock=lambda: NOW).create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {
                "time_utc": (NOW - timedelta(hours=1)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
            "decision_id": decision,
        }
    )
    store.stop()
    with closing(raw(tmp_path)) as conn:
        names = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        tables = {
            n: [tuple(r) for r in conn.execute(f"SELECT * FROM {n} ORDER BY rowid")] for n in names
        }
    return {"tables": tables, "key": (tmp_path / js.KEY_NAME).read_bytes()}


def test_v5_corrected_migration_preserves_every_v4_row(tmp_path) -> None:
    before = _v4_journal(tmp_path)
    store = started(tmp_path, migrations=V5)
    assert store.ready and store.status()["schema_version"] == 5
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v4" in pairs[0].name
    assert (tmp_path / js.KEY_NAME).read_bytes() == before["key"]
    with closing(raw(tmp_path)) as conn:
        for table, rows in before["tables"].items():
            assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] == (
                rows
            ), table
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {r[1] for r in conn.execute("PRAGMA table_info(reconciliation_result)")}
        assert "subject_key" in columns
        columns = {r[1] for r in conn.execute("PRAGMA table_info(cost_policy_record)")}
        assert {"broker_terms", "lot_units"} <= columns
        assert "broker_terms_fingerprint" not in columns
    assert store.verify_audit_chain()["valid"] is True
    again = started(tmp_path, migrations=V5)  # a second start: nothing pending
    assert again.ready and len(again.valid_pairs()) == 1


def test_v5_failed_migration_rolls_back_and_a_retry_succeeds(tmp_path) -> None:
    before = _v4_journal(tmp_path)
    broken = (*V4, (5, js.SCHEMA_V5 + "\nCREATE TABLE broken ("))
    store = started(tmp_path, migrations=broken)
    assert store.status()["reason"] == "migration_failed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='reconciliation_result'"
            ).fetchone()[0]
            == 0
        )
        for table, rows in before["tables"].items():
            assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] == (
                rows
            ), table
    retry = started(tmp_path, migrations=V5)
    assert retry.ready and retry.status()["schema_version"] == 5


def test_v5_missing_or_wrong_key_and_corrupt_journal_fail_closed(tmp_path) -> None:
    _v4_journal(tmp_path)
    key = (tmp_path / js.KEY_NAME).read_bytes()
    (tmp_path / js.KEY_NAME).unlink()
    assert started(tmp_path, migrations=STAGED).status()["reason"] == (
        "migration_requires_verified_key"
    )
    assert not (tmp_path / js.KEY_NAME).exists()
    (tmp_path / js.KEY_NAME).write_bytes(b"\x01" * 32)
    assert started(tmp_path, migrations=STAGED).status()["reason"] == (
        "migration_requires_verified_key"
    )
    (tmp_path / js.KEY_NAME).write_bytes(key)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    corrupt = tmp_path / "c"
    corrupt.mkdir()
    (corrupt / js.DB_NAME).write_bytes(b"corrupt" * 100)
    (corrupt / js.KEY_NAME).write_bytes(key)
    refused = started(corrupt, migrations=STAGED)
    assert refused.status()["reason"] == "corrupt_database"
    assert (corrupt / js.DB_NAME).read_bytes() == b"corrupt" * 100


def test_v5_is_registered_once_after_v4() -> None:
    assert [v for v, _ in js.MIGRATIONS] == [1, 2, 3, 4, 5, 6]  # v6 since P6.2b.5
    assert js.MIGRATIONS[4] == (5, js.SCHEMA_V5)
    assert CostTerms().configured is False
