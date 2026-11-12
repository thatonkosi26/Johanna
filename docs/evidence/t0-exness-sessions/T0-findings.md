# T0: Exness session and history verification, findings

Date: 2026-11-12 · Read-only · Script: `scripts/verify_exness_sessions.py` (repository)

**Evidence files (this directory):**
- `report.json`: full machine-readable results.
- `report.md`: summary.
- **First run:** `..\20261112T092619Z\` (same script before two analysis fixes; its M5 result was
  empty, see §8).

**Source:** Exness Technologies Ltd, `Exness-MT5Real21`, real (Standard Cent) account, terminal
build 6316, "max bars in chart" 100,000.

**Symbols (default watchlist; your saved watchlist wasn't read, to avoid touching `data/`):**

| Pair | EURUSD | GBPUSD | USDJPY | USDCHF | AUDUSD | USDCAD | NZDUSD |
|---|---|---|---|---|---|---|---|
| Broker symbol | EURUSDc | GBPUSDc | USDJPYc | USDCHFc | AUDUSDc | USDCADc | NZDUSDc |

**Time basis re-checked:** raw tick time minus system UTC was −0.2 s to −9.8 s across both runs,
consistent with the server clock being UTC. Raw MT5 times are therefore read as UTC below.

## 1. Samples used

| Data | Symbols | Span | Size |
|---|---|---|---|
| D1 | 7 | EURUSD 2014-01-14 → 2026-11-12; others 2024-03-26 → 2026-11-12 | 3,963 / 824 bars |
| H4 | 7 | EURUSD 2020-09-08 →; others 2024-03-26 → | 10,000 (request limit) / 4,238 |
| H1 | 7 | EURUSD 2022-01-19 →; others 2024-03-26 → | 30,000 (request limit) / 16,382 |
| M5 | EURUSD, USDJPY, AUDUSD | 2025-10-16 → 2026-11-12 (about 281 trading days) | 80,000 bars each |
| M1 | 7 | 2026-10-15 → 2026-11-12 (21 trading days) | about 28,650 bars each |

**Weekends observed:**
- **EURUSD H1:** 255 (since January 2022).
- **Other pairs H1:** 141 each (since March 2024).
- **M5:** 58.
- **M1:** 4.

**Daylight-saving changes covered:**
- **Minute level (M5):** US autumn 2025, US spring 2026, EU spring and autumn 2026, US autumn 2026.
- **Hour level (H1):** every change since 2022.

## 2. Weekly session boundaries and daylight saving

| | US summer time (EDT) | US winter time (EST) | New York local |
|---|---|---|---|
| Last bar closes (Friday) | 21:00 UTC (last M1 bar 20:59) | 22:00 UTC (last M1 bar 21:59) | **17:00** |
| Market reopens (Sunday) | 21:05 UTC (first M5/M1 bar) | 22:05 UTC | **17:05** |

**Weeks matching that pattern:**
- **M5:** EURUSD 35 + 17 of 58 weeks. The rest are the two holiday weeks, two weeks spanning a US
  clock change, and two late first bars (22:10, 21:25).
- **USDJPY:** the first M5 bar sometimes comes later (21:10 in 6 weeks, 21:15 once). That reads
  as a quiet reopening, not a session change.

**The session follows New York time, not UTC.** Close and reopen stay at 17:00 / 17:05 New York
across every observed US change, and move by one hour in UTC:
- **US clocks change** (2025-11-02, 2026-03-08, 2026-11-01): the session moves by one hour in
  UTC.
- **EU clocks change** (2025-03-30, 2025-10-26, 2026-03-29, 2026-10-25): the session doesn't
  move. The EU fall-back weeks of 2025-10-26 and 2026-10-25 still close at 21:00 UTC.
- **Change weekends:** the US change falls on the closed Sunday, so close and reopen sit in
  different UTC offsets that weekend (for example, close Friday 21:00 UTC, reopen Sunday 22:05
  UTC). That's expected.

**Friday D1 candles:** 21 H1 candles in US summer time and 22 in winter time, because the session
ends at 17:00 New York.

## 3. Sunday D1 candles

**A Sunday D1 candle exists every week** on Exness, opening at 00:00 UTC Sunday and holding only
the hours from the reopen (21:05 or 22:05 UTC) to midnight UTC.

| | EURUSD (since 2014) | Other 6 pairs (since March 2024) |
|---|---|---|
| Sunday D1 candles | 628 (D1 counts by weekday Mon–Fri: 665–669) | 136 of 136 weeks |
| H1 candles per Sunday D1 | median 3 (min 2, max 3) | median 3 (min 2, max 3) |
| Sunday vs Monday median tick volume | 180 vs 1,437 (about 13 %) | about 5–8 % (e.g. GBPUSD 2,821 vs 55,160) |
| Sunday range vs following Monday (median) | 0.24 | 0.31–0.50 |

There are no Saturday candles.

**Consequence for analysis:** about one D1 candle in six is a 2–3 hour stub with roughly a third
of a normal day's range. Using it unchanged would weigh D1 ATR, ADX, swing detection and EMA
counts with sub-day candles. How to treat it is a decision for you (§9).

## 4. Daily rollover gaps

**Every** intraday gap in both samples starts within **17:00–17:30 New York**, and **zero** gaps
start outside that window (M1: 21 days × 7 pairs; M5: about 281 days × 3 pairs).

| Pair | Days with a rollover gap (M1, 21 days) | Gap length, median / max (M1) | M5 days with gap (of ~281) |
|---|---|---|---|
| EURUSD | 11 | 2 / 15 min | 36 |
| GBPUSD | 14 | 2 / 8 | — |
| USDJPY | 16 | 5 / 7 | 89 |
| USDCHF | 17 | 2 / 7 | — |
| AUDUSD | 13 | 1 / 5 | 35 |
| USDCAD | 11 | 1 / 5 | — |
| NZDUSD | 15 | 2 / 5 | — |

- **Where gaps start:** most begin at 17:00–17:04 New York; a few begin as late as 17:28.
- **Not every day:** the gap appears on some days only, so a missing rollover gap isn't an
  anomaly.
- **Why most M5 days show no gap:** a 1–4 minute break leaves the 17:00 M5 candle partly filled,
  so it isn't missing.

**H1 and longer timeframes show no intraday gaps** (zero gaps of 2 h or more inside a trading week
on any symbol), because an hourly candle still forms around a few-minute break.

## 5. Holidays

**Closures observed** (H1 since 2022 for EURUSD, since 2024 for the rest; M5 for 2025–26), on all
7 pairs:

| Date | Closure |
|---|---|
| 2023-12-25 (Mon) | Fri 22 Dec 22:00 UTC → Mon 25 Dec 22:00 UTC (72 h; EURUSD only, others have no history then) |
| 2024-01-01 (Mon) | Fri 29 Dec 22:00 → Mon 1 Jan 22:00 (72 h; EURUSD only) |
| 2024-12-25 (Wed) | Tue 24 Dec 22:00 → Wed 25 Dec 22:00 (24 h) |
| 2025-01-01 (Wed) | Tue 31 Dec 22:00 → Wed 1 Jan 22:00 (24 h) |
| 2025-12-25 (Thu) | Wed 24 Dec 22:00 → Thu 25 Dec 22:05 (24 h 5 min, M5) |
| 2026-01-01 (Thu) | Wed 31 Dec 22:00 → Thu 1 Jan 22:05–22:10 (M5) |

That's a full-day closure on **25 December and 1 January**, from 17:00 New York the evening before
to about 17:05 New York on the holiday.

**Not observed** in these samples:
- early closes on 24 or 31 December (each closed at the normal 17:00 New York);
- closures on other holidays (Good Friday, Easter Monday, US or UK bank holidays);
- any other intraweek gap of 24 h or more, or of 2 h or more at H1.

Shorter holiday sessions below H1 resolution before October 2025 can't be ruled out (no M1/M5
history that far back).

## 6. H1 candles per D1 candle

| Weekday | H1 per D1 (median, range) | Explanation |
|---|---|---|
| Monday–Thursday | 24 (min 2) | The minimum of 2 is the Dec 25 / Jan 1 holiday days, holding only 22:00–24:00 UTC |
| Tuesday | min 10 | The first day of the six pairs' history (2024-03-26 from 14:00): a download boundary, not a market effect |
| Friday | 21 (EDT) / 22 (EST) | Close at 17:00 New York |
| Sunday | 3 (2–3) | Reopen 17:05 New York → midnight UTC |

## 7. History depth (broker history available to this terminal)

| Timeframe | EURUSDc | The other 6 pairs |
|---|---|---|
| D1 | 3,963 bars (2014-01-14) | **824 bars** (2024-03-26) |
| H4 | ≥ 10,000 (request limit; from 2020-09) | 4,238 (2024-03-26) |
| H1 | ≥ 30,000 (request limit; from 2022-01) | 16,382 (2024-03-26) |
| M5 | ≥ 80,000 (request limit) | (only 3 pairs requested; USDJPY and AUDUSD also ≥ 80,000) |
| M1 | 28 days requested: about 28,650 | about 28,650 |

**Against Johanna's 1,000-candle request limit:**
- **H1 and H4:** 1,000 candles are available for every pair.
- **D1:** six pairs return only **824** candles, and about 136 of those are Sunday stubs.
  - **EMA200 at the approved 1 % tolerance** needs about 200 + 461 = 661 candles: just met with
    Sunday stubs (824), barely met without them (about 688).
- **Unknown:** whether the six pairs' history begins in March 2024 because of the symbol's
  listing or because of server-side history availability.

## 8. Limitations and inconclusive observations

- **Load during the first run.** The first D1 request (EURUSDc) took **31.4 s**, most likely
  because the terminal downloaded history; the other D1 requests took about 4 s. The terminal
  serves clients one at a time, so your backend's requests may have waited during those seconds.
  - **Evidence of no harm:** your backend's two MT5 worker processes kept their original process
    IDs (11:08:27), and a timed-out worker is always replaced by a new process. So no backend
    worker timed out.
  - **Not checked:** I didn't contact your backend to confirm whether the dashboard briefly showed
    CACHED.
  - **Second run:** its slowest call was 0.36 s.
- **M5 in the first run.** The 400-day M5 request returned no bars. The cause wasn't captured; most
  likely history was still downloading. The second run requested M5 by count and got 80,000 bars,
  and now records the MT5 error code for any empty result (none occurred).
- **Weekend counts.** M1 covers only 4 weekends; the session boundaries rest mainly on M5 (58
  weekends) and H1 (141–255 weekends).
- **Holiday coverage.** It spans 2022–2026 at H1 for EURUSD but only 2024–2026 for the other pairs,
  and minute-level holiday detail exists only from October 2025.
- **Late first bars.** "First bar after reopen" can be a few minutes late in quiet markets (USDJPY
  21:10–21:15 UTC). A gap model needs a tolerance there.
- **Continuity unchecked.** Whether a candle missing inside the session is a feed loss or a
  no-tick interval wasn't assessable from history alone. No unexplained gap was found to examine.

## 9. Proposed session configuration (from this evidence; not implemented)

```yaml
exness_sessions:            # evidence: T0 2026-11-12, report.json in this directory
  reference_zone: America/New_York
  weekly:
    close:  { weekday: Friday, time: "17:00" }        # last bar closes; M5 58/58 weeks
    reopen: { weekday: Sunday, time: "17:05", late_first_bar_tolerance_minutes: 20 }
  daily_rollover:
    window_start: "17:00"
    window_end: "17:30"     # all observed intraday gaps started here (0 outside)
    max_gap_minutes: 15     # largest observed: 15 (EURUSD M1); gap may be absent
  holidays_full_day_closure:   # 17:00 NY on the eve -> ~17:05 NY on the day
    - { month: 12, day: 25 }
    - { month: 1, day: 1 }
  sunday_d1_candle:
    present: true           # every week; 2-3 H1 bars from the reopen to 00:00 UTC
    treatment: undecided    # decision required (see below)
  unexplained_gap: any intraday gap outside the rollover window, any missing candle
                   inside the session at H1 or longer, or any intraweek gap not matching
                   the weekly or holiday rules -> continuity "suspect"; otherwise "unknown"
                   where evidence is insufficient (never assumed continuous)
  revalidate: on broker/server change, and at least yearly (holiday list)
