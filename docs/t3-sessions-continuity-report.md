# T3: session model and continuity classification

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed** · Stopped
after T3 (T4–T10 not started)

## 1. Files changed

| File | Change |
|---|---|
| `src/johanna/analysis/sessions.py` | **New:** `SessionConfig` (validated), `EXNESS_T0_PROVISIONAL` (with per-rule evidence), `Closure`, `closures_overlapping()`, `is_scheduled_closed()`, `in_rollover_window()` |
| `src/johanna/analysis/continuity.py` | **New:** `analyse_continuity()`, classification constants, `TIMEFRAME_SECONDS` |
| `src/johanna/analysis/__init__.py` | Unchanged (from T2) |
| `docs/evidence/t0-exness-sessions/` | **New:** byte-identical copies (`report.json` SHA-256 `9ecff8aa…533e9b`) of the T0 `report.json`, `report.md` and `T0-findings.md`, preserving the evidence behind each rule; the temporary directory could be deleted |
| `tests/test_sessions_continuity.py` | **New:** 46 tests (§5) |

**Not changed:** existing source files (`live_market.py`, `api.py`, adapters, dashboard, chart),
T1 checks, T2 snapshots, collectors and `data/`. T3 is analysis-package code only and isn't
wired into any endpoint or the dashboard.

## 2. Session configuration and evidence

`EXNESS_T0_PROVISIONAL` (`name = "exness-t0-provisional-2026-11-12"`, `provisional = True`):

| Rule | Value (America/New_York) | Evidence (stored in the config and in `docs/evidence/t0-exness-sessions/`) |
|---|---|---|
| Weekly close | Friday 17:00 | Last M1 bar 16:59; M5 58 weekends, H1 141–255 weekends, 7 symbols |
| Weekly reopen | Sunday 17:05 | First M5/M1 bar 17:05 in most weeks |
| Reopen tolerance | 20 min | Later first bars seen up to 17:15, once 17:25 |
| Rollover window | gaps starting 17:00 ≤ t < 17:30 | Every intraday gap observed (M1 21 days × 7 pairs; M5 about 281 days × 3 pairs); 0 outside |
| Rollover maximum | 15 min | Longest observed: 15 min (EURUSD M1) |
| Holiday closures | 25 Dec, 1 Jan: eve 17:00 → day 17:05 | 2023-12-25 and 2024-01-01 (EURUSD), 2024-12-25, 2025-01-01, 2025-12-25, 2026-01-01 (7 pairs) |

**Not configured** (not observed): other holidays, early closes and special sessions. The
configuration is a frozen, validated object (zone, weekday ranges, non-negative tolerances,
rollover window order, valid holiday dates). Alternatives can be passed to every function, so
broker assumptions aren't hard-coded elsewhere.

## 3. Continuity classification rules

