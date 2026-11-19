"""Live MT5 market data on demand: quotes and bars with a short in-memory cache.

There is no background polling. A dashboard request asks for quotes or bars; the service
answers from a cache younger than its TTL or performs one MT5 request and waits at most a
fixed timeout.

**MT5 runs in separate worker processes.** The MetaTrader5 package holds Python's GIL for the
whole duration of each call (measured: 0.25-3 s for bar requests), which would freeze every
thread of the backend, including unrelated HTTP requests. Each worker process owns one
read-only MT5 connection; the backend only waits on a pipe (which releases the GIL). A worker
that exceeds its timeout is terminated and respawned on the next request. Quotes and bars use
separate workers so a large chart download never stalls the watchlist.

Status vocabulary (per quote and per bar set):
- ``LIVE``: fetched from MT5 just now (or within the cache TTL) and the quote is recent.
- ``STALE``: fetched from MT5 just now, but the last tick is old (market closed or quiet).
- ``CACHED``: MT5 is unavailable; the value shown was fetched earlier, or comes from the
  archived Parquet bars. Never presented as live.
- ``UNAVAILABLE``: MT5 is unavailable and there is nothing cached.

The workers never start the MetaTrader 5 terminal. ``MetaTrader5.initialize()`` launches a
terminal that is not running, so a worker first confirms, by full executable path, that the
intended terminal process is already running and then attaches to exactly that path
(``find_running_terminal``). They are read-only: ticks, bars, symbol properties and the
server identity (company, server name, account type -- never the login or balance).

Every bar carries ``complete``: ``False`` while the candle can still change (see
``bar_is_complete``). Indicators must use complete bars only.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import math
import multiprocessing
import ntpath
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.adapters.base import MT5AdapterError
from johanna.adapters.mt5 import MetaTrader5Adapter, MT5TimeBaseError, ServerClock
from johanna.analysis.closure import COMPLETION_GRACE, classify_closure, is_complete  # noqa: F401
from johanna.analysis.snapshot import DEFAULT_LIMITS, SnapshotLimits, build_snapshot

LOGGER = logging.getLogger("johanna.live_market")
LIVE, STALE, CACHED, UNAVAILABLE = "LIVE", "STALE", "CACHED", "UNAVAILABLE"
DEFAULT_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD")
TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}
# Symbols per worker request: keeps each MT5 round trip well inside the request timeout
# (the first request for a symbol also resolves and selects it). A 200-pair watchlist is
# fetched in 8 sequential batches.
QUOTE_BATCH = 25
# COMPLETION_GRACE (imported above) is the single authoritative closure grace: a bar whose
# close has passed by less than this, with no later tick yet, is still treated as forming.
DEFAULT_TERMINAL_IMAGE = "terminal64.exe"
# Failures that mean the live connection (and therefore the clock check) is not current.
_CONNECTION_LOSS = frozenset(
    {
        "terminal_not_running",
        "terminal_path_mismatch",
        "multiple_terminals",
        "terminal_unverified",
        "connect_failed",
        "connection_lost",
        "not_connected_to_server",
        "no_account",
        "timeout",
        "worker_failed",
        "mt5_busy",
    }
)

# P6.1 background (shadow evaluation) reads -- docs/phase-5-architecture.md §7.2. A call made
# inside ``background_reads()`` uses the existing workers, but is admitted only when the worker
# is idle, nobody is waiting for it and it has been quiet for ``quiet_seconds``; otherwise it
# is refused at once with ``BACKGROUND_DEFERRED`` (never queued, never waits). Background reads
# never change the connection state, the server-clock check or the quote and bar caches that
# the dashboard's freshness is judged by, and never fall back to cached or archived bars.
BACKGROUND_DEFERRED = "background_deferred"
BACKGROUND_QUIET_SECONDS = 0.75


@dataclass
class BackgroundReads:
    """One background evaluation attempt's view of worker admission."""

    quiet_seconds: float = BACKGROUND_QUIET_SECONDS
    admitted: int = 0
    deferrals: int = 0


_BACKGROUND: contextvars.ContextVar[BackgroundReads | None] = contextvars.ContextVar(
    "johanna_background_reads", default=None
)


@contextlib.contextmanager
def background_reads(quiet_seconds: float = BACKGROUND_QUIET_SECONDS) -> Iterator[BackgroundReads]:
    """Mark the MT5 reads made by this thread (and context) as background reads."""
    reads = BackgroundReads(quiet_seconds)
    token = _BACKGROUND.set(reads)
    try:
        yield reads
    finally:
        _BACKGROUND.reset(token)


def in_background() -> bool:
    return _BACKGROUND.get() is not None


def _deferred(reads: BackgroundReads, why: str) -> MarketUnavailable:
    reads.deferrals += 1
    return MarketUnavailable(BACKGROUND_DEFERRED, f"Background read deferred: {why}.")


