"""P6.3 read-only broker position monitoring and reconciliation (infrastructure only).

**Not activated.** There is no thread, timer or scheduler here: ``enabled`` is always False in
this phase and an observation happens only when ``observe()`` is called explicitly (tests, or
a future separately approved activation). Nothing modifies, closes or manages a position.

- Reads use the existing serialised MT5 worker through ``live_market.reconciliation_read`` as
  *background* reads: when the dashboard is using the worker the read is deferred, never
  queued (dashboard priority and contention controls preserved). No second MT5 connection.
- Broker identifiers are replaced by HMAC references (``JournalStore.broker_ref``) before
  anything is kept; without a verified journal key the observation is ``UNAVAILABLE`` and no
  raw identifier is stored.
- Observations and reconciliation results are append-only journal rows (staged schema v5).
  Without v5 (production v4) results stay in memory and ``installed`` is False.
- Any failure (worker busy, timeout, MT5 down, journal refused) yields an ``UNAVAILABLE``
  observation; it never raises into market analysis or risk requests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.journal_store import JournalError, new_ulid, tables_installed, utc_iso
from johanna.live_market import MarketUnavailable, background_reads
from johanna.reconciliation import discrepancies, reconcile

LOGGER = logging.getLogger("johanna.broker_monitoring")
TABLES = ("broker_observation", "reconciliation_result")
# M-9 (schema v6): user link decisions and persisted discrepancies.
LINK_TABLES = ("trade_broker_link", "reconciliation_discrepancy")
DEFAULT_LOOKBACK = timedelta(days=30)
_POSITION_KEEP = ("symbol", "type", "volume", "price_open", "sl", "tp", "time_utc")
_ORDER_KEEP = ("symbol", "type", "volume_current", "price_open", "time_utc")
_DEAL_KEEP = ("symbol", "type", "entry", "volume", "price", "time_utc")
# M-4: stored reconciliation details are bounded *before* serialisation (never by cutting
# the JSON text), and every truncation is explicit in the data.
MAX_DETAIL_ITEMS = 20
MAX_DETAIL_TEXT = 300
MAX_DETAIL_CHARS = 2000


class MissingIdentifier(Exception):
    """A broker object without a valid identifier (the observation is refused)."""


def _valid_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _bounded(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str) and len(value) > MAX_DETAIL_TEXT:
        return value[:MAX_DETAIL_TEXT], True
    return value, False


def bounded_detail(detail: Any) -> str:
    """Valid JSON of at most ``MAX_DETAIL_CHARS`` characters, with explicit truncation."""
    if not isinstance(detail, dict):
        detail = {"value": str(detail)}
    out: dict[str, Any] = {}
    truncated = False
    for key, value in detail.items():
        key = str(key)[:60]
        if isinstance(value, list | tuple):
            items = list(value)
            if len(items) > MAX_DETAIL_ITEMS:
                out[f"{key}_omitted"] = len(items) - MAX_DETAIL_ITEMS
                items, truncated = items[:MAX_DETAIL_ITEMS], True
            kept = []
            for item in items:
                item, cut = _bounded(item if isinstance(item, int | float | str) else str(item))
                truncated |= cut
                kept.append(item)
            out[key] = kept
        else:
            value, cut = _bounded(
                value if isinstance(value, int | float | str | bool | None) else str(value)
            )
            truncated |= cut
            out[key] = value
    if truncated:
        out["truncated"] = True
    text = json.dumps(out, sort_keys=True, default=str)
    if len(text) > MAX_DETAIL_CHARS:
        text = json.dumps(
            {"truncated": True, "reason": "detail too large", "keys": sorted(out)[:20]},
            sort_keys=True,
        )
    return text


def _iso(value: Any) -> Any:
    return utc_iso(value) if isinstance(value, datetime) else value


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return value if isinstance(value, datetime) else None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BrokerMonitoringService:
    name = "broker_monitoring"

    def __init__(self, store: Any, live_market: Any, *, clock=None) -> None:
        self.store = store
        self.live_market = live_market
        self._clock = clock or (lambda: datetime.now(UTC))
        self.enabled = False  # P6.3 infrastructure only: never activated in this phase
        self._lock = threading.Lock()
        self._observation: dict[str, Any] | None = None
        self._results: list[dict[str, Any]] | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------ state
    def installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES)
        except JournalError:
            return False

    def links_installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *TABLES, *LINK_TABLES)
        except JournalError:
            return False

    def _links(self) -> dict[str, Any]:
        """Active user-confirmed links and the pairs whose latest decision is a rejection.
        Without v6 there are no user decisions (nothing is ever linked automatically)."""
        if not self.links_installed():
            return {"confirmed": {}, "rejected": set()}
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT l.id, l.trade_id, l.position_ref, l.decision FROM trade_broker_link l "
                "WHERE l.decision IN ('CONFIRMED', 'REJECTED') AND NOT EXISTS (SELECT 1 FROM "
                "trade_broker_link r WHERE r.supersedes_id = l.id) ORDER BY l.rowid"
            ).fetchall()
        latest: dict[tuple[str, str], Any] = {}
        for r in rows:
            latest[(r["trade_id"], r["position_ref"])] = r
        confirmed = {
            r["trade_id"]: {"position_ref": r["position_ref"], "link_id": r["id"]}
            for r in latest.values()
            if r["decision"] == "CONFIRMED"
        }
        rejected = {pair for pair, r in latest.items() if r["decision"] == "REJECTED"}
        return {"confirmed": confirmed, "rejected": rejected}

    # ------------------------------------------------------------------ observe (explicit)
    def observe(self, lookback: timedelta = DEFAULT_LOOKBACK) -> dict[str, Any]:
        """One read-only observation. Never raises; failures become UNAVAILABLE."""
        with self._lock:
            now = self._clock()
            start = now - lookback
            try:
                if not self.store.hmac_available:
                    observation = self._unavailable(now, "hmac_unavailable")
                else:
                    with background_reads():
                        payload = self.live_market.reconciliation_read(start, now)
                    observation = self._sanitise(payload["value"], now)
            except MarketUnavailable as exc:
                observation = self._unavailable(now, exc.code)
            except MissingIdentifier as exc:
                LOGGER.warning("broker observation refused: %s identifier missing", exc)
                observation = self._unavailable(now, "broker_identifier_missing")
            except Exception as exc:  # isolated: monitoring never disturbs anything else
                LOGGER.warning("broker observation failed (%s)", type(exc).__name__)
                observation = self._unavailable(now, "monitoring_error")
            self._last_error = observation.get("reason")
            self._observation = observation
            self._persist_observation(observation)
            return self._summary(observation)

    def _unavailable(self, now: datetime, reason: str) -> dict[str, Any]:
        return {
            "id": None,
            "status": "UNAVAILABLE",
            "reason": reason,
            "observed_utc": now,
            "coverage_from_utc": None,
            "coverage_to_utc": None,
            "positions": [],
            "orders": [],
            "deals": [],
        }

    def _sanitise(self, raw: dict[str, Any], now: datetime) -> dict[str, Any]:
        """M-2: every identifier is validated before any reference is made. A missing or
        invalid identifier would merge distinct broker objects, so the whole observation is
        refused (``MissingIdentifier`` -> UNAVAILABLE); nothing of it is kept."""
        ref = self.store.broker_ref

        def keep(item: dict, names: tuple) -> dict:
            return {k: item.get(k) for k in names}

        def required(item: dict, *names: str, what: str) -> int:
            for name in names:
                if _valid_id(item.get(name)):
                    return item[name]
            raise MissingIdentifier(what)

        positions, orders, deals = [], [], []
        for p in raw.get("positions") or []:
            positions.append(
                {
                    **keep(p, _POSITION_KEEP),
                    "position_ref": required(p, "identifier", "ticket", what="position"),
                }
            )
        for o in raw.get("orders") or []:
            orders.append(
                {**keep(o, _ORDER_KEEP), "order_ref": required(o, "ticket", what="order")}
            )
        for d in raw.get("deals") or []:
            trade_deal = d.get("type") in (0, 1)  # buy/sell deals belong to a position
            position = d.get("position_id")
            if trade_deal and not _valid_id(position):
                raise MissingIdentifier("deal position")
            deals.append(
                {
                    **keep(d, _DEAL_KEEP),
                    "deal_ref": required(d, "ticket", what="deal"),
                    "order_ref": d.get("order") if _valid_id(d.get("order")) else None,
                    "position_ref": position if _valid_id(position) else None,
                }
            )
        # only now, with every identifier validated, are the keyed references made
        for p in positions:
            p["position_ref"] = ref("position", p["position_ref"])
        for o in orders:
            o["order_ref"] = ref("order", o["order_ref"])
        for d in deals:
            d["deal_ref"] = ref("deal", d["deal_ref"])
            d["order_ref"] = ref("order", d["order_ref"]) if d["order_ref"] else None
            d["position_ref"] = ref("position", d["position_ref"]) if d["position_ref"] else None
        return {
            "id": None,
            "status": "LIVE",
            "reason": None,
            "observed_utc": raw.get("retrieved_at") or now,
            "coverage_from_utc": raw.get("coverage_from_utc"),
            "coverage_to_utc": raw.get("coverage_to_utc"),
            "positions": positions,
            "orders": orders,
            "deals": deals,
        }

    def _persist_observation(self, observation: dict[str, Any]) -> None:
        if not self.installed():
            return
        payload = json.dumps(
            {k: observation[k] for k in ("positions", "orders", "deals")},
            sort_keys=True,
            default=_iso,
        )
        fingerprint = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = self._clock()
        live = observation["status"] == "LIVE"
        try:
            with self.store.write() as conn:
                observation_id = new_ulid(now)
                conn.execute(
                    "INSERT INTO broker_observation(id, observed_utc, status, reason, "
                    "coverage_from_utc, coverage_to_utc, positions, orders, deals, payload, "
                    "payload_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        _iso(observation["observed_utc"]),
                        observation["status"],
                        observation["reason"],
                        _iso(observation["coverage_from_utc"]),
                        _iso(observation["coverage_to_utc"]),
                        len(observation["positions"]) if live else None,
                        len(observation["orders"]) if live else None,
                        len(observation["deals"]) if live else None,
                        payload,
                        fingerprint,
                    ),
                )
                self.store.append_audit(
                    conn,
                    action="broker_observation.recorded",
                    table="broker_observation",
                    record_id=observation_id,
                    new={"status": observation["status"], "fingerprint": fingerprint},
                    at=now,
                )
            observation["id"] = observation_id
        except JournalError as exc:
            self._last_error = exc.code

    # ------------------------------------------------------------------ reconcile
    def reconcile(self) -> dict[str, Any]:
        """Reconcile the logbook with the latest observation (read-only towards the broker)."""
        with self._lock:
            now = self._clock()
            try:
                trades = self._trades()
                links = self._links()
            except JournalError as exc:
                return {"status": "journal_unavailable", "reason": exc.code, "results": []}
            results = reconcile(trades, self._observation, now, links=links)
            self._results = results
            persisted = self._persist_results(results)
            return {
                "status": "ok",
                "observation_status": (self._observation or {}).get("status"),
                "persistence": persisted,
                "results": results,
                "counts": _counts(results),
                "discrepancies": discrepancies(results),
            }

    def _trades(self) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            trades = conn.execute(
                "SELECT id, broker_symbol, direction, voided_utc FROM trade ORDER BY rowid"
            ).fetchall()
            fills = conn.execute(
                "SELECT trade_id, kind, time_utc, volume, broker_deal_ref, status FROM trade_fill"
            ).fetchall()
        by_trade: dict[str, list] = {}
        for f in fills:
            by_trade.setdefault(f["trade_id"], []).append(
                {
                    "kind": f["kind"],
                    "time_utc": _parse(f["time_utc"]),
                    "volume": f["volume"],
                    "broker_deal_ref": f["broker_deal_ref"],
                    "status": f["status"],
                }
            )
        return [
            {
                "id": t["id"],
                "broker_symbol": t["broker_symbol"],
                "direction": t["direction"],
                "voided": t["voided_utc"] is not None,
                "fills": by_trade.get(t["id"], []),
            }
            for t in trades
        ]

    def _persist_results(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """M-3: one reconciliation per observation, written with plain INSERTs in one
        transaction. Reconciling the same observation again is explicitly idempotent (nothing
        new is written); any integrity error rolls the whole write back and is reported --
        never silently ignored. Take a new observation to reconcile a changed logbook."""
        observation_id = (self._observation or {}).get("id")
        if observation_id is None:
            return {"status": "not_persisted", "reason": "observation not stored"}
        if not self.installed():
            return {"status": "not_persisted", "reason": "not_installed"}
        now = self._clock()
        try:
            with self.store.write() as conn:
                existing = conn.execute(
                    "SELECT count(*) FROM reconciliation_result WHERE observation_id=?",
                    (observation_id,),
                ).fetchone()[0]
                if existing:
                    return {"status": "already_reconciled", "rows": existing}
                for r in results:
                    key = r["trade_id"] if r["subject"] == "trade" else r["position_ref"]
                    conn.execute(
                        "INSERT INTO reconciliation_result(id, observation_id, subject, "
                        "subject_key, trade_id, position_ref, state, basis, detail, "
                        "recorded_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            new_ulid(now),
                            observation_id,
                            r["subject"],
                            key,
                            r["trade_id"],
                            r["position_ref"],
                            r["state"],
                            r["basis"],
                            bounded_detail(r["detail"]),
                            utc_iso(now),
                        ),
                    )
                findings = discrepancies(results)
                if findings and tables_installed(conn, "reconciliation_discrepancy"):
                    for f in findings:
                        conn.execute(
                            "INSERT INTO reconciliation_discrepancy(id, observation_id, code, "
                            "subject_key, trade_id, position_ref, detail, recorded_utc) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                new_ulid(now),
                                observation_id,
                                f["code"],
                                f["trade_id"] or f["position_ref"],
                                f["trade_id"],
                                f["position_ref"],
                                bounded_detail(f["detail"]),
                                utc_iso(now),
                            ),
                        )
                self.store.append_audit(
                    conn,
                    action="reconciliation.recorded",
                    table="reconciliation_result",
                    record_id=observation_id,
                    new=_counts(results),
                    at=now,
                )
            return {"status": "persisted", "rows": len(results)}
        except sqlite3.IntegrityError as exc:
            LOGGER.error("reconciliation results refused by the journal: %s", exc)
            self._last_error = "reconciliation_persist_failed"
            return {"status": "failed", "reason": "integrity_error", "message": str(exc)[:200]}
        except JournalError as exc:
            self._last_error = exc.code
            return {"status": "failed", "reason": exc.code}

    # ------------------------------------------------------------------ reads
    def status(self) -> dict[str, Any]:
        observation = self._observation
        return {
            "name": self.name,
            "mode": "read_only",
            "enabled": False,
            "activation": "not authorised (P6.3 infrastructure only); observe() is explicit",
            "installed": self.installed(),
            "last_observation": self._summary(observation) if observation else None,
            "last_error": self._last_error,
            "reconciliation_counts": _counts(self._results) if self._results else None,
            "note": "Read-only: Johanna never modifies, closes or manages broker positions.",
        }

    def latest(self) -> dict[str, Any]:
        if not self.installed():
            return {
                "status": "not_installed",
                "results": self._results or [],
                "source": "memory",
            }
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT id, observed_utc, status, reason FROM broker_observation "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return {"status": "installed", "observation": None, "results": []}
            results = [
                {**dict(r), "detail": json.loads(r["detail"])}
                for r in conn.execute(
                    "SELECT subject, trade_id, position_ref, state, basis, detail, recorded_utc "
                    "FROM reconciliation_result WHERE observation_id=? ORDER BY rowid",
                    (row["id"],),
                )
            ]
            found = (
                [
                    dict(r)
                    for r in conn.execute(
                        "SELECT code, trade_id, position_ref, detail FROM "
                        "reconciliation_discrepancy WHERE observation_id=? ORDER BY rowid",
                        (row["id"],),
                    )
                ]
                if tables_installed(conn, "reconciliation_discrepancy")
                else None
            )
        return {
            "status": "installed",
            "observation": dict(row),
            "results": results,
            "discrepancies": found,
        }

    @staticmethod
    def _summary(o: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": o.get("id"),
            "status": o["status"],
            "reason": o["reason"],
            "observed_utc": _iso(o["observed_utc"]),
            "coverage_from_utc": _iso(o["coverage_from_utc"]),
            "coverage_to_utc": _iso(o["coverage_to_utc"]),
            "positions": len(o["positions"]),
            "orders": len(o["orders"]),
            "deals": len(o["deals"]),
        }


def _counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    return counts
