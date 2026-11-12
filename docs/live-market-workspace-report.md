# Live market workspace: completion report

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed**

> **Update:** an audit (`docs/live-market-workspace-audit.md`) found six issues in this work
> (H1, M1, M2, M3, M4, M7). They are fixed and described in
> `docs/live-market-workspace-remediation-report.md`, which supersedes the test counts and the
> MT5-launch, clock-check and candle descriptions below.

## 1. Summary

The live MT5 market workspace is implemented and passes **237 automated tests on Python 3.11
and 3.13** (including 18 headless-browser tests). Live runs against the real terminal confirmed
the main behaviours:
- **Connectivity and clock check:** connection, time-base checking and honest refusal of
  shifted timestamps.
- **Responsiveness:** backend responsiveness during heavy MT5 requests.
- **Persistence and fallback:** restart persistence and the UNAVAILABLE path.

**All 12 acceptance criteria pass.** Criterion 6 (timestamps verified against your Exness
server) was first blocked by an MT5 "Authorization failed" error. After the login was fixed, the
acceptance run on 2026-11-12 at 06:15 UTC against `Exness-MT5Real21` reported
`time_check: verified`. The Exness server clock is **UTC**, and Johanna's default needs no
offset (§6, §9).

That Exness run also exposed one bug, now fixed: pairs just added to Market Watch showed MT5's
all-zero placeholder tick (bid 0.0, timestamp 1970) as a STALE price (§9).

Two findings changed the design during the work:
1. **The connected terminal was not Exness.** It was logged into `MetaQuotes-Demo`, and every
   earlier timestamp observation came from that server. Exness's documented GMT+0 doesn't apply
   to that data.
2. **The MetaTrader5 Python package holds the GIL for each call's full duration** (measured
   0.26–2.9 s for bar requests). That froze the threaded backend in a live run. MT5 now runs in
   **separate worker processes**.

## 2. What was implemented

| Capability | Implementation |
|---|---|
| Live quotes from MT5 | `LiveMarketService.quotes()`: bid, ask, spread (pips), quote time, quote age, status per pair; 1 s in-memory cache; no background polling |
| Broker symbol resolution | `MetaTrader5Adapter.resolve_symbol()`: exact name or account-suffixed name (e.g. `EURUSDm`); "Not offered by this broker" otherwise |
| On-demand OHLC bars | `LiveMarketService.bars()`: M1–D1, up to 1,000 bars including the forming candle; 5 s cache |
| Status vocabulary | `LIVE` / `STALE` (old tick, market closed) / `CACHED` (MT5 unavailable: earlier value or Parquet archive) / `UNAVAILABLE` |
| Server-clock handling | `ServerClock` (IANA zone + whole-hour shift, DST from the zone database); default UTC; configurable via `[integrations] mt5_server_time_zone` / `mt5_server_time_shift_hours` |
| Time-base safety | A tick or bar later than "now" is refused as `server_clock_mismatch`. Bars also fetch a tick first, closing an H4/D1 gap found live (§6). |
| Responsiveness | MT5 work runs in two spawned worker processes (quotes, bars). Requests time out (4 s quotes, 20 s bars); a stuck worker is terminated and respawned. HTTP server is threaded. |
| Never starts MT5 | Workers confirm, by full executable path, that the intended terminal is already running, then attach to exactly that path (`MetaTrader5.initialize` can launch it otherwise). Strengthened in the remediation (M2). |
| Dashboard | Market-first Overview (selected-pair quote strip, candlestick chart, live watchlist, market information, calendar, one "Analysis & setups: next phases" panel); Markets page with live table; Charts page; top bar shows market status and server. The six placeholder rows are gone from the Overview; real service health is on Settings with a one-line "planned" note. |
| Candlestick chart | Canvas candlesticks, right price axis with broker digits, time axis in the user's timezone, live bid line, CACHED/legacy notices, "time base not yet verified" notice |
| Default watchlist | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD, **for new databases only**; existing watchlists are never changed |