class MarketUnavailable(Exception):
    """Live MT5 data cannot be obtained right now; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def _process_image_paths() -> list[str]:
    """Full executable paths of running processes this user can query (Windows, stdlib only).

    Processes that cannot be opened (other users, protected) are skipped; the user's own MT5
    terminal is always queryable.
    """
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    size = 4096
    while True:
        pids = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "EnumProcesses failed")
        if needed.value < ctypes.sizeof(pids):
            break
        size *= 2
    query_limited_information = 0x1000
    paths: list[str] = []
    buffer = ctypes.create_unicode_buffer(32768)
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            continue
        try:
            length = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                paths.append(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    return paths


def _normalized_path(path: str) -> str:
    return ntpath.normcase(ntpath.normpath(path))


def find_running_terminal(
    executable: str | None,
    *,
    processes: Callable[[], list[str]] | None = None,
) -> str:
    """Full path of the already-running MT5 terminal Johanna may attach to.

    ``MetaTrader5.initialize(path)`` starts a terminal that is not running, so Johanna only
    ever calls it with the path of a process confirmed running here:

    - ``executable`` configured: a process with exactly that full path must be running. A
      different terminal with the same image name (another broker's install) does not count.
    - not configured: exactly one terminal process must be running; its path is used.

    Raises ``MarketUnavailable`` with a specific reason otherwise; never starts anything.
    """
    if processes is None:
        if os.name != "nt":
            raise MarketUnavailable(
                "terminal_not_running", "MetaTrader 5 runs only on Windows; none is running here."
            )
        processes = _process_image_paths
    image = ntpath.basename(executable) if executable else DEFAULT_TERMINAL_IMAGE
    try:
        running = processes()
    except OSError:
        raise MarketUnavailable(
            "terminal_unverified",
            "Could not confirm that the MT5 terminal is running; Johanna will not connect, "
            "because connecting could start a terminal.",
        ) from None
    terminals = {
        _normalized_path(path): path
        for path in running
        if ntpath.basename(path).lower() == image.lower()
    }
    if executable:
        match = terminals.get(_normalized_path(executable))
        if match is not None:
            return match
        if terminals:
            raise MarketUnavailable(
                "terminal_path_mismatch",
                f"An MT5 terminal is running, but not the configured one ({executable}). "
                "Johanna never starts a terminal: open that one, or correct MT5_TERMINAL_PATH.",
            )
    elif len(terminals) == 1:
        return next(iter(terminals.values()))
    elif len(terminals) > 1:
        raise MarketUnavailable(
            "multiple_terminals",
            f"{len(terminals)} MT5 terminals are running. Set MT5_TERMINAL_PATH to the one "
            "Johanna should read.",
        )
    raise MarketUnavailable(
        "terminal_not_running",
        "The MetaTrader 5 terminal is not running. Open it to see live data "
        "(Johanna never starts it).",
    )


def bar_is_complete(close_utc: datetime, latest_tick_utc: datetime | None, now: datetime) -> bool:
    """Whether a bar closing at ``close_utc`` can no longer change.

    Evidence, in order: a server tick at or after the close proves the server has moved past
    the bar; otherwise the system clock must be past the close by more than
    ``COMPLETION_GRACE`` (the tolerated clock skew). Without either the bar is still forming.
    This errs toward "forming": e.g. Friday's D1 bar stays forming after the market closes
    until its 24 h have elapsed.
    """
    return is_complete(close_utc, latest_tick_utc, now)  # the shared T1 rule (analysis.closure)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def pip_size(digits: int | None, point: float | None) -> float | None:
    """One pip: 10 points on 3/5-digit quotes (fractional pips), otherwise one point.

    ``None`` when the broker did not provide usable digits/point metadata."""
    if digits is None or point is None:
        return None
    return point * 10 if digits in (3, 5) else point


# ---------------------------------------------------------------------- bar checks
# Per-candle closure evidence (see bar_is_complete):
#   "forming"        still open: values can change;
#   "tick_observed"  closed, and a server tick at/after its close was seen;
#   "clock_only"     closed only because the clock is past its close (no later tick seen);
#   "unknown"        no close time could be established (DST gap/fold, archived bars).
# Closure says the interval has ended; it does NOT prove the feed covered the whole candle.
FORMING, TICK_OBSERVED, CLOCK_ONLY, UNKNOWN_CLOSURE = (
    "forming",
    "tick_observed",
    "clock_only",
    "unknown",
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def ohlc_issues(row: dict[str, Any]) -> list[str]:
    """Problems with one bar's values; an empty list means the values are consistent."""
    prices = [_finite(row.get(name)) for name in ("open", "high", "low", "close")]
    if any(price is None for price in prices):
        return ["non_finite_price"]
    issues = []
    if any(price <= 0 for price in prices):
        issues.append("non_positive_price")
    open_, high, low, close = prices
    if high < max(open_, close, low):
        issues.append("high_below_open_close_or_low")
    if low > min(open_, close, high):
        issues.append("low_above_open_close_or_high")
    volume = row.get("tick_volume")
    if volume is not None and volume < 0:
        issues.append("negative_tick_volume")
    return issues


def check_bars(
    rows: Sequence[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], list[str]]], list[dict[str, Any]], dict[str, int]]:
    """Validate bars in the order received, without discarding anything silently.

    Returns ``(kept, rejected, summary)``:
    - ``kept``: one ``(row, issues)`` per distinct open time, sorted by time. A bar that
      arrived after a later-dated bar gets ``out_of_order``; the kept bar of a duplicated
      time gets ``duplicate_timestamp``; inconsistent values get their OHLC issue codes.
    - ``rejected``: every further bar with an already-seen open time (reported, not used),
      with whether it was identical to the kept one.
    - ``summary``: counts for the response.
    """
    issues: list[list[str]] = [ohlc_issues(row) for row in rows]
    newest = None
    for index, row in enumerate(rows):
        timestamp = row["timestamp"]
        if newest is not None and timestamp < newest:
            issues[index].append("out_of_order")
        newest = timestamp if newest is None else max(newest, timestamp)
    order = sorted(range(len(rows)), key=lambda index: rows[index]["timestamp"])
    first_at: dict[Any, int] = {}
    kept_indices: list[int] = []
    rejected: list[dict[str, Any]] = []
    for index in order:
        timestamp = rows[index]["timestamp"]
        if timestamp not in first_at:
            first_at[timestamp] = index
            kept_indices.append(index)
            continue
        kept = first_at[timestamp]
        if "duplicate_timestamp" not in issues[kept]:
            issues[kept].append("duplicate_timestamp")
        identical = all(
            rows[index].get(name) == rows[kept].get(name)
            for name in ("open", "high", "low", "close", "tick_volume")
        )
        rejected.append(
            {
                **_bar_values(rows[index]),
                "reason": "duplicate_timestamp",
                "identical_to_kept": identical,
            }
        )
    kept = [(rows[index], issues[index]) for index in kept_indices]
    summary = {
        "bars_received": len(rows),
        "bars_returned": len(kept),
        "invalid_bars": sum(1 for _, found in kept if found),
        "duplicates_rejected": len(rejected),
        "out_of_order": sum(1 for found in issues if "out_of_order" in found),
    }
    return kept, rejected, summary


def _is_real_tick(tick: dict[str, Any]) -> bool:
    """False for MT5's placeholder tick (epoch 0, zero prices) before a symbol has loaded."""
    bid, ask = tick.get("bid"), tick.get("ask")
    seconds = tick.get("time_msc") or tick.get("time")
    return bool(seconds) and bool(bid) and bool(ask) and bid > 0 and ask > 0


