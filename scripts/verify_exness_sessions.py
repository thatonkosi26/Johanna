r"""T0 (Phase 2.1): READ-ONLY verification of Exness trading sessions and history depth.

Usage (repository root, MT5 open and logged in):

    .venv311\Scripts\python.exe scripts\verify_exness_sessions.py [--out DIR]

Inspects the broker's own bar history for the default watchlist and reports, from evidence:
weekly opening/closing times and their daylight-saving behaviour, whether Sunday D1 candles
exist, recurring intraday (rollover) gaps, holiday gaps, H1 candles per D1 candle, and the
history depth available per timeframe. Writes ``report.json`` and ``report.md`` to a new
directory outside the repository's ``data/`` (default: %TEMP%\johanna-t0-verification\<UTC>).

Safety:
- Connects only to the MT5 terminal confirmed already running, by full executable path
  (``johanna.live_market.find_running_terminal``); never starts a terminal.
- Every MetaTrader5 call goes through an allow-list of read-only functions; anything else
  raises before reaching MT5. No symbol is selected (Market Watch is not changed), no order,
  position, account or terminal function exists on the list.
- Never reads or writes ``data/`` and never contacts the Johanna backend.
- Requests are bounded and paced so the terminal keeps serving other clients.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from johanna.live_market import DEFAULT_PAIRS, MarketUnavailable, find_running_terminal

READ_ONLY_CALLS = frozenset(
    {
        "initialize",
        "shutdown",
        "last_error",
        "version",
        "terminal_info",
        "account_info",
        "symbol_info",
        "symbols_get",
        "symbol_info_tick",
        "copy_rates_from_pos",
        "copy_rates_range",
    }
)
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
PACE_SECONDS = 1.0  # pause between history requests
# Request sizes: bounded so a single request stays small for the terminal.
DEPTH_REQUESTS = {"D1": 5000, "H4": 10000, "H1": 30000}
M5_COUNT, M5_SYMBOLS = 80000, ("EURUSD", "USDJPY", "AUDUSD")  # ~13 months: 2025-26 DST changes
M1_DAYS = 28  # spans the 25 Oct (EU) and 1 Nov (US) 2026 DST changes
STEP = {"M1": 60, "M5": 300, "H1": 3600, "H4": 14400, "D1": 86400}
DST_CHANGES = {
    "2025-03-09": "US spring forward",
    "2025-03-30": "EU spring forward",
    "2025-10-26": "EU fall back",
    "2025-11-02": "US fall back",
    "2026-03-08": "US spring forward",
    "2026-03-29": "EU spring forward",
    "2026-10-25": "EU fall back",
    "2026-11-01": "US fall back",
}


class ReadOnlyMT5:
    """Allow-list proxy around the MetaTrader5 module; records every call and its duration."""

    def __init__(self, module) -> None:
        self._module = module
        self.calls: Counter[str] = Counter()
        self.durations: list[tuple[str, str, float]] = []

    def __getattr__(self, name: str):
        if name.startswith("TIMEFRAME_"):
            return getattr(self._module, name)
        if name not in READ_ONLY_CALLS:
            raise PermissionError(f"MetaTrader5.{name} is not on the read-only allow-list")
        function = getattr(self._module, name)

        def call(*args, **kwargs):
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.calls[name] += 1
                label = str(args[:2]) if name.startswith("copy_rates") else ""
                self.durations.append((name, label, round(time.perf_counter() - started, 3)))

        return call


def utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def iso(epoch: int) -> str:
    return utc(epoch).isoformat().replace("+00:00", "Z")


def hm(moment: datetime) -> str:
    return moment.strftime("%a %H:%M")


def rows_of(rates) -> list[tuple[int, float, float, float, float, int, int]]:
    if rates is None:
        return []
    return [
        (
            int(r["time"]),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            int(r["tick_volume"]),
            int(r["spread"]),
        )
        for r in rates
    ]


def resolve(mt5, base: str) -> tuple[str | None, str]:
    """Broker symbol for a pair, as Johanna's adapter resolves it; never selects symbols."""
    info = mt5.symbol_info(base)
    if info is not None:
        name = base
    else:
        candidates = sorted(
            (s.name for s in (mt5.symbols_get(group=f"{base}*") or ()) if s.name.startswith(base)),
            key=lambda n: (len(n), n),
        )
        candidates = [n for n in candidates if len(n) <= len(base) + 4]
        if not candidates:
            return None, "not offered"
        name = candidates[0]
        info = mt5.symbol_info(name)
    if info is None or not info.visible:
        return None, f"{name} not visible in Market Watch (not selected by this script)"
    return name, "ok"


