"""P6.R unattended-operation readiness (diagnostics only).

``readiness(app)`` aggregates the state that matters before any future unattended operation
could be considered. It **never enables anything**: unattended operation is reported as not
permitted, with every blocking reason and a pointer into ``docs/operations-runbook.md``.

Read-only by construction: no MT5 read (only cached service state), no journal write, no
backup creation. Journal checks open a separate read-only connection (``mode=ro``):
``PRAGMA quick_check``, ``foreign_key_check`` and the audit-chain verification. No paths,
server names, account identifiers or key material are included.

M-8: the two expensive checks reuse *verified evidence* (``ReadinessEvidence``), never a
previous verdict alone:

- Audit chain: a full verification is cached with its anchor (last sequence and row hash).
  Later requests re-read the anchor (it must be unchanged), require ``count == max(seq)``
  (no deleted or inserted rows before it) and verify every row appended since. A full
  verification runs again when the evidence is older than ``AUDIT_FULL_MAX_AGE``, the
  anchor changed, or anything does not match. Limitation (stated in the output): an edit of
  an older row that keeps its stored hash, which needs the append-only triggers dropped
  first, is detected by the next full verification, at most ``AUDIT_FULL_MAX_AGE`` later.
- Backup pairs: a pair's validation is reused only while the sizes and modification times
  of its three files are unchanged and for at most ``PAIR_MAX_AGE``; new, changed, missing
  or previously invalid pairs are validated in full. Only valid results are cached: an
  invalid state is re-checked on every request and can never become ``ok`` from cache.
- ``quick_check`` and ``foreign_key_check`` run on every request.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna import journal_store as js
from johanna.risk_engine import COST_TERMS

RUNBOOK = "docs/operations-runbook.md"
AUDIT_FULL_MAX_AGE = timedelta(minutes=15)
PAIR_MAX_AGE = timedelta(hours=6)
_REMEDIATION = {
    "journal_refused": f"{RUNBOOK}#journal-refused",
    "journal_integrity": f"{RUNBOOK}#journal-integrity-failure",
    "audit_chain": f"{RUNBOOK}#audit-chain-failure",
    "mt5_unavailable": f"{RUNBOOK}#mt5-unavailable",
    "scheduler_error": f"{RUNBOOK}#scheduler-error",
    "scheduler_disabled": f"{RUNBOOK}#scheduler-activation",
    "costs_unverified": f"{RUNBOOK}#cost-evidence",
    "monitoring_not_activated": f"{RUNBOOK}#monitoring-activation",
    "notifications_not_activated": f"{RUNBOOK}#notifications-lifecycle-and-failure-handling",
    "schema_not_current": f"{RUNBOOK}#schema-v6-migration",
    "execution_disabled": f"{RUNBOOK}#execution-boundary",
}


class ReadinessEvidence:
    """Verified evidence reused across readiness requests (M-8). Thread-safe: concurrent
    requests are serialised, so each sees evidence verified under the lock."""

    def __init__(self, clock=None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._audit: dict[str, Any] | None = None  # only VALID evidence is kept
        self._pairs: dict[str, tuple[Any, datetime]] = {}  # name -> (signature, validated at)

    def invalidate(self) -> None:
        with self._lock:
            self._audit, self._pairs = None, {}

    def audit(self, store: Any) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            with store.read() as conn:
                cached = self._audit
                if cached is not None and now - cached["full_at"] < AUDIT_FULL_MAX_AGE:
                    incremental = self._incremental(conn, cached, now)
                    if incremental is not None:
                        return incremental
                rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            result = js.verify_audit_rows(rows)
            self._audit = (
                {
                    "seq": rows[-1]["seq"] if rows else 0,
                    "row_hash": rows[-1]["row_hash"] if rows else js.GENESIS_HASH,
                    "rows": len(rows),
                    "full_at": now,
                }
                if result["valid"]
                else None
            )
            return {**result, "mode": "full", "evidence_age_seconds": 0.0}

    def _incremental(self, conn, cached: dict[str, Any], now: datetime) -> dict | None:
        count, top = conn.execute("SELECT count(*), max(seq) FROM audit_log").fetchone()
        top = top or 0
        if cached["seq"]:
            anchor = conn.execute(
                "SELECT row_hash FROM audit_log WHERE seq=?", (cached["seq"],)
            ).fetchone()
            if anchor is None or anchor[0] != cached["row_hash"]:
                return None  # changed: full verification
        if count != top or top < cached["seq"]:
            return None
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE seq > ? ORDER BY seq", (cached["seq"],)
        ).fetchall()
        part = js.verify_audit_rows(rows, start_seq=cached["seq"] + 1, previous=cached["row_hash"])
        if not part["valid"]:
            self._audit = None
            return {**part, "rows": count, "mode": "incremental", "evidence_age_seconds": None}
        if rows:
            cached.update(seq=rows[-1]["seq"], row_hash=rows[-1]["row_hash"], rows=count)
        return {
            "valid": True,
            "rows": count,
            "first_bad_seq": None,
            "problem": None,
            "guarantee": "tamper-evident only; no external anchor",
            "mode": "incremental",
            "verified_new_rows": len(rows),
            "evidence_age_seconds": round((now - cached["full_at"]).total_seconds(), 1),
            "next_full_within_seconds": AUDIT_FULL_MAX_AGE.total_seconds(),
        }

    @staticmethod
    def _signature(path) -> tuple | None:
        try:
            return tuple(
                (f.stat().st_size, f.stat().st_mtime_ns)
                for f in (path / js.PAIR_MANIFEST, path / js.DB_NAME, path / js.KEY_NAME)
            )
        except OSError:
            return None

    def valid_pairs(self, store: Any) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            valid, reused, validated = [], 0, 0
            paths = sorted(store.data_dir.glob(f"{js.PAIR_PREFIX}*"))
            for path in paths:
                signature = self._signature(path)
                cached = self._pairs.get(path.name)
                if (
                    signature is not None
                    and cached is not None
                    and cached[0] == signature
                    and now - cached[1] < PAIR_MAX_AGE
                ):
                    valid.append(path.name)
                    reused += 1
                    continue
                validated += 1
                self._pairs.pop(path.name, None)
                if signature is not None and js.validate_pair(path):
                    self._pairs[path.name] = (signature, now)
                    valid.append(path.name)
            names = {p.name for p in paths}
            for gone in [n for n in self._pairs if n not in names]:
                del self._pairs[gone]
            return {"valid": valid, "validated_now": validated, "reused": reused}


def _check(name: str, ok: bool, detail: Any, key: str | None = None) -> dict[str, Any]:
    return {
        "check": name,
        "status": "ok" if ok else "attention",
        "detail": detail,
        "remediation": _REMEDIATION.get(key) if not ok and key else None,
    }


def _journal_checks(
    store: Any, evidence: ReadinessEvidence | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence = evidence or ReadinessEvidence()
    status = store.status()
    summary = {
        "state": status["state"],
        "reason": status["reason"],
        "registered_schema_version": status["schema_version"],
        "key_status": status["key_status"],
        "installed_schema_version": None,
        "valid_backup_pairs": None,
        # Phase 2: v5 is registered; no schema is staged. Kept as a field for the dashboard.
        # P6.2b.5: v6 is registered; nothing is staged. Kept as a field for the dashboard.
        "staged_schema": "none staged (v6 registered; a v5 journal migrates at the next start "
        "after a verified backup)",
    }
    checks = [
        _check(
            "journal_ready",
            status["state"] == "ready",
            {"state": status["state"], "reason": status["reason"]},
            "journal_refused",
        )
    ]
    if status["state"] != "ready":
        return checks, summary
    db = store.data_dir / js.DB_NAME
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
            summary["installed_schema_version"] = c.execute("PRAGMA user_version").fetchone()[0]
            quick = c.execute("PRAGMA quick_check").fetchone()[0]
            fk = len(c.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.Error as exc:
        checks.append(_check("journal_integrity", False, type(exc).__name__, "journal_integrity"))
        return checks, summary
    checks.append(_check("journal_integrity", quick == "ok", quick, "journal_integrity"))
    checks.append(_check("foreign_keys", fk == 0, {"violations": fk}, "journal_integrity"))
    try:
        audit = evidence.audit(store)
    except (js.JournalError, sqlite3.Error) as exc:
        audit = {"valid": False, "rows": None, "first_bad_seq": None, "problem": type(exc).__name__}
    checks.append(
        _check(
            "audit_chain",
            audit["valid"] is True,
            {
                "valid": audit["valid"],
                "rows": audit["rows"],
                "first_bad_seq": audit["first_bad_seq"],
                "mode": audit.get("mode"),
                "evidence_age_seconds": audit.get("evidence_age_seconds"),
            },
            "audit_chain",
        )
    )
    checks.append(
        _check(
            "journal_key",
            status["key_status"] == "verified",
            {"key_status": status["key_status"]},
            "journal_refused",
        )
    )
    pairs = evidence.valid_pairs(store)
    summary["valid_backup_pairs"] = len(pairs["valid"])
    summary["backup_pair_evidence"] = {k: pairs[k] for k in ("validated_now", "reused")}
    summary["schema_matches_registration"] = (
        summary["installed_schema_version"] == status["schema_version"]
    )
    return checks, summary


def readiness(app: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    journal_checks, journal = _journal_checks(
        app.journal_store, getattr(app, "readiness_evidence", None)
    )
    market = app.live_market.status()
    mt5 = {
        "state": market["state"],
        "time_check": (market.get("time_check") or {}).get("result"),
        "last_success_utc": market.get("last_success_utc"),
        "unavailable_reason": market.get("unavailable_reason"),
        "access": "read_only",
    }
    shadow = app.shadow.status()
    scheduler = {
        "enabled": shadow["enabled"],
        "state": shadow["state"],
        "health": shadow["health"],
        "pending": (shadow.get("cycle_counts") or {}).get("PENDING"),
        "running": (shadow.get("cycle_counts") or {}).get("RUNNING"),
        "consecutive_errors": shadow.get("consecutive_errors"),
        "last_error": shadow.get("last_error"),
    }
    services = app.services.status() if getattr(app, "_started", False) else {}
    execution = services.get("broker_execution") or {}
    monitoring = app.monitoring.status()
    notifications = app.notifications.status()
    checks = [
        _check(
            "process",
            bool(getattr(app, "_started", False)) and app.instance_lock.held,
            {
                "started": bool(getattr(app, "_started", False)),
                "single_instance_lock": app.instance_lock.held,
            },
        ),
        *journal_checks,
        _check(
            "mt5_connection",
            mt5["state"] in ("connected", "idle"),
            {"state": mt5["state"], "reason": mt5["unavailable_reason"]},
            "mt5_unavailable",
        ),
        _check(
            "scheduler_health",
            scheduler["state"] != "error" and not scheduler["consecutive_errors"],
            {"state": scheduler["state"], "health": scheduler["health"]},
            "scheduler_error",
        ),
    ]
    blocking = []
    if not scheduler["enabled"]:
        blocking.append(("scheduler_disabled", "shadow scheduler is OFF (user decision)"))
    if not COST_TERMS.configured:
        blocking.append(("costs_unverified", "no approved commission, slippage or holding period"))
    if not monitoring["enabled"]:
        blocking.append(("monitoring_not_activated", "broker monitoring is not activated"))
    if not notifications["automatic_notifications"]:
        blocking.append(("notifications_not_activated", "no notification is sent automatically"))
    if journal.get("installed_schema_version") != journal.get("registered_schema_version"):
        blocking.append(
            ("schema_not_current", "the journal is not at the registered schema version")
        )
    blocking.append(("execution_disabled", "broker execution is disabled by design (manual only)"))
    failing = [c for c in checks if c["status"] != "ok"]
    return {
        "label": "OPERATIONS READINESS — DIAGNOSTIC ONLY; NOTHING IS ENABLED FROM HERE",
        "checked_at_utc": js.utc_iso(now),
        "manual_operation": "ok" if not failing else "attention",
        "unattended_operation_permitted": False,
        "unattended_blocking_reasons": [
            {"code": code, "reason": reason, "remediation": _REMEDIATION[code]}
            for code, reason in blocking
        ],
        "checks": checks,
        "journal": journal,
        "mt5": mt5,
        "scheduler": scheduler,
        "services": {
            "execution": {"state": execution.get("state", "disabled"), "enabled": False},
            "monitoring": {
                "enabled": monitoring["enabled"],
                "installed": monitoring["installed"],
                "last_error": monitoring["last_error"],
            },
            "notifications": {
                "installed": notifications["installed"],
                "external_delivery": notifications["external_delivery"],
                "automatic": notifications["automatic_notifications"],
            },
            "cost_terms_configured": COST_TERMS.configured,
        },
        "runbook": RUNBOOK,
    }
