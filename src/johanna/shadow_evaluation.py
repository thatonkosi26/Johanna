"""P6.1 background shadow evaluation and signal-frequency diagnostics.

**Shadow mode, read-only: no orders are placed.** The scheduler evaluates every closed candle
of EURUSDc, GBPUSDc and USDJPYc on H1, H4 and D1 with the unchanged T6 analysis and, for a
``TRADE_CANDIDATE`` only, the unchanged Phase 4 risk assessment, and records the outcome and
the reason for every skipped or missed window. It needs no open dashboard.

Behaviour (approved P6.1 scope):

- **Enabled state** is an append-only, audited journal record. No record means disabled, so a
  new installation or a migration never enables it. When enabled, it resumes after a restart.
- **Windows**: one per (instrument, timeframe, candle close). The candle close is computed on
  the verified broker server clock (DST-safe), never the host's local time. A window opens
  ``START_DELAY`` after the close and ends at its deadline (``DEADLINES``).
- **Closure**: the candle closing at the window's close must be in the analysed snapshot with
  ``tick_observed`` closure evidence (T1/E1). Otherwise the attempt is retried with bounded
  backoff and, at the deadline, the window is ``SKIPPED`` (``closure_unconfirmed`` or
  ``candle_not_available``). An unconfirmed candle is never evaluated as complete.
- **Market closures** (the T0 session calendar) produce ``SKIPPED`` / ``market_closed`` without
  any MT5 read; nothing can become a candidate.
- **Deduplication**: ``dedup_key`` = instrument, timeframe, candle close and the analysis,
  decision and risk versions (unique in the journal). Status changes are compare-and-set, so
  one window is never processed twice. The decision journal's fingerprint deduplication
  remains the second safeguard.
- **Restart**: windows owed while the scheduler was enabled but not running are recorded
  ``MISSED`` (never evaluated late as if current; catch-up limited to ``CATCH_UP_LIMIT``);
  rows left ``PENDING``/``RUNNING`` by an earlier run are adopted if still inside their window,
  otherwise closed as ``MISSED`` / ``FAILED`` (``interrupted``).
- **MT5 contention**: every read uses the existing workers through
  ``live_market.background_reads`` (non-blocking admission after a quiet window; never
  queued; dashboard state untouched). A deferral is retried within the window only; at the
  deadline the window is ``SKIPPED`` / ``mt5_worker_busy``. A worker timeout or failure pauses
  the scheduler for ``FAILURE_PAUSE``.
- **Failures** are visible: an unexpected analysis error ends the window ``FAILED``; repeated
  scheduler errors stop the scheduler in the ``error`` state (no endless silent restarts).

P6.1.1 (historical data reliability):

- **Missing-slot diagnostics** (``T4.history_slots`` gate rows): the exact candle slots behind a
  ``closed_history_gap`` / ``closed_history_behind_tick`` / ``gap_unknown`` T4 reason, copied
  from T4's own evidence (T3 gap records, E1 slot lists), one row per contiguous run so that no
  detail is truncated. T4's verdict and reason codes are recorded unchanged.
- **History-sync retries** (``T4.history_sync`` gate rows): when T4 rejects a window *only* for
  those history-sync reasons, the attempt is retried with a fresh MT5 read on the existing data
  retry schedule, strictly inside the window's deadline. The unchanged T4 pipeline judges every
  read; a gap that persists to the last attempt ends as the normal fail-closed rejection.

P6.2a (shadow trade intents, HYPOTHETICAL -- NOT AN ORDER):

- A completed candidate whose trade the risk engine constructed gets exactly one
  ``shadow_intent`` row (``johanna.shadow_intents``), written in the same transaction as the
  window's completion, its gates and its audit entry; the journal's unique evaluation
  reference and insert trigger make a second intent for the same window impossible.
- The opportunity funnel counts scheduled windows only (never dashboard requests); windows
  that were not evaluated are reported as such, never as NO_TRADE.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.analysis.decision import DECISION_VERSION
from johanna.analysis.engine import ANALYSIS_VERSION
from johanna.analysis.sessions import EXNESS_T0_PROVISIONAL, SessionConfig, closures_overlapping
from johanna.decision_journal import analysis_record
from johanna.journal_store import JournalError, JournalStore, new_ulid, utc_iso
from johanna.live_market import BACKGROUND_DEFERRED, MarketUnavailable, background_reads
from johanna.risk_engine import RISK_ENGINE_VERSION
from johanna.shadow_intents import (
    build_funnel,
    build_intent,
    get_intent,
    insert_intent,
    list_intents,
)

LOGGER = logging.getLogger("johanna.shadow_evaluation")
BANNER = "SHADOW MODE — READ-ONLY — NO ORDERS WILL BE PLACED"
PAIRS = {"EURUSD": "EURUSDc", "GBPUSD": "GBPUSDc", "USDJPY": "USDJPYc"}
TIMEFRAMES = ("H1", "H4", "D1")
TF_SECONDS = {"H1": 3600, "H4": 14400, "D1": 86400}
START_DELAY = timedelta(seconds=25)
DEADLINES = {"H1": timedelta(minutes=10), "H4": timedelta(minutes=15), "D1": timedelta(minutes=20)}
CONTENTION_RETRY = timedelta(seconds=5)
DATA_RETRY = (15, 30, 60, 120)  # seconds; the last value repeats until the deadline
FAILURE_PAUSE = timedelta(minutes=5)
CATCH_UP_LIMIT = timedelta(days=7)
TICK_SECONDS = 2.0
MAX_CONSECUTIVE_ERRORS = 5
TERMINAL = ("COMPLETED", "SKIPPED", "MISSED", "FAILED")
STATUSES = ("PENDING", "RUNNING", *TERMINAL)
_WORKER_FAILURES = frozenset({"timeout", "worker_failed", "mt5_busy"})
# P6.1.1: T4 reasons that describe temporarily incomplete MT5 history (T3 / E1 codes, unchanged)
HISTORY_SYNC_CODES = frozenset({"closed_history_gap", "closed_history_behind_tick", "gap_unknown"})
HISTORY_SLOTS_GATE = "T4.history_slots"
HISTORY_SYNC_GATE = "T4.history_sync"
_HISTORY_GATES = (HISTORY_SLOTS_GATE, HISTORY_SYNC_GATE)
_RISK_STATUS = {"APPROVED_FOR_MANUAL_REVIEW": "APPROVED", "REJECTED": "REJECTED"}


def iso_z(value: datetime) -> str:
    """Candle-time format used by T1/T6 (``2026-11-11T13:00:00Z``)."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _parse_optional(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return parse_utc(text)
    except ValueError:
        return None


def _iso_or_none(value: datetime | None) -> str | None:
    return utc_iso(value) if value is not None else None


def dedup_key(symbol: str, timeframe: str, close: datetime) -> str:
    return (
        f"{symbol}|{timeframe}|{iso_z(close)}|a{ANALYSIS_VERSION}|d{DECISION_VERSION}"
        f"|r{RISK_ENGINE_VERSION}"
    )


# ====================================================================== schedule (pure)
def window_close_at_or_before(now: datetime, timeframe: str, server_clock: Any) -> datetime:
    """The latest candle close <= ``now``, on the broker server's wall clock."""
    seconds = TF_SECONDS[timeframe]
    wall = server_clock.from_utc(now)
    return server_clock.to_utc(wall - (wall % seconds))


def shift_close(close: datetime, timeframe: str, server_clock: Any, steps: int) -> datetime:
    return server_clock.to_utc(server_clock.from_utc(close) + steps * TF_SECONDS[timeframe])


def market_closed(
    close: datetime, timeframe: str, server_clock: Any, sessions: SessionConfig
) -> bool:
    """True when a scheduled closure covers the candle's whole interval (no candle exists)."""
    opened = shift_close(close, timeframe, server_clock, -1)
    return any(
        c.start <= opened and c.end >= close for c in closures_overlapping(opened, close, sessions)
    )