**Out of scope, not touched:** indicators, strategies, backtesting, signals, sizing, risk,
Gemini, news, ML, execution, migration, collector features.

## 3. Files

**New:**
- `src/johanna/live_market.py`
- `scripts/verify_live_market.py` (read-only live acceptance)
- `tests/conftest.py`, `tests/fake_mt5.py`, `tests/fake_mt5_session.py`
- `tests/test_live_market.py`, `tests/test_mt5_worker_process.py`, `tests/test_server_clock.py`
- this report

**Modified:**
- `src/johanna/adapters/mt5.py`: `ServerClock`, `MT5TimeBaseError`, `server_identity`,
  `resolve_symbol`, `price_format`, `include_current`, M30/H4/D1. This completes the earlier
  partial timestamp work.
- `src/johanna/application.py`, `api.py`, `config.py`, `persistence.py`, `__main__.py`
  (threaded server)
- `src/johanna/web/app.js` (rewritten around live data), `web/styles.css`
- `tests/test_browser_smoke.py` (rewritten for the new UI)
- `tests/test_application.py`, `tests/test_phase1_remediation.py` (3 assertions updated for
  the new service, startup order and default watchlist), `tests/test_collector_locking.py`
  (fake adapter accepts `server_clock`)
- `README.md`

**Kept from the interrupted timestamp work:** `src/johanna/collectors.py`
(`mt5.server_time_zone` / `server_time_shift_hours`, legacy-store guard). No new collector
features.

## 4. How live MT5 requests work

```
browser ─2 s (quotes) / 30 s (bars), only while visible─► GET /api/market/quotes | /bars
        backend thread ─► LiveMarketService (cache hit → answer immediately)
                          └─ cache miss ─► pipe ─► MT5 worker process (quotes | bars)
                                                     └─ MT5Session → MetaTrader5Adapter → terminal
```
- **Workers:** each worker owns one read-only MT5 connection and per-connection caches (broker
  symbol names, digits). The backend waits on the pipe, which releases the GIL. A worker that
  exceeds its timeout is killed and replaced on the next request. Workers start on first use and
  stop with the application.
- **Read-only calls only:** `initialize`, `terminal_info`, `account_info` (only company, server
  name and account type are kept), `symbol_info`, `symbols_get`, `symbol_select` (Market Watch
  visibility only), `symbol_info_tick`, `copy_rates_from_pos`, `shutdown`, `last_error`.
  `tests/fake_mt5.py` implements nothing else, so any other call would fail the tests. `grep` for
  order/position APIs in `src/`: no matches.

## 5. Timestamp handling

- **What MT5 returns:** `time`/`time_msc` are the trade server's wall clock as epoch seconds.
  `ServerClock.to_utc(epoch)` turns the epoch into a naive wall time, subtracts the shift,
  localizes it in the IANA zone (DST-aware) and returns an **aware UTC** datetime. An ambiguous
  or non-existent wall time (DST fold or gap) is **refused, not guessed**.
- **Single conversion:** conversion happens once, in the adapter. Downstream code only formats
  UTC (`…Z`). The browser renders with `Intl.DateTimeFormat` in the user's selected timezone.
  Tests confirm tick and bar times equal `datetime.fromtimestamp(epoch, UTC)` under the UTC
  clock, with no double conversion.
- **Independent of the machine timezone:** subprocess tests under `TZ=UTC0`, `EST5EDT`, `JST-9`
  and unset produce identical results.
- **DST coverage:** New York-anchored clock across 8 Mar 2026 and 1 Nov 2026; Europe/Athens
  across 29 Mar 2026 and 25 Oct 2026; fold/gap refusal.
- **Live verification of the configured clock:**
  - The freshest tick must convert to within −60 s…+120 s of system UTC to count as
    `verified`.
  - Older ticks give `stale_ticks` (named `unverifiable_market_quiet` before the
    remediation); no fresh tick at all gives `unavailable`.
  - Future ticks give `mismatch`, and quotes and bars are refused.
  - The UI shows the result, and the chart says when bar times are not yet verified.

