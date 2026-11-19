"""P5.2 manual trade logbook: recording, accounting, idempotency, integrity and migration.

Every test uses a temporary journal; nothing touches the real ``data/`` directory or MT5.
"""

from __future__ import annotations

import copy
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_journal_store import Acl, raw, started

from johanna import journal_store as js
from johanna.decision_journal import DecisionJournal
from johanna.journal_store import DB_NAME, KEY_NAME, validate_pair
from johanna.trade_logbook import BANNER, LogbookError, TradeLogbook

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SPECS = {
    "EURUSD": ("EUR", "USD", 5),
    "GBPUSD": ("GBP", "USD", 5),
    "USDJPY": ("USD", "JPY", 3),
}


def t(minutes: float) -> str:
    """An ISO time ``minutes`` before NOW."""
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def rid() -> str:
    return str(uuid.uuid4())


class FakeAccount:
    """Phase 3 payload shapes (instrument spec and quotes) without MT5."""

    def __init__(
        self, *, status="LIVE", validation="verified", quotes=None, fail=False, field_status=None
    ):
        self.status, self.validation, self.fail = status, validation, fail
        self.field_status = field_status or {}  # field name -> status override
        self.quotes = quotes or {}
        self.calls = 0

    def _currency(self):
        return {
            "value": "USC",
            "status": "verified",
            "unit": {
                "kind": "cent",
                "major": "USD",
                "units_per_major": 100,
                "confirmation": "consistent",
            },
        }

    def instrument(self, symbol):
        self.calls += 1
        if self.fail:
            raise RuntimeError("market data down")
        base, profit, digits = SPECS[symbol]
        fields = {
            "broker_symbol": f"{symbol}c",
            "contract_size": 100000,
            "digits": digits,
            "point": 10.0**-digits,
            "tick_size": 10.0**-digits,
            "tick_value": 1.0,
            "volume_min": 0.01,
            "volume_max": 200.0,
            "volume_step": 0.01,
            "currency_base": base,
            "currency_profit": profit,
            "trade_mode": 4,
        }
        return {
            "status": self.status,
            "retrieved_at_utc": "2026-11-17T11:59:00Z",
            "account_currency": self._currency(),
            "instrument": {
                "symbol": symbol,
                "validation": self.validation,
                "fields": {
                    k: {"value": v, "status": self.field_status.get(k, "verified")}
                    for k, v in fields.items()
                },
            },
        }

    def instruments(self):
        items = []
        for symbol, (bid, ask, age, freshness) in self.quotes.items():
            items.append(
                {
                    "symbol": symbol,
                    "quote": {
                        "bid": {"value": bid, "status": "verified"},
                        "ask": {"value": ask, "status": "verified"},
                        "quote_age_seconds": age,
                        "freshness": freshness,
                    },
                }
            )
        return {"status": self.status, "instruments": items}


def book(tmp_path, account=None, **kwargs) -> TradeLogbook:
    store = started(tmp_path, clock=lambda: NOW, **kwargs)
    assert store.ready
    return TradeLogbook(store, FakeAccount() if account is None else account, clock=lambda: NOW)


def entry(price=1.1000, volume=0.10, minutes=60, **extra) -> dict:
    return {"time_utc": t(minutes), "price": price, "volume": volume, **extra}


def create(lb: TradeLogbook, symbol="EURUSDc", direction="BUY", **body) -> str:
    body.setdefault("entry", entry())
    status, payload = lb.create_trade(
        {"request_id": rid(), "symbol": symbol, "direction": direction, **body}
    )
    assert status == 201
    return payload["trade_id"]


def fill(lb, trade_id, kind, price, volume, minutes, **extra):
    return lb.add_fill(
        trade_id,
        {
            "request_id": rid(),
            "kind": kind,
            "price": price,
            "volume": volume,
            "time_utc": t(minutes),
            **extra,
        },
    )


def detail(lb, trade_id, quotes=True) -> dict:
    return lb.get(trade_id, unrealised=quotes)["trade"]


def rejected(call, status=400, code=None) -> LogbookError:
    with pytest.raises(LogbookError) as info:
        call()
    assert info.value.status == status
    if code:
        assert info.value.code == code
    return info.value


