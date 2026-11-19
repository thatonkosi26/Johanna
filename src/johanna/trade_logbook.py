"""P5.2 manual trade logbook. See ``docs/phase-5-architecture.md`` §3.4 and the approved P5.2
design.

Records trades **you executed yourself in MT5**. Johanna never places, modifies or closes orders,
and nothing here contacts MT5 except the existing read-only Phase 3 account service (instrument
specification at trade creation; a live quote for the unrealised estimate when asked).

- A trade exists from its first entry fill (no draft state). Its status is derived from its
  recorded fills: ``VOIDED`` / ``OPEN`` / ``PARTIALLY_CLOSED`` / ``CLOSED``.
- Every manual record is ``MANUAL_UNVERIFIED``; broker verification arrives with reconciliation.
- Fills, links and trades are void-only; protection history and notes are append-only; every
  write is audited in the same transaction and is idempotent by client request id.
- Accounting walks the recorded fills in time order. Each exit is valued against the weighted
  average price of the entries recorded **before** it (never later ones); voided fills are
  ignored. Unknown costs are never zero; net P&L is withheld while any cost is unknown.
- Calculated P&L is derived on read from stored inputs (fills and the instrument specification
  captured at creation) so a later correction can never leave a stale stored figure; the
  user-entered figure is stored per exit fill with its own provenance.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from johanna.account_intelligence import INSTRUMENT_REQUIRED
from johanna.journal_store import JournalError, JournalStore, canonical_json, new_ulid, utc_iso

SUPPORTED = {"EURUSD": "EURUSDc", "GBPUSD": "GBPUSDc", "USDJPY": "USDJPYc"}
BANNER = (
    "Record trades you have already executed in MT5. Johanna does not place, modify or close "
    "orders."
)
VERIFICATION = "MANUAL_UNVERIFIED"
STATUSES = ("OPEN", "PARTIALLY_CLOSED", "CLOSED", "VOIDED")
COSTS = ("commission", "swap", "fee")
MAX_QUOTE_AGE_SECONDS = 10.0  # same freshness limit as the Phase 4 risk policy
FUTURE_TOLERANCE = timedelta(seconds=60)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_EPS = Decimal("1e-9")


class LogbookError(Exception):
    """A rejected logbook request: ``status`` is the HTTP status, ``code`` a stable reason."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _bad(code: str, message: str) -> LogbookError:
    return LogbookError(400, code, message)


def resolve_symbol(value: Any) -> tuple[str, str]:
    """``(canonical, broker)`` for EURUSD/GBPUSD/USDJPY or their ``c`` broker symbols."""
    text = str(value or "").strip().upper()
    for canonical, broker in SUPPORTED.items():
        if text in (canonical, broker.upper()):
            return canonical, broker
    raise _bad("unsupported_symbol", "symbol must be EURUSDc, GBPUSDc or USDJPYc")


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise _bad("invalid_number", f"{name} must be a finite number")
    if positive and value <= 0:
        raise _bad("invalid_number", f"{name} must be positive")
    return float(value)


def _optional_number(payload: Mapping[str, Any], name: str) -> float | None:
    value = payload.get(name)
    return None if value is None else _number(value, name)


