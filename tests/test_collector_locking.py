"""Source-level collector coordination between the CLI and the backend monitor.

Offline only: MT5 is replaced by the existing synthetic ``_DryRunMT5`` adapter and the
calendar uses the existing synthetic dry-run provider.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import johanna.collectors as collectors
from johanna.application import JohannaApplication
from johanna.collectors import (
    CollectorBusyError,
    CollectorConfig,
    CollectorConfigError,
    SourceLock,
    run_configured_source,
)
from johanna.config import AppConfig, StorageConfig


def _write_config(tmp_path: Path, *, mt5_enabled: bool = True) -> Path:
    out = (tmp_path / "out").as_posix()
    state = (tmp_path / "state").as_posix()
    path = tmp_path / "collectors.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [collector]
            output_dir = "{out}"
            state_dir = "{state}"

            [mt5]
            enabled = {"true" if mt5_enabled else "false"}
            collect_ticks = false
            symbols = ["EURUSD"]
            timeframes = ["M5"]
            poll_seconds = 60
            bars_per_poll = 3
            max_retries = 0

            [calendar]
            enabled = true
            authorized_for_local_cache = false
            poll_seconds = 900
            """
        ),
        encoding="utf-8",
    )
    return path


def _cli_once(path: Path) -> int:
    return collectors.main(["--config", str(path), "--source", "mt5", "--dry-run", "--once"])


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _is_locked(state_dir: Path, source: str) -> bool:
    probe = SourceLock(state_dir, source)
    try:
        probe.acquire()
    except CollectorBusyError:
        return True
    probe.release()
    return False


def _continuous_dry_run(config: CollectorConfig, source: str):
    stop = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            run_configured_source(config, source, dry_run=True, stop_event=stop)
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop, thread, errors


def test_cli_is_refused_while_another_collector_runs_same_source(tmp_path) -> None:
    path = _write_config(tmp_path)
    config = CollectorConfig.from_file(path)
    stop, thread, errors = _continuous_dry_run(config, "mt5")
    try:
        assert _wait_for(lambda: _is_locked(config.state_dir, "mt5"))
        code = _cli_once(path)
        assert code == 3  # busy, distinct from configuration errors (2)
        with pytest.raises(CollectorBusyError, match="mt5 collector is already running"):
            run_configured_source(config, "mt5", once=True, dry_run=True)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors
    assert not thread.is_alive()
    # Lock released on stop: the next run proceeds.
    assert _cli_once(path) == 0


def test_independent_sources_do_not_block_each_other(tmp_path) -> None:
    config = CollectorConfig.from_file(_write_config(tmp_path))
    stop, thread, errors = _continuous_dry_run(config, "mt5")
    try:
        assert _wait_for(lambda: _is_locked(config.state_dir, "mt5"))
        run_configured_source(config, "calendar", once=True, dry_run=True)  # not blocked
        assert not _is_locked(config.state_dir, "calendar")
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors


def test_lock_is_released_when_collection_raises(tmp_path) -> None:
    config = CollectorConfig.from_file(_write_config(tmp_path, mt5_enabled=False))
    with pytest.raises(CollectorConfigError, match="disabled"):
        run_configured_source(config, "mt5", once=True, dry_run=True)
    assert not _is_locked(config.state_dir, "mt5")


def test_leftover_lock_file_without_owner_does_not_block(tmp_path) -> None:
    config = CollectorConfig.from_file(_write_config(tmp_path))
    lock_path = config.state_dir / "mt5.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("pid=999999 collector:mt5", encoding="utf-8")
    run_configured_source(config, "mt5", once=True, dry_run=True)
    assert lock_path.exists()  # files are never a lock by themselves


def test_lock_held_by_killed_process_is_recovered(tmp_path) -> None:
    state_dir = tmp_path / "state"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from johanna.collectors import SourceLock\n"
            "lock = SourceLock(sys.argv[1], 'mt5')\n"  # keep a reference: GC would release
            "lock.acquire()\n"
            "print('locked', flush=True)\n"
            "time.sleep(120)\n",
            str(state_dir),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(CollectorBusyError):
            SourceLock(state_dir, "mt5").acquire()
    finally:
        holder.kill()  # simulate a crash: no release, lock file left behind
        holder.wait(timeout=10)
    assert (state_dir / "mt5.lock").exists()
    assert _wait_for(lambda: not _is_locked(state_dir, "mt5"))


def test_threads_in_one_process_also_exclude_each_other(tmp_path) -> None:
    first = SourceLock(tmp_path, "mt5")
    first.acquire()
    try:
        with pytest.raises(CollectorBusyError):
            SourceLock(tmp_path, "mt5").acquire()
    finally:
        first.release()
    with SourceLock(tmp_path, "mt5"):
        pass


def _backend(tmp_path: Path, collectors_path: Path) -> JohannaApplication:
    base = AppConfig.load(environ={})
    config = replace(
        base,
        storage=StorageConfig(tmp_path / "data"),
        integrations=replace(base.integrations, collectors_config=collectors_path),
    )
    return JohannaApplication(config)


def test_backend_monitor_fails_visibly_when_cli_holds_the_source(tmp_path) -> None:
    # mt5 disabled: if the lock were (wrongly) not enforced, this fails with a config
    # error instead of ever constructing a real MT5 adapter.
    path = _write_config(tmp_path, mt5_enabled=False)
    config = CollectorConfig.from_file(path)
    cli_lock = SourceLock(config.state_dir, "mt5")
    cli_lock.acquire()
    app = _backend(tmp_path, path)
    try:
        app.start()
        assert _wait_for(lambda: app.status()["market_monitoring_state"] == "error")
        monitor = app.status()["services"]["market_data"]["monitor"]
        assert monitor["error"] == "CollectorBusyError"
        assert app.health()[1] == {"status": "degraded", "failed_services": ["market_data"]}
    finally:
        app.stop()
        cli_lock.release()


def test_cli_is_refused_while_backend_monitor_runs(tmp_path, monkeypatch) -> None:
    # Swap the real adapter for the existing synthetic one: no MT5 terminal is touched.
    monkeypatch.setattr(
        collectors,
        "MetaTrader5Adapter",
        lambda terminal_path=None, **_: collectors._DryRunMT5(collectors._utc_now),
    )
    path = _write_config(tmp_path)
    config = CollectorConfig.from_file(path)
    app = _backend(tmp_path, path)
    app.start()
    try:
        assert _wait_for(lambda: app.status()["market_monitoring_state"] == "running")
        assert _wait_for(lambda: _is_locked(config.state_dir, "mt5"))
        code = _cli_once(path)
        assert code == 3
        assert app.status()["market_monitoring_state"] == "running"  # backend unaffected
        app.save_preferences({"market_monitoring_enabled": False})  # stops the worker
        assert not _is_locked(config.state_dir, "mt5")
        assert _cli_once(path) == 0
    finally:
        app.stop()