# ------------------------------------------------------------------ analyses
def weekend_gaps(rows, step: int) -> list[dict]:
    """Gaps of at least 24 h between consecutive bars: weekly (or holiday) closures."""
    gaps = []
    for (t0, *_), (t1, *_) in zip(rows, rows[1:], strict=False):
        close, reopen = t0 + step, t1
        if reopen - close >= 24 * 3600:
            c, o = utc(close), utc(reopen)
            gaps.append(
                {
                    "last_bar_close_utc": iso(close),
                    "reopen_utc": iso(reopen),
                    "close": hm(c),
                    "reopen": hm(o),
                    "close_new_york": hm(c.astimezone(NEW_YORK)),
                    "reopen_new_york": hm(o.astimezone(NEW_YORK)),
                    "us_dst_at_close": bool(c.astimezone(NEW_YORK).dst()),
                    "gap_hours": round((reopen - close) / 3600, 2),
                }
            )
    return gaps


def summarise_weekends(gaps: list[dict]) -> dict:
    by_regime: dict[str, Counter] = defaultdict(Counter)
    unusual = []
    for gap in gaps:
        regime = "US summer time (EDT)" if gap["us_dst_at_close"] else "US winter time (EST)"
        by_regime[regime][(gap["close"], gap["reopen"])] += 1
        if not (45 <= gap["gap_hours"] <= 51):  # an ordinary FX weekend is ~48 h
            unusual.append(gap)
    return {
        "weekends_observed": len(gaps),
        "first": gaps[0]["last_bar_close_utc"] if gaps else None,
        "last": gaps[-1]["last_bar_close_utc"] if gaps else None,
        "close_reopen_utc_by_regime": {
            regime: [
                {"close_utc": c, "reopen_utc": o, "weeks": n} for (c, o), n in counts.most_common()
            ]
            for regime, counts in by_regime.items()
        },
        "unusual_weekends": unusual,
    }


def intraweek_gaps(rows, step: int, min_gap: int) -> list[tuple[int, int]]:
    """(gap start epoch, gap seconds) for missing bars inside a trading week (< 24 h)."""
    out = []
    for (t0, *_), (t1, *_) in zip(rows, rows[1:], strict=False):
        gap = t1 - (t0 + step)
        if min_gap <= gap < 24 * 3600:
            out.append((t0 + step, gap))
    return out