## 6. Server timezone: evidence and conclusion

**What the terminal reported (read-only; no login or balance read):**

| When | Server | Company | Account | Raw tick − system UTC |
|---|---|---|---|---|
| Until ~00:18 | `MetaQuotes-Demo` | MetaQuotes Ltd. | demo | **+7,190 s** (consistently, in every probe) |
| First login attempt | window title shows `Exness-MT5Real21` (real account) | — | — | not readable: `IPC timeout`, then **`Terminal: Authorization failed` (−6)** for 3+ minutes |
| 2026-11-12 06:12–06:15 UTC, after the login was fixed | `Exness-MT5Real21` | Exness Technologies Ltd | real (cent; symbols suffixed `c`) | **−0.0 s to −5.0 s** for all 7 pairs (normal tick age); latest M1 bar opened at 06:12:00 at UTC 06:13:00 |

**Exness: verified as UTC.** Exness documents GMT+0 for MT5 trading servers, and the live
evidence matches. During market hours, raw tick epochs read as UTC are at most a few seconds old,
and the forming M1 bar opens on the current UTC minute. Johanna's default `ServerClock()` (UTC)
is therefore correct for this account, and no configuration was added. The time check stays in
place in case the account or server ever changes.

**MetaQuotes-Demo (for the record):**
- **Inferred clock:** its clock fits New York + 7 h. The offset switched exactly on the 2026 US
  DST dates across a year of raw bars.
- **Live check:** one diagnostic run configured that clock through environment variables only;
  the time check came back `verified` (freshest tick −0.9 s).
- **Status:** this is inferred, not documented, and it is **not** configured in Johanna.

**Your 793 archived bars (unchanged):**
- They came from MetaQuotes-Demo and are stamped in that server's time, **UTC+3**, because
  23–30 Oct 2026 lies inside US DST.
- 792/793 sit exactly at raw MT5 epochs, so the pipeline added no shift of its own.
- As a CACHED fallback they are flagged on the chart as server time, not UTC.

**Defect found and fixed during live testing:** with a misconfigured clock, H4 and D1 bars passed
the old future-time check, because a +2 h shift can leave their open time in the past. Bars now
fetch a tick first. Live, all timeframes M1–D1 were refused as `server_clock_mismatch` on
MetaQuotes-Demo under the UTC default. (Limitation: when the market is closed, a shift can't be
detected from ticks; see §11.)

## 7. Caching and unavailable states

| Situation | Quotes | Chart |
|---|---|---|
| MT5 answering, recent tick | `LIVE` | `LIVE` bars from MT5 |
| MT5 answering, old tick (weekend) | `STALE` with age | `LIVE` bars; "not yet verified" notice |
| MT5 stops answering after earlier success | `CACHED` (last values, reason shown) | `CACHED` (bars fetched earlier) |
| Terminal not running / not logged in / offline / clock mismatch / timeout | `UNAVAILABLE` + reason | `CACHED` from Parquet archive if present (flagged; legacy server time warned), else `UNAVAILABLE` |

Cached prices are never labelled live. No price is shown when nothing real is available.

## 8. Test results (2026-11-12, Windows 11)

| Command | Exit | Result |
|---|---|---|
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.11.8) | 0 | **237 passed** |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.13.3) | 0 | **237 passed** |
| `python -m ruff check --no-cache .` | 0 | All checks passed |
| `python -m ruff format --check --no-cache .` | 0 | 64 files already formatted |
| `node --check src/johanna/web/app.js` | 0 | OK |
| `grep -rnE "order_send\|order_check\|positions_get\|…" src/` | 1 | no matches |

**New tests:**
- `test_server_clock.py` (18): UTC identity, US and EU DST transitions, fold/gap refusal,
  machine-timezone independence, single conversion, future refusal.
