"""Market-data and economic-calendar application services over the existing local stores.

These services do not add new provider code. They read what the existing collectors and
importers already persist (``ParquetBarStore`` partitions and manifests, collector progress
files, and ``WeeklySnapshotCache`` JSON snapshots) and, when explicitly configured, run the
existing read-only collectors as supervised background workers.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from johanna.adapters.economic_calendar import (
    CalendarAdapterError,
    CalendarSnapshot,
    WeeklySnapshotCache,
)
from johanna.market_data import ParquetBarStore
from johanna.services import BackgroundService

LOGGER = logging.getLogger("johanna.data_services")
_PARTITION_GLOB = "symbol=*/timeframe=*/year=*/month=*/bars.parquet"
Clock = Callable[[], datetime]
Task = Callable[[threading.Event], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.warning("Ignoring unreadable collector state file %s", path)
        return None
    return payload if isinstance(payload, dict) else None


def week_start_utc(now: datetime) -> date:
    """Monday of the current UTC week; matches ``CalendarCollector.poll_once``."""
    today = now.astimezone(UTC).date()
    return today - timedelta(days=today.weekday())


class MarketDataService:
    """Serves stored bars and reports real market-data freshness and monitoring state.

    ``bar_roots`` are ``ParquetBarStore`` roots in ascending precedence: when two roots hold
    a bar for the same symbol, timeframe and timestamp, the later root wins. The application
    passes the imported-history root first and the live collector root last.

    Market monitoring is the optional ``monitor_task`` background worker. Its desired state
    comes from the persisted ``market_monitoring_enabled`` preference via
    :meth:`set_monitoring`; without a configured task, monitoring reports ``not_configured``.
    """

    name = "market_data"

    def __init__(
        self,
        *,
        bar_roots: Sequence[Path],
        collector_state_path: Path | None,
        provider: str,
        monitor_task: Task | None = None,
        stale_after: timedelta = timedelta(minutes=15),
        clock: Clock | None = None,
    ) -> None:
        self.bar_roots = tuple(Path(root) for root in bar_roots)
        self.collector_state_path = collector_state_path
        self.provider = provider
        self.stale_after = stale_after
        self._clock = clock or _utc_now
        self._monitor = BackgroundService("market_monitor", monitor_task) if monitor_task else None
        self._monitoring_enabled = False
        self._started = False
        self._lock = threading.RLock()  # lifecycle (worker start/stop)
        self._cache_lock = threading.Lock()  # bar cache only
        self._cache_signature: tuple | None = None
        self._cache_rows: list[dict[str, Any]] = []

    @property
    def monitor_configured(self) -> bool:
        return self._monitor is not None

    def start(self) -> None:
        with self._lock:
            self._started = True
            if self._monitoring_enabled and self._monitor is not None:
                self._monitor.start()

    def stop(self) -> None:
        with self._lock:
            try:
                if self._monitor is not None:
                    self._monitor.stop()
            finally:
                self._started = False

    def set_monitoring(self, enabled: bool) -> None:
        """Apply the persisted monitoring preference to the worker (start or stop it)."""
        with self._lock:
            self._monitoring_enabled = bool(enabled)
            if not self._started or self._monitor is None:
                return
            if self._monitoring_enabled:
                self._monitor.start()
            else:
                self._monitor.stop()

    def read_bars(self) -> tuple[list[dict[str, Any]], str | None]:
        """Return de-duplicated bars sorted by symbol, timeframe and timestamp.

        Results are cached until a partition file is added, removed or rewritten, so the
        dashboard poll does not re-read unchanged Parquet files.
        """
        with self._cache_lock:
            try:
                files = [
                    path
                    for root in self.bar_roots
                    for path in sorted((root / "clean").glob(_PARTITION_GLOB))
                ]
                signature = tuple(
                    (str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in files
                )
            except OSError:
                LOGGER.exception("Could not inspect local market history")
                return [], "Stored market data could not be read."
            if signature == self._cache_signature:
                return self._cache_rows, None
            try:
                merged: dict[tuple[str, str, datetime], dict[str, Any]] = {}
                for root in self.bar_roots:
                    for row in ParquetBarStore(root).read().to_pylist():
                        merged[(row["symbol"], row["timeframe"], row["timestamp"])] = row
            except Exception:
                LOGGER.exception("Could not read local market history")
                return [], "Stored market data could not be read."
            rows = sorted(
                merged.values(), key=lambda row: (row["symbol"], row["timeframe"], row["timestamp"])
            )
            self._cache_signature = signature
            self._cache_rows = rows
            return rows, None

    def _last_ingestion(self) -> datetime | None:
        latest: datetime | None = None
        for root in self.bar_roots:
            try:
                manifest = ParquetBarStore(root).manifest()
            except (OSError, ValueError):
                LOGGER.warning("Ignoring unreadable market-data manifest under %s", root)
                continue
            if not manifest or manifest.get("status") != "complete":
                continue
            value = _parse_utc(manifest.get("ingestion_time_utc"))
            if value and (latest is None or value > latest):
                latest = value
        return latest

    def _collector_summary(self) -> tuple[datetime | None, list[dict[str, object]]]:
        state = _read_json_file(self.collector_state_path)
        if not state:
            return None, []
        latest: datetime | None = None
        errors: list[dict[str, object]] = []
        for group in ("targets", "tick_targets"):
            targets = state.get(group)
            if not isinstance(targets, dict):
                continue
            for target, values in sorted(targets.items()):
                if not isinstance(values, dict):
                    continue
                success = _parse_utc(values.get("last_successful_retrieval_utc"))
                if group == "targets" and success and (latest is None or success > latest):
                    latest = success
                if values.get("status") == "error" or values.get("last_error"):
                    errors.append(
                        {
                            "target": f"{group}:{target}",
                            "error": str(values.get("last_error") or "error")[:200],
                            "last_attempt_utc": values.get("last_attempt_utc"),
                        }
                    )
        return latest, errors

    def monitoring_state(self) -> str:
        if not self._monitoring_enabled:
            return "paused"
        if self._monitor is None:
            return "not_configured"
        if not self._started:
            return "stopped"
        return str(self._monitor.status()["state"])

    def status(self) -> dict[str, object]:
        now = self._clock()
        rows, read_error = self.read_bars()
        latest_bar = max((row["timestamp"] for row in rows), default=None)
        last_ingestion = self._last_ingestion()
        last_retrieval, target_errors = self._collector_summary()
        candidates = [value for value in (last_ingestion, last_retrieval) if value is not None]
        last_success = max(candidates) if candidates else None
        if read_error:
            freshness = "error"
        elif not rows:
            freshness = "no_data"
        elif last_success is not None and timedelta(0) <= now - last_success <= self.stale_after:
            freshness = "fresh"
        else:
            freshness = "stale"
        monitoring = self.monitoring_state()
        if read_error or monitoring in {"error", "exited"}:
            state = "error"
        elif monitoring == "running" and target_errors:
            state = "degraded"
        elif monitoring == "running":
            state = "running"
        else:
            state = "idle" if self._started else "stopped"
        return {
            "name": self.name,
            "state": state,
            "provider": self.provider,
            "monitoring_enabled": self._monitoring_enabled,
            "monitoring_state": monitoring,
            "monitor": self._monitor.status() if self._monitor else None,
            "freshness": freshness,
            "stale_after_seconds": int(self.stale_after.total_seconds()),
            "latest_bar_utc": iso_utc(latest_bar) if isinstance(latest_bar, datetime) else None,
            "last_successful_update_utc": iso_utc(last_success),
            "bar_count": len(rows),
            "target_errors": target_errors if monitoring == "running" else [],
            "last_recorded_errors": target_errors,
            "error": read_error,
        }


class CalendarService:
    """Serves the cached weekly calendar snapshot with accurate freshness.

    Snapshots are always readable from the cache. Refreshing is the optional
    ``refresh_task`` worker (the existing ``CalendarCollector``), started only when the
    calendar integration is enabled and a task is configured.
    """

    name = "economic_calendar"

    def __init__(
        self,
        *,
        snapshot_dir: Path,
        collector_state_path: Path | None,
        enabled: bool,
        refresh_task: Task | None = None,
        max_age: timedelta = timedelta(hours=6),
        clock: Clock | None = None,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.collector_state_path = collector_state_path
        self.enabled = enabled
        self.max_age = max_age
        self._clock = clock or _utc_now
        self._worker = (
            BackgroundService("calendar_refresh", refresh_task)
            if enabled and refresh_task is not None
            else None
        )
        self._started = False

    def start(self) -> None:
        self._started = True
        if self._worker is not None:
            self._worker.start()

    def stop(self) -> None:
        try:
            if self._worker is not None:
                self._worker.stop()
        finally:
            self._started = False

    def current_week(self) -> tuple[date, CalendarSnapshot | None, str | None]:
        week_start = week_start_utc(self._clock())
        try:
            snapshot = WeeklySnapshotCache(self.snapshot_dir).read(week_start)
        except (CalendarAdapterError, OSError):
            LOGGER.exception("Could not read cached economic calendar")
            return week_start, None, "The saved calendar snapshot could not be read."
        return week_start, snapshot, None

    def freshness(self, snapshot: CalendarSnapshot) -> str:
        """Same rule as ``CachedEconomicCalendar``: fresh only if 0 <= age <= max_age."""
        age = self._clock() - snapshot.retrieved_at_utc
        return "fresh" if timedelta(0) <= age <= self.max_age else "stale"

    def latest_snapshot_week(self) -> str | None:
        weeks = sorted(
            path.stem.removeprefix("week-") for path in self.snapshot_dir.glob("week-*.json")
        )
        return weeks[-1] if weeks else None

    def events(self) -> tuple[list[dict[str, object]], str | None]:
        _, snapshot, error = self.current_week()
        if error or snapshot is None:
            return [], error
        freshness = self.freshness(snapshot)
        return [
            {
                "event_name": event.event_name,
                "currency": event.currency,
                "scheduled_at_utc": iso_utc(event.scheduled_at_utc),
                "impact": event.impact,
                "actual": event.actual,
                "forecast": event.forecast,
                "previous": event.previous,
                "freshness": freshness,
                "source": event.source,
            }
            for event in snapshot.events
        ], None

    def status(self) -> dict[str, object]:
        week_start, snapshot, error = self.current_week()
        if error:
            freshness = "error"
        elif snapshot is None:
            freshness = "no_snapshot"
        else:
            freshness = self.freshness(snapshot)
        worker = self._worker.status() if self._worker else None
        collector_state = _read_json_file(self.collector_state_path) or {}
        if error or (worker and worker["state"] in {"error", "exited"}):
            state = "error"
        elif worker and worker["state"] == "running":
            # The collector records poll failures (403/429/network) in its state file and
            # keeps running; report that instead of a healthy "running".
            state = "degraded" if collector_state.get("status") == "error" else "running"
        elif not self.enabled:
            state = "disabled"
        elif self._worker is None:
            state = "not_configured"
        else:
            state = "idle" if self._started else "stopped"
        return {
            "name": self.name,
            "state": state,
            "enabled": self.enabled,
            "refresh": worker,
            "week_start": week_start.isoformat(),
            "freshness": freshness,
            "max_age_seconds": int(self.max_age.total_seconds()),
            "snapshot_retrieved_at_utc": iso_utc(snapshot.retrieved_at_utc) if snapshot else None,
            "snapshot_source": snapshot.source if snapshot else None,
            "event_count": len(snapshot.events) if snapshot else 0,
            "latest_snapshot_week": self.latest_snapshot_week(),
            "collector_status": collector_state.get("status"),
            "collector_last_error": collector_state.get("last_error"),
            "collector_last_attempt_utc": collector_state.get("last_attempt_utc"),
            "error": error,
        }