def _time(value: Any, name: str, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise _bad("invalid_time", f"{name} must be an ISO-8601 time with a time zone")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bad("invalid_time", f"{name} must be an ISO-8601 time with a time zone") from None
    if parsed.tzinfo is None:
        raise _bad("invalid_time", f"{name} must include a time zone")
    parsed = parsed.astimezone(UTC)
    if parsed > now + FUTURE_TOLERANCE:
        raise _bad("invalid_time", f"{name} is in the future")
    return parsed


def _text(value: Any, name: str, *, required: bool, limit: int) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _bad("invalid_text", f"{name} is required")
        return None
    if not isinstance(value, str):
        raise _bad("invalid_text", f"{name} must be text")
    text = value.strip()
    if len(text) > limit:
        raise _bad("invalid_text", f"{name} is longer than {limit} characters")
    return text


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def _rounded(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


class TradeLogbook:
    name = "trade_logbook"

    def __init__(
        self,
        store: JournalStore,
        account: Any = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.account = account  # Phase 3 AccountIntelligenceService (read-only)
        self._clock = clock or store.now

    # ================================================================== idempotent writes
    def _write(
        self,
        endpoint: str,
        payload: Any,
        action: Callable[[Any, datetime, dict], tuple[int, dict]],
    ) -> tuple[int, dict]:
        """Run ``action`` once per client request id. Same id + same payload: the original
        result; same id + different payload: 409. Rejected requests store nothing."""
        request_id = self._precheck(payload)
        body = {k: v for k, v in payload.items() if k != "request_id"}
        try:
            digest = hashlib.sha256(
                canonical_json({"endpoint": endpoint, "body": body}).encode("utf-8")
            ).hexdigest()
        except ValueError:
            raise _bad("invalid_body", "request body contains non-finite numbers") from None
        request_id = request_id.lower()
        try:
            with self.store.write() as conn:
                row = conn.execute(
                    "SELECT endpoint, payload_hash, status_code, response FROM write_request "
                    "WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if row is not None:
                    if row["endpoint"] == endpoint and row["payload_hash"] == digest:
                        return row["status_code"], json.loads(row["response"])
                    raise LogbookError(
                        409,
                        "request_id_conflict",
                        "this request_id was already used with a different request",
                    )
                now = self._clock()
                status, response = action(conn, now, body)
                conn.execute(
                    "INSERT INTO write_request(request_id, endpoint, payload_hash, status_code, "
                    "response, recorded_utc) VALUES (?,?,?,?,?,?)",
                    (request_id, endpoint, digest, status, canonical_json(response), utc_iso(now)),
                )
                return status, response
        except JournalError as exc:
            raise LogbookError(503, "journal_unavailable", str(exc)) from None

    def _precheck(self, payload: Any) -> str:
        if not isinstance(payload, Mapping):
            raise _bad("invalid_body", "request body must be a JSON object")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
            raise _bad("invalid_request_id", "request_id must be a client-generated UUID")
        if not self.store.ready:
            raise LogbookError(503, "journal_unavailable", "the trading journal is unavailable")
        return request_id

    def _audit(self, conn, action: str, table: str, record_id: str, new: dict, now) -> None:
        self.store.append_audit(
            conn, action=action, table=table, record_id=record_id, new=new, at=now, actor="user"
        )

    # ================================================================== instrument spec
    def instrument_spec(self, canonical: str) -> dict[str, Any]:
        """The verified Phase 3 specification captured with a new trade (read-only)."""
        unverified = {
            "status": "unverified",
            "reason": "Phase 3 instrument specification unavailable",
        }
        if self.account is None:
            return unverified
        try:
            payload = self.account.instrument(canonical)
        except Exception:  # never let a market-data problem block recording a trade
            return unverified
        payload = payload or {}
        item = payload.get("instrument") or {}
        account_ccy = payload.get("account_currency") or {}
        # Only a LIVE read (served within the Phase 3 TTL) qualifies. A CACHED snapshot is what
        # Phase 3 serves, without an age limit, after a failed MT5 read: it never verifies a
        # specification here.
        if payload.get("status") != "LIVE":
            return {**unverified, "reason": "instrument specification not LIVE"}
        if item.get("validation") not in ("verified", "partial"):
            return {**unverified, "reason": "instrument specification not verified"}
        fields = item.get("fields") or {}
        # Every Phase 3 required field must itself be verified; the overall label alone (for
        # example "partial") is never trusted.
        if any(
            (fields.get(name) or {}).get("status") != "verified" for name in INSTRUMENT_REQUIRED
        ):
            return {**unverified, "reason": "a required instrument field is not verified"}
        unit = account_ccy.get("unit") or {}
        if account_ccy.get("status") != "verified" or unit.get("confirmation") != "consistent":
            return {**unverified, "reason": "account currency unit not confirmed"}

        def value(name: str) -> Any:
            report = fields.get(name) or {}
            return report.get("value") if report.get("status") == "verified" else None

        spec = {
            "status": "verified",
            "contract_size": value("contract_size"),
            "digits": value("digits"),
            "volume_min": value("volume_min"),
            "volume_max": value("volume_max"),
            "volume_step": value("volume_step"),
            "currency_base": value("currency_base"),
            "currency_profit": value("currency_profit"),
            "account_currency": account_ccy.get("value"),
            "account_major": unit.get("major"),
            "account_units_per_major": 100 if unit.get("kind") == "cent" else 1,
        }
        if any(v is None for v in spec.values()):
            return {**unverified, "reason": "instrument specification incomplete"}
        spec["retrieved_at_utc"] = payload.get("retrieved_at_utc")
        return spec

    # ================================================================== parsing
    def _parse_fill(self, data: Any, kind: str, now: datetime, spec: Mapping[str, Any]) -> dict:
        if not isinstance(data, Mapping):
            raise _bad("invalid_fill", f"{kind} fill must be an object")
        price = _number(data.get("price"), "price", positive=True)
        volume = _number(data.get("volume"), "volume", positive=True)
        if spec.get("status") == "verified":
            v, lo, hi, step = (
                _dec(volume),
                _dec(spec["volume_min"]),
                _dec(spec["volume_max"]),
                _dec(spec["volume_step"]),
            )
            if v < lo or v > hi:
                raise _bad(
                    "invalid_volume",
                    f"volume must be between {spec['volume_min']} and {spec['volume_max']} lots",
                )
            if ((v - lo) / step) % 1 != 0:
                raise _bad("invalid_volume", f"volume must be a multiple of {spec['volume_step']}")
            try:
                if -_dec(price).as_tuple().exponent > int(spec["digits"]):
                    raise _bad("invalid_price", f"price has more than {spec['digits']} decimals")
            except InvalidOperation:
                raise _bad("invalid_price", "price is not a valid number") from None
        fill: dict[str, Any] = {
            "kind": kind,
            "time_utc": utc_iso(_time(data.get("time_utc"), "time_utc", now)),
            "price": price,
            "volume": volume,
        }
        for cost in COSTS:
            amount = _optional_number(data, cost)
            fill[cost] = amount
            fill[f"{cost}_source"] = "unknown" if amount is None else "user_entered"
        if kind == "exit":
            pnl = _optional_number(data, "realised_pnl")
            fill["realised_pnl_user"] = pnl
            fill["realised_pnl_user_source"] = "unknown" if pnl is None else "user_entered"
        else:
            if data.get("realised_pnl") is not None:
                raise _bad("invalid_fill", "realised_pnl is only recorded on exit fills")
            fill["realised_pnl_user"] = None
            fill["realised_pnl_user_source"] = None
        return fill

    @staticmethod
    def _insert_fill(conn, trade_id: str, fill: dict, now, corrects: str | None = None) -> str:
        fill_id = new_ulid(now)
        conn.execute(
            "INSERT INTO trade_fill(id, trade_id, kind, time_utc, price, volume, commission, "
            "commission_source, swap, swap_source, fee, fee_source, realised_pnl_user, "
            "realised_pnl_user_source, execution_source, status, corrects_fill_id, created_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'manual','RECORDED',?,?)",
            (
                fill_id,
                trade_id,
                fill["kind"],
                fill["time_utc"],
                fill["price"],
                fill["volume"],
                fill["commission"],
                fill["commission_source"],
                fill["swap"],
                fill["swap_source"],
                fill["fee"],
                fill["fee_source"],
                fill["realised_pnl_user"],
                fill["realised_pnl_user_source"],
                corrects,
                utc_iso(now),
            ),
        )
        return fill_id

    @staticmethod
    def _check_timeline(conn, trade_id: str) -> None:
        """Cumulative exits may never exceed cumulative recorded entries at any time; at least
        one recorded entry must remain."""
        rows = conn.execute(
            "SELECT kind, volume FROM trade_fill WHERE trade_id=? AND status='RECORDED' "
            "ORDER BY time_utc, created_utc, id",
            (trade_id,),
        ).fetchall()
        if not any(r["kind"] == "entry" for r in rows):
            raise _bad(
                "no_entry", "a trade needs at least one recorded entry fill; void the trade instead"
            )
        open_volume = Decimal(0)
        for row in rows:
            if row["kind"] == "entry":
                open_volume += _dec(row["volume"])
            else:
                open_volume -= _dec(row["volume"])
                if open_volume < -_EPS:
                    raise _bad(
                        "exit_exceeds_entries",
                        "cumulative exits would exceed the entries recorded before them",
                    )

    @staticmethod
    def _protection_values(data: Mapping[str, Any]) -> tuple[float | None, float | None]:
        sl = (
            None
            if data.get("stop_loss") is None
            else _number(data["stop_loss"], "stop_loss", positive=True)
        )
        tp = (
            None
            if data.get("take_profit") is None
            else _number(data["take_profit"], "take_profit", positive=True)
        )
        return sl, tp

    @staticmethod
    def _check_order(direction: str, sl: float | None, tp: float | None) -> None:
        if sl is not None and tp is not None:
            if (direction == "BUY" and not sl < tp) or (direction == "SELL" and not sl > tp):
                raise _bad("invalid_protection", "stop-loss and take-profit are on the wrong sides")

    def _trade_row(self, conn, trade_id: str, *, writable: bool = True):
        row = conn.execute("SELECT * FROM trade WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            raise LogbookError(404, "not_found", "trade not found")
        if writable and row["voided_utc"] is not None:
            raise LogbookError(409, "trade_voided", "the trade is voided")
        return row

    # ================================================================== write operations
    def create_trade(self, payload: Any) -> tuple[int, dict]:
        self._precheck(payload)
        canonical, broker = resolve_symbol(payload.get("symbol"))
        spec = self.instrument_spec(canonical)  # read-only; done before taking the write lock

        def action(conn, now, body):
            direction = str(body.get("direction") or "").upper()
            if direction not in ("BUY", "SELL"):
                raise _bad("invalid_direction", "direction must be BUY or SELL")
            entry = self._parse_fill(body.get("entry"), "entry", now, spec)
            sl, tp = self._protection_values(body)
            price = entry["price"]
            if direction == "BUY" and (
                (sl is not None and sl >= price) or (tp is not None and tp <= price)
            ):
                raise _bad(
                    "invalid_protection",
                    "a BUY needs its stop-loss below and take-profit above the entry",
                )
            if direction == "SELL" and (
                (sl is not None and sl <= price) or (tp is not None and tp >= price)
            ):
                raise _bad(
                    "invalid_protection",
                    "a SELL needs its stop-loss above and take-profit below the entry",
                )
            tag = _text(body.get("strategy_tag"), "strategy_tag", required=False, limit=100)
            note = _text(body.get("note"), "note", required=False, limit=4000)
            trade_id = new_ulid(now)
            conn.execute(
                "INSERT INTO trade(id, canonical_symbol, broker_symbol, direction, "
                "execution_source, strategy_tag, instrument_spec, created_utc) "
                "VALUES (?,?,?,?,'manual',?,?,?)",
                (trade_id, canonical, broker, direction, tag, canonical_json(spec), utc_iso(now)),
            )
            fill_id = self._insert_fill(conn, trade_id, entry, now)
            protection_id = new_ulid(now)
            conn.execute(
                "INSERT INTO trade_protection(id, trade_id, sl, tp, effective_utc, source, reason, "
                "recorded_utc) VALUES (?,?,?,?,?,'manual','initial',?)",
                (protection_id, trade_id, sl, tp, entry["time_utc"], utc_iso(now)),
            )
            self._audit(
                conn,
                "trade.recorded",
                "trade",
                trade_id,
                {
                    "symbol": broker,
                    "direction": direction,
                    "source": "manual",
                    "specification": spec["status"],
                },
                now,
            )
            self._audit(
                conn,
                "fill.recorded",
                "trade_fill",
                fill_id,
                {"trade_id": trade_id, "kind": "entry"},
                now,
            )
            self._audit(
                conn,
                "protection.recorded",
                "trade_protection",
                protection_id,
                {"trade_id": trade_id, "sl": sl, "tp": tp},
                now,
            )
            if note:
                note_id = new_ulid(now)
                conn.execute(
                    "INSERT INTO trade_note(id, trade_id, text, created_utc) VALUES (?,?,?,?)",
                    (note_id, trade_id, note, utc_iso(now)),
                )
                self._audit(
                    conn, "note.recorded", "trade_note", note_id, {"trade_id": trade_id}, now
                )
            if body.get("decision_id") is not None:
                self._link(conn, trade_id, canonical, body, now)
            return 201, {"trade_id": trade_id, "fill_id": fill_id}

        return self._write("POST /api/trades", payload, action)

    def add_fill(self, trade_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            trade = self._trade_row(conn, trade_id)
            kind = body.get("kind")
            if kind not in ("entry", "exit"):
                raise _bad("invalid_fill", "kind must be entry or exit")
            fill = self._parse_fill(body, kind, now, json.loads(trade["instrument_spec"]))
            fill_id = self._insert_fill(conn, trade_id, fill, now)
            self._check_timeline(conn, trade_id)
            self._audit(
                conn,
                "fill.recorded",
                "trade_fill",
                fill_id,
                {"trade_id": trade_id, "kind": kind},
                now,
            )
            return 201, {"trade_id": trade_id, "fill_id": fill_id}

        return self._write(f"POST /api/trades/{trade_id}/fills", payload, action)

    def void_fill(self, trade_id: str, fill_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            trade = self._trade_row(conn, trade_id)
            reason = _text(body.get("reason"), "reason", required=True, limit=500)
            fill = conn.execute(
                "SELECT * FROM trade_fill WHERE id=? AND trade_id=?", (fill_id, trade_id)
            ).fetchone()
            if fill is None:
                raise LogbookError(404, "not_found", "fill not found")
            if fill["status"] != "RECORDED":
                raise LogbookError(409, "fill_voided", "the fill is already voided")
            conn.execute(
                "UPDATE trade_fill SET status='VOIDED', voided_reason=?, voided_utc=? WHERE id=?",
                (reason, utc_iso(now), fill_id),
            )
            self._audit(
                conn,
                "fill.voided",
                "trade_fill",
                fill_id,
                {"trade_id": trade_id, "reason": reason},
                now,
            )
            replacement_id = None
            if body.get("replacement") is not None:
                data = body["replacement"]
                kind = data.get("kind", fill["kind"]) if isinstance(data, Mapping) else None
                if kind not in ("entry", "exit"):
                    raise _bad("invalid_fill", "replacement kind must be entry or exit")
                parsed = self._parse_fill(data, kind, now, json.loads(trade["instrument_spec"]))
                replacement_id = self._insert_fill(conn, trade_id, parsed, now, corrects=fill_id)
                self._audit(
                    conn,
                    "fill.recorded",
                    "trade_fill",
                    replacement_id,
                    {"trade_id": trade_id, "kind": kind, "corrects_fill_id": fill_id},
                    now,
                )
            self._check_timeline(conn, trade_id)
            return 200, {
                "trade_id": trade_id,
                "voided_fill_id": fill_id,
                "replacement_fill_id": replacement_id,
            }

        return self._write(f"POST /api/trades/{trade_id}/fills/{fill_id}/void", payload, action)

    def void_trade(self, trade_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            self._trade_row(conn, trade_id)
            reason = _text(body.get("reason"), "reason", required=True, limit=500)
            conn.execute(
                "UPDATE trade SET voided_utc=?, voided_reason=? WHERE id=?",
                (utc_iso(now), reason, trade_id),
            )
            self._audit(conn, "trade.voided", "trade", trade_id, {"reason": reason}, now)
            return 200, {"trade_id": trade_id, "status": "VOIDED"}

        return self._write(f"POST /api/trades/{trade_id}/void", payload, action)

    def add_protection(self, trade_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            trade = self._trade_row(conn, trade_id)
            sl, tp = self._protection_values(body)
            self._check_order(trade["direction"], sl, tp)
            effective = utc_iso(_time(body.get("effective_utc"), "effective_utc", now))
            reason = _text(body.get("reason"), "reason", required=False, limit=500)
            protection_id = new_ulid(now)
            conn.execute(
                "INSERT INTO trade_protection(id, trade_id, sl, tp, effective_utc, source, reason, "
                "recorded_utc) VALUES (?,?,?,?,?,'manual',?,?)",
                (protection_id, trade_id, sl, tp, effective, reason, utc_iso(now)),
            )
            self._audit(
                conn,
                "protection.recorded",
                "trade_protection",
                protection_id,
                {"trade_id": trade_id, "sl": sl, "tp": tp},
                now,
            )
            return 201, {"trade_id": trade_id, "protection_id": protection_id}

        return self._write(f"POST /api/trades/{trade_id}/protection", payload, action)

    def add_note(self, trade_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            self._trade_row(conn, trade_id)
            text = _text(body.get("text"), "text", required=True, limit=4000)
            note_id = new_ulid(now)
            conn.execute(
                "INSERT INTO trade_note(id, trade_id, text, created_utc) VALUES (?,?,?,?)",
                (note_id, trade_id, text, utc_iso(now)),
            )
            self._audit(conn, "note.recorded", "trade_note", note_id, {"trade_id": trade_id}, now)
            return 201, {"trade_id": trade_id, "note_id": note_id}

        return self._write(f"POST /api/trades/{trade_id}/notes", payload, action)

    def add_link(self, trade_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            trade = self._trade_row(conn, trade_id)
            link_id = self._link(conn, trade_id, trade["canonical_symbol"], body, now)
            return 201, {"trade_id": trade_id, "link_id": link_id}

        return self._write(f"POST /api/trades/{trade_id}/links", payload, action)

    def _link(self, conn, trade_id: str, canonical: str, body: Mapping[str, Any], now) -> str:
        decision_id = body.get("decision_id")
        if not isinstance(decision_id, str):
            raise _bad("invalid_link", "decision_id is required")
        decision = conn.execute(
            "SELECT id, symbol FROM decision WHERE id=?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise LogbookError(404, "not_found", "decision not found")
        if decision["symbol"] != canonical:
            raise _bad("invalid_link", "the decision is for a different symbol")
        assessment_id = body.get("risk_assessment_id")
        if assessment_id is not None:
            row = conn.execute(
                "SELECT decision_id FROM risk_assessment WHERE id=?", (assessment_id,)
            ).fetchone()
            if row is None or row["decision_id"] != decision_id:
                raise _bad("invalid_link", "the risk assessment does not belong to the decision")
        exists = conn.execute(
            "SELECT 1 FROM trade_decision_link WHERE trade_id=? AND decision_id=? "
            "AND status='ACTIVE'",
            (trade_id, decision_id),
        ).fetchone()
        if exists:
            raise LogbookError(
                409, "already_linked", "the trade is already linked to this decision"
            )
        link_id = new_ulid(now)
        conn.execute(
            "INSERT INTO trade_decision_link(id, trade_id, decision_id, risk_assessment_id, "
            "status, created_utc) VALUES (?,?,?,?,'ACTIVE',?)",
            (link_id, trade_id, decision_id, assessment_id, utc_iso(now)),
        )
        self._audit(
            conn,
            "link.recorded",
            "trade_decision_link",
            link_id,
            {"trade_id": trade_id, "decision_id": decision_id, "risk_assessment_id": assessment_id},
            now,
        )
        return link_id

    def void_link(self, trade_id: str, link_id: str, payload: Any) -> tuple[int, dict]:
        def action(conn, now, body):
            self._trade_row(conn, trade_id)
            reason = _text(body.get("reason"), "reason", required=True, limit=500)
            link = conn.execute(
                "SELECT status FROM trade_decision_link WHERE id=? AND trade_id=?",
                (link_id, trade_id),
            ).fetchone()
            if link is None:
                raise LogbookError(404, "not_found", "link not found")
            if link["status"] != "ACTIVE":
                raise LogbookError(409, "link_voided", "the link is already voided")
            conn.execute(
                "UPDATE trade_decision_link SET status='VOIDED', voided_reason=?, "
                "voided_utc=? WHERE id=?",
                (reason, utc_iso(now), link_id),
            )
            self._audit(
                conn,
                "link.voided",
                "trade_decision_link",
                link_id,
                {"trade_id": trade_id, "reason": reason},
                now,
            )
            return 200, {"trade_id": trade_id, "voided_link_id": link_id}

        return self._write(f"POST /api/trades/{trade_id}/links/{link_id}/void", payload, action)

    # ================================================================== accounting
    @staticmethod
    def accounting(
        trade: Mapping[str, Any],
        fills: list[Mapping[str, Any]],
        quote: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deterministic P&L for one trade from its recorded fills (voided fills ignored)."""
        spec = (
            json.loads(trade["instrument_spec"])
            if isinstance(trade["instrument_spec"], str)
            else trade["instrument_spec"]
        )
        sign = 1.0 if trade["direction"] == "BUY" else -1.0
        recorded = sorted(
            (f for f in fills if f["status"] == "RECORDED"),
            key=lambda f: (f["time_utc"], f["created_utc"], f["id"]),
        )
        verified = spec.get("status") == "verified"
        factor = spec.get("account_units_per_major") if verified else None
        contract = spec.get("contract_size") if verified else None
        exact_ccy = verified and spec.get("currency_profit") == spec.get("account_major")
        via_price = verified and spec.get("currency_base") == spec.get("account_major")

        def to_account(points_value: float, at_price: float) -> tuple[float | None, str]:
            if not verified:
                return None, "unavailable: instrument specification unverified"
            if exact_ccy:
                return points_value * contract * factor, "calculated_exact"
            if via_price:
                return (
                    points_value * contract * factor / at_price,
                    "calculated_estimate: converted at the exit price "
                    "(historical rate unavailable)",
                )
            return None, "unavailable: no conversion path to the account currency"

        open_volume, wap = 0.0, 0.0
        entry_volume = exit_volume = 0.0
        entry_value = exit_value = 0.0
        exits: list[dict[str, Any]] = []
        for fill in recorded:
            volume, price = float(fill["volume"]), float(fill["price"])
            if fill["kind"] == "entry":
                wap = (wap * open_volume + price * volume) / (open_volume + volume)
                open_volume += volume
                entry_volume += volume
                entry_value += price * volume
                continue
            gross, method = to_account((price - wap) * sign * volume, price)
            exits.append(
                {
                    "fill_id": fill["id"],
                    "time_utc": fill["time_utc"],
                    "volume": volume,
                    "exit_price": price,
                    "average_entry_price": round(wap, 8),
                    "gross_calculated": _rounded(gross),
                    "gross_calculated_method": method,
                    "gross_user_entered": _rounded(fill["realised_pnl_user"]),
                    "gross_user_entered_source": fill["realised_pnl_user_source"],
                }
            )
            open_volume = max(0.0, open_volume - volume)
            exit_volume += volume
            exit_value += price * volume
        if not exits:
            gross_calc, calc_method = 0.0, "no exits"
        elif all(e["gross_calculated"] is not None for e in exits):
            gross_calc = sum(e["gross_calculated"] for e in exits)
            calc_method = (
                "calculated_exact"
                if all(e["gross_calculated_method"] == "calculated_exact" for e in exits)
                else "calculated_estimate"
            )
        else:
            gross_calc, calc_method = None, exits[0]["gross_calculated_method"]
        user_values = [e["gross_user_entered"] for e in exits]
        gross_user = sum(user_values) if exits and all(v is not None for v in user_values) else None
        costs: dict[str, Any] = {}
        for cost in COSTS:
            known = [float(f[cost]) for f in recorded if f[f"{cost}_source"] == "user_entered"]
            unknown = sum(1 for f in recorded if f[f"{cost}_source"] == "unknown")
            costs[cost] = {
                "known_total": round(sum(known), 2),
                "unknown_fills": unknown,
                "complete": unknown == 0,
            }
        costs_complete = all(c["complete"] for c in costs.values())
        if gross_calc is not None and costs_complete:
            net = round(gross_calc + sum(c["known_total"] for c in costs.values()), 2)
            net_status = calc_method if exits else "no exits"
        else:
            net = None
            net_status = (
                "incomplete: unknown costs"
                if not costs_complete
                else "unavailable: calculated gross unavailable"
            )
        unrealised = {"value": None, "status": "not applicable: no open volume"}
        if open_volume > 1e-9:
            unrealised = TradeLogbook._unrealised(quote, sign, wap, open_volume, to_account)
        return {
            "entry_volume": round(entry_volume, 8),
            "exit_volume": round(exit_volume, 8),
            "open_volume": round(open_volume, 8),
            "average_entry_price": round(entry_value / entry_volume, 8) if entry_volume else None,
            "open_average_entry_price": round(wap, 8) if open_volume > 1e-9 else None,
            "average_exit_price": round(exit_value / exit_volume, 8) if exit_volume else None,
            "exits": exits,
            "gross_realised_calculated": _rounded(gross_calc),
            "gross_realised_calculated_method": calc_method,
            "gross_realised_user_entered": _rounded(gross_user),
            "costs": costs,
            "costs_complete": costs_complete,
            "net_realised": net,
            "net_realised_status": net_status,
            "unrealised": unrealised,
            "currency": spec.get("account_currency") if verified else None,
            "convention": "costs are signed as in MT5 (negative = cost)",
        }

    @staticmethod
    def _unrealised(quote, sign, wap, open_volume, to_account) -> dict[str, Any]:
        if quote is None:
            return {"value": None, "status": "unavailable: no live quote requested"}
        if not quote:
            return {"value": None, "status": "unavailable: no live quote available"}
        if quote.get("freshness") != "LIVE" or quote.get("status") != "LIVE":
            return {"value": None, "status": "unavailable: quote is not LIVE"}
        age = quote.get("quote_age_seconds")
        if not isinstance(age, int | float) or age > MAX_QUOTE_AGE_SECONDS:
            return {
                "value": None,
                "status": f"unavailable: quote older than {MAX_QUOTE_AGE_SECONDS:g} s",
            }
        price = quote.get("bid") if sign > 0 else quote.get("ask")  # the closing side
        if not isinstance(price, int | float) or price <= 0:
            return {"value": None, "status": "unavailable: no closing-side price"}
        value, method = to_account((price - wap) * sign * open_volume, price)
        if value is None:
            return {"value": None, "status": method}
        return {
            "value": round(value, 2),
            "status": "estimate from live quote, not broker-verified",
            "price": price,
            "quote_age_seconds": age,
        }

    # ================================================================== reads
    def _quotes(self) -> dict[str, dict]:
        if self.account is None:
            return {}
        try:
            payload = self.account.instruments()
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for item in payload.get("instruments") or []:
            q = item.get("quote") or {}
            bid, ask = (q.get("bid") or {}), (q.get("ask") or {})
            out[item.get("symbol")] = {
                "status": payload.get("status"),
                "freshness": q.get("freshness"),
                "quote_age_seconds": q.get("quote_age_seconds"),
                "bid": bid.get("value") if bid.get("status") == "verified" else None,
                "ask": ask.get("value") if ask.get("status") == "verified" else None,
            }
        return out

    _STATUS_SQL = """
        CASE WHEN t.voided_utc IS NOT NULL THEN 'VOIDED'
             WHEN ROUND(COALESCE((SELECT SUM(volume) FROM trade_fill f WHERE f.trade_id = t.id
                  AND f.status = 'RECORDED' AND f.kind = 'exit'), 0), 8) = 0 THEN 'OPEN'
             WHEN ROUND((SELECT SUM(volume) FROM trade_fill f WHERE f.trade_id = t.id
                  AND f.status = 'RECORDED' AND f.kind = 'exit'), 8)
                = ROUND((SELECT SUM(volume) FROM trade_fill f WHERE f.trade_id = t.id
                  AND f.status = 'RECORDED' AND f.kind = 'entry'), 8) THEN 'CLOSED'
             ELSE 'PARTIALLY_CLOSED' END
    """

    def _summary(self, conn, trade, quotes: Mapping[str, dict] | None) -> dict[str, Any]:
        fills = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trade_fill WHERE trade_id=? ORDER BY time_utc, created_utc, id",
                (trade["id"],),
            )
        ]
        protection = conn.execute(
            "SELECT sl, tp, effective_utc FROM trade_protection WHERE trade_id=? "
            "ORDER BY effective_utc DESC, recorded_utc DESC LIMIT 1",
            (trade["id"],),
        ).fetchone()
        sl = protection["sl"] if protection else None
        tp = protection["tp"] if protection else None
        state = (
            "COMPLETE"
            if sl is not None and tp is not None
            else ("MISSING" if sl is None and tp is None else "PARTIAL")
        )
        # None: no quote requested; {}: requested, but no live quote is available.
        quote = None if quotes is None else quotes.get(trade["canonical_symbol"], {})
        accounting = self.accounting(trade, fills, quote)
        spec = json.loads(trade["instrument_spec"])
        return {
            "id": trade["id"],
            "symbol": trade["broker_symbol"],
            "canonical_symbol": trade["canonical_symbol"],
            "direction": trade["direction"],
            "execution_source": trade["execution_source"],
            "status": trade["status"],
            "verification": VERIFICATION,
            "strategy_tag": trade["strategy_tag"],
            "created_utc": trade["created_utc"],
            "voided_utc": trade["voided_utc"],
            "voided_reason": trade["voided_reason"],
            "protection": {
                "state": state,
                "stop_loss": sl,
                "take_profit": tp,
                "effective_utc": protection["effective_utc"] if protection else None,
            },
            "specification": spec.get("status"),
            "accounting": accounting,
            "first_entry_utc": next(
                (
                    f["time_utc"]
                    for f in fills
                    if f["kind"] == "entry" and f["status"] == "RECORDED"
                ),
                None,
            ),
        }

    def list(
        self,
        *,
        status: str | None = None,
        symbol: str | None = None,
        source: str | None = None,
        cursor: int | None = None,
        limit: int = 50,
        unrealised: bool = False,
    ) -> dict[str, Any]:
        clauses, params = [], []
        if status is not None:
            clauses.append(f"({self._STATUS_SQL}) = ?")
            params.append(status)
        if symbol is not None:
            clauses.append("t.canonical_symbol = ?")
            params.append(resolve_symbol(symbol)[0])
        if source is not None:
            clauses.append("t.execution_source = ?")
            params.append(source)
        if cursor is not None:
            clauses.append("t.rowid < ?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100))
        quotes = self._quotes() if unrealised else None
        with self.store.read() as conn:
            rows = conn.execute(
                f"SELECT t.*, ({self._STATUS_SQL}) AS status, t.rowid AS cursor_key FROM trade t "
                f"{where} ORDER BY t.rowid DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            items = [self._summary(conn, row, quotes) for row in rows[:limit]]
        next_cursor = str(rows[limit - 1]["cursor_key"]) if len(rows) > limit else None
        return {"banner": BANNER, "trades": items, "next_cursor": next_cursor}

    def get(self, trade_id: str, *, unrealised: bool = True) -> dict[str, Any] | None:
        quotes = self._quotes() if unrealised else None
        with self.store.read() as conn:
            row = conn.execute(
                f"SELECT t.*, ({self._STATUS_SQL}) AS status FROM trade t WHERE t.id=?", (trade_id,)
            ).fetchone()
            if row is None:
                return None
            trade = self._summary(conn, row, quotes)
            fills = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, kind, time_utc, price, volume, commission, commission_source, "
                    "swap, swap_source, fee, fee_source, realised_pnl_user, "
                    "realised_pnl_user_source, execution_source, status, voided_reason, "
                    "voided_utc, corrects_fill_id, created_utc "
                    "FROM trade_fill WHERE trade_id=? ORDER BY time_utc, created_utc, id",
                    (trade_id,),
                )
            ]
            protection = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, sl, tp, effective_utc, source, reason, recorded_utc "
                    "FROM trade_protection WHERE trade_id=? ORDER BY effective_utc, recorded_utc",
                    (trade_id,),
                )
            ]
            notes = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, text, created_utc FROM trade_note WHERE trade_id=? "
                    "ORDER BY created_utc",
                    (trade_id,),
                )
            ]
            links = [
                dict(r)
                for r in conn.execute(
                    "SELECT l.id, l.decision_id, l.risk_assessment_id, l.status, l.voided_reason, "
                    "l.voided_utc, l.created_utc, d.symbol, d.timeframe, d.analysis_outcome, "
                    "d.t6_rule, d.market_direction, d.as_of_utc FROM trade_decision_link l "
                    "JOIN decision d ON d.id = l.decision_id WHERE l.trade_id=? "
                    "ORDER BY l.created_utc",
                    (trade_id,),
                )
            ]
            ids = [
                trade_id,
                *(f["id"] for f in fills),
                *(p["id"] for p in protection),
                *(n["id"] for n in notes),
                *(lk["id"] for lk in links),
            ]
            marks = ",".join("?" * len(ids))
            audit = [
                dict(r)
                for r in conn.execute(
                    "SELECT seq, recorded_utc, actor, action, table_name, record_id "
                    f"FROM audit_log WHERE record_id IN ({marks}) ORDER BY seq",
                    ids,
                )
            ]
        trade.update(
            fills=fills, protection_history=protection, notes=notes, links=links, audit=audit
        )
        return {"banner": BANNER, "trade": trade}