# ====================================================================== recording
def test_trade_exists_from_its_first_entry_and_is_manual_unverified(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    trade = detail(lb, trade_id)
    assert trade["status"] == "OPEN" and trade["verification"] == "MANUAL_UNVERIFIED"
    assert trade["execution_source"] == "manual" and trade["symbol"] == "EURUSDc"
    assert [f["kind"] for f in trade["fills"]] == ["entry"]
    assert trade["specification"] == "verified"
    assert lb.list()["banner"] == BANNER


@pytest.mark.parametrize(
    ("sl", "tp", "state"),
    [
        (1.09, 1.12, "COMPLETE"),
        (1.09, None, "PARTIAL"),
        (None, 1.12, "PARTIAL"),
        (None, None, "MISSING"),
    ],
)
def test_missing_protection_is_recordable_and_explicit(tmp_path, sl, tp, state) -> None:
    lb = book(tmp_path)
    trade = detail(lb, create(lb, stop_loss=sl, take_profit=tp))
    assert trade["protection"] == {
        "state": state,
        "stop_loss": sl,
        "take_profit": tp,
        "effective_utc": "2026-11-17T11:00:00.000000Z",
    }


def test_protection_history_is_append_only_and_latest_wins(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    status, _ = lb.add_protection(
        trade_id,
        {"request_id": rid(), "stop_loss": 1.095, "effective_utc": t(30), "reason": "added SL"},
    )
    assert status == 201
    trade = detail(lb, trade_id)
    assert trade["protection"]["state"] == "PARTIAL" and trade["protection"]["stop_loss"] == 1.095
    assert [p["sl"] for p in trade["protection_history"]] == [None, 1.095]
    rejected(
        lambda: lb.add_protection(
            trade_id,
            {"request_id": rid(), "stop_loss": 1.2, "take_profit": 1.1, "effective_utc": t(1)},
        ),
        code="invalid_protection",
    )


@pytest.mark.parametrize(
    ("direction", "sl", "tp"),
    [("BUY", 1.11, None), ("BUY", None, 1.09), ("SELL", 1.09, None), ("SELL", None, 1.11)],
)
def test_protection_on_the_wrong_side_of_the_entry_is_rejected(tmp_path, direction, sl, tp):
    lb = book(tmp_path)
    rejected(
        lambda: lb.create_trade(
            {
                "request_id": rid(),
                "symbol": "EURUSDc",
                "direction": direction,
                "entry": entry(),
                "stop_loss": sl,
                "take_profit": tp,
            }
        ),
        code="invalid_protection",
    )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"symbol": "XAUUSD"}, "unsupported_symbol"),
        ({"symbol": "EURUSD.raw"}, "unsupported_symbol"),
        ({"direction": "LONG"}, "invalid_direction"),
        ({"entry": entry(price=0)}, "invalid_number"),
        ({"entry": entry(price=-1)}, "invalid_number"),
        ({"entry": entry(volume=0)}, "invalid_number"),
        ({"entry": entry(volume=True)}, "invalid_number"),
        ({"entry": entry(volume=0.015)}, "invalid_volume"),
        ({"entry": entry(volume=0.001)}, "invalid_volume"),
        ({"entry": entry(price=1.123456)}, "invalid_price"),
        ({"entry": {**entry(), "time_utc": "2026-11-17T10:00:00"}}, "invalid_time"),
        ({"entry": {**entry(), "time_utc": "yesterday"}}, "invalid_time"),
        ({"entry": entry(minutes=-10)}, "invalid_time"),
        ({"entry": entry(realised_pnl=5)}, "invalid_fill"),
        ({"entry": None}, "invalid_fill"),
        ({"note": "x" * 4001}, "invalid_text"),
    ],
)
def test_invalid_trades_are_rejected_and_nothing_is_stored(tmp_path, body, code) -> None:
    lb = book(tmp_path)
    payload = {"request_id": rid(), "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
    rejected(lambda: lb.create_trade({**payload, **body}), code=code)
    with closing(raw(tmp_path)) as conn:
        for table in ("trade", "trade_fill", "write_request"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_all_three_supported_symbols_accept_canonical_or_broker_names(tmp_path) -> None:
    lb = book(tmp_path)
    for symbol, price in (("EURUSD", 1.1), ("gbpusdc", 1.3), ("USDJPYc", 150.0)):
        trade = detail(lb, create(lb, symbol=symbol, entry=entry(price=price)))
        assert trade["symbol"].endswith("c") and trade["symbol"][:-1] == symbol.upper()[:6]


def test_unverified_specification_still_records_but_withholds_calculated_pnl(tmp_path):
    lb = book(tmp_path, account=FakeAccount(fail=True))
    trade_id = create(lb, entry=entry(volume=0.015))  # no step check without a spec
    fill(lb, trade_id, "exit", 1.1100, 0.015, 30, realised_pnl=150.0)
    trade = detail(lb, trade_id)
    acc = trade["accounting"]
    assert trade["specification"] == "unverified"
    assert acc["gross_realised_calculated"] is None
    assert acc["gross_realised_calculated_method"].startswith("unavailable")
    assert acc["gross_realised_user_entered"] == 150.0  # the user's figure is still shown
    assert acc["net_realised"] is None


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"status": "UNAVAILABLE"}, "instrument specification not LIVE"),
        # Phase 3 serves CACHED, without an age limit, after a failed MT5 read
        ({"status": "CACHED"}, "instrument specification not LIVE"),
        ({"validation": "incomplete"}, "instrument specification not verified"),
        ({"validation": "invalid"}, "instrument specification not verified"),
        # a "partial" (or even "verified") label never stands in for a required field
        (
            {"validation": "partial", "field_status": {"tick_value": "unavailable"}},
            "a required instrument field is not verified",
        ),
        (
            {"validation": "partial", "field_status": {"trade_mode": "invalid"}},
            "a required instrument field is not verified",
        ),
        (
            {"validation": "verified", "field_status": {"volume_step": "unverified"}},
            "a required instrument field is not verified",
        ),
    ],
)
def test_specification_needs_live_fully_verified_phase3_data(tmp_path, override, reason) -> None:
    lb = book(tmp_path, account=FakeAccount(**override))
    spec = lb.instrument_spec("EURUSD")
    assert spec == {"status": "unverified", "reason": reason}
    # still recordable; no volume-step check and no calculated P&L without a verified spec
    trade_id = create(lb, entry=entry(volume=0.015))
    fill(lb, trade_id, "exit", 1.1100, 0.015, 30)
    trade = detail(lb, trade_id)
    assert trade["specification"] == "unverified"
    assert trade["accounting"]["gross_realised_calculated"] is None
    assert trade["accounting"]["net_realised"] is None