# ====================================================================== MT5 session
class MT5Session:
    """One read-only MT5 connection and its per-connection caches.

    Runs inside an MT5 worker (a child process in production, a thread in tests). Results
    are plain, picklable data; failures are ``MarketUnavailable``.
    """

    def __init__(
        self,
        adapter_factory: Callable[[], MetaTrader5Adapter],
        terminal_running: Callable[[], bool],
        *,
        reconnect_interval: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter_factory = adapter_factory
        self._terminal_running = terminal_running
        self.reconnect_interval = reconnect_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._adapter: MetaTrader5Adapter | None = None
        self._next_connect = 0.0
        self._last_failure: MarketUnavailable | None = None
        self._symbols: dict[str, str | None] = {}
        self._formats: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        adapter, self._adapter = self._adapter, None
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception:
                LOGGER.debug("MT5 shutdown failed", exc_info=True)

    def quotes(self, pairs: Sequence[str]) -> dict[str, Any]:
        def job(adapter: MetaTrader5Adapter) -> dict[str, Any]:
            results: dict[str, dict[str, Any]] = {}
            for pair in pairs:
                symbol = self._broker_symbol(adapter, pair)
                if symbol is None:
                    results[pair] = {"error": "symbol_not_offered"}
                    continue
                fmt = self._format(adapter, symbol)
                tick = adapter.get_tick(symbol)
                bid, ask = tick.get("bid"), tick.get("ask")
                if not _is_real_tick(tick):
                    # Just added to Market Watch: MT5 returns an all-zero placeholder tick
                    # until the first real one arrives. Never show it as a price.
                    results[pair] = {"error": "symbol_loading"}
                    continue
                pip = pip_size(fmt["digits"], fmt["point"])
                results[pair] = {
                    "broker_symbol": symbol,
                    "bid": bid,
                    "ask": ask,
                    "digits": fmt["digits"],
                    "point": fmt["point"],
                    "pip_size": pip,
                    "spread_pips": round((ask - bid) / pip, 2) if bid and ask and pip else None,
                    "quote_time": tick.get("time_utc"),
                    "fetched_at": self._clock(),
                }
            return results

        return self._run(job)

    def bars(self, pair: str, timeframe: str, count: int) -> dict[str, Any]:
        def job(adapter: MetaTrader5Adapter) -> dict[str, Any]:
            symbol = self._broker_symbol(adapter, pair)
            if symbol is None:
                raise MarketUnavailable("symbol_not_offered", f"The broker does not offer {pair}.")
            fmt = self._format(adapter, symbol)
            # A tick is the precise time-base check: on H4/D1 a misconfigured server clock
            # can shift bar open times without pushing them into the future. Its time is also
            # the server-side evidence for which candles have closed.
            tick = adapter.get_tick(symbol)
            tick_retrieved = self._clock()  # MT5 retrieval time of the snapshot tick
            # Read as received: anomalies are reported per bar instead of failing the request.
            rows = adapter.read_bars(symbol, timeframe, count, include_current=True)
            bars_retrieved = self._clock()  # MT5 retrieval time of the bars
            real_tick = _is_real_tick(tick)
            latest_tick = tick.get("time_utc") if real_tick else None
            now = bars_retrieved
            seconds = TIMEFRAME_SECONDS[timeframe]
            kept, rejected, summary = check_bars(rows)
            bars = []
            for row, issues in kept:
                try:
                    close = adapter.server_clock.bar_close_utc(row["timestamp"], seconds)
                except MT5AdapterError:  # close falls in a DST gap/fold: cannot prove closed
                    bars.append(
                        _bar(
                            row, close=None, complete=False, closure=UNKNOWN_CLOSURE, issues=issues
                        )
                    )
                    continue
                closure = classify_closure(close, latest_tick, now)  # shared T1 rule
                complete = closure != FORMING
                bars.append(
                    _bar(row, close=close, complete=complete, closure=closure, issues=issues)
                )
            return {
                "symbol": pair,
                "broker_symbol": symbol,
                "timeframe": timeframe,
                "digits": fmt["digits"],
                "point": fmt["point"],
                "pip_size": pip_size(fmt["digits"], fmt["point"]),
                "bars": bars,
                "rejected_bars": rejected,
                "quality": summary,
                "latest_tick_utc": _iso(latest_tick),
                # The snapshot tick: read on the same connection immediately before the bars.
                "tick": {
                    "bid": tick.get("bid") if real_tick else None,
                    "ask": tick.get("ask") if real_tick else None,
                    "time_utc": _iso(latest_tick),
                    "retrieved_at_utc": _iso(tick_retrieved),
                },
                "retrieved_at_utc": _iso(bars_retrieved),
                "fetched_at": now,
                "source": "mt5",
                "time_basis": "utc",
            }

        return self._run(job)

    def broker(self, pairs: Sequence[str]) -> dict[str, Any]:
        """Raw account and instrument properties for Phase 3 (read-only; validated elsewhere).

        Account first, then each symbol's specification and tick, then the server identity is
        read again: the reads are sequential on one connection, not atomic, so each part
        carries its own retrieval time and the caller can detect a server change mid-read.
        """

        def job(adapter: MetaTrader5Adapter) -> dict[str, Any]:
            account = adapter.account_snapshot()
            account_retrieved = self._clock()
            symbols: dict[str, dict[str, Any]] = {}
            for pair in pairs:
                symbol = self._broker_symbol(adapter, pair)
                if symbol is None:
                    symbols[pair] = {"error": "symbol_not_offered"}
                    continue
                self._format(adapter, symbol)  # selects it into Market Watch if hidden
                spec = adapter.symbol_snapshot(symbol)
                tick = adapter.get_tick(symbol)
                real = _is_real_tick(tick)
                symbols[pair] = {
                    "broker_symbol": symbol,
                    "spec": spec,
                    "tick": {
                        "bid": tick.get("bid") if real else None,
                        "ask": tick.get("ask") if real else None,
                        "time_utc": tick.get("time_utc") if real else None,
                    },
                    "retrieved_at": self._clock(),
                }
            return {
                **account,
                "account_retrieved_at": account_retrieved,
                "symbols": symbols,
                "identity_after": adapter.server_identity(),
            }

        return self._run(job)

    def exposure(self, day_start_utc: datetime) -> dict[str, Any]:
        """Phase 4: open positions, pending orders and today's deals (read-only).

        The risk to stop-loss of each position and pending order is computed here with MT5's
        own profit calculator, on the same connection. ``None`` risk means no stop-loss.
        """

        def job(adapter: MetaTrader5Adapter) -> dict[str, Any]:
            clock = adapter.server_clock
            now = self._clock()
            raw = adapter.exposure_snapshot(clock.from_utc(day_start_utc), clock.from_utc(now))
            retrieved = self._clock()
            for item in raw["positions"]:
                side = {0: "buy", 1: "sell"}.get(item.get("type"))
                item["side"] = side
                item["risk_to_stop"] = _risk_to_stop(
                    adapter,
                    side,
                    item.get("symbol"),
                    item.get("volume"),
                    item.get("price_open"),
                    item.get("sl"),
                )
            for item in raw["orders"]:
                code = item.get("type")
                side = None
                if isinstance(code, int) and 2 <= code <= 7:
                    side = "buy" if code % 2 == 0 else "sell"
                item["side"] = side
                if code in (6, 7):  # BUY/SELL stop-limit: execution at the limit price
                    item["risk_to_stop"] = _stop_limit_risk(adapter, side, item)
                    continue
                item["risk_to_stop"] = _risk_to_stop(
                    adapter,
                    side,
                    item.get("symbol"),
                    item.get("volume_current"),
                    item.get("price_open"),
                    item.get("sl"),
                )
            for deal in raw["deals"]:
                seconds = deal.get("time")
                deal["time_utc"] = clock.to_utc(float(seconds)) if seconds is not None else None
            return {**raw, "day_start_utc": day_start_utc, "retrieved_at": retrieved}

        return self._run(job)

    def reconciliation(self, from_utc: datetime, to_utc: datetime) -> dict[str, Any]:
        """P6.3: positions, orders and deals in [from_utc, to_utc] with broker identifiers
        (read-only; converted to HMAC references by the caller before use)."""

        def job(adapter: MetaTrader5Adapter) -> dict[str, Any]:
            clock = adapter.server_clock
            raw = adapter.reconciliation_snapshot(clock.from_utc(from_utc), clock.from_utc(to_utc))
            retrieved = self._clock()
            for group, key in (("positions", "time"), ("orders", "time_setup"), ("deals", "time")):
                for item in raw[group]:
                    seconds = item.get(key)
                    item["time_utc"] = clock.to_utc(float(seconds)) if seconds is not None else None
            return {
                **raw,
                "coverage_from_utc": from_utc,
                "coverage_to_utc": to_utc,
                "retrieved_at": retrieved,
            }

        return self._run(job)

    def risk_calc(self, requests: Sequence[tuple]) -> list[dict[str, Any]]:
        """Phase 4: MT5 calculator results for ``("profit", side, symbol, volume, open, close)``
        and ``("margin", side, symbol, volume, price)`` requests. Per-request failures are
        returned as ``{"error": ...}``; calculations only, nothing is sent to the server."""

        def job(adapter: MetaTrader5Adapter) -> list[dict[str, Any]]:
            results = []
            for request in requests:
                kind, args = request[0], request[1:]
                try:
                    if kind == "profit":
                        results.append({"value": adapter.calc_profit(*args)})
                    elif kind == "margin":
                        results.append({"value": adapter.calc_margin(*args)})
                    else:
                        results.append({"error": f"unknown calculation {kind!r}"})
                except (MT5AdapterError, ValueError, TypeError) as exc:
                    results.append({"error": str(exc)})
            return results

        return self._run(job)

    # -- internals
    def _run(self, job: Callable[[MetaTrader5Adapter], Any]) -> dict[str, Any]:
        try:
            adapter = self._connected_adapter()
            identity = self._identity(adapter)
            value = job(adapter)
        except MT5TimeBaseError as exc:
            raise self._failed(MarketUnavailable("server_clock_mismatch", str(exc))) from None
        except MarketUnavailable as exc:
            raise self._failed(exc) from None
        except MT5AdapterError as exc:
            self.close()
            raise self._failed(MarketUnavailable("mt5_error", str(exc))) from None
        self._last_failure = None
        self._next_connect = 0.0
        return {"identity": identity, "value": value}

    def _failed(self, exc: MarketUnavailable) -> MarketUnavailable:
        self._last_failure = exc
        return exc

    def _connected_adapter(self) -> MetaTrader5Adapter:
        if self._adapter is not None:
            return self._adapter
        now = time.monotonic()
        if now < self._next_connect and self._last_failure is not None:
            raise self._last_failure
        self._next_connect = now + self.reconnect_interval
        if not self._terminal_running():
            raise MarketUnavailable(
                "terminal_not_running",
                "The MetaTrader 5 terminal is not running. Open it to see live data "
                "(Johanna never starts it).",
            )
        adapter = self._adapter_factory()
        try:
            adapter.initialize()
        except MT5AdapterError as exc:
            raise MarketUnavailable("connect_failed", str(exc)) from None
        self._adapter = adapter
        self._symbols.clear()
        self._formats.clear()
        return adapter

    def _identity(self, adapter: MetaTrader5Adapter) -> dict[str, Any]:
        try:
            identity = adapter.server_identity()
        except MT5AdapterError as exc:
            self.close()
            raise MarketUnavailable("connection_lost", str(exc)) from None
        if not identity.get("terminal_connected"):
            raise MarketUnavailable(
                "not_connected_to_server",
                "MetaTrader 5 is running but not connected to its trade server.",
            )
        if identity.get("server") is None:
            raise MarketUnavailable("no_account", "MetaTrader 5 has no logged-in account.")
        return identity

    def _broker_symbol(self, adapter: MetaTrader5Adapter, pair: str) -> str | None:
        if pair not in self._symbols:
            self._symbols[pair] = adapter.resolve_symbol(pair)
        return self._symbols[pair]

    def _format(self, adapter: MetaTrader5Adapter, symbol: str) -> dict[str, Any]:
        if symbol not in self._formats:
            fmt = adapter.price_format(symbol)
            if not fmt["visible"]:
                # Make the symbol's quotes available in Market Watch; a read-only view change.
                adapter.select_symbol(symbol)
            self._formats[symbol] = fmt
        return self._formats[symbol]


def _risk_to_stop(
    adapter: MetaTrader5Adapter,
    side: str | None,
    symbol: Any,
    volume: Any,
    price_open: Any,
    sl: Any,
) -> dict[str, Any]:
    """Money at risk from the open price to the stop-loss (MT5 calculator; read-only)."""
    if side is None or not isinstance(symbol, str):
        return {"status": "unverified", "reason": "unknown position or order type"}
    if not sl:
        return {"status": "unbounded", "reason": "no stop-loss set"}
    try:
        value = adapter.calc_profit(side, symbol, float(volume), float(price_open), float(sl))
    except (MT5AdapterError, TypeError, ValueError) as exc:
        return {"status": "unverified", "reason": str(exc)}
    # A stop beyond the open price in the profitable direction locks in a gain: no risk.
    return {"status": "verified", "amount": max(0.0, -value), "calculator_result": value}


def _stop_limit_risk(adapter: MetaTrader5Adapter, side: str | None, item: dict) -> dict[str, Any]:
    """F4: a stop-limit order is filled at its limit price (or better), never assumed at the
    trigger. Risk is the larger of the two entry prices to the stop-loss; without a readable
    limit price the exposure cannot be bounded -> unverified."""
    trigger, limit = item.get("price_open"), item.get("price_stoplimit")
    valid = [
        p
        for p in (trigger, limit)
        if isinstance(p, int | float) and not isinstance(p, bool) and math.isfinite(p) and p > 0
    ]
    if len(valid) != 2:
        return {"status": "unverified", "reason": "stop-limit trigger or limit price unreadable"}
    results = [
        _risk_to_stop(
            adapter, side, item.get("symbol"), item.get("volume_current"), price, item.get("sl")
        )
        for price in (trigger, limit)
    ]
    for result in results:
        if result["status"] != "verified":
            return result  # unbounded (no stop-loss) or unverified
    worst = max(results, key=lambda result: result["amount"])
    return {
        **worst,
        "basis": "stop-limit: larger risk of the trigger and limit entry prices",
        "risk_at_trigger": results[0]["amount"],
        "risk_at_limit": results[1]["amount"],
    }


# ====================================================================== runners
class ThreadSessionRunner:
    """Runs an ``MT5Session`` on one dedicated thread. For tests with an in-memory fake;
    production uses ``ProcessSessionRunner`` because the real package holds the GIL."""

    def __init__(self, session_factory: Callable[[], MT5Session]) -> None:
        self._session_factory = session_factory
        self._session: MT5Session | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: dict[tuple, Future] = {}
        self._job_started: float | None = None
        self._last_finished = float("-inf")
        self._lock = threading.Lock()

    def run(self, op: str, args: tuple, timeout: float) -> Any:
        key = (op, *args)
        background = _BACKGROUND.get()
        with self._lock:
            if background is not None:
                # Admitted only when idle, nothing in flight and quiet; never queued.
                if self._job_started is not None or any(
                    not f.done() for f in self._inflight.values()
                ):
                    raise _deferred(background, "the worker is busy")
                if time.monotonic() - self._last_finished < background.quiet_seconds:
                    raise _deferred(background, "the worker was used moments ago")
                background.admitted += 1
                key = ("background", *key)  # never joins a dashboard job's result
            started = self._job_started
            if started is not None and time.monotonic() - started > timeout:
                raise MarketUnavailable(
                    "mt5_busy", f"MT5 has not answered for {time.monotonic() - started:.0f} s."
                )
            future = self._inflight.get(key)
            if future is None or future.done():
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(1, thread_name_prefix="johanna-mt5")
                future = self._executor.submit(self._call, op, args, background is not None)
                self._inflight[key] = future
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            raise MarketUnavailable(
                "timeout", f"MT5 did not respond within {timeout:.0f} s"
            ) from None

    def _call(self, op: str, args: tuple, background: bool = False) -> Any:
        with self._lock:
            self._job_started = time.monotonic()
        try:
            if self._session is None:
                self._session = self._session_factory()
            return getattr(self._session, op)(*args)
        finally:
            with self._lock:
                self._job_started = None
                if not background:  # the quiet window measures foreground activity only
                    self._last_finished = time.monotonic()

    def close(self) -> None:
        executor, self._executor = self._executor, None
        if executor is None:
            return
        session = self._session
        if session is not None:
            try:
                executor.submit(session.close).result(timeout=5)
            except Exception:
                LOGGER.warning("MT5 session did not close cleanly")
        executor.shutdown(wait=False, cancel_futures=True)


def _real_session(terminal_path: str | None, zone: str, shift: int) -> MT5Session:
    """Session factory used inside MT5 worker processes (real MetaTrader5 package)."""
    clock = ServerClock(zone, shift)
    verified: dict[str, str] = {}

    def terminal_running() -> bool:
        # Raises MarketUnavailable (with the reason) unless the intended terminal is running.
        verified["path"] = find_running_terminal(terminal_path)
        return True

    # Always attach by the verified path: initialize() without one could start a terminal.
    return MT5Session(
        lambda: MetaTrader5Adapter(verified["path"], server_clock=clock),
        terminal_running,
    )


def _worker_main(connection, factory: Callable[..., MT5Session], factory_args: tuple) -> None:
    """Entry point of an MT5 worker process: serve requests until closed."""
    try:
        session = factory(*factory_args)
    except Exception as exc:  # report and exit; the parent shows MT5 as unavailable
        connection.send(("unavailable", "worker_failed", f"{type(exc).__name__}: {exc}"))
        return
    connection.send(("ready",))
    try:
        while True:
            try:
                op, args = connection.recv()
            except (EOFError, OSError):
                break
            if op == "close":
                break
            try:
                connection.send(("ok", getattr(session, op)(*args)))
            except MarketUnavailable as exc:
                connection.send(("unavailable", exc.code, exc.message))
            except Exception as exc:
                connection.send(("unavailable", "mt5_error", f"{type(exc).__name__}: {exc}"))
    finally:
        session.close()


class ProcessSessionRunner:
    """Runs an ``MT5Session`` in a child process, so MT5 calls cannot block the backend.

    One request at a time per worker. A request that exceeds its timeout terminates the
    worker. Worker failures are of two kinds, both followed by a bounded exponential backoff
    (``backoff_initial`` doubling up to ``backoff_max``) during which requests are answered
    at once with the last failure instead of spawning another process:

    - ``worker_failed``: the worker could not start, or died;
    - ``timeout``: the worker did not answer in time (MT5 hung) and was terminated.

    Normal MT5 unavailability (terminal closed, offline, clock mismatch) is answered by a
    healthy worker; that is not a worker failure (the session has its own reconnect pacing).
    Any reply from a worker resets the backoff.
    """

    def __init__(
        self,
        factory: Callable[..., MT5Session] = _real_session,
        factory_args: tuple = (),
        *,
        name: str = "mt5",
        start_timeout: float = 20.0,
        backoff_initial: float = 2.0,
        backoff_max: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory
        self._factory_args = factory_args
        self.name = name
        self.start_timeout = start_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._monotonic = monotonic
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._connection = None
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_at = 0.0
        self._last_failure: MarketUnavailable | None = None
        self.spawn_count = 0
        # P6.1 admission: foreground callers waiting for the lock, and when it was last freed.
        self._waiting = 0
        self._waiting_lock = threading.Lock()
        self._last_release = float("-inf")

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def backoff_seconds(self) -> float:
        """Seconds until the next worker start is allowed (0 when not backing off)."""
        return max(0.0, self._retry_at - self._monotonic()) if self._failures else 0.0

    def _acquire(self, timeout: float) -> None:
        background = _BACKGROUND.get()
        if background is None:
            with self._waiting_lock:
                self._waiting += 1
            try:
                acquired = self._lock.acquire(timeout=timeout)
            finally:
                with self._waiting_lock:
                    self._waiting -= 1
            if not acquired:
                raise MarketUnavailable("mt5_busy", "MT5 is still answering a previous request.")
            return
        # Background: never waits and never queues behind (or ahead of) a foreground request.
        if self._waiting:
            raise _deferred(background, "a foreground request is waiting")
        if self._monotonic() - self._last_release < background.quiet_seconds:
            raise _deferred(background, "the worker was used moments ago")
        if not self._lock.acquire(blocking=False):
            raise _deferred(background, "the worker is busy")
        background.admitted += 1

    def run(self, op: str, args: tuple, timeout: float) -> Any:
        self._acquire(timeout)
        try:
            self._refuse_during_backoff()
            try:
                self._ensure_worker()
            except MarketUnavailable as exc:
                raise self._worker_failed(exc) from None
            try:
                self._connection.send((op, args))
            except (OSError, EOFError, ValueError):
                self._kill()
                raise self._worker_failed(
                    MarketUnavailable("worker_failed", "The MT5 worker stopped unexpectedly.")
                ) from None
            if not self._connection.poll(timeout):
                LOGGER.warning("MT5 %s worker exceeded %.0f s; terminating it", self.name, timeout)
                self._kill()
                raise self._worker_failed(
                    MarketUnavailable("timeout", f"MT5 did not respond within {timeout:.0f} s.")
                )
            try:
                reply = self._connection.recv()
            except (EOFError, OSError):
                self._kill()
                raise self._worker_failed(
                    MarketUnavailable("worker_failed", "The MT5 worker exited.")
                ) from None
            self._failures, self._retry_at, self._last_failure = 0, 0.0, None
        finally:
            if _BACKGROUND.get() is None:  # the quiet window measures foreground activity only
                self._last_release = self._monotonic()
            self._lock.release()
        if reply[0] == "ok":
            return reply[1]
        raise MarketUnavailable(reply[1], reply[2])

    def _refuse_during_backoff(self) -> None:
        if self._process is not None or not self._failures or self._last_failure is None:
            return
        remaining = self._retry_at - self._monotonic()
        if remaining > 0:
            last = self._last_failure
            raise MarketUnavailable(
                last.code, f"{last.message} Next attempt in {max(1, round(remaining))} s."
            )

    def _worker_failed(self, exc: MarketUnavailable) -> MarketUnavailable:
        self._failures += 1
        delay = min(self.backoff_max, self.backoff_initial * 2 ** (self._failures - 1))
        self._retry_at = self._monotonic() + delay
        self._last_failure = exc
        LOGGER.warning(
            "MT5 %s worker failure %d (%s); next start in %.0f s",
            self.name,
            self._failures,
            exc.code,
            delay,
        )
        return exc

    def _ensure_worker(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._kill()
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_worker_main,
            args=(child, self._factory, self._factory_args),
            name=f"johanna-{self.name}-worker",
            daemon=True,
        )
        process.start()
        self.spawn_count += 1
        child.close()

        def abandon(code: str, message: str) -> MarketUnavailable:
            parent.close()
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            return MarketUnavailable(code, message)

        if not parent.poll(self.start_timeout):
            raise abandon("worker_failed", "The MT5 worker did not start in time.")
        try:
            message = parent.recv()
        except (EOFError, OSError):
            raise abandon("worker_failed", "The MT5 worker exited on start.") from None
        if message[0] != "ready":
            raise abandon("worker_failed", message[2])
        self._process, self._connection = process, parent

    def _kill(self) -> None:
        process, connection = self._process, self._connection
        self._process = self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is not None and process.is_alive():
            process.terminate()
            process.join(timeout=5)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None and self._process is not None:
                try:
                    self._connection.send(("close", ()))
                    self._process.join(timeout=5)
                except (OSError, EOFError, ValueError):
                    pass
            self._kill()


def _default_runner(terminal_path: str | None, server_clock: ServerClock, name: str):
    return ProcessSessionRunner(
        _real_session, (terminal_path, server_clock.zone, server_clock.shift_hours), name=name
    )


# ====================================================================== service
class LiveMarketService:
    name = "live_market"

    def __init__(
        self,
        *,
        terminal_path: str | None = None,
        server_clock: ServerClock | None = None,
        adapter_factory: Callable[[], MetaTrader5Adapter] | None = None,
        terminal_running: Callable[[], bool] | None = None,
        cached_bars: Callable[[str, str], tuple[list[dict[str, Any]], str | None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        quote_ttl: timedelta = timedelta(seconds=1),
        bars_ttl: timedelta = timedelta(seconds=5),
        stale_after: timedelta = timedelta(seconds=60),
        request_timeout: float = 4.0,
        bars_timeout: float = 20.0,
        reconnect_interval: float = 5.0,
        verification_ttl: timedelta = timedelta(seconds=30),
        snapshot_limits: SnapshotLimits = DEFAULT_LIMITS,
    ) -> None:
        self.terminal_path = terminal_path
        self.snapshot_limits = snapshot_limits
        self.verification_ttl = verification_ttl
        self.server_clock = server_clock or ServerClock()
        self._cached_bars = cached_bars
        self._clock = clock or (lambda: datetime.now(UTC))
        self.quote_ttl = quote_ttl
        self.bars_ttl = bars_ttl
        self.stale_after = stale_after
        self.request_timeout = request_timeout
        self.bars_timeout = bars_timeout
        if adapter_factory is not None:  # in-process (tests with an in-memory fake)
            running = terminal_running or (lambda: True)

            def session() -> MT5Session:
                return MT5Session(
                    adapter_factory,
                    running,
                    reconnect_interval=reconnect_interval,
                    clock=self._clock,
                )

            self._quote_runner: Any = ThreadSessionRunner(session)
            self._bar_runner: Any = ThreadSessionRunner(session)
        else:
            self._quote_runner = _default_runner(terminal_path, self.server_clock, "quotes")
            self._bar_runner = _default_runner(terminal_path, self.server_clock, "bars")

        self._lock = threading.Lock()
        self._started = False
        self._quotes: dict[str, dict[str, Any]] = {}
        self._bars: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._identity: dict[str, Any] | None = None
        self._unavailable: MarketUnavailable | None = None
        self._last_success: datetime | None = None
        # Clock check vocabulary: not_checked | verified | stale_ticks | unavailable | mismatch.
        self._time_check: dict[str, Any] = {"result": "not_checked"}
        self._time_check_at: datetime | None = None
        self._last_verified: datetime | None = None

    # -- lifecycle
    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        for runner in (self._quote_runner, self._bar_runner):
            try:
                runner.close()
            except Exception:
                LOGGER.warning("MT5 worker did not stop cleanly", exc_info=True)

    def status(self) -> dict[str, object]:
        with self._lock:
            unavailable = self._unavailable
            identity = dict(self._identity) if self._identity else None
            last_success = self._last_success
            time_check = dict(self._time_check)
            checked_at = self._time_check_at
            last_verified = self._last_verified
        if (
            time_check["result"] == "verified"
            and checked_at is not None
            and self._clock() - checked_at > self.verification_ttl
        ):
            # Verification is a statement about the current connection; it expires unless
            # a fresh live tick re-confirms it.
            time_check = {"result": "unavailable", "reason": "not_rechecked"}
        time_check["last_verified_utc"] = _iso(last_verified)
        if not self._started:
            state = "stopped"
        elif unavailable is not None:
            state = "unavailable"
        elif last_success is None:
            state = "idle"
        else:
            state = "connected"
        return {
            "name": self.name,
            "state": state,
            "source": "mt5",
            "server": identity,
            "server_clock": self.server_clock.describe(),
            "time_check": time_check,
            "last_success_utc": _iso(last_success),
            "unavailable_reason": unavailable.code if unavailable else None,
            "message": unavailable.message if unavailable else None,
        }

    def _set_time_check(self, result: str, now: datetime | None = None, **fields: Any) -> None:
        """Record the clock check; callers hold no lock. ``last_verified_utc`` is kept apart."""
        now = now or self._clock()
        with self._lock:
            self._time_check = {"result": result, "checked_at_utc": _iso(now), **fields}
            self._time_check_at = now
            if result == "verified":
                self._last_verified = now

    def _request(self, runner: Any, op: str, args: tuple, timeout: float) -> Any:
        if in_background():  # P6.1: no connection, clock-check or freshness state changes
            return runner.run(op, args, timeout)["value"]
        try:
            payload = runner.run(op, args, timeout)
        except MarketUnavailable as exc:
            with self._lock:
                self._unavailable = exc
            if exc.code == "server_clock_mismatch":
                self._set_time_check("mismatch", message=exc.message)
            elif exc.code in _CONNECTION_LOSS:
                self._set_time_check("unavailable", reason=exc.code)
            raise
        with self._lock:
            self._identity = payload["identity"]
            self._unavailable = None
            self._last_success = self._clock()
        return payload["value"]

    # -- quotes
    def quotes(self, pairs: Sequence[str]) -> dict[str, Any]:
        pairs = list(dict.fromkeys(pairs))
        now = self._clock()
        with self._lock:
            cached = {pair: self._quotes.get(pair) for pair in pairs}
        all_fresh = bool(pairs) and all(
            quote is not None and now - quote["fetched_at"] < self.quote_ttl
            for quote in cached.values()
        )
        failures: dict[str, MarketUnavailable] = {}
        fetched: dict[str, dict[str, Any]] = {}
        if all_fresh:
            fetched = {pair: quote for pair, quote in cached.items() if quote}
        else:
            for start in range(0, len(pairs), QUOTE_BATCH):
                batch = tuple(pairs[start : start + QUOTE_BATCH])
                try:
                    result = self._request(
                        self._quote_runner, "quotes", (batch,), self.request_timeout
                    )
                except MarketUnavailable as exc:
                    failures.update(dict.fromkeys(batch, exc))
                    continue
                fetched.update(result)
                with self._lock:
                    for pair, quote in result.items():
                        if "error" not in quote:
                            self._quotes[pair] = quote
        now = self._clock()
        rows = [
            self._quote_row(pair, fetched.get(pair), cached.get(pair), now, failures.get(pair))
            for pair in pairs
        ]
        if not all_fresh:  # cached ticks never (re)verify the clock
            self._update_time_check(rows, now, failures)
        return {
            "status": self._overall(rows),
            "requested_at_utc": _iso(now),
            "connection": self.status(),
            "quotes": rows,
        }

    def _quote_row(
        self,
        pair: str,
        fetched: dict[str, Any] | None,
        previous: dict[str, Any] | None,
        now: datetime,
        unavailable: MarketUnavailable | None,
    ) -> dict[str, Any]:
        if fetched is not None and "error" in fetched:
            return {"symbol": pair, "status": UNAVAILABLE, "reason": fetched["error"]}
        quote, status = None, UNAVAILABLE
        if unavailable is None and fetched is not None:
            quote = fetched
            age = (now - quote["quote_time"]).total_seconds() if quote["quote_time"] else None
            status = LIVE if age is not None and age <= self.stale_after.total_seconds() else STALE
        elif previous is not None:
            quote, status = previous, CACHED
        if quote is None:
            return {
                "symbol": pair,
                "status": UNAVAILABLE,
                "reason": unavailable.code if unavailable else "no_data",
            }
        quote_time = quote["quote_time"]
        return {
            "symbol": pair,
            "broker_symbol": quote["broker_symbol"],
            "status": status,
            "bid": quote["bid"],
            "ask": quote["ask"],
            "spread_pips": quote["spread_pips"],
            "digits": quote["digits"],
            "point": quote.get("point"),
            "pip_size": quote.get("pip_size"),
            "quote_time_utc": _iso(quote_time),
            "quote_age_seconds": round((now - quote_time).total_seconds(), 1)
            if quote_time
            else None,
            "fetched_at_utc": _iso(quote["fetched_at"]),
            "reason": unavailable.code if status == CACHED and unavailable else None,
        }

    def _update_time_check(
        self,
        rows: list[dict[str, Any]],
        now: datetime,
        failures: dict[str, MarketUnavailable],
    ) -> None:
        """Check the configured server clock against ticks fetched from MT5 just now.

        Only rows fetched in this request count (LIVE/STALE); CACHED values never verify.
        - ``verified``: the freshest tick converts to within [-60 s, +120 s] of system UTC.
        - ``stale_ticks``: connected, but every tick is older (market closed or quiet, or a
          server clock behind UTC); not verifiable now.
        - ``mismatch``: ticks ahead of UTC were refused.
        - ``unavailable``: no fresh tick could be obtained (disconnected, timeout, ...).
        """
        ages = [
            row["quote_age_seconds"]
            for row in rows
            if row["status"] in (LIVE, STALE) and row.get("quote_age_seconds") is not None
        ]
        if ages:
            freshest = min(ages)
            result = "verified" if -60 <= freshest <= 120 else "stale_ticks"
            self._set_time_check(result, now, freshest_quote_age_seconds=freshest)
            return
        failure = next(iter(failures.values()), None)
        if failure is not None and failure.code == "server_clock_mismatch":
            self._set_time_check("mismatch", now, message=failure.message)
        else:
            self._set_time_check(
                "unavailable", now, reason=failure.code if failure else "no_usable_tick"
            )

    @staticmethod
    def _overall(rows: list[dict[str, Any]]) -> str:
        statuses = {row["status"] for row in rows}
        for status in (LIVE, STALE, CACHED):
            if status in statuses:
                return status
        return UNAVAILABLE

    # -- account and broker (Phase 3)
    def broker_read(self, pairs: Sequence[str]) -> dict[str, Any]:
        """One read-only account + instrument read on the quotes worker's connection.

        Returns ``{"identity": ..., "value": ...}`` (identity as read before the job) or raises
        ``MarketUnavailable``. Not cached here; ``AccountIntelligenceService`` owns caching
        and validation. Uses the existing worker, so no additional MT5 connection is opened
        and calls never run concurrently with quotes.
        """
        return self._worker_read("broker", (tuple(dict.fromkeys(pairs)),))

    def exposure_read(self, day_start_utc: datetime) -> dict[str, Any]:
        """Phase 4: positions, pending orders and deals since ``day_start_utc`` (read-only)."""
        return self._worker_read("exposure", (day_start_utc,))

    def reconciliation_read(self, from_utc: datetime, to_utc: datetime) -> dict[str, Any]:
        """P6.3: read-only reconciliation snapshot on the existing quotes worker."""
        return self._worker_read("reconciliation", (from_utc, to_utc))

    def risk_calculations(self, requests: Sequence[tuple]) -> dict[str, Any]:
        """Phase 4: MT5 calculator results (``order_calc_profit``/``order_calc_margin``)."""
        return self._worker_read("risk_calc", (tuple(requests),))

    def _worker_read(self, op: str, args: tuple) -> dict[str, Any]:
        """One read-only job on the quotes worker; ``{"identity", "value"}``."""
        if in_background():  # P6.1: no connection-state changes
            return self._quote_runner.run(op, args, self.request_timeout)
        try:
            payload = self._quote_runner.run(op, args, self.request_timeout)
        except MarketUnavailable as exc:
            with self._lock:
                self._unavailable = exc
            raise
        with self._lock:
            self._identity = payload["identity"]
            self._unavailable = None
            self._last_success = self._clock()
        return payload

    # -- bars
    def bars(self, pair: str, timeframe: str, count: int = 300) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {', '.join(TIMEFRAMES)}")
        key = (pair, timeframe, count)
        now = self._clock()
        with self._lock:
            cached = self._bars.get(key)
        if cached is not None and now - cached["fetched_at"] < self.bars_ttl:
            # Within the cache TTL: same MT5 observation, served again (never presented as new).
            return self._bars_payload(cached, LIVE, now, from_cache=True)
        if in_background():
            # P6.1: a fresh MT5 read only -- never stored in the dashboard's cache, never a
            # CACHED or archived fallback (a failure or deferral is raised to the scheduler).
            entry = self._request(self._bar_runner, "bars", key, self.bars_timeout)
            return self._bars_payload(entry, LIVE, self._clock())
        try:
            entry = self._request(self._bar_runner, "bars", key, self.bars_timeout)
        except MarketUnavailable as exc:
            if cached is not None:
                return self._bars_payload(
                    cached, CACHED, now, reason=exc.code, message=exc.message, from_cache=True
                )
            return self._archived_bars(pair, timeframe, count, exc, now)
        with self._lock:
            self._bars[key] = entry
        return self._bars_payload(entry, LIVE, self._clock())

    def snapshot(self, pair: str, timeframe: str, count: int = 300) -> dict[str, Any]:
        """Coordinated market-data snapshot (T2) for one symbol and timeframe.

        Built from one bars response: the snapshot tick and the bars come from the same
        bars-worker job, read one after the other, each with its retrieval time. The quotes
        worker is not consulted. Eligibility is judged at assembly time; callers re-evaluate
        with ``johanna.analysis.snapshot.evaluate_snapshot`` at their own calculation time.
        """
        payload = self.bars(pair, timeframe, count)
        return build_snapshot(
            payload,
            snapshot_id=uuid.uuid4().hex,
            assembled_at=self._clock(),
            limits=self.snapshot_limits,
        )

    def _archived_bars(
        self, pair: str, timeframe: str, count: int, exc: MarketUnavailable, now: datetime
    ) -> dict[str, Any]:
        rows, error = self._cached_bars(pair, timeframe) if self._cached_bars else ([], None)
        if error or not rows:
            return {
                "symbol": pair,
                "timeframe": timeframe,
                "status": UNAVAILABLE,
                "reason": exc.code,
                "message": exc.message,
                "bars": [],
            }
        rows = rows[-count:]
        legacy = any(r.get("source") == "MT5" and r.get("source_timezone") == "UTC" for r in rows)
        kept, rejected, summary = check_bars(rows)
        entry = {
            "symbol": pair,
            "broker_symbol": None,
            "timeframe": timeframe,
            "digits": None,
            "point": None,
            "pip_size": None,
            # The collector stores completed bars only (it never requests the forming one);
            # there is no tick evidence for archived bars, so their closure is "unknown".
            "bars": [
                _bar(row, close=None, complete=True, closure=UNKNOWN_CLOSURE, issues=issues)
                for row, issues in kept
            ],
            "rejected_bars": rejected,
            "quality": summary,
            "latest_tick_utc": None,
            "fetched_at": None,
            "source": "parquet_archive",
            # Bars collected before server-clock conversion carry MT5 server time.
            "time_basis": "legacy_server_time" if legacy else "utc",
        }
        return self._bars_payload(entry, CACHED, now, reason=exc.code, message=exc.message)

    @staticmethod
    def _bars_payload(
        entry: dict[str, Any],
        status: str,
        now: datetime,
        *,
        reason: str | None = None,
        message: str | None = None,
        from_cache: bool = False,
    ) -> dict[str, Any]:
        """Bars response. ``retrieved_at_utc`` (and ``tick.retrieved_at_utc``) are MT5
        retrieval times and never change for a cached entry; ``requested_at_utc`` is when this
        response was served; ``served_from_cache`` says whether it is a repeat."""
        return {
            "tick": None,
            "retrieved_at_utc": None,
            **{key: value for key, value in entry.items() if key != "fetched_at"},
            "status": status,
            "fetched_at_utc": _iso(entry.get("fetched_at")),
            "requested_at_utc": _iso(now),
            "served_from_cache": from_cache,
            "reason": reason,
            "message": message,
        }


def _bar_values(row: dict[str, Any]) -> dict[str, Any]:
    """A bar's values as sent: unchanged, except non-finite numbers become ``None`` (JSON)."""
    timestamp = row["timestamp"]
    return {
        "t": _iso(timestamp) if isinstance(timestamp, datetime) else str(timestamp),
        "o": _finite(row["open"]),
        "h": _finite(row["high"]),
        "l": _finite(row["low"]),
        "c": _finite(row["close"]),
        "v": row.get("tick_volume"),
        "spread_points": row.get("spread_points"),
    }


def _bar(
    row: dict[str, Any],
    *,
    close: datetime | None,
    complete: bool,
    closure: str,
    issues: list[str],
) -> dict[str, Any]:
    """Wire format of one bar.

    ``t`` UTC open time; ``o h l c v`` as received (non-finite values sent as ``None``);
    ``close_t`` UTC close time (``None`` when unknown); ``complete`` whether the candle's
    interval has ended; ``closure`` the evidence for that (see ``check_bars``);
    ``spread_points`` MT5's per-bar spread in points (``None`` if not provided); ``valid``
    and ``issues`` the result of the bar checks.
    """
    return {
        **_bar_values(row),
        "close_t": _iso(close),
        "complete": complete,
        "closure": closure,
        "valid": not issues,
        "issues": list(issues),
    }