- `test_live_market.py` (21): quotes and spreads, suffix resolution, cache, STALE, placeholder
  tick of a loading symbol never shown as a price, closed
  terminal never launched, mid-session loss → CACHED, offline, mismatch, timeout, bars,
  archive flag, read-only calls, API validation, no collectors, restart-safe watchlist,
  config validation.
- `test_mt5_worker_process.py` (5): real IPC; **GIL isolation** (backend thread stall < 0.3 s
  while the worker holds its GIL for 1.5 s); timeout kill and respawn; start failure; clean
  close.
- `test_browser_smoke.py` (18): live quotes and candlesticks (green and red candles drawn),
  on-demand refresh, pair/timeframe switching, navigation, settings persistence, services
  panel, failed collector, UNAVAILABLE + CACHED archive, STALE, mismatch, Markets and Calendar,
  400 px layouts, mobile menu.
- `conftest.py` guarantees **no test can reach a real terminal**.

## 9. Live verification (real `python -m johanna`, collectors disabled, temp data dir)

| Check | Result |
|---|---|
| Startup | OK; `collectors_config` unset; `market_monitoring_state: not_configured` |
| MetaQuotes-Demo, default UTC clock | All 7 pairs and all timeframes **refused as `server_clock_mismatch`** (2.00 h ahead); nothing shifted displayed |
| Responsiveness (diagnostic run with per-run NY+7 clock so real downloads execute) | 1,000-bar requests M1–D1 all `LIVE` (H4 took 3.3 s); **`/health` max 78 ms over 56 samples**; quotes max 2.8 s during the H4 download (terminal-side) |
| Restart persistence | Watchlist `[EURUSD, GBPUSD, USDJPY, EURGBP]`, pair `USDJPY`, timeframe `M15` restored after kill and restart |
| Closed terminal (simulated) | `MT5_TERMINAL_PATH` pointed to a non-running executable: all quotes `UNAVAILABLE` (`terminal_not_running`), no price shown, bars `UNAVAILABLE`. Your terminal was not closed. |
| Exness, first run (06:13 UTC) | Connected, `time_check: verified`, bars LIVE on all timeframes. **Bug found:** AUDUSD, USDCAD and NZDUSD came back `STALE` with bid/ask 0.0 and a 1970 timestamp. These were MT5's placeholder ticks for symbols just added to Market Watch, which the direct read a moment later showed as real prices. |
| Fix | `MT5Session.quotes` now rejects the placeholder tick and reports the pair `UNAVAILABLE` with reason `symbol_loading` ("Waiting for MT5 to load this symbol"); the next 2 s poll shows the real price. A regression test was added. |
| **Exness, after the fix (06:15 UTC, default UTC clock)** | `Exness-MT5Real21`, real account. **All 7 pairs `LIVE`** (resolved to `EURUSDc` … `NZDUSDc`); **`time_check: verified`** (freshest quote 0.3 s old). **Cross-check:** spreads equal a direct MT5 read for all 7 pairs, and bids for 6 of 7; the GBPUSD bid differed by one point (1.32565 vs 1.32566) because the price ticked between the two reads. Raw tick − system UTC: −0.0 s to −5.0 s. EURUSDc H1: 300 bars `LIVE`, `time_basis: utc`, OHLC identical to direct MT5; all timeframes M1–D1 `LIVE` with 1,000 bars. `/health` max 31 ms during requests. Restart persistence and the closed-terminal simulation passed again. |
| Backend log errors | none |

**To repeat the live acceptance** while MT5 is connected:
1. Make sure MT5 shows your Exness account **connected** (no "Authorization failed"; check
   File → Login to Trade Account, the Exness server name and the password; the investor
   password is enough).
2. From the repo root run: `.venv311\Scripts\python.exe scripts\verify_live_market.py`
3. Expect: `connection.server.server` = your Exness server; `time_check.result` = `verified`
   (during market hours); `cross_check` bids and spreads equal the direct MT5 read;
   `raw_tick_minus_system_s` ≈ 0; `data_dir_unchanged: true`.