def test_partial_label_passes_only_when_every_required_field_is_verified(tmp_path) -> None:
    lb = book(tmp_path, account=FakeAccount(validation="partial"))
    assert lb.instrument_spec("EURUSD")["status"] == "verified"
    rejected(lambda: create(lb, entry=entry(volume=0.015)), code="invalid_volume")


def test_spec_is_captured_once_at_creation_and_used_for_later_fills(tmp_path) -> None:
    account = FakeAccount()
    lb = book(tmp_path, account=account)
    trade_id = create(lb)
    account.status = "UNAVAILABLE"  # MT5 gone later: the captured snapshot still applies
    rejected(lambda: fill(lb, trade_id, "exit", 1.1, 0.015, 30), code="invalid_volume")
    assert detail(lb, trade_id, quotes=False)["specification"] == "verified"


# ====================================================================== accounting
def test_weighted_average_uses_only_entries_recorded_before_each_exit(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, entry=entry(1.1000, 0.20, minutes=100))
    fill(lb, trade_id, "entry", 1.1200, 0.20, 90)
    fill(lb, trade_id, "exit", 1.1300, 0.20, 80)  # avg entry 1.1100 -> +0.0200
    fill(lb, trade_id, "entry", 1.0000, 0.20, 70)  # later entry must not touch the first exit
    fill(lb, trade_id, "exit", 1.1200, 0.40, 60)
    acc = detail(lb, trade_id)["accounting"]
    first, second = acc["exits"]
    assert first["average_entry_price"] == pytest.approx(1.11)
    assert first["gross_calculated"] == pytest.approx(0.02 * 20000 * 100)  # 400 USD in USC
    # remaining 0.20 @1.1100 + 0.20 @1.0000 -> 1.0550 average
    assert second["average_entry_price"] == pytest.approx(1.055)
    assert second["gross_calculated"] == pytest.approx(0.065 * 40000 * 100)
    assert acc["gross_realised_calculated"] == pytest.approx(40000 + 260000)
    assert acc["gross_realised_calculated_method"] == "calculated_exact"
    assert acc["open_volume"] == 0 and detail(lb, trade_id)["status"] == "CLOSED"