```

**Decisions needed from you:**
1. **Sunday D1 candles:** (a) exclude them from D1 analysis and label that; (b) keep them as they
   are; or (c) present D1 analysis both ways. Merging Sunday into Monday would create candles
   that aren't Exness's own, so I wouldn't recommend it without approval.
2. **Whether these values become the configuration:** the weekly, rollover and holiday values
   above, or whether you want more evidence first (for example M1 over a longer period, or the
   2026 Christmas week once it happens).

## 10. Safety confirmation

- **Read-only:**
  - every MT5 call went through an allow-list (`initialize`, `terminal_info`, `account_info`,
    `symbol_info`, `symbols_get`, `symbol_info_tick`, `copy_rates_from_pos`, `copy_rates_range`,
    `last_error`, `shutdown`);
  - an offline test confirmed `order_send`, `symbol_select`, `positions_get` and `login` are
    blocked;
  - no symbol was selected, and no order, position, account or terminal setting was touched;
  - the terminal was attached only after its full path was confirmed running.
- **`data/` unchanged:** SHA-256, size and modification time of all 11 readable files were
  identical before and after, the file count is still 12, and `johanna.lock` was untouched.
- **Backend undisturbed:** the running backend (PID 38172 and its two workers) wasn't stopped,
  restarted or contacted, and its processes are unchanged.
- **Nothing else enabled or changed:** no collector, no other provider, no change to existing
  services or the dashboard. The only repository change is the new script
  `scripts/verify_exness_sessions.py`.
