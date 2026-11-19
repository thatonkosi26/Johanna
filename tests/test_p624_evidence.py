"""P6.2b.4 C-12: evidence records -- immutable, keyed, correction-linked, never approval when
missing, stale, unverified or conflicting. Temporary v6 journals only."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import envelope, propose, synthetic_terms
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.decision_journal import DecisionJournal
from johanna.evidence import EvidenceError, EvidenceRegistry

V6 = js.MIGRATIONS  # registered in P6.2b.5
T0 = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def setup(tmp_path, migrations=V6):
    clock = Clock()
    store = started(tmp_path, migrations=migrations, clock=clock)
    journal = DecisionJournal(store, clock=clock)
    env = envelope(evaluated_at=T0)
    decision = journal.record_analysis(env, "analysis_request")
    risk = journal.record_risk(propose(env=envelope(), costs=synthetic_terms()), decision)
    return EvidenceRegistry(store, clock=clock), store, clock, decision, risk


def spread(**change):
    base = {
        "kind": "market_data",
        "source": "MT5 symbol_info (read-only)",
        "topic": "spread_points",
        "symbol": "EURUSD",
        "status": "VERIFIED",
        "observed_utc": T0,
        "valid_until_utc": T0 + timedelta(hours=1),
        "content": {"spread_points": 12, "digits": 5},
    }
    return {**base, **change}


REQUIRED = [("market_data", "EURUSD", "spread_points")]


# ====================================================================== creation + links
def test_evidence_is_recorded_linked_and_verified(tmp_path) -> None:
    reg, store, _, decision, risk = setup(tmp_path)
    rec = reg.record(**spread())
    assert rec["status"] == "recorded"
    reg.link(rec["id"], "decision", decision)
    reg.link(rec["id"], "risk_assessment", risk)
    for subject, sid in (("decision", decision), ("risk_assessment", risk)):
        result = reg.assess(subject, sid, REQUIRED)
        assert result["sufficient"] is True and result["items"][0]["status"] == "VERIFIED"
    assert reg.verify(rec["id"]) == {"valid": True, "fingerprint": True, "mac": True}
    with closing(raw(tmp_path)) as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log")]
    assert actions.count("evidence.recorded") == 1 and actions.count("evidence.linked") == 2
    assert store.verify_audit_chain()["valid"]


def test_missing_evidence_is_never_sufficient(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path)
    result = reg.assess("decision", decision, REQUIRED)
    assert result["sufficient"] is False and result["items"][0]["status"] == "MISSING"
    assert reg.assess("decision", decision, [])["sufficient"] is False  # nothing is not approval


def test_unverified_and_stale_evidence_are_not_sufficient(tmp_path) -> None:
    reg, _, clock, decision, _ = setup(tmp_path)
    unverified = reg.record(**spread(status="UNVERIFIED", content={"spread_points": 13}))
    reg.link(unverified["id"], "decision", decision)
    assert reg.assess("decision", decision, REQUIRED)["items"][0]["status"] == "UNVERIFIED"
    reg2, _, clock2, d2, _ = setup(tmp_path / "b")
    rec = reg2.record(**spread())
    reg2.link(rec["id"], "decision", d2)
    clock2.now = T0 + timedelta(hours=2)
    result = reg2.assess("decision", d2, REQUIRED)
    assert result["items"][0]["status"] == "STALE" and result["sufficient"] is False


def test_conflicting_evidence_is_detected(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path)
    a = reg.record(**spread())
    b = reg.record(**spread(source="broker statement", content={"spread_points": 30}))
    reg.link(a["id"], "decision", decision)
    reg.link(b["id"], "decision", decision)
    result = reg.assess("decision", decision, REQUIRED)
    assert result["items"][0]["status"] == "CONFLICTING" and result["sufficient"] is False


def test_invalid_references_are_refused(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path)
    rec = reg.record(**spread())
    for args in (
        ("01JNOSUCHEVIDENCE000000000", "decision", decision),
        (rec["id"], "decision", "01JNOSUCHDECISION000000000"),
        (rec["id"], "risk_assessment", decision),  # a decision id is not a risk assessment
    ):
        with pytest.raises(EvidenceError) as err:
            reg.link(*args)
        assert err.value.code == "invalid_reference"
    with pytest.raises(EvidenceError) as err:
        reg.link(rec["id"], "trade", decision)
    assert err.value.code == "invalid_subject"


def test_duplicate_submissions_and_links_are_idempotent(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path)
    first = reg.record(**spread())
    again = reg.record(**spread())
    assert again == {"id": first["id"], "status": "duplicate"}
    assert reg.link(first["id"], "decision", decision) == reg.link(
        first["id"], "decision", decision
    )
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM evidence_record").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM evidence_link").fetchone()[0] == 1


# ====================================================================== corrections + immutability
def test_corrections_are_linked_and_the_original_is_kept(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path)
    wrong = reg.record(**spread(content={"spread_points": 120}))
    reg.link(wrong["id"], "decision", decision)
    fixed = reg.correct(wrong["id"], reason="digits misread", **spread())
    assert fixed["status"] == "recorded" and fixed["id"] != wrong["id"]
    result = reg.assess("decision", decision, REQUIRED)  # follows the correction
    assert result["items"][0]["evidence"] == [fixed["id"]] and result["sufficient"]
    with pytest.raises(EvidenceError) as err:
        reg.correct(wrong["id"], reason="again", **spread(content={"spread_points": 11}))
    assert err.value.code == "already_corrected"
    with pytest.raises(EvidenceError) as err:
        reg.correct(fixed["id"], reason="x", **spread(topic="other_topic"))
    assert err.value.code == "correction_changes_subject"
    with pytest.raises(EvidenceError):
        reg.record(**spread(), supersedes=wrong["id"])  # a correction needs a reason
    with closing(raw(tmp_path)) as conn:
        original = conn.execute(
            "SELECT content FROM evidence_record WHERE id=?", (wrong["id"],)
        ).fetchone()[0]
        assert '"spread_points":120' in original  # never overwritten
        for sql in (
            "UPDATE evidence_record SET content='{}'",
            "DELETE FROM evidence_record",
            "UPDATE evidence_link SET subject='decision'",
            "DELETE FROM evidence_link",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_tampering_is_detected_by_the_keyed_mac(tmp_path) -> None:
    reg, _, _, _, _ = setup(tmp_path)
    rec = reg.record(**spread())
    with closing(raw(tmp_path)) as conn:
        conn.execute("DROP TRIGGER evidence_record_no_update")
        conn.execute(
            'UPDATE evidence_record SET content=\'{"digits":5,"spread_points":1}\' WHERE id=?',
            (rec["id"],),
        )
    assert reg.verify(rec["id"])["valid"] is False


def test_hmac_is_required_and_audit_chain_stays_valid(tmp_path) -> None:
    reg, store, _, _, _ = setup(tmp_path)
    reg.record(**spread())
    assert store.verify_audit_chain()["valid"]
    store._key_status = "missing"  # simulate an unverified key
    with pytest.raises(js.JournalError) as err:
        reg.record(**spread(content={"spread_points": 14}))
    assert err.value.code == "hmac_unavailable"


# ====================================================================== content safety
@pytest.mark.parametrize(
    "content",
    [
        {"account": "x"},
        {"login_hint": "x"},
        {"ticket": 1},
        {"note": "position 12345678"},
        {"server": "Exness-MT5Trial9"},
        {"nested": {"password": "x"}},
        {"value": float("nan")},
        {"big_id": 555001234},
        {"object": object()},
    ],
)
def test_sensitive_or_invalid_content_is_refused(tmp_path, content) -> None:
    reg, _, _, _, _ = setup(tmp_path)
    with pytest.raises(EvidenceError):
        reg.record(**spread(content=content))


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "rumour"},
        {"status": "APPROVED"},
        {"symbol": "XAUUSD"},
        {"observed_utc": datetime(2026, 11, 19, 9, 0)},  # naive
        {"valid_until_utc": T0 - timedelta(seconds=1)},
        {"source": ""},
        {"content": ["not", "an", "object"]},
    ],
)
def test_invalid_records_are_refused(tmp_path, change) -> None:
    reg, _, _, _, _ = setup(tmp_path)
    with pytest.raises(EvidenceError):
        reg.record(**spread(**change))


def test_v5_journals_report_not_installed(tmp_path) -> None:
    reg, _, _, decision, _ = setup(tmp_path, migrations=js.MIGRATIONS[:5])
    assert reg.installed() is False
    with pytest.raises(EvidenceError) as err:
        reg.record(**spread())
    assert err.value.code == "not_installed"
    result = reg.assess("decision", decision, REQUIRED)
    assert result["status"] == "not_installed" and result["sufficient"] is False