def test_backdated_entry_after_the_fact_only_changes_later_exits(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, entry=entry(1.1000, 0.10, minutes=100))
    fill(lb, trade_id, "exit", 1.1050, 0.05, 50)
    before = detail(lb, trade_id)["accounting"]["exits"][0]["gross_calculated"]
    fill(lb, trade_id, "entry", 1.2000, 0.10, 20)  # recorded now, but executed after the exit
    after = detail(lb, trade_id)["accounting"]["exits"][0]["gross_calculated"]
    assert before == after == pytest.approx(0.005 * 5000 * 100)


def test_sell_partial_close_and_status(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, direction="SELL", entry=entry(1.3000, 0.10), symbol="GBPUSDc")
    fill(lb, trade_id, "exit", 1.2950, 0.04, 30)
    trade = detail(lb, trade_id, quotes=False)
    assert trade["status"] == "PARTIALLY_CLOSED"
    acc = trade["accounting"]
    assert acc["exits"][0]["gross_calculated"] == pytest.approx(0.005 * 4000 * 100)
    assert acc["open_volume"] == pytest.approx(0.06)
    assert acc["open_average_entry_price"] == pytest.approx(1.3)


def test_usdjpy_conversion_is_labelled_estimated(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, symbol="USDJPYc", direction="SELL", entry=entry(150.0, 0.10))
    fill(lb, trade_id, "exit", 149.0, 0.10, 30)
    acc = detail(lb, trade_id)["accounting"]
    assert acc["exits"][0]["gross_calculated"] == pytest.approx(1.0 * 10000 / 149.0 * 100, abs=0.01)
    assert acc["exits"][0]["gross_calculated_method"].startswith("calculated_estimate")
    assert acc["gross_realised_calculated_method"] == "calculated_estimate"
    assert acc["currency"] == "USC"


def test_unknown_costs_are_never_zero_and_withhold_net(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, entry=entry(commission=-70.0))
    fill(lb, trade_id, "exit", 1.1010, 0.10, 30, commission=-70.0)
    acc = detail(lb, trade_id)["accounting"]
    assert acc["costs"]["commission"] == {
        "known_total": -140.0,
        "unknown_fills": 0,
        "complete": True,
    }
    assert acc["costs"]["swap"]["complete"] is False and acc["costs"]["swap"]["unknown_fills"] == 2
    assert acc["costs_complete"] is False
    assert acc["net_realised"] is None and acc["net_realised_status"] == "incomplete: unknown costs"
    assert acc["gross_realised_calculated"] == pytest.approx(1000.0)


def test_known_costs_give_net_realised(tmp_path) -> None:
    lb = book(tmp_path)
    costs = {"commission": -70.0, "swap": 0.0, "fee": 0.0}
    trade_id = create(lb, entry=entry(**costs))
    fill(lb, trade_id, "exit", 1.1010, 0.10, 30, commission=-70.0, swap=-12.5, fee=0.0)
    acc = detail(lb, trade_id)["accounting"]
    assert acc["costs_complete"] is True
    assert acc["net_realised"] == pytest.approx(1000.0 - 140.0 - 12.5)
    assert acc["net_realised_status"] == "calculated_exact"