# ====================================================================== gate outcomes (pure)
def _detail(text: Any) -> str | None:
    if text is None:
        return None
    return str(text)[:500]


T4_STATES = ("accepted", "provisional", "rejected")  # johanna.analysis.window verdict states


def t4_state(envelope: Mapping[str, Any]) -> str:
    """The T4 window-validation verdict state the evaluation used (``verdict["state"]``):
    ``accepted`` / ``provisional`` / ``rejected``; ``not_run`` when T4 did not run; and
    ``state_missing`` for a verdict without a state. Never empty, so a correctly recorded T4
    gate always carries a reason code."""
    validation = envelope.get("window_validation") or {}
    if not validation.get("available"):
        return "not_run"
    state = (validation.get("verdict") or {}).get("state")
    return state if isinstance(state, str) and state else "state_missing"


def analysis_gates(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Gate outcomes from the T6 envelope, using T4/T6 identifiers unchanged."""
    result = envelope.get("result") or {}
    decision = result.get("decision") or {}
    gates: list[dict[str, Any]] = []
    validation = envelope.get("window_validation") or {}
    codes = validation.get("reason_codes") or []
    if validation.get("available"):
        state = t4_state(envelope)
        gates.append(
            {
                "stage": "data",
                "gate": "T4.window_validation",
                # T4's own semantics: only an accepted window passes (provisional and rejected
                # windows fail this gate); the exact state is the reason code.
                "result": "PASS" if state == "accepted" else "FAIL",
                "reason_code": state,
                "detail": _detail(", ".join(str(c) for c in codes) if codes else None),
            }
        )
    else:
        gates.append(
            {
                "stage": "data",
                "gate": "T4.window_validation",
                "result": "FAIL",
                "reason_code": "not_run",
                "detail": _detail(validation.get("reason")),
            }
        )
    gate = result.get("gate") or {}
    if gate:
        gates.append(
            {
                "stage": "analysis",
                "gate": "T6.gate",
                "result": "PASS" if gate.get("passed") else "FAIL",
                "reason_code": "passed" if gate.get("passed") else "failed",
                "detail": _detail("; ".join(str(r) for r in gate.get("reasons") or []) or None),
            }
        )
    outcome = decision.get("outcome")
    reasons = decision.get("reasons") or []
    gates.append(
        {
            "stage": "analysis",
            "gate": f"T6.{decision.get('rule') or 'unknown'}",
            "result": "PASS" if outcome == "TRADE_CANDIDATE" else "FAIL",
            "reason_code": outcome,
            "detail": _detail(reasons[0] if reasons else None),
        }
    )
    opportunity = result.get("opportunity") or {}
    mapping = {"pass": "PASS", "fail": "FAIL", "unavailable": "BLOCKED"}
    for item in opportunity.get("checklist") or []:
        gates.append(
            {
                "stage": "strategy",
                "gate": str(item.get("id")),
                "result": mapping.get(item.get("result"), "SKIPPED"),
                "reason_code": item.get("evidence_id") or item.get("result"),
                "detail": _detail(item.get("detail")),
            }
        )
    for veto in opportunity.get("triggered_vetoes") or []:
        name = veto.get("id") if isinstance(veto, Mapping) else veto
        gates.append(
            {
                "stage": "strategy",
                "gate": f"veto.{name}",
                "result": "FAIL",
                "reason_code": "veto_triggered",
                "detail": None,
            }
        )
    return gates


def risk_gates(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Gate outcomes from a Phase 4 result: check identifiers and outcomes only (free-text
    details are not stored, as in the decision journal's risk evidence)."""
    mapping = {"passed": "PASS", "failed": "FAIL", "unverified": "BLOCKED"}
    status = result.get("status")
    gates = [
        {
            "stage": "risk",
            "gate": "phase4.status",
            "result": {"APPROVED_FOR_MANUAL_REVIEW": "PASS", "REJECTED": "FAIL"}.get(
                status, "BLOCKED"
            ),
            "reason_code": status,
            "detail": None,
        }
    ]
    for area, validation in (result.get("validations") or {}).items():
        for check in validation.get("checks") or []:
            gates.append(
                {
                    "stage": "risk",
                    "gate": f"{area}.{check.get('id')}",
                    "result": mapping.get(check.get("outcome"), "SKIPPED"),
                    "reason_code": check.get("outcome"),
                    "detail": None,
                }
            )
    return gates


# ====================================================================== history sync (pure)
def _json_detail(value: Mapping[str, Any]) -> str:
    """Compact JSON gate detail. Never truncated: a detail that would not fit the journal's
    500-character limit is an error (visible), never silently cut."""
    text = json.dumps(value, separators=(",", ":"), sort_keys=False)
    if len(text) > 500:
        raise ValueError(f"gate detail of {len(text)} characters exceeds 500")
    return text


def t4_fail_codes(envelope: Mapping[str, Any]) -> list[str]:
    """T4 verdict reason codes of FAIL severity, in T4's order, each once."""
    reasons = ((envelope.get("window_validation") or {}).get("verdict") or {}).get("reasons")
    codes: list[str] = []
    for reason in reasons or []:
        code = reason.get("code") if isinstance(reason, Mapping) else None
        if reason.get("severity") == "fail" and isinstance(code, str) and code not in codes:
            codes.append(code)
    return codes


def history_sync_retryable(envelope: Mapping[str, Any]) -> bool:
    """True when T4 rejected the window *exclusively* for history-sync reasons: every FAIL
    reason is one of ``HISTORY_SYNC_CODES`` (a mixture with any other failure is not)."""
    if t4_state(envelope) != "rejected":
        return False
    codes = t4_fail_codes(envelope)
    return bool(codes) and set(codes) <= HISTORY_SYNC_CODES


def _runs(slots: Iterable[datetime], step: timedelta) -> list[list[datetime]]:
    """Consecutive slots (exactly ``step`` apart) grouped into runs, in time order."""
    runs: list[list[datetime]] = []
    for slot in sorted(set(slots)):
        if runs and slot - runs[-1][-1] == step:
            runs[-1].append(slot)
        else:
            runs.append([slot])
    return runs


def history_slot_gates(
    envelope: Mapping[str, Any], *, result: str, attempt: int
) -> list[dict[str, Any]]:
    """``T4.history_slots`` rows: the exact slots behind each history-sync reason of the T4
    verdict, from T4's own evidence (nothing is recomputed). ``kind`` is ``missing`` (no candle
    inside the window) or ``behind_tick`` (closed slots after the window's newest candle that the
    snapshot tick says have closed). One row per contiguous run of slots."""
    validation = envelope.get("window_validation") or {}
    timeframe = envelope.get("timeframe")
    if not validation.get("available") or timeframe not in TF_SECONDS:
        return []
    step = timedelta(seconds=TF_SECONDS[timeframe])
    window = validation.get("window") or {}
    base = {
        "symbol": envelope.get("symbol"),
        "timeframe": timeframe,
        "attempt": attempt,
        "window": [window.get("first_t"), window.get("last_t")],
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(code: str, detail: dict[str, Any]) -> None:
        text = _json_detail({**base, **detail})
        if text not in seen:  # R2 and R4 can report the same slots
            seen.add(text)
            rows.append(
                {
                    "stage": "data",
                    "gate": HISTORY_SLOTS_GATE,
                    "result": result,
                    "reason_code": code,
                    "detail": text,
                }
            )

    reasons = (validation.get("verdict") or {}).get("reasons") or []
    for reason in reasons:
        code = reason.get("code") if isinstance(reason, Mapping) else None
        if code not in HISTORY_SYNC_CODES:
            continue
        if code == "gap_unknown":  # T3: every gap record, exact and never truncated
            for gap in reason.get("gaps") or []:
                start, end = parse_utc(gap["start_utc"]), parse_utc(gap["end_utc"])
                add(
                    code,
                    {
                        "kind": "missing",
                        "source": "T3",
                        "first": iso_z(start),
                        "last": iso_z(end - step),
                        "count": gap.get("missing_slots"),
                        "required": gap.get("required_missing_slots"),
                    },
                )
            continue
        # E1: the listed required slots (T4 lists at most 50, always with the total count)
        listed = reason.get("missing_required_slots") or {}
        slots = [parse_utc(s) for s in listed.get("slots") or []]
        kind = "behind_tick" if code == "closed_history_behind_tick" else "missing"
        extra: dict[str, Any] = {}
        if kind == "behind_tick":
            extra = {
                "after": reason.get("latest_window_t"),
                "expected": reason.get("expected_latest_closed_t"),
            }
        for run in _runs(slots, step):
            add(
                code,
                {
                    "kind": kind,
                    "source": "E1",
                    "first": iso_z(run[0]),
                    "last": iso_z(run[-1]),
                    "count": len(run),
                    **extra,
                },
            )
        total = listed.get("count") or 0
        if total > len(slots):  # explicit, never an ambiguous truncation
            add(
                code,
                {
                    "kind": kind,
                    "source": "E1",
                    "unlisted": total - len(slots),
                    "after_listed": iso_z(max(slots)) if slots else None,
                    "last": listed.get("last"),
                    "note": "T4 lists 50 slots; the T3 gap rows give the exact ranges",
                    **extra,
                },
            )
    return rows


def _history_outcome(envelope: Mapping[str, Any], retries: int, attempts: int) -> list[dict]:
    """The final ``T4.history_sync`` row of a completed window (none when history sync played
    no part): ``recovered`` (retried, then accepted or provisional), ``retries_exhausted``
    (still history-sync only at the last attempt), ``deadline_no_retry`` (history-sync only, but
    no retry fitted before the deadline), ``not_retried_mixed`` (with another failure) or
    ``not_eligible`` (a rejection for other reasons)."""
    state = t4_state(envelope)
    codes = t4_fail_codes(envelope)
    if state == "rejected":
        if history_sync_retryable(envelope):
            result, reason = (
                ("FAIL", "retries_exhausted") if retries else ("SKIPPED", "deadline_no_retry")
            )
        elif set(codes) & HISTORY_SYNC_CODES:
            result, reason = "SKIPPED", "not_retried_mixed"
        else:
            result, reason = "SKIPPED", "not_eligible"
    elif retries and state in ("accepted", "provisional"):
        result, reason = "PASS", "recovered"
    elif retries:
        result, reason = "SKIPPED", "not_eligible"
    else:
        return []
    return [
        {
            "stage": "data",
            "gate": HISTORY_SYNC_GATE,
            "result": result,
            "reason_code": reason,
            "detail": _json_detail(
                {"retries": retries, "attempts": attempts, "verdict": state, "codes": codes}
            ),
        }
    ]


# ====================================================================== the service
class _Retry(Exception):
    """The attempt cannot complete now; retry later in the same window.

    ``at`` fixes the next attempt time (history-sync retries, computed against the deadline);
    ``gates`` are recorded with the retry (append-only diagnostics of this attempt)."""

    def __init__(
        self,
        reason: str,
        detail: str | None,
        delay: timedelta,
        *,
        at: datetime | None = None,
        gates: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason, self.detail, self.delay = reason, detail, delay
        self.at, self.gates = at, gates or []


class ShadowEvaluationService:
    name = "shadow_evaluation"

    def __init__(
        self,
        store: JournalStore,
        decisions: Any,
        analysis: Any,
        risk: Any,
        server_clock: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        sessions: SessionConfig = EXNESS_T0_PROVISIONAL,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        self.store = store
        self.decisions = decisions
        self.analysis = analysis
        self.risk = risk
        self.server_clock = server_clock
        self.sessions = sessions
        self.tick_seconds = tick_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()  # lifecycle and enable/disable
        self._work = threading.Lock()  # one evaluation pass at a time
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._run_id: str | None = None
        self._state = "stopped"
        self._error: str | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._consecutive_errors = 0
        self._pause_until: datetime | None = None
        self._last_tick: datetime | None = None
        self.attempts = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Resume if the stored state is enabled. Never raises."""
        with self._lock:
            self._started = True
            try:
                if not self.store.ready:
                    self._state = "blocked"
                    return
                self._recover_interrupted(self._clock())
                if self.enabled():
                    self._start_thread()
                else:
                    self._state = "disabled"
            except Exception as exc:  # the rest of Johanna keeps running
                LOGGER.exception("Shadow evaluation could not start")
                self._state, self._error = "error", type(exc).__name__

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._stop_thread()
            if self._state == "running":
                self._state = "stopped"

    def _start_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._run_id = new_ulid(self._clock())
        self._error, self._consecutive_errors, self._pause_until = None, 0, None
        self._state = "running"
        self._thread = threading.Thread(
            target=self._loop, args=(self._stop,), name="johanna-shadow-evaluation", daemon=True
        )
        self._thread.start()

    def _stop_thread(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=60)  # an attempt in progress is bounded by the MT5 timeouts
        if thread.is_alive():
            raise RuntimeError("shadow evaluation did not stop within 60 seconds")
        self._thread = None

    def _loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once(stop)
                self._consecutive_errors = 0
                self._error = None  # recovered; the last error stays reported separately
            except Exception as exc:
                self._consecutive_errors += 1
                self._error = self._last_error = type(exc).__name__
                self._last_error_at = self._clock()
                LOGGER.exception("Shadow evaluation pass failed (%d)", self._consecutive_errors)
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    self._state = "error"  # visible; not restarted automatically
                    return
            stop.wait(self.tick_seconds)

    # ------------------------------------------------------------------ enabled state
    def enabled(self) -> bool:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT enabled FROM shadow_scheduler_setting ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return bool(row and row["enabled"])

    def _enabled_since(self, conn) -> datetime | None:
        row = conn.execute(
            "SELECT enabled, changed_utc FROM shadow_scheduler_setting ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if not row or not row["enabled"]:
            return None
        return parse_utc(row["changed_utc"])

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        """Persist the user's choice (audited) and start or stop the scheduler."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        with self._lock:
            if not self.store.ready:
                raise JournalError("journal_unavailable")
            if self.enabled() != enabled:
                now = self._clock()
                setting_id = new_ulid(now)
                with self.store.write() as conn:
                    conn.execute(
                        "INSERT INTO shadow_scheduler_setting(id, enabled, changed_utc, actor) "
                        "VALUES (?,?,?,'user')",
                        (setting_id, int(enabled), utc_iso(now)),
                    )
                    self.store.append_audit(
                        conn,
                        action="shadow_scheduler.enabled"
                        if enabled
                        else "shadow_scheduler.disabled",
                        table="shadow_scheduler_setting",
                        record_id=setting_id,
                        new={"enabled": enabled},
                        at=now,
                        actor="user",
                    )
            if enabled and self._started:
                self._start_thread()
            elif not enabled:
                self._stop_thread()
                self._close_pending("SKIPPED", "scheduler_disabled", self._clock())
                self._state = "disabled"
        return self.status()

    # ------------------------------------------------------------------ one pass
    def run_once(self, stop: threading.Event | None = None) -> None:
        """Create due windows, attempt due work and expire overdue windows (one pass)."""
        if not self._work.acquire(blocking=False):
            return  # a pass is already running: never overlap
        try:
            now = self._clock()
            self._last_tick = now
            if not self.store.ready:
                raise JournalError("journal_unavailable")
            self._ensure_windows(now)
            self._expire(now)
            if self._pause_until and now < self._pause_until:
                return
            for row in self._due(now):
                if stop is not None and stop.is_set():
                    return
                self._attempt(row)
                if self._pause_until and self._clock() < self._pause_until:
                    return
        finally:
            self._work.release()

    def _ensure_windows(self, now: datetime) -> None:
        with self.store.read() as conn:
            since = self._enabled_since(conn)
            if since is None:
                return
            latest = {
                (r["symbol"], r["timeframe"]): r["last"]
                for r in conn.execute(
                    "SELECT symbol, timeframe, max(window_close_utc) AS last FROM evaluation_cycle "
                    "GROUP BY symbol, timeframe"
                )
            }
        for symbol in PAIRS:
            for timeframe in TIMEFRAMES:
                self._ensure_key(symbol, timeframe, now, since, latest.get((symbol, timeframe)))

    def _ensure_key(
        self, symbol: str, timeframe: str, now: datetime, since: datetime, last: str | None
    ) -> None:
        deadline = DEADLINES[timeframe]
        current = window_close_at_or_before(now, timeframe, self.server_clock)
        earliest = max(since, now - CATCH_UP_LIMIT)
        # first window owed: the earliest whose deadline falls after the scheduler was enabled
        close = window_close_at_or_before(earliest, timeframe, self.server_clock)
        if close + deadline <= since:
            close = shift_close(close, timeframe, self.server_clock, 1)
        if last is not None:
            close = max(close, shift_close(parse_utc(last), timeframe, self.server_clock, 1))
        while close <= current and close + START_DELAY <= now:
            self._create_window(symbol, timeframe, close, now)
            close = shift_close(close, timeframe, self.server_clock, 1)

    def _create_window(self, symbol: str, timeframe: str, close: datetime, now: datetime) -> None:
        deadline = close + DEADLINES[timeframe]
        if market_closed(close, timeframe, self.server_clock, self.sessions):
            status, reason, detail = "SKIPPED", "market_closed", "scheduled market closure"
        elif now >= deadline:
            status, reason, detail = "MISSED", "scheduler_not_running", "window passed unevaluated"
        else:
            status, reason, detail = "PENDING", None, None
        cycle_id = new_ulid(now)
        with self.store.write() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO evaluation_cycle(id, dedup_key, symbol, broker_symbol, "
                "timeframe, window_close_utc, analysis_version, decision_version, risk_version, "
                "status, reason_code, reason_detail, owner_run, next_attempt_utc, deadline_utc, "
                "created_utc, completed_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cycle_id,
                    dedup_key(symbol, timeframe, close),
                    symbol,
                    PAIRS[symbol],
                    timeframe,
                    iso_z(close),
                    ANALYSIS_VERSION,
                    DECISION_VERSION,
                    RISK_ENGINE_VERSION,
                    status,
                    reason,
                    detail,
                    self._run_id,
                    utc_iso(close + START_DELAY) if status == "PENDING" else None,
                    utc_iso(deadline),
                    utc_iso(now),
                    utc_iso(now) if status != "PENDING" else None,
                ),
            ).rowcount
            if inserted and status != "PENDING":
                self._gates(
                    conn,
                    cycle_id,
                    [
                        {
                            "stage": "session" if reason == "market_closed" else "schedule",
                            "gate": "session.open" if reason == "market_closed" else "window",
                            "result": "SKIPPED",
                            "reason_code": reason,
                            "detail": detail,
                        }
                    ],
                    now,
                )
                self._audit(conn, cycle_id, status, reason, now)

    def _due(self, now: datetime) -> list[Any]:
        with self.store.read() as conn:
            return conn.execute(
                "SELECT * FROM evaluation_cycle WHERE status='PENDING' AND next_attempt_utc <= ? "
                "ORDER BY window_close_utc, timeframe, symbol",
                (utc_iso(now),),
            ).fetchall()

    def _expire(self, now: datetime) -> None:
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT id, reason_code, reason_detail FROM evaluation_cycle "
                "WHERE status='PENDING' AND deadline_utc <= ?",
                (utc_iso(now),),
            ).fetchall()
        for row in rows:
            reason = row["reason_code"] or "not_attempted"
            self._finish(row["id"], "PENDING", "SKIPPED", reason, row["reason_detail"], now)
        # Passes are serialised and this backend owns the data directory, so a RUNNING row
        # seen here is not being worked on: past its deadline it was interrupted.
        with self.store.read() as conn:
            stuck = conn.execute(
                "SELECT id FROM evaluation_cycle WHERE status='RUNNING' AND deadline_utc <= ?",
                (utc_iso(now),),
            ).fetchall()
        for row in stuck:
            self._finish(row["id"], "RUNNING", "FAILED", "interrupted", None, now)

    # ------------------------------------------------------------------ attempts
    def _attempt(self, row: Any) -> None:
        now = self._clock()
        with self.store.write() as conn:
            claimed = conn.execute(
                "UPDATE evaluation_cycle SET status='RUNNING', attempts=attempts+1, owner_run=?, "
                "started_utc=COALESCE(started_utc, ?) WHERE id=? AND status='PENDING'",
                (self._run_id, utc_iso(now), row["id"]),
            ).rowcount
        if not claimed:
            return  # another pass took it (never processed twice)
        self.attempts += 1
        close = parse_utc(row["window_close_utc"])
        deadline = parse_utc(row["deadline_utc"])
        with background_reads() as reads:
            try:
                outcome = self._evaluate(
                    row["symbol"], row["timeframe"], close, reads, row=row, deadline=deadline
                )
            except _Retry as retry:
                deferrals = reads.deferrals
                self._retry(row, retry, deferrals, deadline)
                return
            except Exception as exc:
                LOGGER.exception("Shadow evaluation of %s failed", row["dedup_key"])
                self._finish(
                    row["id"],
                    "RUNNING",
                    "FAILED",
                    "evaluation_error",
                    type(exc).__name__,
                    self._clock(),
                    deferrals=reads.deferrals,
                )
                return
        try:
            self._complete(row["id"], outcome, reads.deferrals)
        except Exception as exc:  # never leave a window RUNNING after a failed completion
            LOGGER.exception("Shadow evaluation of %s could not be recorded", row["dedup_key"])
            self._finish(
                row["id"],
                "RUNNING",
                "FAILED",
                "evaluation_error",
                type(exc).__name__,
                self._clock(),
                deferrals=reads.deferrals,
            )

    def _retry(self, row: Any, retry: _Retry, deferrals: int, deadline: datetime) -> None:
        now = self._clock()
        if retry.reason in ("mt5_worker_busy", "history_sync_stale_read"):
            delay = CONTENTION_RETRY
        elif retry.reason == "worker_backoff":
            self._pause_until = now + FAILURE_PAUSE
            delay = FAILURE_PAUSE
        else:
            index = min(row["attempts"], len(DATA_RETRY) - 1)  # attempts before this one
            delay = timedelta(seconds=DATA_RETRY[index])
        next_at = retry.at if retry.at is not None else now + delay
        if next_at >= deadline:
            self._finish(
                row["id"],
                "RUNNING",
                "SKIPPED",
                retry.reason,
                retry.detail,
                now,
                deferrals=deferrals,
            )
            return
        with self.store.write() as conn:
            updated = conn.execute(
                "UPDATE evaluation_cycle SET status='PENDING', reason_code=?, reason_detail=?, "
                "next_attempt_utc=?, contention_deferrals=contention_deferrals+? "
                "WHERE id=? AND status='RUNNING'",
                (retry.reason, _detail(retry.detail), utc_iso(next_at), deferrals, row["id"]),
            ).rowcount
            if updated and retry.gates:  # this attempt's diagnostics, in the same transaction
                self._gates(conn, row["id"], retry.gates, now)

    def _evaluate(
        self,
        symbol: str,
        timeframe: str,
        close: datetime,
        reads: Any,
        *,
        row: Any = None,
        deadline: datetime | None = None,
    ) -> dict:
        try:
            envelope, snapshot = self.analysis.analyse_background(symbol, timeframe)
        except MarketUnavailable as exc:
            raise self._unavailable(exc) from None
        if (envelope or {}).get("symbol") != symbol or envelope.get("timeframe") != timeframe:
            raise ValueError("envelope_mismatch: the analysis is not for this window's key")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        bars_read = _parse_optional((snapshot.get("bars") or {}).get("retrieved_at_utc"))
        if row is not None:
            previous = self._last_history_read(row["id"])
            if previous is not None and (bars_read is None or bars_read <= previous):
                # Defensive: a history-sync retry must judge a newer MT5 read than the attempt
                # it retries (never the earlier snapshot); the bars cache TTL is shorter than
                # any data retry, so this is not expected to happen.
                raise _Retry(
                    "history_sync_stale_read",
                    f"bars read {_iso_or_none(bars_read)} is not newer than {iso_z(previous)}",
                    CONTENTION_RETRY,
                )
        upstream = snapshot.get("upstream") or {}  # T2 snapshot: the bars source status
        if upstream.get("status") != "LIVE":
            raise _Retry(
                "market_data_unavailable",
                f"bars {upstream.get('status')}: {upstream.get('reason')}",
                timedelta(seconds=DATA_RETRY[0]),
            )
        items = (snapshot.get("bars") or {}).get("items") or []
        candle = next((b for b in items if b.get("close_t") == iso_z(close)), None)
        if candle is None:
            raise _Retry(
                "candle_not_available",
                f"no candle closing at {iso_z(close)} in the snapshot",
                timedelta(0),
            )
        if candle.get("closure") != "tick_observed":
            raise _Retry(
                "closure_unconfirmed",
                f"closure evidence {candle.get('closure')!r}; a tick after the close is required",
                timedelta(0),
            )
        if row is not None and deadline is not None and history_sync_retryable(envelope):
            # P6.1.1: temporarily incomplete MT5 history. Retry with a fresh read on the data
            # retry schedule, only if the next attempt still starts before the deadline;
            # otherwise this attempt completes as the normal (fail-closed) T4 rejection.
            index = min(row["attempts"], len(DATA_RETRY) - 1)  # attempts before this one
            next_at = self._clock() + timedelta(seconds=DATA_RETRY[index])
            if next_at < deadline:
                attempt = row["attempts"] + 1
                codes = t4_fail_codes(envelope)
                window = (envelope.get("window_validation") or {}).get("window") or {}
                record = {
                    "stage": "data",
                    "gate": HISTORY_SYNC_GATE,
                    "result": "SKIPPED",
                    "reason_code": "retry_scheduled",
                    "detail": _json_detail(
                        {
                            "attempt": attempt,
                            "codes": codes,
                            "next_attempt_utc": utc_iso(next_at),
                            "bars_retrieved_utc": _iso_or_none(bars_read),
                            "window": [window.get("first_t"), window.get("last_t")],
                        }
                    ),
                }
                raise _Retry(
                    "history_sync",
                    ", ".join(codes),
                    next_at - self._clock(),
                    at=next_at,
                    gates=[
                        *history_slot_gates(envelope, result="SKIPPED", attempt=attempt),
                        record,
                    ],
                )
        result = envelope.get("result") or {}
        decision = result.get("decision") or {}
        risk_result = None
        if decision.get("outcome") == "TRADE_CANDIDATE":
            before = reads.deferrals
            risk_result = self.risk.assess(symbol, timeframe, envelope)
            if reads.deferrals > before:  # a contended read would distort the risk result
                raise _Retry("mt5_worker_busy", "risk inputs deferred", CONTENTION_RETRY)
        return {
            "envelope": envelope,
            "risk": risk_result,
            "closure": candle.get("closure"),
            "window_close_utc": iso_z(close),
        }

    def _last_history_read(self, cycle_id: str) -> datetime | None:
        """MT5 bars retrieval time of the attempt the latest history-sync retry was for."""
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT detail FROM gate_outcome WHERE evaluation_id=? AND gate=? "
                "AND reason_code='retry_scheduled' ORDER BY seq DESC LIMIT 1",
                (cycle_id, HISTORY_SYNC_GATE),
            ).fetchone()
        if row is None:
            return None
        try:
            return _parse_optional(json.loads(row["detail"]).get("bars_retrieved_utc"))
        except (TypeError, ValueError):
            return None

    def _unavailable(self, exc: MarketUnavailable) -> _Retry:
        if exc.code == BACKGROUND_DEFERRED:
            return _Retry("mt5_worker_busy", exc.message, CONTENTION_RETRY)
        if exc.code in _WORKER_FAILURES:
            return _Retry("worker_backoff", f"{exc.code}: {exc.message}", FAILURE_PAUSE)
        return _Retry(
            "market_data_unavailable",
            f"{exc.code}: {exc.message}",
            timedelta(seconds=DATA_RETRY[0]),
        )

    def _complete(self, cycle_id: str, outcome: dict, deferrals: int) -> None:
        envelope, risk_result = outcome["envelope"], outcome["risk"]
        result = envelope.get("result") or {}
        decision = result.get("decision") or {}
        decision_id = self.decisions.record_analysis(envelope, "scheduled_evaluation")
        assessment_id = None
        intent = None
        if risk_result is not None:
            assessment_id = self.decisions.record_risk(risk_result, decision_id)
            # P6.2a: pure; an inconsistent result raises before anything is completed
            intent = build_intent(
                risk_result,
                evaluation_id=cycle_id,
                decision_id=decision_id,
                risk_assessment_id=assessment_id,
                window_close_utc=outcome["window_close_utc"],
            )
        gates = [
            {
                "stage": "session",
                "gate": "session.open",
                "result": "PASS",
                "reason_code": "open",
                "detail": None,
            },
            {
                "stage": "closure",
                "gate": "closure.tick_observed",
                "result": "PASS",
                "reason_code": outcome["closure"],
                "detail": None,
            },
            *analysis_gates(envelope),
            *(risk_gates(risk_result) if risk_result is not None else []),
        ]
        now = self._clock()
        risk_status = None
        if risk_result is not None:
            risk_status = _RISK_STATUS.get(risk_result.get("status"), "UNVERIFIED")
        with self.store.write() as conn:
            updated = conn.execute(
                "UPDATE evaluation_cycle SET status='COMPLETED', reason_code=NULL, "
                "reason_detail=NULL, data_quality=?, t6_outcome=?, t6_rule=?, market_direction=?, "
                "risk_status=?, decision_id=?, risk_assessment_id=?, broker_symbol=COALESCE(?, "
                "broker_symbol), contention_deferrals=contention_deferrals+?, completed_utc=? "
                "WHERE id=? AND status='RUNNING'",
                (
                    t4_state(envelope),
                    decision.get("outcome"),
                    decision.get("rule"),
                    analysis_record(envelope)["market_direction"],
                    risk_status,
                    decision_id,
                    assessment_id,
                    result.get("broker_symbol"),
                    deferrals,
                    utc_iso(now),
                    cycle_id,
                ),
            ).rowcount
            if updated:
                attempts = conn.execute(
                    "SELECT attempts FROM evaluation_cycle WHERE id=?", (cycle_id,)
                ).fetchone()[0]
                retries = conn.execute(
                    "SELECT count(*) FROM gate_outcome WHERE evaluation_id=? AND gate=? "
                    "AND reason_code='retry_scheduled'",
                    (cycle_id, HISTORY_SYNC_GATE),
                ).fetchone()[0]
                history = [
                    *history_slot_gates(envelope, result="FAIL", attempt=attempts),
                    *_history_outcome(envelope, retries, attempts),
                ]
                if history:  # directly after the T4 gate, which records the verdict unchanged
                    at = next(
                        (i + 1 for i, g in enumerate(gates) if g["gate"] == "T4.window_validation"),
                        len(gates),
                    )
                    gates = [*gates[:at], *history, *gates[at:]]
                intent_id = None
                if intent is not None:  # same transaction: intent, its audit and completion
                    intent_id = insert_intent(self.store, conn, intent, now)
                if risk_result is not None:
                    gates.append(
                        {
                            "stage": "risk",
                            "gate": "shadow_intent",
                            "result": "PASS" if intent_id else "SKIPPED",
                            "reason_code": intent.eligibility if intent else "not_constructed",
                            "detail": None,  # risk gates carry no detail; found by evaluation
                        }
                    )
                self._gates(conn, cycle_id, gates, now)
                self._audit(
                    conn,
                    cycle_id,
                    "COMPLETED",
                    None,
                    now,
                    extra={
                        "t6_outcome": decision.get("outcome"),
                        "risk_status": risk_status,
                        "shadow_intent_id": intent_id,
                    },
                )

    def _finish(
        self,
        cycle_id: str,
        expected: str,
        status: str,
        reason: str,
        detail: str | None,
        now: datetime,
        *,
        deferrals: int = 0,
    ) -> None:
        stage = {
            "mt5_worker_busy": "contention",
            "worker_backoff": "contention",
            "closure_unconfirmed": "closure",
            "candle_not_available": "closure",
            "market_data_unavailable": "data",
            "history_sync": "data",
            "history_sync_stale_read": "data",
            "market_closed": "session",
            "evaluation_error": "analysis",
        }.get(reason, "schedule")
        with self.store.write() as conn:
            updated = conn.execute(
                "UPDATE evaluation_cycle SET status=?, reason_code=?, reason_detail=?, "
                "contention_deferrals=contention_deferrals+?, completed_utc=? "
                "WHERE id=? AND status=?",
                (status, reason, _detail(detail), deferrals, utc_iso(now), cycle_id, expected),
            ).rowcount
            if updated:
                self._gates(
                    conn,
                    cycle_id,
                    [
                        {
                            "stage": stage,
                            "gate": f"{stage}.{reason}",
                            "result": "SKIPPED" if status in ("SKIPPED", "MISSED") else "FAIL",
                            "reason_code": reason,
                            "detail": detail,
                        }
                    ],
                    now,
                )
                self._audit(conn, cycle_id, status, reason, now)

    def _close_pending(self, status: str, reason: str, now: datetime) -> None:
        with self.store.read() as conn:
            ids = [
                r["id"]
                for r in conn.execute("SELECT id FROM evaluation_cycle WHERE status='PENDING'")
            ]
        for cycle_id in ids:
            self._finish(cycle_id, "PENDING", status, reason, None, now)

    def _recover_interrupted(self, now: datetime) -> None:
        """Rows left PENDING/RUNNING by an earlier process are never left looking active."""
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT id, status, deadline_utc FROM evaluation_cycle "
                "WHERE status IN ('PENDING', 'RUNNING')"
            ).fetchall()
        enabled = self.enabled()
        for row in rows:
            overdue = parse_utc(row["deadline_utc"]) <= now
            if row["status"] == "RUNNING":
                if overdue or not enabled:
                    self._finish(row["id"], "RUNNING", "FAILED", "interrupted", None, now)
                else:
                    with self.store.write() as conn:
                        conn.execute(
                            "UPDATE evaluation_cycle SET status='PENDING', "
                            "reason_code='interrupted',"
                            " next_attempt_utc=? WHERE id=? AND status='RUNNING'",
                            (utc_iso(now), row["id"]),
                        )
            elif overdue:
                self._finish(row["id"], "PENDING", "MISSED", "scheduler_not_running", None, now)
            elif not enabled:
                self._finish(row["id"], "PENDING", "SKIPPED", "scheduler_disabled", None, now)

    # ------------------------------------------------------------------ records
    @staticmethod
    def _gates(conn, cycle_id: str, gates: Iterable[Mapping[str, Any]], now: datetime) -> None:
        # Append after any rows already recorded for this window (history-sync attempts).
        first = conn.execute(
            "SELECT COALESCE(max(seq), 0) + 1 FROM gate_outcome WHERE evaluation_id=?",
            (cycle_id,),
        ).fetchone()[0]
        for seq, gate in enumerate(gates, start=first):
            conn.execute(
                "INSERT INTO gate_outcome(id, evaluation_id, seq, stage, gate, result, "
                "reason_code, detail, recorded_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    new_ulid(now),
                    cycle_id,
                    seq,
                    gate["stage"],
                    gate["gate"],
                    gate["result"],
                    gate.get("reason_code"),
                    _detail(gate.get("detail")),
                    utc_iso(now),
                ),
            )

    def _audit(self, conn, cycle_id: str, status: str, reason: str | None, now, extra=None):
        self.store.append_audit(
            conn,
            action=f"evaluation.{status.lower()}",
            table="evaluation_cycle",
            record_id=cycle_id,
            new={"status": status, "reason_code": reason, **(extra or {})},
            at=now,
        )

    # ------------------------------------------------------------------ status and reads
    def next_scheduled(self, now: datetime) -> str | None:
        upcoming = []
        for timeframe in TIMEFRAMES:
            close = shift_close(
                window_close_at_or_before(now, timeframe, self.server_clock),
                timeframe,
                self.server_clock,
                1,
            )
            upcoming.append(close + START_DELAY)
        return utc_iso(min(upcoming)) if upcoming else None

    def status(self) -> dict[str, Any]:
        now = self._clock()
        thread_alive = self._thread is not None and self._thread.is_alive()
        state = self._state
        if state == "running" and not thread_alive:
            state = "error" if self._error else "stopped"
        enabled, last_completed, counts, stale = None, None, {}, 0
        if self.store.ready:
            try:
                enabled = self.enabled()
                with self.store.read() as conn:
                    counts = {
                        r["status"]: r["n"]
                        for r in conn.execute(
                            "SELECT status, count(*) AS n FROM evaluation_cycle GROUP BY status"
                        )
                    }
                    row = conn.execute(
                        "SELECT max(completed_utc) AS t FROM evaluation_cycle "
                        "WHERE status='COMPLETED'"
                    ).fetchone()
                    last_completed = row["t"] if row else None
                    stale = conn.execute(
                        "SELECT count(*) FROM evaluation_cycle WHERE status IN "
                        "('PENDING','RUNNING') AND (owner_run IS NOT ? )",
                        (self._run_id,),
                    ).fetchone()[0]
            except JournalError:
                state = "blocked"
        else:
            state = "blocked"
        paused = self._pause_until is not None and now < self._pause_until
        # Health: only an enabled scheduler whose thread is running, with no current error and
        # no failure pause, is healthy. Everything else enabled is unhealthy, with the reason.
        if enabled is None:
            health, health_reason = "unavailable", "journal unavailable"
        elif not enabled:
            health, health_reason = "disabled", None
        elif state != "running":
            health, health_reason = "unhealthy", f"scheduler {state}"
        elif self._error:
            health, health_reason = "unhealthy", f"last pass failed: {self._error}"
        elif paused:
            health, health_reason = "unhealthy", "paused after an MT5 worker failure"
        else:
            health, health_reason = "healthy", None
        return {
            "name": self.name,
            "banner": BANNER,
            "mode": "shadow",
            "execution": "none",
            "enabled": enabled,
            "state": state,
            "health": health,
            "health_reason": health_reason,
            "error": self._error,
            "last_error": self._last_error,
            "last_error_utc": utc_iso(self._last_error_at) if self._last_error_at else None,
            "consecutive_errors": self._consecutive_errors,
            "paused_until_utc": utc_iso(self._pause_until) if self._pause_until else None,
            "last_pass_utc": utc_iso(self._last_tick) if self._last_tick else None,
            "last_completed_utc": last_completed,
            "next_scheduled_utc": self.next_scheduled(now) if enabled else None,
            "cycle_counts": {status: counts.get(status, 0) for status in STATUSES},
            "rows_from_other_runs_active": stale,
            "scope": {"instruments": list(PAIRS.values()), "timeframes": list(TIMEFRAMES)},
            "schedule": {
                "start_delay_seconds": START_DELAY.total_seconds(),
                "deadline_seconds": {tf: d.total_seconds() for tf, d in DEADLINES.items()},
                "contention_retry_seconds": CONTENTION_RETRY.total_seconds(),
                "failure_pause_seconds": FAILURE_PAUSE.total_seconds(),
                "catch_up_limit_days": CATCH_UP_LIMIT.days,
                "clock": "broker server clock (verified); never the host's local time",
            },
        }

    _CYCLE_FIELDS = (
        "id, symbol, broker_symbol, timeframe, window_close_utc, status, reason_code, "
        "reason_detail, data_quality, t6_outcome, t6_rule, market_direction, risk_status, "
        "decision_id, risk_assessment_id, attempts, contention_deferrals, deadline_utc, "
        "created_utc, started_utc, completed_utc, analysis_version, decision_version, "
        "risk_version"
    )

    def cycles(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        status: str | None = None,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses, params = [], []
        for column, value in (("symbol", symbol), ("timeframe", timeframe), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if cursor is not None:
            clauses.append("rowid < ?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100))
        with self.store.read() as conn:
            rows = conn.execute(
                f"SELECT {self._CYCLE_FIELDS}, rowid AS cursor_key FROM evaluation_cycle {where} "
                "ORDER BY rowid DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        items = [{k: r[k] for k in r.keys() if k != "cursor_key"} for r in rows[:limit]]
        next_cursor = str(rows[limit - 1]["cursor_key"]) if len(rows) > limit else None
        return {"banner": BANNER, "evaluations": items, "next_cursor": next_cursor}

    def cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self.store.read() as conn:
            row = conn.execute(
                f"SELECT {self._CYCLE_FIELDS} FROM evaluation_cycle WHERE id=?", (cycle_id,)
            ).fetchone()
            if row is None:
                return None
            gates = [
                dict(g)
                for g in conn.execute(
                    "SELECT seq, stage, gate, result, reason_code, detail, recorded_utc "
                    "FROM gate_outcome WHERE evaluation_id=? ORDER BY seq",
                    (cycle_id,),
                )
            ]
        return {"banner": BANNER, "evaluation": {**dict(row), "gates": gates}}

    # ------------------------------------------------------------------ P6.2a reads
    def intents(self, **filters: Any) -> dict[str, Any]:
        """Shadow trade intents (read-only; HYPOTHETICAL -- NOT AN ORDER)."""
        with self.store.read() as conn:
            return {"banner": BANNER, **list_intents(conn, **filters)}

    def intent(self, intent_id: str) -> dict[str, Any] | None:
        with self.store.read() as conn:
            payload = get_intent(conn, intent_id)
        return {"banner": BANNER, **payload} if payload is not None else None

    def funnel(self, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Opportunity funnel over scheduled windows whose candle closed in [start, end)."""
        clauses, params = [], []
        if start is not None:
            clauses.append("c.window_close_utc >= ?")
            params.append(start)
        if end is not None:
            clauses.append("c.window_close_utc < ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT c.id, c.rowid AS rid, c.symbol, c.timeframe, c.window_close_utc, "
                "c.status, c.reason_code, c.t6_outcome, c.market_direction, c.risk_status, "
                "ra.risk_evidence, si.eligibility AS intent_eligibility "
                "FROM evaluation_cycle c "
                "LEFT JOIN risk_assessment ra ON ra.id = c.risk_assessment_id "
                f"LEFT JOIN shadow_intent si ON si.evaluation_id = c.id {where}",
                params,
            ).fetchall()
            defects = {
                r["evaluation_id"]
                for r in conn.execute(
                    f"SELECT g.evaluation_id FROM gate_outcome g {_T4_DEFECT_JOIN}"
                )
            }
        return {"banner": BANNER, **build_funnel(rows, defect_ids=defects, start=start, end=end)}

    def diagnostics(self) -> dict[str, Any]:
        """Signal-frequency diagnostics from persisted records only. Populations are kept
        apart; execution populations are reported unavailable (no execution in P6.1)."""
        with self.store.read() as conn:
            status_counts = _pairs(
                conn, "SELECT status AS k, count(*) AS n FROM evaluation_cycle GROUP BY status"
            )
            reasons = [
                {"status": r["status"], "reason_code": r["reason_code"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT status, reason_code, count(*) AS n FROM evaluation_cycle "
                    "WHERE status IN ('SKIPPED','MISSED','FAILED') "
                    "GROUP BY status, reason_code ORDER BY n DESC"
                )
            ]
            outcomes = _pairs(
                conn,
                "SELECT t6_outcome AS k, count(*) AS n FROM evaluation_cycle "
                "WHERE status='COMPLETED' GROUP BY t6_outcome",
            )
            matrix = [
                dict(r)
                for r in conn.execute(
                    "SELECT symbol, timeframe, count(*) AS windows, "
                    "sum(status='COMPLETED') AS completed, sum(status='SKIPPED') AS skipped, "
                    "sum(status='MISSED') AS missed, sum(status='FAILED') AS failed, "
                    "sum(t6_outcome='TRADE_CANDIDATE') AS candidates, "
                    "sum(risk_status='APPROVED') AS risk_approved "
                    "FROM evaluation_cycle GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
                )
            ]
            risk = _pairs(
                conn,
                "SELECT risk_status AS k, count(*) AS n FROM evaluation_cycle "
                "WHERE status='COMPLETED' AND t6_outcome='TRADE_CANDIDATE' GROUP BY risk_status",
            )
            risk_reasons = [
                {"gate": r["gate"], "result": r["result"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT g.gate, g.result, count(DISTINCT g.evaluation_id) AS n "
                    "FROM gate_outcome g JOIN evaluation_cycle c ON c.id = g.evaluation_id "
                    "WHERE c.status='COMPLETED' AND g.stage='risk' AND g.result IN "
                    "('FAIL','BLOCKED') AND g.gate <> 'phase4.status' "
                    "GROUP BY g.gate, g.result ORDER BY n DESC"
                )
            ]
            defects = [
                r["evaluation_id"]
                for r in conn.execute(
                    f"SELECT g.evaluation_id FROM gate_outcome g {_T4_DEFECT_JOIN} "
                    "ORDER BY g.evaluation_id"
                )
            ]
            strategy_reasons = [
                {"gate": r["gate"], "result": r["result"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT g.gate, g.result, count(*) AS n FROM gate_outcome g "
                    "JOIN evaluation_cycle c ON c.id = g.evaluation_id "
                    "WHERE c.status='COMPLETED' AND g.stage IN ('strategy','data') "
                    "AND g.result IN ('FAIL','BLOCKED') "
                    f"AND NOT ({_T4_DEFECT_WHERE}) "  # historical recording defects
                    # history-sync diagnostics are reported in "history_sync", not as gates
                    f"AND g.gate NOT IN ('{HISTORY_SLOTS_GATE}', '{HISTORY_SYNC_GATE}') "
                    "GROUP BY g.gate, g.result ORDER BY n DESC LIMIT 25"
                )
            ]
            sync = _pairs(
                conn,
                "SELECT reason_code AS k, count(*) AS n FROM gate_outcome "
                f"WHERE gate='{HISTORY_SYNC_GATE}' GROUP BY reason_code",
            )
            sync_cycles = conn.execute(
                "SELECT count(DISTINCT evaluation_id) FROM gate_outcome "
                f"WHERE gate='{HISTORY_SYNC_GATE}' AND reason_code='retry_scheduled'"
            ).fetchone()[0]
            sync_ended = [
                {"status": r["status"], "reason_code": r["reason_code"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT c.status, c.reason_code, count(*) AS n FROM evaluation_cycle c "
                    "WHERE c.status IN ('SKIPPED','MISSED','FAILED') AND EXISTS (SELECT 1 FROM "
                    f"gate_outcome g WHERE g.evaluation_id = c.id AND g.gate='{HISTORY_SYNC_GATE}' "
                    "AND g.reason_code='retry_scheduled') GROUP BY c.status, c.reason_code "
                    "ORDER BY n DESC"
                )
            ]
            slot_rows = [
                {"reason_code": r["reason_code"], "result": r["result"], "count": r["n"]}
                for r in conn.execute(
                    "SELECT reason_code, result, count(*) AS n FROM gate_outcome "
                    f"WHERE gate='{HISTORY_SLOTS_GATE}' GROUP BY reason_code, result "
                    "ORDER BY reason_code, result"
                )
            ]
            t4_states = _pairs(
                conn,
                "SELECT g.reason_code AS k, count(*) AS n FROM gate_outcome g "
                "JOIN evaluation_cycle c ON c.id = g.evaluation_id "
                "WHERE c.status='COMPLETED' AND g.gate='T4.window_validation' "
                f"AND NOT ({_T4_DEFECT_WHERE}) GROUP BY g.reason_code",
            )
            rules = _pairs(
                conn,
                "SELECT t6_rule AS k, count(*) AS n FROM evaluation_cycle "
                "WHERE status='COMPLETED' GROUP BY t6_rule",
            )
            blocked_costs = _count_gate(conn, "costs.net_reward_risk", "BLOCKED")
            blocked_target = _count_gate(conn, "construction.target_structure", "FAIL")
            deferrals = conn.execute(
                "SELECT COALESCE(sum(contention_deferrals), 0) FROM evaluation_cycle"
            ).fetchone()[0]
            intents = _pairs(
                conn,
                "SELECT eligibility AS k, count(*) AS n FROM shadow_intent GROUP BY eligibility",
            )
        completed = status_counts.get("COMPLETED", 0)
        candidates = outcomes.get("TRADE_CANDIDATE", 0)
        proposals = sum(risk.get(k, 0) for k in ("APPROVED", "REJECTED", "UNVERIFIED"))
        reason_total = {(r["status"], r["reason_code"]): r["count"] for r in reasons}
        return {
            "banner": BANNER,
            "populations": {
                "evaluation_windows": sum(status_counts.values()),
                "completed_evaluations": completed,
                "market_candidates": candidates,
                "risk_proposals": proposals,
                "risk_approved_proposals": risk.get("APPROVED", 0),
                # P6.2a: hypothetical records, never orders
                "shadow_intents": sum(intents.values()),
                "risk_approved_shadow_intents": intents.get("RISK_APPROVED", 0),
                "executable_opportunities": None,
                "actual_trades": None,
                "unavailable_note": "Executable opportunities, orders, fills and closed trades "
                "are not available: P6.1 is shadow mode and implements no execution.",
            },
            "cycles": {status: status_counts.get(status, 0) for status in STATUSES},
            "skip_reasons": reasons,
            "t6_outcomes": {
                k: outcomes.get(k, 0) for k in ("TRADE_CANDIDATE", "MARKET_CONDITION", "NO_TRADE")
            },
            "t6_rules": rules,
            "candidate_rate_per_100_completed": round(candidates * 100 / completed, 2)
            if completed
            else None,
            "risk": {
                "proposals": proposals,
                "approved": risk.get("APPROVED", 0),
                "rejected": risk.get("REJECTED", 0),
                "unverified": risk.get("UNVERIFIED", 0),
                "rejection_and_block_reasons": risk_reasons,
                "blocked_by_unverified_costs": blocked_costs,
                "blocked_by_target_structure": blocked_target,
            },
            "strategy_and_data_failures": strategy_reasons,
            # Genuine T4 verdicts of completed evaluations (accepted / provisional / rejected /
            # not_run / state_missing), historical recording defects excluded.
            "t4_verdicts": t4_states,
            "historical_recording_defects": {
                "t4_gate_empty_reason_code": {
                    "count": len(defects),
                    "evaluation_ids": defects,
                    "criteria": "T4.window_validation gate with an empty reason code on a "
                    "COMPLETED cycle whose data_quality is empty: the signature of the P6.1 "
                    "recording defect (verdict 'status' read instead of 'state'), which the "
                    "corrected code can never produce",
                    "treatment": "kept unchanged and visible; excluded from T4 failure counts; "
                    "the original T4 verdict was not recorded and is not recalculated",
                }
            },
            # P6.1.1: temporary MT5 history incompleteness and its bounded retries. A recovered
            # window is the same evaluation of the same candle (counted once, never a failure).
            "history_sync": {
                "eligible_codes": sorted(HISTORY_SYNC_CODES),
                "retries_scheduled": sync.get("retry_scheduled", 0),
                "windows_with_retries": sync_cycles,
                "recovered_after_retry": sync.get("recovered", 0),
                "rejected_after_retries": sync.get("retries_exhausted", 0),
                "rejected_no_time_to_retry": sync.get("deadline_no_retry", 0),
                "not_retried_mixed_reasons": sync.get("not_retried_mixed", 0),
                "not_retried_other_rejections": sync.get("not_eligible", 0),
                "retried_then_not_completed": sync_ended,
                "missing_slot_records": slot_rows,
            },
            "contention": {
                "skipped_mt5_worker_busy": reason_total.get(("SKIPPED", "mt5_worker_busy"), 0),
                "skipped_worker_backoff": reason_total.get(("SKIPPED", "worker_backoff"), 0),
                "total_deferrals": deferrals,
            },
            "data_and_closure": {
                "closure_unconfirmed": reason_total.get(("SKIPPED", "closure_unconfirmed"), 0),
                "candle_not_available": reason_total.get(("SKIPPED", "candle_not_available"), 0),
                "market_data_unavailable": reason_total.get(
                    ("SKIPPED", "market_data_unavailable"), 0
                ),
                "market_closed": reason_total.get(("SKIPPED", "market_closed"), 0),
            },
            "by_instrument_timeframe": matrix,
        }


# Rows written by the defective P6.1 build: an empty T4 reason code (the corrected build always
# records the T4 state, ``not_run`` or ``state_missing``) on a COMPLETED cycle whose
# data_quality was left empty (the corrected build always records it).
_T4_DEFECT_WHERE = (
    "g.gate = 'T4.window_validation' AND g.reason_code IS NULL "
    "AND c.status = 'COMPLETED' AND c.data_quality IS NULL"
)
_T4_DEFECT_JOIN = f"JOIN evaluation_cycle c ON c.id = g.evaluation_id WHERE {_T4_DEFECT_WHERE}"


def _pairs(conn, sql: str) -> dict[Any, int]:
    return {r["k"]: r["n"] for r in conn.execute(sql)}


def _count_gate(conn, gate: str, result: str) -> int:
    return conn.execute(
        "SELECT count(DISTINCT g.evaluation_id) FROM gate_outcome g "
        "JOIN evaluation_cycle c ON c.id = g.evaluation_id "
        "WHERE c.status='COMPLETED' AND g.gate=? AND g.result=?",
        (gate, result),
    ).fetchone()[0]


def to_json(value: Any) -> str:  # pragma: no cover - debugging aid
    return json.dumps(value, default=str, indent=1)
