"""M-9 user-confirmed links between logbook trades and broker positions (foundation only).

**Not activated.** No route can call ``confirm``/``reject``/``revoke`` in this phase: the API
endpoint exists but answers ``403 not_activated`` until a separate approval. Nothing here
talks to the broker; a link is a journal record of the *user's* decision.

- Decisions are append-only (schema v6 ``trade_broker_link``): ``CONFIRMED``,
  ``REJECTED`` and ``REVOKED`` (a revocation names the confirmation it ends). The actor is
  always ``user``; Johanna never links anything itself, and a candidate match is never
  converted into a link.
- Exclusive: a broker position has at most one active confirmed trade, and a trade at most one
  active confirmed position (database trigger, so concurrent confirmations cannot both win).
- A decision must reference a stored ``LIVE`` broker observation that contains the position
  (as an open position or in its deals), so the user decides on evidence Johanna actually
  observed. A symbol mismatch between the trade and the observed position is refused; volume
  differences are allowed (partial closes) and surface as ``VOLUME_MISMATCH`` discrepancies.
- Only HMAC position references are stored; no broker ticket or identifier.
- Every decision is audited. Errors are ``LinkError`` with a code, never raw database errors.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from johanna.journal_store import JournalError, new_ulid, tables_installed, utc_iso

TABLES = ("trade_broker_link", "broker_observation", "trade")
_REF = re.compile(r"[0-9a-f]{64}")


class LinkError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradeLinkService:
    name = "trade_links"

    def __init__(self, store: Any, *, clock=None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self.confirmation_enabled = False  # API stays inactive until separately approved

    def installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES)
        except JournalError:
            return False

    # ------------------------------------------------------------------ decisions
    def confirm(self, trade_id: str, position_ref: str, observation_id: str, reason: str) -> str:
        return self._decide("CONFIRMED", trade_id, position_ref, observation_id, reason)

    def reject(self, trade_id: str, position_ref: str, observation_id: str, reason: str) -> str:
        return self._decide("REJECTED", trade_id, position_ref, observation_id, reason)

    def revoke(self, link_id: str, reason: str) -> str:
        if not self.installed():
            raise LinkError("not_installed")
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT trade_id, position_ref, observation_id FROM trade_broker_link "
                "WHERE id=? AND decision='CONFIRMED'",
                (link_id,),
            ).fetchone()
        if row is None:
            raise LinkError("link_not_found")
        return self._decide(
            "REVOKED",
            row["trade_id"],
            row["position_ref"],
            row["observation_id"],
            reason,
            supersedes=link_id,
        )

    def _decide(
        self,
        decision: str,
        trade_id: str,
        position_ref: str,
        observation_id: str,
        reason: str,
        *,
        supersedes: str | None = None,
    ) -> str:
        if not self.installed():
            raise LinkError("not_installed")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise LinkError("invalid_reason")
        if not isinstance(position_ref, str) or not _REF.fullmatch(position_ref):
            raise LinkError("invalid_position_ref")
        now = self._clock()
        try:
            with self.store.write() as conn:
                trade = conn.execute(
                    "SELECT broker_symbol, direction, voided_utc FROM trade WHERE id=?",
                    (trade_id,),
                ).fetchone()
                if trade is None:
                    raise LinkError("trade_not_found")
                if trade["voided_utc"] is not None and decision != "REVOKED":
                    raise LinkError("trade_voided")
                observed = self._observed_position(conn, observation_id, position_ref)
                if decision == "CONFIRMED" and observed["symbol"] != trade["broker_symbol"]:
                    raise LinkError("symbol_mismatch")
                link_id = new_ulid(now)
                conn.execute(
                    "INSERT INTO trade_broker_link(id, trade_id, position_ref, observation_id, "
                    "decision, supersedes_id, reason, actor, decided_utc) "
                    "VALUES (?,?,?,?,?,?,?,'user',?)",
                    (
                        link_id,
                        trade_id,
                        position_ref,
                        observation_id,
                        decision,
                        supersedes,
                        reason.strip(),
                        utc_iso(now),
                    ),
                )
                self.store.append_audit(
                    conn,
                    action=f"trade_link.{decision.lower()}",
                    table="trade_broker_link",
                    record_id=link_id,
                    new={"trade_id": trade_id, "decision": decision, "supersedes": supersedes},
                    at=now,
                    actor="user",
                )
                return link_id
        except sqlite3.IntegrityError as exc:
            text = str(exc)
            if "already has an active confirmed link" in text:
                raise LinkError("already_linked") from None
            if (
                "revocation" in text
                or "UNIQUE constraint failed: trade_broker_link.supersedes" in text
            ):
                raise LinkError("invalid_revocation") from None
            raise LinkError("integrity_error") from None

    @staticmethod
    def _observed_position(conn, observation_id: str, position_ref: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT status, payload FROM broker_observation WHERE id=?", (observation_id,)
        ).fetchone()
        if row is None:
            raise LinkError("observation_not_found")
        if row["status"] != "LIVE":
            raise LinkError("observation_not_live")
        payload = json.loads(row["payload"])
        for p in payload.get("positions") or []:
            if p.get("position_ref") == position_ref:
                return {"symbol": p.get("symbol"), "source": "open_position"}
        for d in payload.get("deals") or []:
            if d.get("position_ref") == position_ref:
                return {"symbol": d.get("symbol"), "source": "deal_history"}
        raise LinkError("position_not_in_observation")

    # ------------------------------------------------------------------ reads
    def links(self) -> dict[str, Any]:
        if not self.installed():
            return {"status": "not_installed", "links": []}
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT l.id, l.trade_id, l.position_ref, l.decision, l.supersedes_id, "
                "l.reason, l.decided_utc, EXISTS (SELECT 1 FROM trade_broker_link r WHERE "
                "r.supersedes_id = l.id) AS revoked FROM trade_broker_link l ORDER BY l.rowid"
            ).fetchall()
        items = []
        for r in rows:
            item = dict(r)
            item["active"] = r["decision"] == "CONFIRMED" and not r["revoked"]
            item["revoked"] = bool(r["revoked"])
            items.append(item)
        return {"status": "installed", "links": items}

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed(),
            "confirmation_endpoint": "inactive (separate approval required)",
            "automatic_linking": False,
        }