def recurring_gaps(rows, step: int, trading_days: int) -> dict:
    """Intraday gaps grouped by UTC start time and duration, split by US DST regime."""
    groups: dict[tuple[bool, str, int], list[str]] = defaultdict(list)
    for start, gap in intraweek_gaps(rows, step, step):
        moment = utc(start)
        key = (bool(moment.astimezone(NEW_YORK).dst()), moment.strftime("%H:%M"), gap // 60)
        groups[key].append(moment.date().isoformat())
    recurring, occasional = [], []
    for (dst, start, minutes), dates in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        entry = {
            "us_regime": "EDT" if dst else "EST",
            "start_utc": start,
            "minutes": minutes,
            "occurrences": len(dates),
            "first": dates[0],
            "last": dates[-1],
        }
        (recurring if len(dates) >= max(3, trading_days * 0.3) else occasional).append(entry)
    return {"recurring": recurring, "occasional_top": occasional[:15], "groups": len(groups)}


def rollover_window(rows, step: int) -> dict:
    """Intraweek gaps relative to the New York 17:00 rollover.

    Counts trading days having at least one gap that starts within 17:00-17:30 New York
    time, the gap durations there, and every gap starting outside that window.
    """
    inside_days: dict[str, list[int]] = defaultdict(list)
    outside = []
    for start, gap in intraweek_gaps(rows, step, step):
        moment = utc(start)
        local = moment.astimezone(NEW_YORK)
        minutes_after_17 = (local.hour - 17) * 60 + local.minute
        if 0 <= minutes_after_17 < 30:
            inside_days[local.date().isoformat()].append(gap // 60)
        else:
            outside.append(
                {"start_utc": iso(start), "start_new_york": hm(local), "minutes": gap // 60}
            )
    days = trading_days(rows)
    durations = [m for v in inside_days.values() for m in v]
    starts = Counter(
        utc(s).astimezone(NEW_YORK).strftime("%H:%M")
        for s, g in intraweek_gaps(rows, step, step)
        if 0
        <= (utc(s).astimezone(NEW_YORK).hour - 17) * 60 + utc(s).astimezone(NEW_YORK).minute
        < 30
    )
    return {
        "trading_days": days,
        "days_with_gap_in_17_00_17_30_new_york": len(inside_days),
        "gap_minutes_in_window": {
            "count": len(durations),
            "median": statistics.median(durations) if durations else None,
            "max": max(durations) if durations else None,
        },
        "most_common_window_starts_new_york": starts.most_common(5),
        "gaps_outside_window": len(outside),
        "gaps_outside_window_samples": outside[:20],
    }


def trading_days(rows) -> int:
    return len({utc(t).date() for t, *_ in rows if utc(t).weekday() < 5})


def sunday_d1(d1_rows, h1_rows) -> dict:
    weekday_counts = Counter(utc(t).strftime("%a") for t, *_ in d1_rows)
    h1_per_day: dict[str, int] = Counter(utc(t).date().isoformat() for t, *_ in h1_rows)
    h1_days = set(h1_per_day)
    per_weekday: dict[str, list[int]] = defaultdict(list)
    for t, *_ in d1_rows:
        day = utc(t).date().isoformat()
        if day in h1_days:
            per_weekday[utc(t).strftime("%a")].append(h1_per_day[day])
    sundays = [r for r in d1_rows if utc(r[0]).weekday() == 6]
    by_date = {utc(r[0]).date(): r for r in d1_rows}
    ratios = []
    for t, o, h, low, c, tv, sp in sundays:
        monday = by_date.get(utc(t).date() + timedelta(days=1))
        if monday and monday[2] - monday[3] > 0:
            ratios.append((h - low) / (monday[2] - monday[3]))
    return {
        "d1_bars": len(d1_rows),
        "d1_range": [iso(d1_rows[0][0]), iso(d1_rows[-1][0])] if d1_rows else None,
        "d1_bars_by_weekday": dict(weekday_counts),
        "sunday_d1_bars": len(sundays),
        "sunday_first_last": [iso(sundays[0][0]), iso(sundays[-1][0])] if sundays else None,
        "sunday_median_tick_volume": statistics.median([r[5] for r in sundays])
        if sundays
        else None,
        "monday_median_tick_volume": statistics.median(
            [r[5] for r in d1_rows if utc(r[0]).weekday() == 0]
        )
        if d1_rows
        else None,
        "sunday_range_vs_monday_median": round(statistics.median(ratios), 3) if ratios else None,
        "h1_bars_per_d1_by_weekday": {
            day: {
                "days": len(v),
                "median": statistics.median(v),
                "min": min(v),
                "max": max(v),
            }
            for day, v in per_weekday.items()
        },
    }


def holiday_candidates(h1_rows, daily_break_starts: set[str]) -> list[dict]:
    """Intraweek H1 gaps of 2 h or more, other than the recurring daily break."""
    out = []
    for start, gap in intraweek_gaps(h1_rows, 3600, 2 * 3600):
        moment = utc(start)
        if moment.strftime("%H:%M") in daily_break_starts and gap <= 2 * 3600:
            continue
        out.append(
            {
                "start_utc": iso(start),
                "hours": round(gap / 3600, 2),
                "weekday": moment.strftime("%a"),
            }
        )
    return out


def dst_transition_weeks(gaps: list[dict]) -> list[dict]:
    """Weekly close/reopen times in the weeks around each known DST change."""
    out = []
    for date_text, label in DST_CHANGES.items():
        change = datetime.fromisoformat(date_text).replace(tzinfo=UTC)
        near = [
            g
            for g in gaps
            if abs(datetime.fromisoformat(g["reopen_utc"].replace("Z", "+00:00")) - change)
            <= timedelta(days=15)
        ]
        if near:
            out.append(
                {
                    "change": f"{date_text} {label}",
                    "weeks": [
                        {
                            "close_utc": g["last_bar_close_utc"],
                            "reopen_utc": g["reopen_utc"],
                            "close_new_york": g["close_new_york"],
                            "reopen_new_york": g["reopen_new_york"],
                        }
                        for g in near
                    ],
                }
            )
    return out


# ------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    default_out = Path(tempfile.gettempdir()) / "johanna-t0-verification" / stamp
    parser.add_argument("--out", type=Path, default=default_out)
    args = parser.parse_args()
    out_dir: Path = args.out
    repo_data = (Path(__file__).resolve().parents[1] / "data").resolve()
    if repo_data in out_dir.resolve().parents or out_dir.resolve() == repo_data:
        raise SystemExit("Refusing to write inside the repository's data/ directory.")

    try:
        terminal = find_running_terminal(os.environ.get("MT5_TERMINAL_PATH"))
    except MarketUnavailable as exc:
        raise SystemExit(f"Not verified, nothing done: {exc.code}: {exc.message}") from None

    import MetaTrader5

    mt5 = ReadOnlyMT5(MetaTrader5)
    started = datetime.now(UTC)
    if not mt5.initialize(terminal):
        raise SystemExit(f"initialize failed: {mt5.last_error()}")
    report: dict = {"generated_at_utc": started.isoformat(), "terminal_path": terminal}
    try:
        account = mt5.account_info()
        info = mt5.terminal_info()
        report["environment"] = {
            "company": account.company if account else None,
            "server": account.server if account else None,
            "account_type": {0: "demo", 1: "contest", 2: "real"}.get(account.trade_mode)
            if account
            else None,
            "terminal_connected": bool(info.connected) if info else None,
            "terminal_build": info.build if info else None,
            "terminal_maxbars": info.maxbars if info else None,
            "package_version": MetaTrader5.__version__,
        }
        if not (info and info.connected and account):
            raise SystemExit("Terminal not connected to its trade server; nothing verified.")

        symbols, skipped = {}, {}
        for pair in DEFAULT_PAIRS:
            name, why = resolve(mt5, pair)
            if name:
                symbols[pair] = name
            else:
                skipped[pair] = why
        report["symbols"] = symbols
        report["symbols_skipped"] = skipped

        # Time base: raw tick epoch vs this computer's UTC clock.
        report["time_base"] = {}
        for pair, name in symbols.items():
            tick = mt5.symbol_info_tick(name)
            if tick and tick.time:
                report["time_base"][pair] = round(tick.time_msc / 1000 - time.time(), 1)

        now = int(time.time())
        data: dict[str, dict[str, list]] = defaultdict(dict)
        depth = defaultdict(dict)
        empty_results: list[dict] = []

        def fetched(rates, what: str) -> list:
            rows = rows_of(rates)
            if not rows:
                empty_results.append({"request": what, "last_error": list(mt5.last_error())})
            return rows

        for pair, name in symbols.items():
            for tf, count in DEPTH_REQUESTS.items():
                rows = fetched(
                    mt5.copy_rates_from_pos(name, getattr(mt5, f"TIMEFRAME_{tf}"), 0, count),
                    f"{name} {tf} from_pos {count}",
                )
                data[pair][tf] = rows
                depth[pair][tf] = {
                    "requested": count,
                    "returned": len(rows),
                    "oldest_utc": iso(rows[0][0]) if rows else None,
                    "newest_utc": iso(rows[-1][0]) if rows else None,
                    "limited_by_request": len(rows) == count,
                }
                time.sleep(PACE_SECONDS)
            rows = fetched(
                mt5.copy_rates_range(name, mt5.TIMEFRAME_M1, now - M1_DAYS * 86400, now),
                f"{name} M1 range {M1_DAYS} days",
            )
            data[pair]["M1"] = rows
            depth[pair]["M1 (last 28 days)"] = {
                "returned": len(rows),
                "oldest_utc": iso(rows[0][0]) if rows else None,
            }
            time.sleep(PACE_SECONDS)
            if pair in M5_SYMBOLS:
                # A first run's 400-day copy_rates_range returned nothing; request by bounded
                # count instead (about 400 trading days of M5).
                rows = fetched(
                    mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_M5, 0, M5_COUNT),
                    f"{name} M5 from_pos {M5_COUNT}",
                )
                data[pair]["M5"] = rows
                depth[pair][f"M5 (last {M5_COUNT} bars)"] = {
                    "returned": len(rows),
                    "oldest_utc": iso(rows[0][0]) if rows else None,
                }
                time.sleep(PACE_SECONDS)
        report["history_depth"] = depth
        report["empty_history_results"] = empty_results
    finally:
        mt5.shutdown()
    finished = datetime.now(UTC)

    # --------------------------------------------------------- analysis (offline)
    weekends, recurring, sundays, holidays, transitions, rollover = {}, {}, {}, {}, {}, {}
    for pair in symbols:
        tfs = data[pair]
        weekends[pair] = {
            tf: summarise_weekends(weekend_gaps(tfs[tf], STEP[tf]))
            for tf in ("M1", "M5", "H1")
            if tfs.get(tf)
        }
        finest = "M5" if tfs.get("M5") else "H1"
        transitions[pair] = dst_transition_weeks(weekend_gaps(tfs[finest], STEP[finest]))
        recurring[pair] = {
            "M1 (last 28 days)": recurring_gaps(tfs["M1"], 60, trading_days(tfs["M1"])),
        }
        rollover[pair] = {"M1 (last 28 days)": rollover_window(tfs["M1"], 60)}
        if tfs.get("M5"):
            recurring[pair]["M5"] = recurring_gaps(tfs["M5"], 300, trading_days(tfs["M5"]))
            rollover[pair]["M5"] = rollover_window(tfs["M5"], 300)
        sundays[pair] = sunday_d1(tfs["D1"], tfs["H1"])
        breaks = {g["start_utc"] for g in recurring[pair]["M1 (last 28 days)"]["recurring"]}
        breaks |= {f"{h:02d}:00" for h in range(19, 24)}  # H1 resolution of a short daily break
        holidays[pair] = holiday_candidates(tfs["H1"], breaks)

    # Cross-symbol holiday evidence: the same intraweek H1 gap on every symbol.
    gap_sets = {pair: {(h["start_utc"], h["hours"]) for h in v} for pair, v in holidays.items()}
    market_wide = sorted(set.intersection(*gap_sets.values())) if gap_sets else []
    report.update(
        {
            "collection_window_utc": [started.isoformat(), finished.isoformat()],
            "mt5_calls": dict(mt5.calls),
            "slowest_calls_s": sorted(mt5.durations, key=lambda d: -d[2])[:10],
            "weekly_sessions": weekends,
            "dst_transition_weeks": transitions,
            "recurring_intraday_gaps": recurring,
            "rollover_window": rollover,
            "sunday_d1": sundays,
            "intraweek_h1_gaps_2h_plus": holidays,
            "market_wide_h1_gaps": [{"start_utc": s, "hours": h} for s, h in market_wide],
        }
    )
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render(report), encoding="utf-8")
    print(f"T0 report written to {out_dir}")
    return 0


def render(report: dict) -> str:
    env = report["environment"]
    lines = [
        "# T0 Exness session verification (read-only)",
        "",
        f"Generated {report['generated_at_utc']} · {env['company']} · {env['server']} · "
        f"{env['account_type']} · terminal build {env['terminal_build']} · maxbars "
        f"{env['terminal_maxbars']}",
        "",
        f"Symbols: {report['symbols']}  Skipped: {report['symbols_skipped'] or 'none'}",
        f"Raw tick epoch minus system UTC (s): {report['time_base']}",
        f"MT5 calls: {report['mt5_calls']}",
        "",
        "## History depth",
    ]
    for pair, tfs in report["history_depth"].items():
        lines.append(f"- {pair}: " + "; ".join(f"{tf} {v}" for tf, v in tfs.items()))
    lines += ["", "## Weekly sessions (close / reopen, UTC, by US DST regime)"]
    for pair, tfs in report["weekly_sessions"].items():
        for tf, summary in tfs.items():
            lines.append(
                f"- {pair} {tf}: {summary['weekends_observed']} weekends "
                f"({summary['first']} .. {summary['last']}): "
                f"{json.dumps(summary['close_reopen_utc_by_regime'])}; "
                f"unusual: {len(summary['unusual_weekends'])}"
            )
    lines += ["", "## Sunday D1"]
    for pair, s in report["sunday_d1"].items():
        lines.append(f"- {pair}: {json.dumps(s)}")
    lines += ["", "## Recurring intraday gaps"]
    for pair, g in report["recurring_intraday_gaps"].items():
        for tf, r in g.items():
            lines.append(f"- {pair} {tf}: {json.dumps(r['recurring'])}")
    lines += ["", "## Rollover window (17:00-17:30 New York)"]
    for pair, g in report["rollover_window"].items():
        for tf, r in g.items():
            summary = {k: v for k, v in r.items() if k != "gaps_outside_window_samples"}
            lines.append(f"- {pair} {tf}: {json.dumps(summary)}")
    lines += ["", f"Empty history results: {report['empty_history_results'] or 'none'}"]
    lines += ["", "## Market-wide intraweek H1 gaps (all symbols)"]
    lines += [f"- {g}" for g in report["market_wide_h1_gaps"]] or ["- none"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