**Input:** candles of one symbol and one timeframe (`t` open times from a bars response),
optional `peers` (other symbols' open times on the same timeframe), and optional
`excluded_opens`.

**Required slots:** for each pair of consecutive candles, the slots between them are *missing*.
A slot is **required** when the schedule says the market was open during it, outside scheduled
closures and the 20-minute reopen tolerance after each closure.

**Each gap gets exactly one classification:**

| Classification | Rule |
|---|---|
| `configured_closure` | No required slot missing. `closure_kinds` ⊆ {`weekly`, `holiday`, `reopen_delay`}; `reopen_delay` means the first candle came after the last scheduled reopen the gap reaches, within tolerance. |
| `expected_no_tick` | **M1/M5 only:** the gap starts inside the rollover window and lasts ≤ 15 min (`possible_causes: ["daily_rollover_break"]`). |
| `analytical_exclusion` | The only missing required slots are open times the caller deliberately excluded (`excluded_opens`). A mechanism only: T3 doesn't decide what to exclude. |
| `market_wide_unverified` | Required slots missing here **and** in every informative peer. Not treated as a legitimate closure. |
| `symbol_missing` | Required slots missing here while at least one peer has candles in them |
| `unknown` | Required slots missing and no peer covers the gap period |

**Causes and certainty for unexplained gaps:**
- **M1/M5:** `possible_causes = ["no_tick_interval", "feed_interruption"]`. A quiet minute and a
  feed loss can't be told apart, and that stays true even with peer evidence (`certainty:
  "uncertain"`).
- **H1 and longer:** `["feed_interruption", "missing_history"]`; the expected-candle schedule
  decides what's required.

**Peer coverage:** a peer is *informative* only if its data spans the missing required slots.

**Per candle:** `gap_before` (the classification of the gap preceding it, or `null`) and `notes`:
- `first_in_window`: nothing is known about what came before;
- `sunday_partial_day`: an Exness Sunday D1 candle; noted, never altered or merged;
- `zero_tick_volume`.

**Window summary verdicts:**
- `unexplained_gaps`: any `symbol_missing` or `market_wide_unverified`.
- `unknown`: any `unknown` gap or input error.
- `no_unexplained_gaps_detected`: otherwise.

Never "continuous": the summary always states that the absence of detected gaps isn't proof of
continuous feed coverage. Adjacent candles and positive tick volume aren't treated as proof.

**Input errors are reported, not guessed:**
- missing, malformed or timezone-less timestamps;
- timestamps not aligned to the timeframe grid;
- duplicates;
- out-of-order timestamps.

The remaining candles are analysed sorted and de-duplicated, and the verdict becomes `unknown`.

**Sunday D1.**
- **Raw candles untouched:** nothing is modified, deleted or merged.
- **Raw D1 sequence:** Friday → Sunday has a Saturday gap, classified `configured_closure`; Sunday
  → Monday is adjacent.
- **Missing Sunday candle:** Friday → Monday leaves the Sunday slot (open from 17:05 New York)
  required, so it's `unknown`, never silently a closure.
- **Future exclusion supported:** when the future analysis layer excludes Sunday candles, it can
  pass them as `excluded_opens`. The step is then `analytical_exclusion`, not a data gap. The
  exclusion policy itself isn't implemented in T3.

## 4. Daylight saving

- **Defined in New York time:** every rule is a New York wall time.
- **Converted per date:** each date's boundaries go to UTC with `zoneinfo`
  (`datetime.combine(day, 17:00, tzinfo=America/New_York).astimezone(UTC)`).
- **US changes:** the UTC times move by an hour. For example, the Friday close is 21:00 UTC in
  US summer time and 22:00 UTC in winter.
- **Transition weekends:** on these the close and reopen fall in different offsets: the 2026
  fall-back closes Friday 21:00 UTC and reopens Sunday 22:05 UTC; the spring-forward closes
  22:00 UTC and reopens 21:05 UTC. Both match T0.
- **EU changes** don't affect the schedule (tested for 2025-10-26, 2026-03-29 and 2026-10-25).
- **No ambiguous times:** the boundaries (17:00 / 17:05) never fall in a DST gap or fold (02:00
  local), so no ambiguous or nonexistent local time arises.
- **No machine dependence:** the machine's timezone isn't used.

## 5. Tests added (`tests/test_sessions_continuity.py`, 46)

| Area | Tests |
|---|---|
| Weekly close and reopen, both US DST states | 10 boundary instants (Friday 20:59/21:00 EDT, 21:59/22:00 EST; Sunday 21:04/21:05, 22:04/22:05; weekday; Saturday) |
| US transition weekends and EU changes | 6 weekends (2025 and 2026 fall-back, 2026 spring-forward, 3 EU changes); plus a gap across the 2026 fall-back weekend; plus a missing first hour after the spring reopen |
| Reopen tolerance | First M1 candle at 21:24 (within: `reopen_delay`) vs 21:26 (outside: required slot missing) |
| Rollover | 7 cases: EST and EDT starts; 15 min allowed and 16 min not; starts at 17:29 (in) and 17:30 (out); a 16:00 New York start (out); not applied to H1; New York wall time |
| Holidays | Christmas (Thursday), New Year (Wednesday), Monday holiday (weekly + holiday, no false reopen delay); US Independence Day not assumed |
| Gaps outside closures | In-session H1 gap: `unknown`, never "continuous" |
| Market-wide vs symbol-specific | Simultaneous gap: `market_wide_unverified`; peer has data: `symbol_missing`; peer not spanning: `unknown` |
| No-tick vs unknown | M1 uncertainty reported with and without peer evidence |
| No silent continuity | 50 random in-session gap patterns: never `no_unexplained_gaps_detected` |
| H1, H4, D1 schedules | Full EST H1 week; EDT H4 week (missing Friday 20:00 H4 detected); D1 Friday/Sunday/Monday; Sunday note; missing Sunday; analytical exclusion; Christmas D1 candle expected |
| Inputs | Malformed, missing, timezone-less, misaligned, duplicate and out-of-order timestamps; unsupported timeframe |
| Determinism | 50 identical runs |
| Configuration | Values, evidence and validation; an alternative config changes the result |

**Bug the tests caught during development:** a Monday holiday was wrongly reported as a
"reopen delay" against the weekend's own reopen. Reopen delay is now judged against the last
scheduled reopen the gap reaches.

**Results:** `tests/test_sessions_continuity.py`: 46 passed (3.13, verbose run), and also within both full runs.

The `analysis` package import ban from T2 (`test_analysis_package_imports_no_broker_provider_or_network_code`)
covers both new modules automatically: no MT5, adapter, collector, live-market or network imports.

## 6. Full test results

| Command | Result |
|---|---|
| Baseline (end of T2) | 365 passed on 3.11 and 3.13 |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **411 passed**, 0 skipped, 0 failed (includes the 50 browser tests) |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **411 passed**, 0 skipped, 0 failed (includes the 50 browser tests) |
| Separate browser-only run, `tests/test_browser_smoke.py` (3.11.8), **first attempt** | **1 failed, 41 passed, 9 errors after 74 minutes** (normal duration about 3 minutes) |
| Same, **re-run** | **50 passed** in 175 s |

The increase is exactly the 46 new tests, and no existing test was modified.

**The stalled browser-only run.** It ran after both full suites had already passed all 50 browser
tests.
- **Duration:** it took 74 minutes instead of about 3. It ended close to 13:52 local time, when
  your Johanna backend was restarted (new process IDs at 13:52:24).
- **Cause unknown:** only the summary line was captured, not the failure details. A suspended or
  stalled machine or browser would be consistent with the duration, but that isn't confirmed.
- **Unrelated to T3 code:** T3 changes no browser-facing code, the tests use their own temporary
  backends, and an immediate re-run passed 50 of 50.

## 7. Ruff, formatting and JavaScript

| Command | Result |
|---|---|
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 82 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |

## 8. Existing functionality preserved

- **Additive only:** T3 adds modules and doesn't change any existing source file.
- **Endpoints and dashboard unchanged:** market-data endpoints, quotes, bars, snapshots, the chart,
  automatic refresh, zoom, pan, price-axis scaling and Go Live are unchanged.
- **Tests:** all existing tests, including the 50 browser tests, pass unchanged (§6).

## 9. Data and backend

- **`data/` contents unchanged:** SHA-256 hashes of all 11 readable files under `data/` are
  identical before and after T3, and the file count is still 12.
- **Two modification times changed, by your backend:** `data/johanna.lock` and
  `data/johanna.sqlite3` now show 13:52:26 instead of 12:19:54. That coincides exactly with your
  backend being restarted at 13:52:24. Johanna's startup rewrites its configuration snapshot and
  lock (known behaviour), and the database content hash is unchanged.
- **This wasn't T3:** T3 never opened `data/`, never touched the backend, and all its tests used
  temporary directories.
- **Your backend wasn't stopped or restarted by me.** Its processes were 35808/12788/31896
  (started 12:19) at the T3 baseline and are 44532/38208/30208 (started 13:52) now. That restart
  didn't come from T3: nothing in this work starts, stops or contacts your backend.

## 10. Limitations and assumptions

- **Provisional rules.** The configuration reflects the T0 sample only:
  - minute-level holiday evidence goes back only to October 2025;
  - other holidays, early closes or schedule changes would show up as unexplained gaps until the
    configuration is updated from new evidence;
  - the holiday rule assumes holiday closures mirror the daily 17:00 / 17:05 boundaries (T0
    showed this; it's not universal).
- **Peers are evidence, not proof.** `market_wide_unverified` deliberately isn't a closure.
  `symbol_missing` on M1/M5 can still be a genuine no-tick interval for that symbol.
- **No continuity proof.** As agreed, nothing here proves continuous feed coverage.
- **Reopen tolerance** applies after holiday closures too (T0 holiday reopens were 17:05–17:10).
- **Rollover detail:** the rule tests the gap's start and total duration. A gap starting at 17:29
  may extend to 17:44.
- **Fixed grid:** timeframes are fixed-length UTC grids (true for the Exness UTC server).
  Misaligned timestamps are reported as input errors rather than handled.
- **Not wired in.** T3 isn't wired into the API, the snapshot or the dashboard; that's for later
  approved stages (T4 and later).
- **Not validated live:** I haven't checked the classification against real Exness bars beyond
  the T0 sample, because T3 had no MT5 access by design.
- **Carried forward:**
  - **T1:** conflicting duplicates must make T4 reject the window; keep OHLC validity, timestamp
    ordering and continuity separate.
  - **T2:** the 30 s clock limit is redundant with the 2 s tick limit (same tick read), but the
    metadata and checks are kept.
  - **Live verification** of T1/T2 against the real terminal is still outstanding, and your
    backend still needs your permission to restart.
