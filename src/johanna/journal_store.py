"""Phase 5 trading-journal store (P5.1). See ``docs/phase-5-architecture.md`` §3, §5, §9.

A separate SQLite database, ``{data_dir}/trading_journal.sqlite3``, with its HMAC key
``{data_dir}/trading_journal.key``. Unlike ``ApplicationStore`` it **fails closed**: a corrupt or
unexpected database is refused, never quarantined or recreated, and a key is never regenerated.

Only these names are ever touched in ``data_dir``: the database (and SQLite's own ``-wal`` /
``-shm`` sidecars), the key, published backup-pair directories ``trading_journal.backup-*`` and
temporary backup directories ``.trading_journal.backup-tmp-*``. Nothing else is listed or read.

Audit chain (§9): every audit row is hashed together with the previous row's hash. The chain is
**tamper-evident, not tamper-proof**: there is no independent external anchor for the chain
head, so someone able to rewrite the database file can rewrite rows *and* recompute the chain.
It detects accidental or unauthorised edits of individual rows only.

Canonical audit hash (exact specification):

- Hashed fields, in this order: ``seq`` (integer), ``recorded_utc`` (UTC ISO-8601 with
  microseconds and ``Z``, e.g. ``2026-11-17T10:31:46.024241Z``), ``actor``, ``action``,
  ``table_name``, ``record_id`` (text or null), ``old_values`` and ``new_values`` (each a
  canonical JSON string or null), ``prev_hash`` (64 lowercase hex characters).
- Canonical JSON for values: ``json.dumps(value, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False, allow_nan=False)``.
- Row bytes: the nine fields as a JSON array, serialised with ``separators=(",", ":")``,
  ``ensure_ascii=False`` and ``allow_nan=False``, encoded as UTF-8.
- ``row_hash = SHA-256(row bytes)`` as lowercase hex. The genesis ``prev_hash`` is 64 zeros;
  every later row's ``prev_hash`` is the previous row's ``row_hash``.
- Verification walks rows by ``seq`` from 1: sequence numbers must be contiguous (detects
  deletion and reordering), each ``prev_hash`` must equal the previous ``row_hash`` and each
  ``row_hash`` must equal the recomputed hash (detects modification).

Insertion and verification use the same function (``audit_row_hash``) over the same stored field
values. Limitations: verification hashes the *stored* ``old_values``/``new_values`` text and does
not re-canonicalise it, so any byte change is detected, including a semantically equivalent
re-serialisation; non-finite numbers (NaN, ±Infinity) are rejected at insertion; floats are
serialised by Python's shortest round-trip ``repr``, so the chain is reproducible by any
verifier using the same rule; the chain cannot detect a full rewrite with recomputed hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("johanna.journal")

DB_NAME = "trading_journal.sqlite3"
KEY_NAME = "trading_journal.key"
PAIR_PREFIX = "trading_journal.backup-"
TMP_PREFIX = ".trading_journal.backup-tmp-"
PAIR_MANIFEST = "pair.json"
SIDECARS = ("-wal", "-shm", "-journal")
KEY_BYTES = 32
KEY_CHECK_MESSAGE = b"johanna-journal-key-check"
GENESIS_HASH = "0" * 64
DAILY_RETENTION = 7
_PAIR_NAME = re.compile(r"trading_journal\.backup-(daily|premigration-v\d+)-(\d{8}T\d{12}Z)")

# --------------------------------------------------------------------------- schema
# Forward-only migrations: (target user_version, SQL). Never edit a released migration.
SCHEMA_V1 = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE decision (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    broker_symbol TEXT,
    timeframe TEXT NOT NULL,
    as_of_utc TEXT,
    analysis_outcome TEXT NOT NULL
        CHECK (analysis_outcome IN ('TRADE_CANDIDATE', 'MARKET_CONDITION', 'NO_TRADE')),
    t6_rule TEXT,
    market_direction TEXT NOT NULL CHECK (market_direction IN ('BUY', 'SELL', 'NONE')),
    t6_evidence TEXT NOT NULL,
    analysis_fingerprint TEXT NOT NULL,
    snapshot_id TEXT,
    evaluated_at_utc TEXT,
    previous_decision_id TEXT REFERENCES decision(id),
    capture_source TEXT NOT NULL CHECK (capture_source IN ('analysis_request', 'risk_request')),
    recorded_utc TEXT NOT NULL,
    UNIQUE (symbol, timeframe, analysis_fingerprint, previous_decision_id)
);
CREATE INDEX decision_key ON decision (symbol, timeframe);
CREATE TABLE decision_observation (
    decision_id TEXT PRIMARY KEY REFERENCES decision(id),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    observations INTEGER NOT NULL CHECK (observations >= 1)
);
CREATE TABLE risk_assessment (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision(id),
    risk_status TEXT NOT NULL
        CHECK (risk_status IN ('APPROVED', 'REJECTED', 'UNVERIFIED', 'NOT_EVALUATED')),
    phase4_status_raw TEXT,
    risk_evidence TEXT NOT NULL,
    risk_fingerprint TEXT NOT NULL,
    evaluated_at_utc TEXT,
    previous_assessment_id TEXT REFERENCES risk_assessment(id),
    recorded_utc TEXT NOT NULL,
    UNIQUE (decision_id, risk_fingerprint, previous_assessment_id)
);
CREATE INDEX risk_by_decision ON risk_assessment (decision_id);
CREATE TABLE risk_assessment_observation (
    assessment_id TEXT PRIMARY KEY REFERENCES risk_assessment(id),
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    observations INTEGER NOT NULL CHECK (observations >= 1)
);
CREATE TABLE event (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    subject_ref TEXT,
    dedup_key TEXT NOT NULL UNIQUE,
    severity TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_utc TEXT NOT NULL,
    recorded_utc TEXT NOT NULL,
    recovered INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0, 1))
);
CREATE TABLE audit_log (
    seq INTEGER PRIMARY KEY,
    recorded_utc TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    table_name TEXT NOT NULL,
    record_id TEXT,
    old_values TEXT,
    new_values TEXT,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);

-- Immutable evidence, append-only events, audit and meta: UPDATE and DELETE abort.
CREATE TRIGGER meta_no_update BEFORE UPDATE ON meta
BEGIN SELECT RAISE(ABORT, 'meta is insert-only'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON meta
BEGIN SELECT RAISE(ABORT, 'meta is insert-only'); END;
CREATE TRIGGER decision_no_update BEFORE UPDATE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;
CREATE TRIGGER decision_no_delete BEFORE DELETE ON decision
BEGIN SELECT RAISE(ABORT, 'decision evidence is immutable'); END;
CREATE TRIGGER risk_no_update BEFORE UPDATE ON risk_assessment
BEGIN SELECT RAISE(ABORT, 'risk assessment evidence is immutable'); END;
CREATE TRIGGER risk_no_delete BEFORE DELETE ON risk_assessment
BEGIN SELECT RAISE(ABORT, 'risk assessment evidence is immutable'); END;
CREATE TRIGGER event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

-- Observation metadata: only last_seen_utc and a growing observation count may change.
CREATE TRIGGER decision_obs_guard BEFORE UPDATE ON decision_observation
WHEN NEW.decision_id IS NOT OLD.decision_id
  OR NEW.first_seen_utc IS NOT OLD.first_seen_utc
  OR NEW.observations < OLD.observations
  OR NEW.last_seen_utc < OLD.last_seen_utc
BEGIN SELECT RAISE(ABORT, 'observation metadata may only advance'); END;
CREATE TRIGGER decision_obs_no_delete BEFORE DELETE ON decision_observation
BEGIN SELECT RAISE(ABORT, 'observation metadata cannot be deleted'); END;
CREATE TRIGGER risk_obs_guard BEFORE UPDATE ON risk_assessment_observation
WHEN NEW.assessment_id IS NOT OLD.assessment_id
  OR NEW.first_seen_utc IS NOT OLD.first_seen_utc
  OR NEW.observations < OLD.observations
  OR NEW.last_seen_utc < OLD.last_seen_utc
BEGIN SELECT RAISE(ABORT, 'observation metadata may only advance'); END;
CREATE TRIGGER risk_obs_no_delete BEFORE DELETE ON risk_assessment_observation
BEGIN SELECT RAISE(ABORT, 'observation metadata cannot be deleted'); END;
"""
MIGRATIONS: tuple[tuple[int, str], ...] = ((1, SCHEMA_V1),)


