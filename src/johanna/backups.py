"""M-7 journal backup management (infrastructure only; **no schedule, nothing automatic**).

Policy
------
- **Pre-migration pairs** (``premigration-vN``) are recovery points made by the journal before
  a migration. They are never pruned by this module.
- **Routine pairs** (``daily``) are made only by an explicit ``create_routine()`` call. There
  is no timer or thread: ``BackupPolicy.enabled`` defaults to False and nothing in Johanna
  calls ``create_routine`` (a schedule needs a separate approval).
- Every pair is built by ``JournalStore.backup`` (SQLite online backup API: consistent and
  WAL-safe), verified (integrity, audit chain, key pairing, file hashes) and published by one
  directory rename, so a failed or interrupted backup never replaces or damages an existing
  pair. ``validate_pair`` decides what counts as a backup.
- **Retention** (``retention_plan``) only ever selects *valid routine* pairs beyond
  ``keep_daily`` and never the newest valid pair of any kind, so it cannot remove the only
  usable recovery point. Invalid or incomplete directories are reported for manual review,
  never deleted. ``prune`` is a dry run unless ``apply=True`` and re-validates the pairs it
  keeps immediately before deleting anything.
- **Disk space**: a routine backup needs roughly one more copy of the journal (database and
  WAL); ``create_routine`` refuses with ``insufficient_space`` when free space is below
  ``space_factor`` times that size plus ``min_free_bytes``.
- **Restore rehearsal** copies a pair into a *separate* empty directory (never the journal's
  own directory) and opens it there read-only-equivalently (a journal limited to the pair's
  schema version, so nothing is migrated), verifying integrity, key and audit chain.

Every method returns a result dict and never raises into callers.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from johanna import journal_store as js

LOGGER = logging.getLogger("johanna.backups")


@dataclass(frozen=True)
class BackupPolicy:
    enabled: bool = False  # no automatic backups unless separately approved
    interval_hours: int = 24
    keep_daily: int = 7
    space_factor: float = 2.0
    min_free_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.keep_daily < 1:
            raise ValueError("keep_daily must be at least 1 (a recovery point is always kept)")
        if self.interval_hours < 1 or self.space_factor < 1 or self.min_free_bytes < 0:
            raise ValueError("invalid backup policy")


def _pair_info(path: Path) -> dict[str, Any] | None:
    match = js._PAIR_NAME.fullmatch(path.name)
    if match is None:
        return None
    return {"name": path.name, "kind": match[1], "stamp": match[2]}


def retention_plan(pairs: list[dict[str, Any]], keep_daily: int) -> dict[str, list[str]]:
    """Pure: which pairs a prune would delete. ``pairs`` items: name, kind, stamp, valid.

    Deletes only valid ``daily`` pairs older than the newest ``keep_daily`` valid daily
    pairs; never pre-migration pairs, never invalid pairs (manual review), and never the
    newest valid pair overall."""
    if keep_daily < 1:
        raise ValueError("keep_daily must be at least 1")
    valid = sorted((p for p in pairs if p["valid"]), key=lambda p: p["stamp"])
    newest = valid[-1]["name"] if valid else None
    daily = [p for p in valid if p["kind"] == "daily"]
    delete = [p["name"] for p in daily[:-keep_daily] if p["name"] != newest]
    return {
        "delete": delete,
        "keep": [p["name"] for p in valid if p["name"] not in delete],
        "manual_review": sorted(p["name"] for p in pairs if not p["valid"]),
    }


class BackupManager:
    name = "journal_backups"

    def __init__(
        self,
        store: Any,
        policy: BackupPolicy | None = None,
        *,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or BackupPolicy()
        self._disk_usage = disk_usage
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self.last_result: dict[str, Any] | None = None

    # ------------------------------------------------------------------ creation
    def required_space(self) -> int:
        db = self.store.data_dir / js.DB_NAME
        size = sum(p.stat().st_size for p in (db, Path(f"{db}-wal")) if p.exists())
        return int(size * self.policy.space_factor) + self.policy.min_free_bytes

    def create_routine(self) -> dict[str, Any]:
        """One explicit routine backup. Fails closed; never raises."""
        with self._lock:
            now = self._clock()
            result: dict[str, Any] = {"kind": "daily", "at_utc": js.utc_iso(now)}
            try:
                if not self.store.ready or not self.store.hmac_available:
                    raise js.JournalError("journal_not_ready_or_key_unverified")
                need = self.required_space()
                free = int(self._disk_usage(self.store.data_dir).free)
                if free < need:
                    raise js.JournalError("insufficient_space")
                pair = self.store.backup("daily")
                if not js.validate_pair(pair):  # defence in depth: re-verify what was published
                    raise js.JournalError("backup_unverified")
                result.update(status="created", pair=pair.name)
            except js.JournalError as exc:
                result.update(status="failed", reason=exc.code)
            except Exception as exc:  # never escapes; reported
                result.update(status="failed", reason=type(exc).__name__)
            if result["status"] == "failed":
                LOGGER.error("journal backup failed: %s", result["reason"])
            self.last_result = result
            return result

    # ------------------------------------------------------------------ inventory
    def inventory(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.store.data_dir.glob(f"{js.PAIR_PREFIX}*")):
            info = _pair_info(path) or {"name": path.name, "kind": "unknown", "stamp": ""}
            info["valid"] = js.validate_pair(path)
            items.append(info)
        return items

    def retention(self) -> dict[str, list[str]]:
        return retention_plan(self.inventory(), self.policy.keep_daily)

    def prune(self, *, apply: bool = False) -> dict[str, Any]:
        """Dry run unless ``apply``. Before deleting, every kept pair is re-validated; if any
        no longer validates, nothing is deleted."""
        with self._lock:
            plan = self.retention()
            if not apply or not plan["delete"]:
                return {"status": "dry_run" if not apply else "nothing_to_delete", **plan}
            for name in plan["keep"]:
                if not js.validate_pair(self.store.data_dir / name):
                    return {"status": "refused", "reason": "kept_pair_invalid", **plan}
            if not plan["keep"]:
                return {"status": "refused", "reason": "no_recovery_point_left", **plan}
            removed = []
            for name in plan["delete"]:
                shutil.rmtree(self.store.data_dir / name)
                removed.append(name)
            return {"status": "pruned", "removed": removed, **plan}

    # ------------------------------------------------------------------ restore rehearsal
    def restore_rehearsal(self, pair_name: str, target: Path) -> dict[str, Any]:
        """Copy a valid pair into an empty directory outside the journal's directory and open
        it there, limited to the pair's own schema version (nothing is migrated)."""
        source = self.store.data_dir / pair_name
        target = Path(target)
        try:
            data_dir = self.store.data_dir.resolve()
            resolved = target.resolve()
            if resolved == data_dir or data_dir in resolved.parents:
                return {"status": "refused", "reason": "target_inside_journal_directory"}
            if target.exists() and any(target.iterdir()):
                return {"status": "refused", "reason": "target_not_empty"}
            if not js.validate_pair(source):
                return {"status": "refused", "reason": "pair_invalid"}
            manifest = json.loads((source / js.PAIR_MANIFEST).read_text(encoding="utf-8"))
            version = int(manifest["schema_version"])
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / js.DB_NAME, target / js.DB_NAME)
            shutil.copy2(source / js.KEY_NAME, target / js.KEY_NAME)
            acl = getattr(self.store, "_acl", None) or js._default_acl
            acl(target / js.KEY_NAME, False)  # the copied key is user-only like the original
            rehearsal = js.JournalStore(
                target,
                migrations=js.MIGRATIONS[:version],
                acl=getattr(self.store, "_acl", None),
                acl_check=getattr(self.store, "_acl_check", None),
            )
            rehearsal.start()
            try:
                status = rehearsal.status()
                if status["state"] != "ready":
                    return {"status": "failed", "reason": status["reason"]}
                audit = rehearsal.verify_audit_chain()
                with contextlib.closing(sqlite3.connect(target / js.DB_NAME)) as conn:
                    tables = [
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' "
                            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                        )
                    ]
                    counts = {
                        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
                    }
                return {
                    "status": "ok"
                    if audit["valid"] and status["key_status"] == "verified"
                    else "failed",
                    "schema_version": version,
                    "key_status": status["key_status"],
                    "audit_valid": audit["valid"],
                    "row_counts": counts,
                }
            finally:
                rehearsal.stop()
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            return {"status": "failed", "reason": type(exc).__name__}

    # ------------------------------------------------------------------ diagnostics
    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "automatic_backups": "disabled" if not self.policy.enabled else "approved_schedule",
            "scheduled": False,  # no scheduler exists in this phase
            "policy": {
                "keep_daily": self.policy.keep_daily,
                "interval_hours": self.policy.interval_hours,
                "space_factor": self.policy.space_factor,
                "min_free_bytes": self.policy.min_free_bytes,
            },
            "last_result": self.last_result,
        }