4. If the time check says `mismatch` instead, the server isn't on UTC. Don't configure an offset
   by guesswork; check that server's documentation first.

## 10. Collectors and `data/`

- **Collectors were disabled** in every acceptance run (`JOHANNA_COLLECTORS_CONFIG` removed;
  status `not_configured`). No collector code was extended.
- **This session's work did not modify `data/`.** The acceptance script fingerprints every file
  (SHA-256) before and after: unchanged in each run. Market data files keep their original
  times (bars and manifests 2026-10-30/31).
- **Disclosures:**
  - **An earlier backend start rewrote the config snapshot.** `data/johanna.sqlite3` changed at
    2026-11-11 23:22:56–23:24:26, and `data/johanna.lock` was created then. That was a Johanna
    backend started on the default `data` directory (PID 39540), with code from the first
    remediation round. No test or script of mine starts an app there (all use temporary
    directories), and I didn't launch one at that time, so it was most likely a manual
    `python -m johanna`. A startup only rewrites the config snapshot; preferences and watchlist
    hold defaults (EURUSD / H1 / dark).
  - **My read-only inspection created two SQLite sidecar files.** While investigating the above,
    I opened `data/johanna.sqlite3` read-only, and SQLite created `johanna.sqlite3-wal`
    (0 bytes) and `johanna.sqlite3-shm`. Content is unchanged. I left them in place, since
    removing files in `data/` would itself be a modification; Johanna's next start uses them
    normally.

## 11. Outstanding limitations

1. **Real-money account.** The connected Exness account is a real (cent) account. Johanna makes
   only read calls, but an Exness demo account is the safer choice for development sessions.
2. **Time-check blind spots.** A server clock *behind* UTC can't be detected (ticks look old:
   STALE / unverifiable, never "verified"). Any offset is undetectable while the market is
   closed. The chart flags unverified bar times.
3. **Terminal-side serialization.** Quote requests can wait up to ~3 s while a large bar download
   runs; the terminal appears to answer its clients one at a time. Still within the 4 s timeout.
4. **Process footprint.** Two MT5 worker processes run while the dashboard is in use (each holds
   its own read-only connection).
5. **Market Watch visibility.** `symbol_select` makes a hidden broker symbol visible in Market
   Watch. That's a view change, not an account setting.
6. **Closed-terminal test method.** The closed-terminal criterion was verified by tests and a
   live simulation, not by closing your terminal.
7. **Collector can still launch MT5.** The deferred collector path (`python -m johanna.collectors`)
   still calls `initialize(path)`, which may start MT5. It was out of scope, and unchanged.
8. **Windows only.** Live MT5 works only on Windows (the MT5 package is Windows-only); elsewhere
   the workspace correctly shows UNAVAILABLE.

## 12. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Johanna starts | PASS |
| 2 | Collectors can stay disabled | PASS |
| 3 | Watchlist quotes directly from MT5 | PASS: all 7 pairs LIVE from Exness |
| 4 | Bid, ask, spread, age accurate | PASS: matches direct MT5 reads for all 7 Exness pairs (one GBPUSD bid one point apart from a price tick between reads); placeholder-tick bug fixed |
| 5 | Chart uses real MT5 OHLC | PASS (1,000-bar live downloads all timeframes) |
| 6 | Timestamps verified against the connected server | PASS: Exness `time_check: verified`, server clock is UTC; mismatch detection also verified live on MetaQuotes-Demo |
| 7 | Closed MT5 → honest UNAVAILABLE | PASS (tests + live simulation) |
| 8 | Cached clearly distinguished | PASS |
| 9 | Dashboard responsive during retrieval | PASS (live: `/health` ≤ 78 ms on MetaQuotes-Demo, ≤ 31 ms on Exness) |
| 10 | Preferences and watchlist survive restarts | PASS |
| 11 | No historical data modified | PASS for this session (fingerprints). Disclosures in §10. |
| 12 | Existing tests pass; new tests added | PASS (237/237 on 3.11 and 3.13) |
