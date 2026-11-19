"""C-12 evidence recording foundation (schema v6; nothing populated automatically).

An evidence record says *what was observed, where, when, about what, and how well it was
verified*. Records are immutable (database triggers); a mistake is fixed by a **correction**:
a new record that names the one it supersedes (each record can be superseded once) with a
reason. Records are associated with decisions and risk assessments through append-only links.

Statuses
--------
Stored: ``VERIFIED`` or ``UNVERIFIED`` (as established by the source). Derived when read:

- ``MISSING``     -- no evidence of a required kind/topic is linked to the subject;
- ``STALE``       -- the effective record's ``valid_until_utc`` has passed;
- ``CONFLICTING`` -- two or more effective records of the same kind/symbol/topic are linked
  to the subject with different content.

``assess`` is ``sufficient`` only when every required item is ``VERIFIED`` and current:
missing, stale, unverified or conflicting evidence is never approval.

Integrity: each record carries a keyed MAC (HMAC-SHA-256 with the verified journal key, over
its canonical content) and a content fingerprint (duplicate submissions return the existing
record). Every write is audited. Content is checked for identifier-like or sensitive data
(account, login, ticket, server, password, keys, long digit strings) and refused if found.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from johanna.journal_store import JournalError, new_ulid, tables_installed, utc_iso

TABLES = ("evidence_record", "evidence_link")
KINDS = (
    "market_data",
    "broker_terms",
    "broker_observation",
    "cost_evidence",
    "reconciliation",
    "readiness",
    "analysis",
    "risk_input",
    "other",
)
SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")
SUBJECTS = {"decision": "decision", "risk_assessment": "risk_assessment"}
_SENSITIVE_KEY = re.compile(
    r"account|login|ticket|server|password|passwd|secret|token|api[_ -]?key|hmac_key|"
    r"position_id|order_id|deal_id|identifier",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(r"(?<![\d.])\d{7,}(?![\d.])|password|secret|@")
MAX_CONTENT_CHARS = 8000


class EvidenceError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _check_content(value: Any, depth: int = 0) -> None:
    if depth > 6:
        raise EvidenceError("content_too_deep")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str) or _SENSITIVE_KEY.search(k):
                raise EvidenceError("sensitive_content")
            _check_content(v, depth + 1)
    elif isinstance(value, list | tuple):
        for v in value:
            _check_content(v, depth + 1)
    elif isinstance(value, str):
        if _SENSITIVE_VALUE.search(value):
            raise EvidenceError("sensitive_content")
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int | float):
        if value != value or value in (float("inf"), float("-inf")):
            raise EvidenceError("non_finite_number")  # never silently converted
        if isinstance(value, int) and abs(value) >= 10**6 and not isinstance(value, bool):
            raise EvidenceError("sensitive_content")  # identifier-like integers
    else:
        raise EvidenceError("unsupported_content")


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceError(f"invalid_{name}")
    return value


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


class EvidenceRegistry:
    name = "evidence"

    def __init__(self, store: Any, *, clock=None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES)
        except JournalError:
            return False

    # ------------------------------------------------------------------ canonical form
    @staticmethod
    def _canonical(fields: Mapping[str, Any]) -> bytes:
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    # ------------------------------------------------------------------ writes
    def record(
        self,
        *,
        kind: str,
        source: str,
        topic: str,
        status: str,
        observed_utc: datetime,
        content: Mapping[str, Any],
        symbol: str | None = None,
        valid_until_utc: datetime | None = None,
        actor: str = "johanna",
        supersedes: str | None = None,
        correction_reason: str | None = None,
    ) -> dict[str, Any]:
        """Record evidence; a duplicate submission returns the existing record."""
        if not self.installed():
            raise EvidenceError("not_installed")
        if kind not in KINDS:
            raise EvidenceError("invalid_kind")
        if status not in ("VERIFIED", "UNVERIFIED"):
            raise EvidenceError("invalid_status")
        if symbol is not None and symbol not in SYMBOLS:
            raise EvidenceError("invalid_symbol")
        if actor not in ("user", "johanna"):
            raise EvidenceError("invalid_actor")
        for text, name, limit in ((source, "source", 200), (topic, "topic", 120)):
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= limit:
                raise EvidenceError(f"invalid_{name}")
            if _SENSITIVE_VALUE.search(text):
                raise EvidenceError("sensitive_content")
        observed = _aware(observed_utc, "observed_utc")
        until = None if valid_until_utc is None else _aware(valid_until_utc, "valid_until_utc")
        if until is not None and until < observed:
            raise EvidenceError("invalid_validity")
        if not isinstance(content, Mapping):
            raise EvidenceError("invalid_content")
        _check_content(content)
        if (supersedes is None) != (correction_reason is None):
            raise EvidenceError("correction_needs_reason")
        canonical = {
            "kind": kind,
            "source": source.strip(),
            "symbol": symbol,
            "topic": topic.strip(),
            "status": status,
            "observed_utc": utc_iso(observed),
            "valid_until_utc": utc_iso(until) if until else None,
            "content": dict(content),
            "supersedes": supersedes,
        }
        try:
            body = self._canonical(canonical)
        except ValueError:
            raise EvidenceError("non_finite_number") from None
        content_text = json.dumps(dict(content), sort_keys=True, separators=(",", ":"))
        if len(content_text) > MAX_CONTENT_CHARS:
            raise EvidenceError("content_too_large")
        fingerprint = "sha256:" + hashlib.sha256(body).hexdigest()
        mac = self.store.keyed_digest("evidence.record", body)  # refuses without a verified key
        now = self._clock()
        try:
            with self.store.write() as conn:
                existing = conn.execute(
                    "SELECT id FROM evidence_record WHERE content_fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing is not None:
                    return {"id": existing["id"], "status": "duplicate"}
                if supersedes is not None:
                    target = conn.execute(
                        "SELECT kind, symbol, topic FROM evidence_record WHERE id=?",
                        (supersedes,),
                    ).fetchone()
                    if target is None:
                        raise EvidenceError("invalid_reference")
                    if (target["kind"], target["symbol"], target["topic"]) != (
                        kind,
                        symbol,
                        topic.strip(),
                    ):
                        raise EvidenceError("correction_changes_subject")
                rid = new_ulid(now)
                conn.execute(
                    "INSERT INTO evidence_record(id, kind, source, symbol, topic, status, "
                    "observed_utc, valid_until_utc, content, content_fingerprint, mac, "
                    "supersedes_id, correction_reason, actor, recorded_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        kind,
                        canonical["source"],
                        symbol,
                        canonical["topic"],
                        status,
                        canonical["observed_utc"],
                        canonical["valid_until_utc"],
                        content_text,
                        fingerprint,
                        mac,
                        supersedes,
                        correction_reason,
                        actor,
                        utc_iso(now),
                    ),
                )
                self.store.append_audit(
                    conn,
                    action="evidence.corrected" if supersedes else "evidence.recorded",
                    table="evidence_record",
                    record_id=rid,
                    new={
                        "kind": kind,
                        "status": status,
                        "fingerprint": fingerprint,
                        "supersedes": supersedes,
                    },
                    at=now,
                    actor=actor,
                )
                return {"id": rid, "status": "recorded"}
        except sqlite3.IntegrityError as exc:
            if "supersedes_id" in str(exc):
                raise EvidenceError("already_corrected") from None
            raise EvidenceError("integrity_error") from None

    def correct(self, evidence_id: str, *, reason: str, **fields: Any) -> dict[str, Any]:
        """A correction: a new record superseding ``evidence_id`` (which stays unchanged)."""
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise EvidenceError("correction_needs_reason")
        return self.record(supersedes=evidence_id, correction_reason=reason.strip(), **fields)

    def link(self, evidence_id: str, subject: str, subject_id: str, *, actor="johanna") -> str:
        if not self.installed():
            raise EvidenceError("not_installed")
        if subject not in SUBJECTS:
            raise EvidenceError("invalid_subject")
        if actor not in ("user", "johanna"):
            raise EvidenceError("invalid_actor")
        now = self._clock()
        column = "decision_id" if subject == "decision" else "risk_assessment_id"
        try:
            with self.store.write() as conn:
                if (
                    conn.execute(
                        "SELECT 1 FROM evidence_record WHERE id=?", (evidence_id,)
                    ).fetchone()
                    is None
                ):
                    raise EvidenceError("invalid_reference")
                table = "decision" if subject == "decision" else "risk_assessment"
                if (
                    conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (subject_id,)).fetchone()
                    is None
                ):
                    raise EvidenceError("invalid_reference")
                existing = conn.execute(
                    "SELECT id FROM evidence_link WHERE evidence_id=? AND subject=? AND "
                    "subject_key=?",
                    (evidence_id, subject, subject_id),
                ).fetchone()
                if existing is not None:
                    return existing["id"]  # idempotent
                lid = new_ulid(now)
                conn.execute(
                    f"INSERT INTO evidence_link(id, evidence_id, subject, subject_key, {column}, "
                    "actor, linked_utc) VALUES (?,?,?,?,?,?,?)",
                    (lid, evidence_id, subject, subject_id, subject_id, actor, utc_iso(now)),
                )
                self.store.append_audit(
                    conn,
                    action="evidence.linked",
                    table="evidence_link",
                    record_id=lid,
                    new={"evidence_id": evidence_id, "subject": subject},
                    at=now,
                    actor=actor,
                )
                return lid
        except sqlite3.IntegrityError:
            raise EvidenceError("invalid_reference") from None

    # ------------------------------------------------------------------ reads
    def verify(self, evidence_id: str) -> dict[str, Any]:
        """Recompute fingerprint and keyed MAC from the stored record."""
        with self.store.read() as conn:
            r = conn.execute("SELECT * FROM evidence_record WHERE id=?", (evidence_id,)).fetchone()
        if r is None:
            return {"valid": False, "reason": "not_found"}
        body = self._canonical(
            {
                "kind": r["kind"],
                "source": r["source"],
                "symbol": r["symbol"],
                "topic": r["topic"],
                "status": r["status"],
                "observed_utc": r["observed_utc"],
                "valid_until_utc": r["valid_until_utc"],
                "content": json.loads(r["content"]),
                "supersedes": r["supersedes_id"],
            }
        )
        fingerprint_ok = "sha256:" + hashlib.sha256(body).hexdigest() == r["content_fingerprint"]
        try:
            mac_ok = self.store.keyed_digest("evidence.record", body) == r["mac"]
        except JournalError:
            return {"valid": False, "reason": "hmac_unavailable"}
        return {"valid": fingerprint_ok and mac_ok, "fingerprint": fingerprint_ok, "mac": mac_ok}

    def _effective(self, conn, evidence_id: str) -> Any:
        """Follow corrections to the current record (the newest in the chain)."""
        row = conn.execute("SELECT * FROM evidence_record WHERE id=?", (evidence_id,)).fetchone()
        seen = set()
        while row is not None and row["id"] not in seen:
            seen.add(row["id"])
            nxt = conn.execute(
                "SELECT * FROM evidence_record WHERE supersedes_id=?", (row["id"],)
            ).fetchone()
            if nxt is None:
                return row
            row = nxt
        return row

    def assess(
        self,
        subject: str,
        subject_id: str,
        required: Iterable[tuple[str, str | None, str]],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Per required (kind, symbol, topic): VERIFIED / UNVERIFIED / STALE / CONFLICTING /
        MISSING; ``sufficient`` only if every item is VERIFIED."""
        if subject not in SUBJECTS:
            raise EvidenceError("invalid_subject")
        now = now or self._clock()
        required = list(required)
        if not self.installed():
            return {
                "status": "not_installed",
                "sufficient": False,
                "items": [
                    {"kind": k, "symbol": s, "topic": t, "status": "MISSING"}
                    for k, s, t in required
                ],
            }
        with self.store.read() as conn:
            linked = conn.execute(
                "SELECT evidence_id FROM evidence_link WHERE subject=? AND subject_key=? "
                "ORDER BY rowid",
                (subject, subject_id),
            ).fetchall()
            effective: dict[str, Any] = {}
            for row in linked:
                record = self._effective(conn, row["evidence_id"])
                if record is not None:
                    effective[record["id"]] = record
        items = []
        for kind, symbol, topic in required:
            matches = [
                r
                for r in effective.values()
                if (r["kind"], r["symbol"], r["topic"]) == (kind, symbol, topic)
            ]
            item = {
                "kind": kind,
                "symbol": symbol,
                "topic": topic,
                "evidence": [r["id"] for r in matches],
            }
            if not matches:
                item["status"] = "MISSING"
            elif (
                len({r["content_fingerprint"] for r in matches}) > 1
                and len({r["content"] for r in matches}) > 1
            ):
                item["status"] = "CONFLICTING"
            else:
                r = matches[-1]
                until = _parse(r["valid_until_utc"])
                if until is not None and until < now:
                    item["status"] = "STALE"
                else:
                    item["status"] = r["status"]
            items.append(item)
        return {
            "status": "installed",
            "sufficient": bool(items) and all(i["status"] == "VERIFIED" for i in items),
            "items": items,
        }

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "installed": self.installed(), "automatic_recording": False}