def test_user_entered_and_calculated_pnl_are_kept_separately(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    fill(lb, trade_id, "exit", 1.1010, 0.05, 40, realised_pnl=480.0)
    fill(lb, trade_id, "exit", 1.1020, 0.05, 30)
    acc = detail(lb, trade_id)["accounting"]
    assert [e["gross_user_entered"] for e in acc["exits"]] == [480.0, None]
    assert [e["gross_user_entered_source"] for e in acc["exits"]] == ["user_entered", "unknown"]
    assert acc["gross_realised_user_entered"] is None  # one exit has no user figure
    assert acc["gross_realised_calculated"] == pytest.approx(500.0 + 1000.0)


def test_unrealised_needs_a_fresh_live_quote_and_is_an_estimate(tmp_path) -> None:
    account = FakeAccount(quotes={"EURUSD": (1.1050, 1.1052, 1.0, "LIVE")})
    lb = book(tmp_path, account=account)
    trade_id = create(lb)
    unrealised = detail(lb, trade_id)["accounting"]["unrealised"]
    assert unrealised["value"] == pytest.approx(0.005 * 10000 * 100)  # closes at the bid
    assert unrealised["status"] == "estimate from live quote, not broker-verified"
    account.quotes = {"EURUSD": (1.1050, 1.1052, 30.0, "STALE")}
    assert detail(lb, trade_id)["accounting"]["unrealised"]["value"] is None
    account.quotes = {"EURUSD": (1.1050, 1.1052, 12.0, "LIVE")}
    assert detail(lb, trade_id)["accounting"]["unrealised"]["value"] is None
    account.quotes = {}
    missing = detail(lb, trade_id)["accounting"]["unrealised"]
    assert missing == {"value": None, "status": "unavailable: no live quote available"}
    assert detail(lb, trade_id, quotes=False)["accounting"]["unrealised"]["value"] is None


def test_sell_unrealised_closes_at_the_ask(tmp_path) -> None:
    lb = book(tmp_path, account=FakeAccount(quotes={"EURUSD": (1.0990, 1.0995, 0.5, "LIVE")}))
    trade_id = create(lb, direction="SELL")
    assert detail(lb, trade_id)["accounting"]["unrealised"]["value"] == pytest.approx(5.0 * 100)


# ====================================================================== timeline rules
def test_exits_may_never_exceed_prior_entries(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, entry=entry(volume=0.10, minutes=60))
    rejected(lambda: fill(lb, trade_id, "exit", 1.1, 0.11, 30), code="exit_exceeds_entries")
    # an exit dated before the first entry is also impossible
    rejected(lambda: fill(lb, trade_id, "exit", 1.1, 0.05, 90), code="exit_exceeds_entries")
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM trade_fill").fetchone()[0] == 1


# ====================================================================== voids and corrections
def test_void_fill_with_replacement_links_the_correction(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    _, added = fill(lb, trade_id, "exit", 1.1010, 0.10, 30)
    status, result = lb.void_fill(
        trade_id,
        added["fill_id"],
        {
            "request_id": rid(),
            "reason": "typo in price",
            "replacement": {"price": 1.1020, "volume": 0.10, "time_utc": t(30)},
        },
    )
    assert status == 200 and result["replacement_fill_id"]
    fills = {f["id"]: f for f in detail(lb, trade_id)["fills"]}
    assert fills[added["fill_id"]]["status"] == "VOIDED"
    assert fills[added["fill_id"]]["voided_reason"] == "typo in price"
    replacement = fills[result["replacement_fill_id"]]
    assert replacement["corrects_fill_id"] == added["fill_id"] and replacement["kind"] == "exit"
    acc = detail(lb, trade_id)["accounting"]
    assert len(acc["exits"]) == 1 and acc["gross_realised_calculated"] == pytest.approx(2000.0)


def test_voiding_a_fill_that_would_break_the_timeline_is_rejected(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    first_entry = detail(lb, trade_id)["fills"][0]["id"]
    rejected(
        lambda: lb.void_fill(trade_id, first_entry, {"request_id": rid(), "reason": "x"}),
        code="no_entry",
    )
    _, extra = fill(lb, trade_id, "entry", 1.1, 0.10, 50)
    fill(lb, trade_id, "exit", 1.1, 0.15, 30)  # needs the extra entry
    rejected(lambda: lb.void_fill(trade_id, extra["fill_id"], {"request_id": rid(), "reason": "x"}))
    assert all(f["status"] == "RECORDED" for f in detail(lb, trade_id)["fills"])


def test_void_requires_a_reason_and_is_one_way(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    _, added = fill(lb, trade_id, "entry", 1.1, 0.10, 30)
    rejected(
        lambda: lb.void_fill(trade_id, added["fill_id"], {"request_id": rid()}), code="invalid_text"
    )
    lb.void_fill(trade_id, added["fill_id"], {"request_id": rid(), "reason": "duplicate"})
    rejected(
        lambda: lb.void_fill(trade_id, added["fill_id"], {"request_id": rid(), "reason": "again"}),
        409,
    )


def test_voided_trade_keeps_its_records_and_refuses_writes(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, note="first note")
    status, _ = lb.void_trade(trade_id, {"request_id": rid(), "reason": "recorded twice"})
    assert status == 200
    trade = detail(lb, trade_id)
    assert trade["status"] == "VOIDED" and trade["voided_reason"] == "recorded twice"
    assert len(trade["fills"]) == 1 and len(trade["notes"]) == 1
    for call in (
        lambda: fill(lb, trade_id, "entry", 1.1, 0.1, 10),
        lambda: lb.add_note(trade_id, {"request_id": rid(), "text": "x"}),
        lambda: lb.void_trade(trade_id, {"request_id": rid(), "reason": "again"}),
    ):
        rejected(call, 409, "trade_voided")
    assert [x["id"] for x in lb.list(status="VOIDED")["trades"]] == [trade_id]
    assert lb.list(status="OPEN")["trades"] == []


def test_unknown_ids_are_404(tmp_path) -> None:
    lb = book(tmp_path)
    missing = "01JZZZZZZZZZZZZZZZZZZZZZZZ"
    rejected(lambda: lb.add_note(missing, {"request_id": rid(), "text": "x"}), 404)
    trade_id = create(lb)
    rejected(lambda: lb.void_fill(trade_id, missing, {"request_id": rid(), "reason": "x"}), 404)
    assert lb.get(missing) is None


# ====================================================================== idempotency
def test_same_request_id_and_payload_returns_the_original_result(tmp_path) -> None:
    lb = book(tmp_path)
    payload = {"request_id": rid(), "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
    first = lb.create_trade(copy.deepcopy(payload))
    second = lb.create_trade(copy.deepcopy(payload))
    assert first == second
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM trade").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM write_request").fetchone()[0] == 1


def test_key_order_does_not_change_the_payload_identity(tmp_path) -> None:
    lb = book(tmp_path)
    request_id = rid()
    a = lb.create_trade(
        {
            "request_id": request_id,
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {"price": 1.1, "volume": 0.1, "time_utc": t(5)},
        }
    )
    b = lb.create_trade(
        {
            "entry": {"time_utc": t(5), "volume": 0.1, "price": 1.1},
            "direction": "BUY",
            "symbol": "EURUSDc",
            "request_id": request_id,
        }
    )
    assert a == b


def test_same_request_id_with_a_different_payload_is_409(tmp_path) -> None:
    lb = book(tmp_path)
    request_id = rid()
    status, created = lb.create_trade(
        {"request_id": request_id, "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
    )
    rejected(
        lambda: lb.create_trade(
            {
                "request_id": request_id,
                "symbol": "EURUSDc",
                "direction": "BUY",
                "entry": entry(price=1.2),
            }
        ),
        409,
        "request_id_conflict",
    )
    # reuse against a different endpoint is also a conflict
    rejected(
        lambda: lb.add_note(created["trade_id"], {"request_id": request_id, "text": "x"}),
        409,
        "request_id_conflict",
    )
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM trade").fetchone()[0] == 1


def test_rejected_requests_are_not_remembered(tmp_path) -> None:
    lb = book(tmp_path)
    request_id = rid()
    rejected(
        lambda: lb.create_trade(
            {"request_id": request_id, "symbol": "EURUSDc", "direction": "UP", "entry": entry()}
        )
    )
    status, _ = lb.create_trade(
        {"request_id": request_id, "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
    )
    assert status == 201


@pytest.mark.parametrize("request_id", [None, "", "abc", 123, "not-a-uuid-at-all-0000000000000000"])
def test_request_id_must_be_a_uuid(tmp_path, request_id) -> None:
    lb = book(tmp_path)
    rejected(
        lambda: lb.create_trade(
            {"request_id": request_id, "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
        ),
        code="invalid_request_id",
    )


def test_refused_journal_is_503_and_reads_nothing_from_mt5(tmp_path) -> None:
    store = started(tmp_path, acl=Acl(fail=True))
    assert not store.ready
    account = FakeAccount()
    lb = TradeLogbook(store, account, clock=lambda: NOW)
    rejected(
        lambda: lb.create_trade(
            {"request_id": rid(), "symbol": "EURUSDc", "direction": "BUY", "entry": entry()}
        ),
        503,
        "journal_unavailable",
    )
    assert account.calls == 0


# ====================================================================== links and decisions
def _decision(tmp_path, lb, symbol="EURUSD") -> str:
    from risk_fixtures import BUY_LEVEL, envelope

    env = envelope(level=BUY_LEVEL)
    env["result"]["symbol"] = symbol
    return DecisionJournal(lb.store, clock=lambda: NOW).record_analysis(env, "analysis_request")


def test_links_derive_decision_execution_status(tmp_path) -> None:
    lb = book(tmp_path)
    journal = DecisionJournal(lb.store, clock=lambda: NOW)
    decision_id = _decision(tmp_path, lb)
    assert journal.get(decision_id)["decision"]["execution_status"] == "NONE"
    trade_id = create(lb, decision_id=decision_id)
    assert journal.get(decision_id)["decision"]["execution_status"] == "MANUALLY_RECORDED"
    assert [d["id"] for d in journal.list(execution="MANUALLY_RECORDED")["decisions"]] == [
        decision_id
    ]
    assert journal.list(execution="NONE")["decisions"] == []
    assert journal.list(execution="BROKER_VERIFIED")["decisions"] == []
    link = detail(lb, trade_id)["links"][0]
    assert link["decision_id"] == decision_id and link["status"] == "ACTIVE"
    rejected(
        lambda: lb.add_link(trade_id, {"request_id": rid(), "decision_id": decision_id}),
        409,
        "already_linked",
    )
    lb.void_link(trade_id, link["id"], {"request_id": rid(), "reason": "wrong decision"})
    assert journal.get(decision_id)["decision"]["execution_status"] == "NONE"
    lb.add_link(trade_id, {"request_id": rid(), "decision_id": decision_id})
    assert journal.get(decision_id)["decision"]["execution_status"] == "MANUALLY_RECORDED"
    lb.void_trade(trade_id, {"request_id": rid(), "reason": "mistake"})
    assert journal.get(decision_id)["decision"]["execution_status"] == "NONE"


def test_link_validation(tmp_path) -> None:
    lb = book(tmp_path)
    decision_id = _decision(tmp_path, lb)
    gbp = create(lb, symbol="GBPUSDc", entry=entry(price=1.3))
    rejected(
        lambda: lb.add_link(gbp, {"request_id": rid(), "decision_id": decision_id}),
        code="invalid_link",
    )
    rejected(
        lambda: lb.add_link(
            gbp, {"request_id": rid(), "decision_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ"}
        ),
        404,
    )
    eur = create(lb)
    rejected(
        lambda: lb.add_link(
            eur,
            {
                "request_id": rid(),
                "decision_id": decision_id,
                "risk_assessment_id": "01JZZZZZZZZZZZZZZZZZZZZZZZ",
            },
        ),
        code="invalid_link",
    )


def test_linking_never_creates_a_trade(tmp_path) -> None:
    lb = book(tmp_path)
    _decision(tmp_path, lb)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM trade").fetchone()[0] == 0


# ====================================================================== integrity
def test_every_write_is_audited_by_the_user_and_the_chain_verifies(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, note="why")
    fill(lb, trade_id, "exit", 1.1, 0.05, 30)
    lb.add_protection(trade_id, {"request_id": rid(), "stop_loss": 1.09, "effective_utc": t(20)})
    audit = detail(lb, trade_id)["audit"]
    assert [a["action"] for a in audit] == [
        "trade.recorded",
        "fill.recorded",
        "protection.recorded",
        "note.recorded",
        "fill.recorded",
        "protection.recorded",
    ]
    assert {a["actor"] for a in audit} == {"user"}
    assert lb.store.verify_audit_chain()["valid"] is True


def test_triggers_forbid_deleting_or_rewriting_trade_records(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb, note="n")
    statements = [
        "DELETE FROM trade",
        "DELETE FROM trade_fill",
        "DELETE FROM trade_protection",
        "DELETE FROM trade_note",
        "DELETE FROM write_request",
        "UPDATE trade_fill SET price = 2",
        "UPDATE trade SET direction = 'SELL'",
        "UPDATE trade_note SET text = 'edited'",
        "UPDATE trade_protection SET sl = 1",
        "UPDATE write_request SET status_code = 200",
    ]
    with closing(raw(tmp_path)) as conn:
        for sql in statements:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        conn.execute("UPDATE trade SET voided_utc='x', voided_reason='y' WHERE id=?", (trade_id,))
        with pytest.raises(sqlite3.IntegrityError):  # un-voiding is impossible
            conn.execute("UPDATE trade SET voided_utc=NULL, voided_reason=NULL")


def test_no_sensitive_broker_identifiers_are_stored_or_returned(tmp_path) -> None:
    lb = book(tmp_path)
    trade_id = create(lb)
    trade = detail(lb, trade_id)
    text = repr(trade)
    for word in ("ticket", "login", "position_id", "broker_deal_ref", "holder"):
        assert word not in text
    with closing(raw(tmp_path)) as conn:
        row = conn.execute(
            "SELECT broker_deal_ref, execution_attempt_id FROM trade_fill"
        ).fetchone()
        assert tuple(row) == (None, None)


def test_list_filters_and_pagination(tmp_path) -> None:
    lb = book(tmp_path)
    ids = [create(lb) for _ in range(3)]
    gbp = create(lb, symbol="GBPUSDc", entry=entry(price=1.3))
    assert [x["id"] for x in lb.list(symbol="GBPUSDc")["trades"]] == [gbp]
    page1 = lb.list(limit=2)
    assert [x["id"] for x in page1["trades"]] == [gbp, ids[2]] and page1["next_cursor"]
    page2 = lb.list(limit=2, cursor=int(page1["next_cursor"]))
    assert [x["id"] for x in page2["trades"]] == [ids[1], ids[0]] and page2["next_cursor"] is None
    assert lb.list(source="automated")["trades"] == []


# ====================================================================== migration v1 -> v2
V1_ONLY = js.MIGRATIONS[:1]
V1_TO_V2 = js.MIGRATIONS[:2]  # these tests cover the P5.2 v1 -> v2 migration specifically


def test_v1_journal_migrates_to_v2_after_a_verified_backup(tmp_path) -> None:
    v1 = started(tmp_path, migrations=V1_ONLY, clock=lambda: NOW)
    journal = DecisionJournal(v1, clock=lambda: NOW)
    from risk_fixtures import BUY_LEVEL, envelope

    decision_id = journal.record_analysis(envelope(level=BUY_LEVEL), "analysis_request")
    key_before = (tmp_path / KEY_NAME).read_bytes()
    with closing(raw(tmp_path)) as conn:
        audit_before = [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
    v1.stop()

    store = started(tmp_path, migrations=V1_TO_V2, clock=lambda: NOW)
    assert store.ready and store.status()["schema_version"] == 2
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v1" in pairs[0].name and validate_pair(pairs[0])
    with closing(sqlite3.connect(pairs[0] / DB_NAME)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
    assert (tmp_path / KEY_NAME).read_bytes() == key_before
    with closing(raw(tmp_path)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")] == (
            audit_before
        )
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "trade",
        "trade_fill",
        "trade_protection",
        "trade_note",
        "trade_decision_link",
        "write_request",
        "decision",
        "risk_assessment",
        "audit_log",
    } <= tables
    assert store.verify_audit_chain()["valid"] is True
    assert (
        DecisionJournal(store, clock=lambda: NOW).get(decision_id)["decision"]["execution_status"]
        == "NONE"
    )
    lb = TradeLogbook(store, FakeAccount(), clock=lambda: NOW)
    create(lb, decision_id=decision_id)


def test_failed_v2_migration_leaves_v1_intact(tmp_path) -> None:
    v1 = started(tmp_path, migrations=V1_ONLY, clock=lambda: NOW)
    from risk_fixtures import BUY_LEVEL, envelope

    DecisionJournal(v1, clock=lambda: NOW).record_analysis(
        envelope(level=BUY_LEVEL), "analysis_request"
    )
    v1.stop()
    broken = (*V1_ONLY, (2, js.SCHEMA_V2 + "\nCREATE TABLE broken ("))
    store = started(tmp_path, migrations=broken, clock=lambda: NOW)
    assert store.status()["reason"] == "migration_failed" and not store.ready
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='trade'").fetchone() is None
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
    assert len(store.valid_pairs()) == 1  # the verified backup still exists
    retry = started(tmp_path, migrations=V1_TO_V2, clock=lambda: NOW)  # real v2 succeeds
    assert retry.ready and retry.status()["schema_version"] == 2
