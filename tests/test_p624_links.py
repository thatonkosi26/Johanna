"""P6.2b.4 M-9: user-confirmed trade links and logbook/broker discrepancy detection.

Pure reconciliation cases use the Phase 1 helpers; service cases use temporary v6 journals and
the in-process fake MT5 (read-only calls only). A candidate is never presented as a link."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_decision_journal import _app, _call
from test_journal_store import raw
from test_p6_corrective import _POS, NOW, _deal, _deal_row, _monitor, _obs, _states, _trade

from johanna import journal_store as js
from johanna.api import BackendAPI
from johanna.reconciliation import discrepancies, reconcile
from johanna.trade_links import LinkError, TradeLinkService
from johanna.trade_logbook import TradeLogbook

V6 = js.MIGRATIONS  # registered in P6.2b.5
P1, P2 = "a" * 64, "b" * 64


def _position(ref, *, volume=0.1, symbol="EURUSDc", side=0):
    return {"position_ref": ref, "symbol": symbol, "type": side, "volume": volume}


def _exit(volume=0.1):
    return {
        "kind": "exit",
        "time_utc": NOW - timedelta(hours=1),
        "volume": volume,
        "broker_deal_ref": None,
        "status": "RECORDED",
    }


def _codes(result) -> list[str]:
    return result["detail"].get("discrepancies", [])


def _linked(trade_id, ref):
    return {"confirmed": {trade_id: {"position_ref": ref, "link_id": "L1"}}, "rejected": set()}


# ====================================================================== candidates (pure)
def test_one_unambiguous_candidate_is_never_presented_as_a_link() -> None:
    r = _states(reconcile([_trade("T1")], _obs([_deal("D1", P1)]), NOW))["T1"]
    assert r["state"] == "CANDIDATE_MATCH" and r["detail"]["link_status"] == "CANDIDATE"
    assert r["position_ref"] is None  # not tied to the position (persisted as unlinked)
    assert r["detail"]["candidate_position_ref"] == P1 and _codes(r) == []


def test_multiple_possible_candidates_are_ambiguous_with_a_discrepancy() -> None:
    deals = [_deal("D1", P1), _deal("D2", P2, time=NOW - timedelta(days=1, seconds=30))]
    r = _states(reconcile([_trade("T1")], _obs(deals), NOW))["T1"]
    assert r["state"] == "AMBIGUOUS" and r["detail"]["link_status"] == "AMBIGUOUS"
    assert _codes(r) == ["MULTIPLE_POSSIBLE_MATCHES"]


def test_two_trades_competing_for_one_deal() -> None:
    results = _states(reconcile([_trade("T1"), _trade("T2")], _obs([_deal("D1", P1)]), NOW))
    assert {r["state"] for r in results.values()} == {"AMBIGUOUS"}
    assert all(_codes(r) == ["MULTIPLE_POSSIBLE_MATCHES"] for r in results.values())


def test_rejected_candidate_is_never_offered_again() -> None:
    links = {"confirmed": {}, "rejected": {("T1", P1)}}
    r = _states(reconcile([_trade("T1")], _obs([_deal("D1", P1)]), NOW, links=links))["T1"]
    assert r["state"] == "UNMATCHED" and r["detail"]["link_status"] == "REJECTED"
    assert r["detail"]["rejected_candidates"] == 1


def test_a_linked_position_is_claimed_and_not_a_candidate_for_other_trades() -> None:
    trades = [_trade("T1"), _trade("T2")]
    results = _states(
        reconcile(trades, _obs([_deal("D1", P1)], [_position(P1)]), NOW, links=_linked("T1", P1))
    )
    assert results["T1"]["state"] == "CONFIRMED_OPEN"
    assert results["T1"]["detail"]["link_status"] == "USER_CONFIRMED"
    assert results["T2"]["state"] == "UNMATCHED"  # D1 belongs to the linked position


# ====================================================================== discrepancies (pure)
def test_user_confirmed_open_position_without_discrepancy() -> None:
    obs = _obs([_deal("D1", P1)], [_position(P1)])
    r = _states(reconcile([_trade("T1")], obs, NOW, links=_linked("T1", P1)))["T1"]
    assert (r["state"], r["basis"], r["position_ref"]) == ("CONFIRMED_OPEN", "broker_confirmed", P1)
    assert _codes(r) == []


def test_logbook_closed_while_broker_position_is_open() -> None:
    trade = _trade("T1")
    trade["fills"].append(_exit())
    obs = _obs([_deal("D1", P1)], [_position(P1)])
    r = _states(reconcile([trade], obs, NOW, links=_linked("T1", P1)))["T1"]
    assert _codes(r) == ["LOGBOOK_CLOSED_BROKER_OPEN"]


def test_broker_closed_while_logbook_is_open() -> None:
    out = {**_deal("D2", P1, time=NOW - timedelta(hours=2)), "entry": 1, "type": 1}
    r = _states(
        reconcile([_trade("T1")], _obs([_deal("D1", P1), out]), NOW, links=_linked("T1", P1))
    )
    assert r["T1"]["state"] == "CONFIRMED_CLOSED"
    assert _codes(r["T1"]) == ["BROKER_CLOSED_LOGBOOK_OPEN"]


def test_symbol_direction_and_volume_mismatches() -> None:
    obs = _obs([], [_position(P1, symbol="GBPUSDc", side=1, volume=0.2)])
    r = _states(reconcile([_trade("T1")], obs, NOW, links=_linked("T1", P1)))["T1"]
    assert _codes(r) == ["SYMBOL_MISMATCH", "DIRECTION_MISMATCH", "VOLUME_MISMATCH"]
    flat = discrepancies([r])
    assert {f["code"] for f in flat} == set(_codes(r)) and flat[0]["trade_id"] == "T1"


def test_identifier_conflict_between_recorded_reference_and_link() -> None:
    trade = _trade("T1", ref="D1")  # recorded reference to a deal of position P1
    obs = _obs([_deal("D1", P1)], [_position(P2)])
    r = _states(reconcile([trade], obs, NOW, links=_linked("T1", P2)))["T1"]
    assert "IDENTIFIER_CONFLICT" in _codes(r)


def test_linked_position_absent_from_the_observation_is_never_inferred_closed() -> None:
    r = _states(reconcile([_trade("T1")], _obs([]), NOW, links=_linked("T1", P1)))["T1"]
    assert r["state"] == "PENDING" and _codes(r) == ["LINKED_POSITION_NOT_OBSERVED"]


def test_correction_linked_fills_use_the_corrected_volume() -> None:
    trade = _trade("T1", volume=0.2)
    trade["fills"][0]["status"] = "VOIDED"  # the mistaken entry, kept for history
    trade["fills"].append({**_trade("x")["fills"][0], "volume": 0.1})  # its correction
    obs = _obs([_deal("D1", P1)], [_position(P1, volume=0.1)])
    r = _states(reconcile([trade], obs, NOW, links=_linked("T1", P1)))["T1"]
    assert r["state"] == "CONFIRMED_OPEN" and _codes(r) == []
    uncorrected = _trade("T1", volume=0.2)
    r2 = _states(reconcile([uncorrected], obs, NOW, links=_linked("T1", P1)))["T1"]
    assert _codes(r2) == ["VOLUME_MISMATCH"]


def test_stale_and_missing_observations_are_unavailable_evidence() -> None:
    stale = {**_obs([_deal("D1", P1)]), "observed_utc": NOW - timedelta(minutes=10)}
    r = _states(reconcile([_trade("T1")], stale, NOW, links=_linked("T1", P1)))["T1"]
    assert (r["state"], r["detail"]["reason_code"], r["detail"]["link_status"]) == (
        "UNAVAILABLE",
        "observation_stale",
        "UNAVAILABLE",
    )
    assert _codes(r) == []  # no discrepancy is inferred from unavailable evidence
    missing = _states(reconcile([_trade("T1")], None, NOW))["T1"]
    assert missing["detail"]["reason_code"] == "no_observation"


def test_repeated_reconciliation_is_deterministic() -> None:
    args = ([_trade("T1"), _trade("T2")], _obs([_deal("D1", P1)], [_position(P2)]), NOW)
    assert reconcile(*args) == reconcile(*args)


# ====================================================================== service + journal
def _stored(tmp_path, *, position=True, trades=1):
    positions = [{**_POS, "identifier": 555001, "ticket": 555001}] if position else []
    service, store, fake = _monitor(tmp_path, positions, [_deal_row()], migrations=V6)
    book = TradeLogbook(store, None)
    ids = []
    for _ in range(trades):
        _, created = book.create_trade(
            {
                "request_id": str(uuid.uuid4()),
                "symbol": "EURUSDc",
                "direction": "BUY",
                "entry": {
                    "time_utc": (datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
                    "price": 1.1,
                    "volume": 0.1,
                },
            }
        )
        ids.append(created["trade_id"])
    service.observe()
    obs = service._observation
    return service, store, ids, obs["id"], obs["deals"][0]["position_ref"]


def test_candidate_needs_an_explicit_user_confirmation(tmp_path) -> None:
    service, store, (tid,), obs_id, ref = _stored(tmp_path)
    first = service.reconcile()
    assert _states(first["results"])[tid]["state"] == "CANDIDATE_MATCH"
    links = TradeLinkService(store)
    assert links.links()["links"] == []  # nothing linked automatically
    link_id = links.confirm(tid, ref, obs_id, "matches my MT5 ticket screen")
    service.observe()
    after = _states(service.reconcile()["results"])[tid]
    assert after["state"] == "CONFIRMED_OPEN" and after["detail"]["link_status"] == "USER_CONFIRMED"
    assert after["detail"]["link_id"] == link_id
    with closing(raw(tmp_path)) as conn:
        actor = conn.execute(
            "SELECT actor FROM audit_log WHERE action='trade_link.confirmed'"
        ).fetchone()[0]
        assert actor == "user"


def test_rejected_candidate_through_the_service(tmp_path) -> None:
    service, store, (tid,), obs_id, ref = _stored(tmp_path)
    TradeLinkService(store).reject(tid, ref, obs_id, "not my trade")
    service.observe()
    r = _states(service.reconcile()["results"])[tid]
    assert r["detail"]["link_status"] == "REJECTED"


def test_links_are_exclusive_and_revocable_once(tmp_path) -> None:
    service, store, (t1, t2), obs_id, ref = _stored(tmp_path, trades=2)
    links = TradeLinkService(store)
    link = links.confirm(t1, ref, obs_id, "mine")
    with pytest.raises(LinkError) as err:
        links.confirm(t2, ref, obs_id, "also mine?")  # one position, one trade
    assert err.value.code == "already_linked"
    revoked = links.revoke(link, "wrong trade")
    with pytest.raises(LinkError) as err:
        links.revoke(link, "again")
    assert err.value.code == "invalid_revocation"
    assert links.confirm(t2, ref, obs_id, "the right trade")
    listed = {r["id"]: r for r in links.links()["links"]}
    assert listed[link]["revoked"] and not listed[link]["active"] and listed[revoked]
    with closing(raw(tmp_path)) as conn:
        for sql in ("UPDATE trade_broker_link SET reason='x'", "DELETE FROM trade_broker_link"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):  # actor is always the user
            conn.execute(
                "INSERT INTO trade_broker_link VALUES ('X', ?, ?, ?, 'CONFIRMED', NULL, 'r', "
                "'johanna', 't')",
                (t1, "c" * 64, obs_id),
            )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"trade_id": "01JNOSUCHTRADE000000000000"}, "trade_not_found"),
        ({"observation_id": "01JNOSUCHOBSERVATION000000"}, "observation_not_found"),
        ({"position_ref": "d" * 64}, "position_not_in_observation"),
        ({"position_ref": "555001"}, "invalid_position_ref"),
        ({"reason": " "}, "invalid_reason"),
    ],
)
def test_link_decisions_are_validated(tmp_path, change, code) -> None:
    _, store, (tid,), obs_id, ref = _stored(tmp_path)
    args = {"trade_id": tid, "position_ref": ref, "observation_id": obs_id, "reason": "ok"}
    with pytest.raises(LinkError) as err:
        TradeLinkService(store).confirm(**{**args, **change})
    assert err.value.code == code


def test_symbol_mismatch_confirmation_is_refused(tmp_path) -> None:
    service, store, _, obs_id, ref = _stored(tmp_path)
    _, created = TradeLogbook(store, None).create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "GBPUSDc",
            "direction": "BUY",
            "entry": {"time_utc": datetime.now(UTC).isoformat(), "price": 1.3, "volume": 0.1},
        }
    )
    with pytest.raises(LinkError) as err:
        TradeLinkService(store).confirm(created["trade_id"], ref, obs_id, "x")
    assert err.value.code == "symbol_mismatch"


def test_missing_broker_identifiers_are_unavailable_evidence(tmp_path) -> None:
    service, store, fake = _monitor(tmp_path, [], [_deal_row(ticket=None)], migrations=V6)
    TradeLogbook(store, None).create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {"time_utc": datetime.now(UTC).isoformat(), "price": 1.1, "volume": 0.1},
        }
    )
    assert service.observe()["status"] == "UNAVAILABLE"
    (r,) = [x for x in service.reconcile()["results"] if x["subject"] == "trade"]
    assert r["state"] == "UNAVAILABLE" and r["detail"]["link_status"] == "UNAVAILABLE"
    assert r["detail"]["reason"] == "broker_identifier_missing"
    with pytest.raises(LinkError) as err:
        TradeLinkService(store).confirm(r["trade_id"], "e" * 64, service._observation["id"], "x")
    assert err.value.code == "observation_not_live"


def test_duplicate_observations_and_repeated_reconciliation(tmp_path) -> None:
    service, store, (tid,), _, _ = _stored(tmp_path)
    first = service.reconcile()
    assert service.reconcile()["persistence"]["status"] == "already_reconciled"
    service.observe()  # identical broker state: a second, separate observation
    second = service.reconcile()
    assert second["persistence"]["status"] == "persisted"
    assert _states(first["results"])[tid]["state"] == _states(second["results"])[tid]["state"]
    with closing(raw(tmp_path)) as conn:
        fps = [r[0] for r in conn.execute("SELECT payload_fingerprint FROM broker_observation")]
        assert len(fps) == 2 and len(set(fps)) == 1
        # one trade result per observation (the candidate's position is not "unrecorded")
        assert conn.execute("SELECT count(*) FROM reconciliation_result").fetchone()[0] == 2


def test_discrepancies_are_persisted_append_only_and_readable(tmp_path) -> None:
    service, store, (tid,), obs_id, ref = _stored(tmp_path)
    TradeLinkService(store).confirm(tid, ref, obs_id, "mine")
    TradeLogbook(store, None).add_fill(
        tid,
        {
            "request_id": str(uuid.uuid4()),
            "kind": "exit",
            "time_utc": datetime.now(UTC).isoformat(),
            "price": 1.11,
            "volume": 0.1,
            "realised_pnl_user": None,
        },
    )
    service.observe()
    outcome = service.reconcile()
    assert {d["code"] for d in outcome["discrepancies"]} == {"LOGBOOK_CLOSED_BROKER_OPEN"}
    latest = service.latest()
    assert [d["code"] for d in latest["discrepancies"]] == ["LOGBOOK_CLOSED_BROKER_OPEN"]
    with closing(raw(tmp_path)) as conn:
        for sql in (
            "UPDATE reconciliation_discrepancy SET code='SYMBOL_MISMATCH'",
            "DELETE FROM reconciliation_discrepancy",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_v5_has_no_link_decisions_and_nothing_is_linked(tmp_path) -> None:
    service, store, fake = _monitor(tmp_path, [], [_deal_row()], migrations=js.MIGRATIONS[:5])
    assert service.links_installed() is False and service._links() == {
        "confirmed": {},
        "rejected": set(),
    }
    with pytest.raises(LinkError) as err:
        TradeLinkService(store).confirm("T", "a" * 64, "O", "r")
    assert err.value.code == "not_installed"


def test_confirmation_endpoint_is_inactive(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        api = BackendAPI(app)
        status, body, _ = _call(api, "/api/reconciliation/links")
        assert status == "200 OK" and body["status"] == "installed" and body["links"] == []
        assert body["service"]["confirmation_endpoint"].startswith("inactive")
        response: dict = {}

        def start(status, headers):
            response["status"] = status

        from io import BytesIO  # noqa: PLC0415

        payload = b'{"trade_id": "x"}'
        out = api(
            {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/reconciliation/links",
                "HTTP_HOST": "127.0.0.1:8765",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(payload)),
                "wsgi.input": BytesIO(payload),
            },
            start,
        )
        assert response["status"].startswith("403")
        assert b"not_activated" in b"".join(out)
    finally:
        app.stop()