class JournalError(RuntimeError):
    """The journal refused an operation. ``code`` is safe to show; no key material ever."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


# --------------------------------------------------------------------------- helpers
def utc_iso(value: datetime) -> str:
    """UTC ISO-8601 with microseconds and ``Z`` (the audit timestamp representation)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def audit_row_hash(fields: Sequence[Any]) -> str:
    """SHA-256 of the canonical audit row (see module docstring)."""
    data = json.dumps(list(fields), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now: datetime) -> str:
    """26-character ULID (48-bit millisecond time + 80 random bits), stdlib only."""
    value = (int(now.timestamp() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def key_check_value(key: bytes) -> str:
    return hmac.new(key, KEY_CHECK_MESSAGE, hashlib.sha256).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- ACL (A10)
def _system32(tool: str) -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", tool)


def _run(args: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed executable, explicit arguments, no shell
        args,
        shell=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise JournalError("acl_failed", "user-only permissions could not be applied")
    return result.stdout


def _windows_user() -> tuple[str, str]:
    out = _run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"]).strip()
    match = re.fullmatch(r'"(?P<name>[^"]+)","(?P<sid>S-[0-9-]+)"', out)
    if not match:
        raise JournalError("acl_failed", "the current Windows user could not be determined")
    return match["name"], match["sid"]


def _icacls_entries(output: str, path: Path) -> list[str]:
    lines = output.splitlines()
    entries: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            break
        text = line[len(str(path)) :] if index == 0 and line.startswith(str(path)) else line
        entries.append(text.strip())
    return [entry for entry in entries if entry]


def restrict_to_current_user(path: Path, *, directory: bool) -> None:
    """Grant access to the current user only and verify by reading the ACL back (A10)."""
    if os.name == "nt":
        name, sid = _windows_user()
        rights = "(OI)(CI)(F)" if directory else "(F)"
        _run([_system32("icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"*{sid}:{rights}"])
        entries = _icacls_entries(_run([_system32("icacls.exe"), str(path)]), path)
        expected = f"{name.lower()}:{rights.lower()}"
        if len(entries) != 1 or entries[0].lower() != expected:
            raise JournalError("acl_failed", "user-only permissions could not be verified")
        return
    os.chmod(path, 0o700 if directory else 0o600)
    verify_user_only(path, directory=directory)


def verify_user_only(path: Path, *, directory: bool) -> None:
    """Read-only check that ``path`` is user-only (no modification)."""
    if os.name == "nt":
        name, _ = _windows_user()
        rights = "(OI)(CI)(F)" if directory else "(F)"
        entries = _icacls_entries(_run([_system32("icacls.exe"), str(path)]), path)
        if len(entries) != 1 or entries[0].lower() != f"{name.lower()}:{rights.lower()}":
            raise JournalError("acl_failed", "permissions are not user-only")
        return
    mode = path.stat().st_mode & 0o777
    if mode != (0o700 if directory else 0o600):
        raise JournalError("acl_failed", "permissions are not user-only")


AclFunction = Callable[[Path, bool], None]


def _default_acl(path: Path, directory: bool) -> None:
    restrict_to_current_user(path, directory=directory)


def _default_acl_check(path: Path, directory: bool) -> None:
    verify_user_only(path, directory=directory)


# --------------------------------------------------------------------------- store
class JournalStore:
    """Fail-closed SQLite journal with HMAC key, triggers, audit chain and backups.

    States: ``stopped``, ``ready`` (decisions usable; ``hmac_available`` says whether the
    key verified) and ``refused`` (with a reason code). ``start`` never raises, so a journal
    problem never prevents the rest of Johanna from starting.
    """

    name = "trading_journal"

    def __init__(
        self,
        data_dir: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        acl: AclFunction | None = None,
        acl_check: AclFunction | None = None,
        migrations: Sequence[tuple[int, str]] = MIGRATIONS,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._db = self.data_dir / DB_NAME
        self._key_path = self.data_dir / KEY_NAME
        self._clock = clock or (lambda: datetime.now(UTC))
        self._acl = acl or _default_acl
        self._acl_check = acl_check or _default_acl_check
        self._migrations = tuple(sorted(migrations))
        self._fault = fault or (lambda step: None)
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = "stopped"
        self._reason: str | None = None
        self._key: bytes | None = None
        self._key_status = "unknown"
        self._init_failed = False
        self._created_now = False

    @property
    def latest_version(self) -> int:
        return self._migrations[-1][0] if self._migrations else 0

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        with self._state_lock:
            if self._init_failed:
                self._refuse("initialisation_failed")
                return
            try:
                self._open()
            except JournalError as exc:
                self._refuse(exc.code)
            except Exception as exc:  # never leak details (paths, data) into logs
                LOGGER.error("trading journal refused (%s)", type(exc).__name__)
                self._refuse("unexpected_error")

    def stop(self) -> None:
        with self._state_lock:
            if self._state == "ready":
                with contextlib.suppress(sqlite3.Error):
                    with contextlib.closing(sqlite3.connect(self._db, timeout=10)) as conn:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._state = "stopped"
            self._key = None

    def _refuse(self, code: str) -> None:
        self._state, self._reason, self._key = "refused", code, None
        LOGGER.error("trading journal refused: %s", code)

    @property
    def ready(self) -> bool:
        return self._state == "ready"

    @property
    def hmac_available(self) -> bool:
        return self.ready and self._key_status == "verified" and self._key is not None

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self._state,
            "reason": self._reason,
            "schema_version": self.latest_version,
            "key_status": self._key_status,
            "hmac_available": self.hmac_available,
            # The verified key is only a prerequisite; the capabilities arrive in P5.3/P5.4.
            "hmac_prerequisite": "verified" if self.hmac_available else "refused",
            "monitoring_and_reconciliation": "not_implemented",
            "stale_backup_temp_dirs": len(self._temp_dirs()),
        }

    # -- opening -----------------------------------------------------------------------
    def _journal_dirs(self) -> list[Path]:
        """Published or temporary backup directories: only the journal's own prefixes."""
        return sorted(self.data_dir.glob(f"{PAIR_PREFIX}*")) + self._temp_dirs()

    def _temp_dirs(self) -> list[Path]:
        return sorted(self.data_dir.glob(f"{TMP_PREFIX}*")) if self.data_dir.exists() else []

    def _sidecars(self) -> list[Path]:
        return [Path(f"{self._db}{suffix}") for suffix in SIDECARS]

    def _open(self) -> None:
        if not self._db.exists():
            if self._key_path.exists():
                raise JournalError("orphaned_key")
            if self.data_dir.exists() and self._journal_dirs():
                raise JournalError("restore_required")
            if any(p.exists() for p in self._sidecars()):
                raise JournalError("stale_sidecars")
            self._first_creation()
        else:
            self._open_existing()
        self._state, self._reason = "ready", None

    def _first_creation(self) -> None:
        """Genuine first creation (§5.3). On failure remove only what this attempt created."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        conn: sqlite3.Connection | None = None
        try:
            key = secrets.token_bytes(KEY_BYTES)
            _write_exclusive(self._key_path, key)
            created.append(self._key_path)
            self._fault("after_key")
            self._acl(self._key_path, False)
            self._fault("after_acl")
            _write_exclusive(self._db, b"")  # proves this attempt created the database file
            created.append(self._db)
            created.extend(self._sidecars())  # absent before (checked); any now are ours
            conn = self._connect()
            conn.execute("PRAGMA journal_mode = WAL")
            self._fault("after_db_file")
            for version, sql in self._migrations:
                self._apply_migration(conn, version, sql)
            now = self._clock()
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("key_check", key_check_value(key)),
                    ("created_utc", utc_iso(now)),
                    ("journal_format", "1"),
                ],
            )
            self.append_audit(
                conn,
                action="journal.created",
                table="meta",
                record_id=None,
                new={"schema_version": self.latest_version},
                at=now,
            )
            conn.execute("COMMIT")
            self._fault("after_meta")
            self._check_integrity(conn)
        except BaseException as exc:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
                conn = None
            self._init_failed = True
            self._cleanup(created)
            if isinstance(exc, JournalError) and exc.code == "acl_failed":
                raise JournalError("initialisation_failed", "key permissions failed") from None
            raise JournalError("initialisation_failed") from None
        finally:
            if conn is not None:
                conn.close()
        self._key, self._key_status, self._created_now = key, "verified", True

    @staticmethod
    def _cleanup(created: list[Path]) -> None:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A leftover key makes every later start refuse ("orphaned_key"): fail closed.
                LOGGER.error("trading journal: cleanup of a failed initialisation incomplete")

    def _open_existing(self) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                self._check_integrity(conn)
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version == 0 or version > self.latest_version:
                    raise JournalError("schema_unsupported")
                row = conn.execute("SELECT value FROM meta WHERE key = 'key_check'").fetchone()
        except sqlite3.DatabaseError:
            raise JournalError("corrupt_database") from None
        if row is None:
            raise JournalError("corrupt_database")
        self._verify_key(row[0])
        pending = [(v, sql) for v, sql in self._migrations if v > version]
        if pending:
            if self._key_status != "verified":
                raise JournalError("migration_requires_verified_key")
            self.backup(f"premigration-v{version}", _allow_before_ready=True)
            with contextlib.closing(self._connect()) as conn:
                for target, sql in pending:
                    self._apply_migration(conn, target, sql)
                    self._fault(f"after_migration_{target}")

    def _verify_key(self, stored_check: str) -> None:
        self._key = None
        if not self._key_path.exists():
            self._key_status = "missing"
            return
        try:
            key = self._key_path.read_bytes()
        except OSError:
            self._key_status = "unreadable"
            return
        if len(key) != KEY_BYTES or not hmac.compare_digest(key_check_value(key), stored_check):
            self._key_status = "mismatch"
            return
        try:
            self._acl_check(self._key_path, False)
        except JournalError:
            self._key_status = "permissions_not_user_only"
            return
        self._key, self._key_status = key, "verified"

    def _apply_migration(self, conn: sqlite3.Connection, version: int, sql: str) -> None:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version != current + 1:
            raise JournalError("migration_order")
        statements = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {int(version)};\nCOMMIT;"
        try:
            conn.executescript(statements)
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise JournalError("migration_failed") from None

    @staticmethod
    def _check_integrity(conn: sqlite3.Connection) -> None:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError:
            raise JournalError("corrupt_database") from None
        if result is None or result[0] != "ok":
            raise JournalError("corrupt_database")

    # -- connections -------------------------------------------------------------------
    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self._db, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def _require_ready(self) -> None:
        if not self.ready:
            raise JournalError("journal_unavailable")

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One short write transaction under the process-wide write lock."""
        self._require_ready()
        with self._write_lock, contextlib.closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self._require_ready()
        with contextlib.closing(self._connect()) as conn:
            yield conn

    def now(self) -> datetime:
        return self._clock()

    # -- audit -------------------------------------------------------------------------
    def append_audit(
        self,
        conn: sqlite3.Connection,
        *,
        action: str,
        table: str,
        record_id: str | None,
        old: Any = None,
        new: Any = None,
        at: datetime,
        actor: str = "johanna",
    ) -> int:
        last = conn.execute(
            "SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = (last["seq"] + 1) if last else 1
        prev = last["row_hash"] if last else GENESIS_HASH
        fields = (
            seq,
            utc_iso(at),
            actor,
            action,
            table,
            record_id,
            canonical_json(old),
            canonical_json(new),
            prev,
        )
        conn.execute(
            "INSERT INTO audit_log(seq, recorded_utc, actor, action, table_name, record_id, "
            "old_values, new_values, prev_hash, row_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*fields, audit_row_hash(fields)),
        )
        return seq

    def verify_audit_chain(self) -> dict[str, object]:
        with self.read() as conn:
            return verify_audit_rows(
                conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
            )

    # -- HMAC references (A4; used by later milestones) --------------------------------
    def broker_ref(self, kind: str, raw_id: object) -> str:
        if not self.hmac_available or self._key is None:
            raise JournalError("hmac_unavailable")
        if kind not in ("position", "deal", "order"):
            raise ValueError("unknown reference kind")
        return hmac.new(self._key, f"{kind}:{raw_id}".encode(), hashlib.sha256).hexdigest()

    # -- backups (§5.4) ----------------------------------------------------------------
    def backup(self, kind: str = "daily", *, _allow_before_ready: bool = False) -> Path:
        """WAL-safe backup pair built privately and published by one directory rename."""
        if not _allow_before_ready:
            self._require_ready()
        if kind != "daily" and not re.fullmatch(r"premigration-v\d+", kind):
            raise ValueError("unknown backup kind")
        if self._key is None:
            raise JournalError("backup_requires_verified_key")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        final = self.data_dir / f"{PAIR_PREFIX}{kind}-{stamp}"
        if final.exists():
            raise JournalError("backup_failed")
        temp = self.data_dir / f"{TMP_PREFIX}{stamp}-{secrets.token_hex(4)}"
        temp.mkdir()  # exclusive: fails if it exists
        try:
            snapshot = temp / DB_NAME
            with contextlib.closing(sqlite3.connect(self._db, timeout=10)) as source:
                with contextlib.closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)  # online backup API: consistent, WAL-safe snapshot
            self._fault("backup_after_snapshot")
            with contextlib.closing(sqlite3.connect(snapshot, isolation_level=None)) as conn:
                conn.execute("PRAGMA journal_mode = DELETE")  # self-contained single file
                self._check_integrity(conn)
                stored = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
            key_bytes = self._key_path.read_bytes()
            _write_exclusive(temp / KEY_NAME, key_bytes)
            self._fault("backup_after_key")
            if stored is None or not hmac.compare_digest(key_check_value(key_bytes), stored[0]):
                raise JournalError("backup_key_mismatch")
            self._acl(temp / KEY_NAME, False)
            self._acl(temp, True)
            self._fault("backup_after_acl")
            manifest = {
                "format": 1,
                "kind": kind,
                "created_utc": stamp,
                "schema_version": self.latest_version,
                "key_check": stored[0],
                "files": {DB_NAME: _sha256_file(snapshot), KEY_NAME: _sha256_file(temp / KEY_NAME)},
                "verified": True,
            }
            _write_exclusive(temp / PAIR_MANIFEST, json.dumps(manifest, sort_keys=True).encode())
            self._fault("backup_before_publish")
            if final.exists():
                raise JournalError("backup_failed")
            os.rename(temp, final)  # one same-filesystem rename publishes the complete pair
        except BaseException as exc:
            shutil.rmtree(temp, ignore_errors=True)  # only this attempt's temporary directory
            code = exc.code if isinstance(exc, JournalError) else "backup_failed"
            raise JournalError(code) from None
        return final

    def valid_pairs(self) -> list[Path]:
        return [
            path for path in sorted(self.data_dir.glob(f"{PAIR_PREFIX}*")) if validate_pair(path)
        ]

    def prune_daily(self, keep: int = DAILY_RETENTION) -> list[Path]:
        """Delete the oldest *valid* daily pairs beyond ``keep``; never pre-migration pairs,
        never invalid or incomplete directories."""
        daily = [p for p in self.valid_pairs() if _PAIR_NAME.fullmatch(p.name)[1] == "daily"]
        daily.sort(key=lambda p: _PAIR_NAME.fullmatch(p.name)[2])
        removed = daily[:-keep] if keep > 0 and len(daily) > keep else []
        for path in removed:
            shutil.rmtree(path)
        return removed


def validate_pair(path: Path) -> bool:
    """A valid pair: published name, ``pair.json`` verified, hashes and key check matching,
    and an intact database. Anything else is never a backup."""
    try:
        if not _PAIR_NAME.fullmatch(path.name) or not path.is_dir():
            return False
        manifest = json.loads((path / PAIR_MANIFEST).read_text(encoding="utf-8"))
        if manifest.get("verified") is not True:
            return False
        files = manifest.get("files") or {}
        db, key = path / DB_NAME, path / KEY_NAME
        if files.get(DB_NAME) != _sha256_file(db) or files.get(KEY_NAME) != _sha256_file(key):
            return False
        key_bytes = key.read_bytes()
        if len(key_bytes) != KEY_BYTES or key_check_value(key_bytes) != manifest.get("key_check"):
            return False
        uri = f"file:{db.as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            stored = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()
        return stored is not None and stored[0] == manifest["key_check"]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def verify_audit_rows(rows: Sequence[sqlite3.Row | dict]) -> dict[str, object]:
    """Verify a full audit chain (tamper-evident only; see module docstring)."""
    previous = GENESIS_HASH
    for expected_seq, row in enumerate(rows, start=1):
        seq = row["seq"]
        if seq != expected_seq:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "sequence gap or reordering",
            }
        if row["prev_hash"] != previous:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "previous hash mismatch",
            }
        fields = (
            seq,
            row["recorded_utc"],
            row["actor"],
            row["action"],
            row["table_name"],
            row["record_id"],
            row["old_values"],
            row["new_values"],
            row["prev_hash"],
        )
        if audit_row_hash(fields) != row["row_hash"]:
            return {
                "valid": False,
                "rows": len(rows),
                "first_bad_seq": seq,
                "problem": "row hash mismatch",
            }
        previous = row["row_hash"]
    return {
        "valid": True,
        "rows": len(rows),
        "first_bad_seq": None,
        "problem": None,
        "guarantee": "tamper-evident only; no external anchor",
    }
