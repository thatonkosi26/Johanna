r"""READ-ONLY live acceptance of the live market workspace against the running MT5 terminal.

Usage (from the repository root, with MT5 open and logged in):

    .venv311\Scripts\python.exe scriptserify_live_market.py

Prints a JSON report. Uses a temporary data directory; never writes to data/.

- Real entry point (`python -m johanna`), all collectors disabled, temporary data directory.
- Johanna's quotes/bars cross-checked against an independent direct MetaTrader5 read.
- Restart persistence of preferences/watchlist.
- Responsiveness while MT5 requests are in flight.
- Closed-terminal behaviour simulated with a terminal path whose process is not running
  (the user's terminal is never closed, started or modified).
- Never starts MT5: the direct read attaches only to a terminal confirmed running by full
  executable path (``johanna.live_market.find_running_terminal``), exactly as the backend's
  workers do. Set MT5_TERMINAL_PATH to choose among several installed terminals.
- The repository's data/ directory is fingerprinted before and after.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from johanna.live_market import MarketUnavailable, find_running_terminal

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable
# None: attach to the single running terminal (refused if several are running).
TERMINAL = os.environ.get("MT5_TERMINAL_PATH")
OUT = Path(tempfile.mkdtemp(prefix="johanna-live-acceptance-"))
PORT = 8790
BASE = f"http://127.0.0.1:{PORT}"


def fingerprint(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def get(path, timeout=15):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read())


def post(path, payload):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def launch(tag, **extra_env):
    env = dict(
        os.environ,
        JOHANNA_DATA_DIR=str(OUT / "data"),
        JOHANNA_SERVER_PORT=str(PORT),
        PYTHONDONTWRITEBYTECODE="1",
        **extra_env,
    )
    env.pop("JOHANNA_COLLECTORS_CONFIG", None)  # collectors stay disabled
    log = open(OUT / f"{tag}.log", "wb")
    proc = subprocess.Popen(
        [PY, "-m", "johanna"], cwd=str(OUT), env=env, stdout=log, stderr=subprocess.STDOUT
    )
    for _ in range(100):
        try:
            get("/health", timeout=2)
            return proc, log
        except Exception:
            time.sleep(0.1)
    raise SystemExit(f"{tag}: backend did not start")


def stop(proc, log):
    proc.kill()
    proc.wait()
    log.close()


def direct_read(quotes, terminal_path, *, mt5=None, processes=None):
    """Independent direct MetaTrader5 read (raw epochs, no Johanna data path).

    Guarded like the backend: ``initialize`` is called only with the full path of a
    terminal confirmed already running, so it can never start one. Returns
    ``{"skipped": reason}`` when the terminal cannot be verified.
    """
    try:
        path = find_running_terminal(terminal_path, processes=processes)
    except MarketUnavailable as exc:
        return {"skipped": f"{exc.code}: {exc.message}"}
    if mt5 is None:
        import MetaTrader5 as mt5
    if not mt5.initialize(path):
        return {"skipped": f"initialize failed: {mt5.last_error()}"}
    try:
        cross = []
        for q in quotes["quotes"]:
            if not q.get("broker_symbol"):
                continue
            tick = mt5.symbol_info_tick(q["broker_symbol"])
            info = mt5.symbol_info(q["broker_symbol"])
            pip = info.point * (10 if info.digits in (3, 5) else 1)
            cross.append(
                {
                    "symbol": q["symbol"],
                    "johanna_bid": q.get("bid"),
                    "direct_bid": tick.bid,
                    "johanna_spread_pips": q.get("spread_pips"),
                    "direct_spread_pips": round((tick.ask - tick.bid) / pip, 2),
                    "raw_tick_minus_system_s": round(tick.time_msc / 1000 - time.time(), 1),
                }
            )
        first = next((q["broker_symbol"] for q in quotes["quotes"] if q.get("broker_symbol")), None)
        rates = mt5.copy_rates_from_pos(first, mt5.TIMEFRAME_H1, 1, 3) if first else None
    finally:
        mt5.shutdown()
    return {"cross": cross, "rates": rates}


def main():
    data_before = fingerprint(REPO / "data")
    report = {}

    # ------------------------------------------------------------ 1. live run
    proc, log = launch("run1")
    try:
        started = time.monotonic()
        quotes = get("/api/market/quotes")
        report["quotes_latency_s"] = round(time.monotonic() - started, 2)
        report["overall_status"] = quotes["status"]
        report["connection"] = {
            k: quotes["connection"].get(k)
            for k in (
                "state",
                "server",
                "server_clock",
                "time_check",
                "unavailable_reason",
                "message",
            )
        }
        report["quotes"] = [
            {
                k: q.get(k)
                for k in (
                    "symbol",
                    "broker_symbol",
                    "status",
                    "bid",
                    "ask",
                    "spread_pips",
                    "quote_time_utc",
                    "quote_age_seconds",
                    "reason",
                )
            }
            for q in quotes["quotes"]
        ]
        status = get("/status")
        report["collectors"] = {
            "collectors_config": None,
            "market_monitoring_state": status["market_monitoring_state"],
        }

        # Independent cross-check, only against a terminal confirmed running.
        direct = direct_read(quotes, TERMINAL)
        report["cross_check"] = direct.get("cross", direct.get("skipped"))
        rates = direct.get("rates")

        bars = get("/api/market/bars?symbol=EURUSD&timeframe=H1&count=300")
        report["bars"] = {
            k: bars.get(k)
            for k in ("status", "source", "broker_symbol", "time_basis", "reason", "message")
        }
        report["bars"]["count"] = len(bars.get("bars", []))
        if bars.get("bars"):
            # Completion comes from the data contract, never from array position.
            closed_bars = [bar for bar in bars["bars"] if bar["complete"]]
            report["bars"]["forming_bars"] = [b["t"] for b in bars["bars"] if not b["complete"]]
            report["bars"]["latest_tick_utc"] = bars.get("latest_tick_utc")
            report["bars"]["now_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
            if closed_bars:
                report["bars"]["last_closed_bar_utc"] = closed_bars[-1]["t"]
            if rates is not None and len(rates):
                raw = rates[-1]
                raw_t = datetime.fromtimestamp(int(raw["time"]), UTC)
                report["bars"]["raw_last_closed_epoch_as_utc"] = raw_t.isoformat()
                raw_iso = raw_t.isoformat().replace("+00:00", "Z")
                same = next((b for b in bars["bars"] if b["t"] == raw_iso), None)
                report["bars"]["raw_bar_marked_complete"] = bool(same and same["complete"])
                report["bars"]["ohlc_matches_direct"] = same is not None and all(
                    abs(same[k] - float(raw[n])) < 1e-9
                    for k, n in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"))
                )

        # Responsiveness: /health while several MT5 requests are in flight.
        tf_status = {}

        def hammer():
            for tf in ("M1", "M5", "M15", "M30", "H4", "D1"):
                result = get(
                    f"/api/market/bars?symbol=EURUSD&timeframe={tf}&count=1000", timeout=40
                )
                tf_status[tf] = (
                    result["status"],
                    result.get("reason"),
                    len(result.get("bars", [])),
                )

        worker = threading.Thread(target=hammer)
        worker.start()
        latencies = []
        while worker.is_alive():
            t0 = time.monotonic()
            get("/health", timeout=5)
            latencies.append(time.monotonic() - t0)
            time.sleep(0.05)
        report["bars_by_timeframe"] = tf_status
        report["health_latency_ms_during_mt5_requests"] = {
            "samples": len(latencies),
            "max": round(max(latencies) * 1000, 1) if latencies else None,
        }

        post(
            "/api/preferences",
            {
                "currency_pair_watchlist": ["EURUSD", "GBPUSD", "USDJPY", "EURGBP"],
                "selected_currency_pair": "USDJPY",
                "selected_chart_timeframe": "M15",
            },
        )
    finally:
        stop(proc, log)

    # ------------------------------------------------------------ 2. restart persistence
    proc, log = launch("run2")
    try:
        prefs = get("/api/dashboard")["preferences"]
        report["restart_restored"] = {
            "watchlist": prefs["currency_pair_watchlist"],
            "selected_currency_pair": prefs["selected_currency_pair"],
            "selected_chart_timeframe": prefs["selected_chart_timeframe"],
        }
    finally:
        stop(proc, log)

    # ------------------------------------------------------------ 3. closed terminal (simulated)
    proc, log = launch("run3", MT5_TERMINAL_PATH="C:/Johanna-Simulation/terminal_not_running.exe")
    try:
        closed = get("/api/market/quotes")
        bars = get("/api/market/bars?symbol=EURUSD&timeframe=H1&count=50")
        report["terminal_not_running"] = {
            "overall_status": closed["status"],
            "reasons": sorted({q.get("reason") for q in closed["quotes"]}),
            "any_price_shown": any(q.get("bid") is not None for q in closed["quotes"]),
            "bars_status": bars["status"],
            "bars_count": len(bars.get("bars", [])),
        }
    finally:
        stop(proc, log)

    logs = "".join((OUT / f"run{i}.log").read_text(errors="replace") for i in (1, 2, 3))
    report["log_errors"] = [
        line[:200] for line in logs.splitlines() if '"ERROR"' in line or "Traceback" in line
    ][:5]
    report["data_dir_unchanged"] = fingerprint(REPO / "data") == data_before
    report["data_dir_files"] = len(data_before)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
