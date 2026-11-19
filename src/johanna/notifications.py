"""P6.4 local notification infrastructure (no external delivery).

**Nothing is sent anywhere.** Only local adapters exist: ``InMemoryAdapter`` (tests) and
``DashboardInboxAdapter`` (the notification simply becomes *available* for the dashboard to
read from the journal). There is no WhatsApp, email, push or network adapter, and nothing in
Johanna calls ``notify`` automatically yet (wiring it to the scheduler or monitoring needs a
separate approval).

- Lifecycle per notification: ``pending -> available -> presented -> acknowledged``, each once
  and in order (database trigger); ``acknowledged`` only by the user. Reads never change state.
- Idempotent: one notification per ``dedup_key`` (unique), so retries and restarts cannot
  duplicate it; delivery attempts are numbered per adapter (unique), bounded by
  ``MAX_ATTEMPTS`` and recorded with a sanitised error code.
- M-6 (schema v6): an attempt is *claimed* before the adapter is called (unique per
  notification/adapter/attempt), so two workers can never deliver the same attempt; a
  delivery row needs its claim and a notification is delivered at most once (database
  index). A claim whose owner never reports back becomes a visible ``claim_expired``
  failure after ``CLAIM_TIMEOUT``. Without v6 nothing is delivered (fail closed).
- L-5: creation, every delivery outcome, exhaustion, presentation and acknowledgement are
  audited.
- Content comes only from fixed templates over structured facts. It carries no account,
  broker or ticket details (identifier-like values and secrets are rejected), and a trade
  candidate is always worded as *not an approved trade and not an order*.
- ``notify`` and ``deliver_pending`` never raise: a notification failure cannot affect market
  analysis, risk calculations or the journal.
- Storage is the staged schema v5 (``notification``, ``notification_state``,
  ``notification_delivery``); on the production v4 journal ``installed`` is False and nothing
  is stored.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from johanna.journal_store import JournalError, new_ulid, tables_installed, utc_iso

LOGGER = logging.getLogger("johanna.notifications")
TABLES = ("notification", "notification_state", "notification_delivery")
CATEGORIES = ("information", "trade_candidate", "risk_rejection", "broker_event")
LIFECYCLE = ("pending", "available", "presented", "acknowledged")
MAX_ATTEMPTS = 5
# M-6: an unresolved claim older than this is recorded as a failed attempt (owner crashed).
CLAIM_TIMEOUT = timedelta(minutes=5)
EXPIRED = "claim_expired"
OUTCOMES = (
    "delivered",
    "failed",
    "exhausted",
    "in_progress",
    "already_delivered",
    "conflict",
)
SYMBOLS = ("EURUSDc", "GBPUSDc", "USDJPYc")
TIMEFRAMES = ("H1", "H4", "D1")
_FORBIDDEN = re.compile(
    r"\d{6,}|@|password|passwd|secret|token|api[_ -]?key|hmac|login|ticket|account number",
    re.IGNORECASE,
)
_MISLEADING = re.compile(
    r"approved trade|buy now|sell now|execute|place (an )?order", re.IGNORECASE
)


class NotificationError(ValueError):
    """A refused explicit state change (``code``: not_installed, not_found,
    invalid_transition). Never a raw database error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeliveryError(Exception):
    """A local adapter could not deliver; ``code`` is safe to store."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DeliveryAdapter(Protocol):
    name: str

    def deliver(self, message: Mapping[str, Any]) -> None: ...


class InMemoryAdapter:
    """Test/local adapter: keeps delivered messages in memory."""

    def __init__(self, name: str = "memory", *, fail: int = 0) -> None:
        self.name = name
        self.delivered: list[dict[str, Any]] = []
        self.fail = fail  # fail this many times first

    def deliver(self, message: Mapping[str, Any]) -> None:
        if self.fail > 0:
            self.fail -= 1
            raise DeliveryError("adapter_unavailable")
        self.delivered.append(dict(message))


class DashboardInboxAdapter:
    """Local only: makes the notification available to the dashboard (which reads the
    journal). It sends nothing anywhere."""

    name = "dashboard_inbox"

    def deliver(self, message: Mapping[str, Any]) -> None:
        return None


# ====================================================================== content (pure)
def _safe(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("notification text is required")
    if _FORBIDDEN.search(text):
        raise ValueError("notification text contains identifier-like or sensitive content")
    return text.strip()


def _choice(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {allowed}")
    return value


def compose(category: str, facts: Mapping[str, Any]) -> tuple[str, str]:
    """Title and body from fixed templates. Raises ValueError for unsafe or invalid facts."""
    _choice(category, CATEGORIES, "category")
    if category == "trade_candidate":
        symbol = _choice(facts.get("symbol"), SYMBOLS, "symbol")
        tf = _choice(facts.get("timeframe"), TIMEFRAMES, "timeframe")
        direction = _choice(facts.get("direction"), ("BUY", "SELL"), "direction")
        eligibility = _choice(
            facts.get("eligibility", "UNVERIFIED"),
            ("RISK_APPROVED", "REJECTED", "UNVERIFIED"),
            "eligibility",
        )
        title = f"Market candidate: {symbol} {tf} {direction}"
        body = (
            f"A scheduled shadow evaluation found a T6 trade candidate on {symbol} {tf} "
            f"({direction}). This is not an approved trade and not an order; Johanna places no "
            f"orders. Hypothetical risk eligibility: {eligibility}."
        )
    elif category == "risk_rejection":
        symbol = _choice(facts.get("symbol"), SYMBOLS, "symbol")
        tf = _choice(facts.get("timeframe"), TIMEFRAMES, "timeframe")
        reason = _safe(str(facts.get("reason", "")))[:200]
        title = f"Candidate not risk-approved: {symbol} {tf}"
        body = f"A candidate on {symbol} {tf} did not pass the risk checks: {reason}. No order."
    elif category == "broker_event":
        if facts.get("basis") != "broker_confirmed":
            raise ValueError("broker events are only for broker-confirmed facts")
        symbol = _choice(facts.get("symbol"), SYMBOLS, "symbol")
        event = _choice(
            facts.get("event"),
            ("position_confirmed_open", "position_confirmed_closed", "unrecorded_position"),
            "event",
        )
        title = f"Broker-confirmed: {event.replace('_', ' ')} ({symbol})"
        body = (
            f"Broker history confirms: {event.replace('_', ' ')} on {symbol}. Read-only "
            "observation; Johanna did not act on the position."
        )
    else:
        title = _safe(str(facts.get("title", "")))[:120]
        body = _safe(str(facts.get("text", "")))[:1000]
    for text in (title, body):
        if _FORBIDDEN.search(text):
            raise ValueError("notification text contains identifier-like or sensitive content")
        if category != "trade_candidate" and _MISLEADING.search(text):
            raise ValueError("notification text could be read as a trading instruction")
    return title, body


# ====================================================================== centre
class NotificationCenter:
    name = "notifications"

    def __init__(self, store: Any, adapters: Sequence[Any] = (), *, clock=None) -> None:
        self.store = store
        self.adapters = tuple(adapters)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.failures = 0
        self.last_failure: str | None = None
        self.worker = f"pid{os.getpid()}-{id(self):x}"[:80]  # claim owner (no identifiers)
        for adapter in self.adapters:
            if getattr(adapter, "name", None) not in ("memory", "dashboard_inbox") and not str(
                getattr(adapter, "name", "")
            ).startswith("test_"):
                raise ValueError("only local adapters are allowed in this phase")

    def installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES)
        except JournalError:
            return False

    def _failed(self, code: str) -> None:
        self.failures += 1
        self.last_failure = code
        LOGGER.warning("notification failure (%s)", code)

    def notify(
        self,
        category: str,
        dedup_key: str,
        facts: Mapping[str, Any],
        *,
        subject_ref: str | None = None,
    ) -> str | None:
        """Create (once per ``dedup_key``) a pending notification. Never raises."""
        try:
            title, body = compose(category, facts)
            if not isinstance(dedup_key, str) or not 1 <= len(dedup_key) <= 200:
                raise ValueError("dedup_key must be 1-200 characters")
            if not self.installed():
                self._failed("not_installed")
                return None
            now = self._clock()
            with self.store.write() as conn:
                existing = conn.execute(
                    "SELECT id FROM notification WHERE dedup_key=?", (dedup_key,)
                ).fetchone()
                if existing is not None:
                    return existing["id"]  # idempotent: retries and restarts never duplicate
                nid = new_ulid(now)
                conn.execute(
                    "INSERT INTO notification(id, dedup_key, category, title, body, subject_ref, "
                    "created_utc) VALUES (?,?,?,?,?,?,?)",
                    (nid, dedup_key, category, title, body, subject_ref, utc_iso(now)),
                )
                self._state(conn, nid, "pending", "johanna", now)
                self.store.append_audit(
                    conn,
                    action="notification.created",
                    table="notification",
                    record_id=nid,
                    new={"category": category},
                    at=now,
                )
            return nid
        except (ValueError, JournalError) as exc:
            self._failed(getattr(exc, "code", None) or type(exc).__name__)
            return None
        except Exception as exc:  # isolated: never affects the caller
            self._failed(type(exc).__name__)
            return None

    @staticmethod
    def _state(conn, nid: str, state: str, actor: str, now: datetime) -> None:
        conn.execute(
            "INSERT INTO notification_state(id, notification_id, state, actor, changed_utc) "
            "VALUES (?,?,?,?,?)",
            (new_ulid(now), nid, state, actor, utc_iso(now)),
        )

    def claims_installed(self) -> bool:
        """M-6: claim-based delivery needs the staged v6 ``notification_claim`` table."""
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES, "notification_claim")
        except JournalError:
            return False

    def deliver_pending(self) -> dict[str, Any]:
        """Try every pending notification on the local adapters (bounded). Never raises.

        M-6: an attempt is first *claimed* in its own write transaction (unique per
        notification, adapter and attempt), the adapter is called only by the claim's owner,
        and the outcome is then recorded against that claim. A second worker sees the claim
        and skips (``in_progress``); a lost race is a ``conflict``, never an exception. Without
        the claim table (journals before v6) nothing is delivered: fail closed."""
        summary = dict.fromkeys(OUTCOMES, 0)
        if not self.installed():
            return {**summary, "status": "not_installed"}
        if not self.adapters:
            return {**summary, "status": "no_adapter"}
        if not self.claims_installed():
            return {**summary, "status": "claims_not_installed"}
        try:
            with self.store.read() as conn:
                pending = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT n.* FROM notification n WHERE NOT EXISTS (SELECT 1 FROM "
                        "notification_state s WHERE s.notification_id = n.id AND "
                        "s.state <> 'pending') ORDER BY n.rowid"
                    )
                ]
            for message in pending:
                for adapter in self.adapters:
                    outcome = self._attempt(message, adapter)
                    summary[outcome] += 1
                    if outcome in ("delivered", "already_delivered", "in_progress"):
                        break
        except JournalError as exc:
            self._failed(exc.code)
            return {**summary, "status": "journal_unavailable"}
        except sqlite3.Error as exc:  # never escapes: reported, nothing half-written
            self._failed(type(exc).__name__)
            return {**summary, "status": "journal_error"}
        return {**summary, "status": "ok"}

    def _attempt(self, message: dict[str, Any], adapter: Any) -> str:
        claim = self._claim(message["id"], adapter.name)
        if not isinstance(claim, int):
            return claim
        view = {k: message[k] for k in ("id", "category", "title", "body", "created_utc")}
        try:
            adapter.deliver(view)
            outcome, code = "delivered", None
        except DeliveryError as exc:
            outcome, code = "failed", exc.code[:60]
        except Exception as exc:  # an adapter bug is a failed attempt, not a crash
            outcome, code = "failed", type(exc).__name__[:60]
        return self._record(message["id"], adapter.name, claim, outcome, code)

    def _claim(self, nid: str, adapter: str) -> int | str:
        """Claim the next attempt (its number) or say why not, in one write transaction."""
        now = self._clock()
        try:
            with self.store.write() as conn:
                if conn.execute(
                    "SELECT 1 FROM notification_delivery WHERE notification_id=? "
                    "AND outcome='delivered'",
                    (nid,),
                ).fetchone():
                    return "already_delivered"
                last = conn.execute(
                    "SELECT c.attempt, c.claimed_utc, (SELECT d.outcome FROM "
                    "notification_delivery d WHERE d.notification_id=c.notification_id AND "
                    "d.adapter=c.adapter AND d.attempt=c.attempt) AS outcome FROM "
                    "notification_claim c WHERE c.notification_id=? AND c.adapter=? "
                    "ORDER BY c.attempt DESC LIMIT 1",
                    (nid, adapter),
                ).fetchone()
                attempts = last["attempt"] if last else 0
                if last is not None and last["outcome"] is None:
                    claimed = datetime.fromisoformat(last["claimed_utc"].replace("Z", "+00:00"))
                    if now - claimed < CLAIM_TIMEOUT:
                        return "in_progress"  # another worker owns this attempt
                    # The owner never reported back (crash): the attempt is visibly failed.
                    self._insert_delivery(conn, nid, adapter, attempts, "failed", EXPIRED, now)
                    self._audit_outcome(conn, nid, adapter, attempts, "failed", EXPIRED, now)
                if attempts >= MAX_ATTEMPTS:
                    return "exhausted"
                conn.execute(
                    "INSERT INTO notification_claim(id, notification_id, adapter, attempt, "
                    "worker, claimed_utc) VALUES (?,?,?,?,?,?)",
                    (new_ulid(now), nid, adapter, attempts + 1, self.worker, utc_iso(now)),
                )
                return attempts + 1
        except sqlite3.IntegrityError:
            return "conflict"  # another worker claimed this attempt first

    def _record(self, nid: str, adapter: str, attempt: int, outcome: str, code) -> str:
        now = self._clock()
        try:
            with self.store.write() as conn:
                self._insert_delivery(conn, nid, adapter, attempt, outcome, code, now)
                if outcome == "delivered":
                    self._state(conn, nid, "available", "johanna", now)
                self._audit_outcome(conn, nid, adapter, attempt, outcome, code, now)
            return outcome
        except sqlite3.IntegrityError:
            # e.g. delivered meanwhile through another adapter: the claim stays unresolved
            # and later becomes a visible ``claim_expired`` failure; nothing is lost.
            self._failed("delivery_record_conflict")
            return "conflict"

    @staticmethod
    def _insert_delivery(conn, nid, adapter, attempt, outcome, code, now) -> None:
        conn.execute(
            "INSERT INTO notification_delivery(id, notification_id, adapter, attempt, "
            "outcome, error_code, attempted_utc) VALUES (?,?,?,?,?,?,?)",
            (new_ulid(now), nid, adapter, attempt, outcome, code, utc_iso(now)),
        )

    def _audit_outcome(self, conn, nid, adapter, attempt, outcome, code, now) -> None:
        """L-5: every delivery outcome is audited; the last permitted failure also records
        the exhaustion, so an exhausted notification is visible in the audit trail."""
        action = "notification.delivered" if outcome == "delivered" else "notification.failed"
        detail = {"adapter": adapter, "attempt": attempt, "error_code": code}
        self.store.append_audit(
            conn, action=action, table="notification_delivery", record_id=nid, new=detail, at=now
        )
        if outcome == "failed" and attempt >= MAX_ATTEMPTS:
            self.store.append_audit(
                conn,
                action="notification.exhausted",
                table="notification_delivery",
                record_id=nid,
                new={"adapter": adapter, "attempts": attempt},
                at=now,
            )

    def _transition(self, nid: str, state: str, actor: str) -> dict[str, Any]:
        if not self.installed():
            raise NotificationError("not_installed")
        now = self._clock()
        try:
            with self.store.write() as conn:
                if conn.execute("SELECT 1 FROM notification WHERE id=?", (nid,)).fetchone() is None:
                    raise NotificationError("not_found")
                self._state(conn, nid, state, actor, now)
                self.store.append_audit(
                    conn,
                    action=f"notification.{state}",
                    table="notification_state",
                    record_id=nid,
                    new={"state": state},
                    at=now,
                    actor=actor,
                )
        except sqlite3.IntegrityError:
            raise NotificationError("invalid_transition") from None
        return {"id": nid, "state": state}

    def mark_presented(self, nid: str) -> dict[str, Any]:
        """The dashboard showed it (explicit call; never from a GET). Audited (L-5)."""
        return self._transition(nid, "presented", "johanna")

    def acknowledge(self, nid: str) -> dict[str, Any]:
        """The user acknowledged it (explicit user action). Audited."""
        return self._transition(nid, "acknowledged", "user")

    # ------------------------------------------------------------------ reads (no mutation)
    def list(self, *, state: str | None = None, limit: int = 50) -> dict[str, Any]:
        if not self.installed():
            return {"status": "not_installed", "notifications": []}
        if state is not None and state not in LIFECYCLE:
            raise ValueError("unknown state")
        limit = max(1, min(int(limit), 100))
        claims = self.claims_installed()
        now = self._clock()
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT n.id, n.category, n.title, n.body, n.created_utc, "
                "(SELECT s.state FROM notification_state s WHERE s.notification_id = n.id "
                " ORDER BY s.rowid DESC LIMIT 1) AS state, "
                "(SELECT count(*) FROM notification_delivery d WHERE d.notification_id = n.id) "
                "AS attempts FROM notification n ORDER BY n.rowid DESC"
            ).fetchall()
            items = [dict(r) for r in rows if state is None or r["state"] == state][:limit]
            for item in items:
                item.update(self._delivery_view(conn, item["id"], claims, now))
        return {"status": "installed", "notifications": items}

    def _delivery_view(self, conn, nid: str, claims: bool, now: datetime) -> dict[str, Any]:
        """Read-only delivery status: delivered / in_progress / exhausted / failed_retryable /
        not_attempted. Exhausted and failed deliveries stay visible (M-6)."""
        rows = conn.execute(
            "SELECT adapter, attempt, outcome, error_code FROM notification_delivery "
            "WHERE notification_id=? ORDER BY rowid",
            (nid,),
        ).fetchall()
        last_error = next((r["error_code"] for r in reversed(rows) if r["error_code"]), None)
        if any(r["outcome"] == "delivered" for r in rows):
            return {"delivery": "delivered", "last_error": last_error}
        if claims:
            open_claim = conn.execute(
                "SELECT c.claimed_utc FROM notification_claim c WHERE c.notification_id=? AND "
                "NOT EXISTS (SELECT 1 FROM notification_delivery d WHERE "
                "d.notification_id=c.notification_id AND d.adapter=c.adapter AND "
                "d.attempt=c.attempt) ORDER BY c.rowid DESC LIMIT 1",
                (nid,),
            ).fetchone()
            if open_claim is not None:
                claimed = datetime.fromisoformat(open_claim[0].replace("Z", "+00:00"))
                stale = now - claimed >= CLAIM_TIMEOUT
                return {
                    "delivery": "claim_stale" if stale else "in_progress",
                    "last_error": last_error,
                }
        per_adapter: dict[str, int] = {}
        for r in rows:
            per_adapter[r["adapter"]] = max(per_adapter.get(r["adapter"], 0), r["attempt"])
        if (
            per_adapter
            and all(n >= MAX_ATTEMPTS for n in per_adapter.values())
            and ({a.name for a in self.adapters} <= set(per_adapter))
        ):
            return {"delivery": "exhausted", "last_error": last_error}
        if rows:
            return {"delivery": "failed_retryable", "last_error": last_error}
        return {"delivery": "not_attempted", "last_error": None}

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed(),
            "claims_installed": self.claims_installed(),
            "external_delivery": "none (local adapters only; not authorised)",
            "adapters": [a.name for a in self.adapters],
            "automatic_notifications": False,
            "failures": self.failures,
            "last_failure": self.last_failure,
        }
