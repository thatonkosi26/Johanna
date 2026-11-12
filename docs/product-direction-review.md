# Johanna — Product direction review

Date: 2026-11-11 · Author: Claude Code (Opus 5.5) · Review only: no code, configuration, data or
dependency was changed to produce this document.

## Summary

Johanna is meant to help you decide whether to take a Forex trade. Today it cannot do any part
of that.

- **About 60% of the source code collects, stores, validates or coordinates data.** The trading
  logic (indicators, strategies, signals, risk, sizing, AI) is **zero lines**.
- **The dashboard's "price" is not live.** It is the close of the last bar a background
  collector wrote to disk.
- **The terminal already holds history.** MT5 returned about a year of hourly bars in about a
  second, so the app does not need its own collection pipeline to analyse the market.

**Recommendation:** stop infrastructure work now. Make MT5 the live, on-demand data source, then
build the decision pipeline: indicators, then strategies with backtests, then risk and position
sizing, then trade setups with a decision journal. Calendar, news and Gemini come after that, and
execution comes last.

Collectors, Parquet history, Twelve Data, FRED, file ingestion and quality reports stay as they
are, **deferred, not deleted**.

Proposed next task (§H): **live MT5 market workspace, with no collector required**.

---

## Status of in-progress work (read first)

This review interrupted the MT5 timestamp investigation partway through. The working tree
contains its partial changes, left in place because this task forbids code changes:

- `src/johanna/adapters/mt5.py`: a new `ServerClock` (an IANA zone plus a whole-hour shift)
  converts MT5 epochs to UTC. A new guard **refuses any tick or bar timestamped more than 60 s
  in the future**. With the default `ServerClock()` (UTC), your broker's +2/+3 h timestamps now
  fail loudly instead of being stored shifted. **Live MT5 collection therefore errors until a
  server clock is configured.** This is deliberate, but unfinished.
- `src/johanna/collectors.py`: new settings `mt5.server_time_zone` and
  `mt5.server_time_shift_hours`, plus a guard that refuses to collect into a store that still
  holds uncorrected bars. Your `collectors.toml` was **not** changed.
- **One test fails:** `tests/test_collector_locking.py::test_cli_is_refused_while_backend_monitor_runs`.
  Its fake adapter doesn't accept the new `server_clock` argument; the test needs a one-line
  update. The other 174 non-browser tests pass.
- Not written: the migration utility, `docs/mt5-timestamps.md` (referenced in an error message),
  and tests for the new conversion.
- **Proven root cause (from read-only probes):**
  - The raw MT5 `time`/`time_msc` integers are the trade server's wall clock, not UTC.
  - They were exactly +7,200 s ahead of the system epoch whether the probe process ran with
    `TZ` unset, `UTC0` or `EST5EDT`. That rules out Python conversion and local-timezone leakage.
  - Across about a year of raw H1 bars the offset was +2 h in US winter and +3 h from
    8 Mar 2026 to 1 Nov 2026. It switched on the **US** DST dates and stayed +3 h after the EU
    switch (25 Oct). The server clock is **New York time + 7 h** (week opens Monday 00:00 server
    time). Your PC's fixed UTC+2 zone cannot produce +3 h.
  - The stored 793 bars (23–30 Oct 2026) are all at raw epochs, i.e. **+3 h**.
  - Correction: my previous report said +2 h for those bars. That was wrong.

How this fits the new direction is covered in §G and §H.

---

## A. Product vision assessment

**The current implementation does not reflect Johanna's purpose.** Measured against the six
capabilities in the brief:

| Intended capability | What exists | Status |
|---|---|---|
| 1. Market monitoring (prices, conditions, movements) | Background collector writes bars to Parquet; dashboard shows the last stored bar's close and a close-price line | **Partial**: not live, no conditions or movement detection |
| 2. Technical and quantitative analysis | Nothing | **Not implemented** |
| 3. AI market intelligence (Gemini, news) | Protocol stub `domain/intelligence.py`; `domain/news.py` stub; no news source | **Placeholder** |
| 4. Signal generation (setup with entry, SL, TP, size, R:R, risk, reasons, "no trade") | `domain/signals.py` stub | **Placeholder** |
| 5. Risk management (equity, leverage, margin, broker constraints, sizing, limits) | `domain/risk.py` stub; `adapters/mt5.py` can read symbol specs and `account_info`, but only uses them for diagnostics | **Placeholder** (useful raw inputs exist) |
| 6. Decision support and execution | Execution refused by `DisabledExecutionService` (correct); no decision explanations exist | **Not implemented** (execution intentionally absent) |

**Where the effort went (source lines, `src/johanna`, ≈7,360 total):**

| Category | Modules | ≈ Lines | Share |
|---|---|---|---|
| Data acquisition, collection, storage, quality | `collectors`, `market_data`, `ticks`, `ingestion`, `quality`, `adapters/{mt5,twelve_data,fred,economic_calendar}`, `mt5_smoke`, `time`, `fsutil`, `instance_lock` | 4,450 | **60%** |
| Application shell, lifecycle, persistence, API | `application`, `data_services`, `persistence`, `services`, `config`, `api`, `__main__`, `logging_config` | 1,870 | 25% |
| Dashboard | `web/*` | 670 | 9% |
| Domain contracts (stubs only) | `domain/*` | 330 | 4% |
| **Trading intelligence** | — | **0** | **0%** |

All 4,010 test lines verify infrastructure (storage, locks, lifecycle, security, rendering).
None evaluates the quality of a trading decision, because no decision logic exists.

**How it drifted (candidly).** The Week 1–2 plan was explicitly a data foundation
(`docs/week-1-2-operations.md`: "collects and validates data; it does not contain trading
strategies…"). The Phase 1 audit and remediation rounds then optimised that foundation:
locks, health, persistence and acceptance tests. Much of that hardening was mine: I added
instance and source locks, Windows file-replace retries, a browser test harness, and the
timestamp work. Each piece was correct for the question it answered. Taken together, they kept
the project in infrastructure mode while the product itself never started.

---

## B. Current architecture assessment

```
MT5 terminal ──(collector thread or CLI, every 60 s)──► Parquet bars + tick snapshots
                                                       + collector_state/*.json
Forex Factory ──(calendar collector, 6 h cache)──────► calendar JSON snapshots
Twelve Data / FRED adapters ─────────────────────────► (no consumer)
File ingestion / quality report ─────────────────────► (CLI only, no consumer)

Backend (wsgiref) ── JohannaApplication
   ├─ ApplicationStore (SQLite: preferences, watchlist, config/service/runtime/cache tables)
   ├─ MarketDataService  → reads Parquet + manifests + collector state from disk
   ├─ CalendarService    → reads snapshot JSON from disk
   └─ 6 domain stubs (news, intelligence, signals, rules, risk, execution) → refuse
API: /health /status /api/dashboard /api/preferences  →  vanilla-JS dashboard
```

**Drift points:**
1. **Indirect market data.** The UI never asks MT5 for anything. Price and chart lag by at least
   one bar plus the poll interval, and show nothing at all unless a collector has been running.
   A trading assistant needs the current bid/ask and spread on request.
2. **Duplicated historical storage.** MT5 already serves deep history on demand; the probe pulled
   6,000 H1 bars in about a second. The collector rebuilds a much smaller copy of that history,
   with checkpointing, pending-batch replay, retries and locks, and nothing consumes it except
   the chart.
3. **Coordination machinery serves the collectors, not the product.** The source lock, Windows
   replace-retry and pending-batch recovery all exist because collectors run continuously and
   write files.
4. **Unused data sources.** Twelve Data (with a persisted credit ledger), FRED, HistData/MT5
   file ingestion, the sampled tick store and the quality reports have no product consumer.
5. **Placeholders that look like progress.** The six domain stubs show up as six health rows
   ("Disabled") on the Overview. They make the system look broader than it is.
6. **Unused persistence tables.** `provider_cache` and `runtime_state` have no runtime writer.
   SQLite holds no decision records, which are the most valuable thing the product should keep.

**What is sound and directly reusable:** the read-only MT5 adapter (including symbol specs), the
economic calendar adapter, SQLite preferences and watchlist, the config and lifecycle core, the
API pattern, and the dashboard shell.

---

## C. Component classification

| Component | Files | Class | Reasoning |
|---|---|---|---|
| MT5 read-only adapter | `adapters/mt5.py`, `adapters/base.py` | **RETAIN** (extend) | The primary market source. It already reads ticks, bars, symbol specs (contract size, volume limits, filling modes) and `account_info`, which are exactly the inputs analysis and risk need. Finish the server-clock conversion (§G). |
| MT5 timestamp correction | `adapters/mt5.py` (partial) | **REPRIORITISE** (finish, small) | Indicators, session logic and calendar proximity are wrong if bar times are off by 2–3 h. Needed before analysis, but only at the adapter boundary (no migration; see §G). |
| Continuous MT5 collector + pending-batch replay | `collectors.py` (MT5 part) | **DEFER** | Not needed for monitoring or analysis once bars are fetched on demand. Keep it working as an optional archiving tool; stop extending it. |
| Sampled tick store | `ticks.py`, tick part of `collectors.py` | **DEFER** | One quote per minute has no analytical use and no consumer. Live quotes come straight from MT5. |
| Parquet bar store | `market_data.py` | **RETAIN** (frozen) | Fine as an optional cache or backtest dataset format, and holds the existing 793 bars. No new features. |
| Historical file ingestion | `ingestion.py` | **DEFER** | Only needed if backtests require history the broker doesn't provide. Revisit in roadmap phase 3 if that happens. |
| Data quality report | `quality.py` | **DEFER** | Audits the collection pipeline, which is itself deferred. |
| Twelve Data adapter + credit ledger | `adapters/twelve_data.py` | **DEFER** | A second price source isn't needed while MT5 is primary. Harmless and tested; keep it. |
| FRED adapter | `adapters/fred.py` | **DEFER** | Macro series are slow-moving context; possibly useful to Gemini later. Not MVP. |
| Economic calendar adapter | `adapters/economic_calendar.py` | **RETAIN / REPRIORITISE** | Directly product-relevant: "high-impact event within X minutes" is a core trade filter. It needs to be *used* by the pipeline, not just displayed. |
| Calendar collector worker | calendar part of `collectors.py`, `CalendarService` refresh worker | **SIMPLIFY** | Replace the separate collector, lock and state file with an on-demand fetch behind the existing 6-hour cache. Behaviour stays the same with fewer moving parts. |
| Source lock, Windows replace-retry | `instance_lock.SourceLock`, `fsutil.py` | **RETAIN** (frozen) | Small and correct. They only matter while collectors run, so no further work. |
| Instance lock, service manager, `BackgroundService` | `instance_lock.py`, `services.py` | **RETAIN** | Small. Prevent real problems (double backend). Stop adding lifecycle features. |
| SQLite application store | `persistence.py` | **RETAIN / REPRIORITISE** | Keep preferences and watchlist. Its most important future job is the **decision journal** (setups, inputs, reasons, outcomes). The unused tables can stay for now (simplify later). |
| `MarketDataService` (disk-based) | `data_services.py` | **SIMPLIFY** | Becomes a live MT5 provider with a short in-memory cache and freshness labels. Reading Parquet becomes an optional fallback ("cached"). |
| `CalendarService` | `data_services.py` | **RETAIN** | Already reads snapshots and reports freshness accurately. |
| Domain contract stubs | `domain/*.py` | **SIMPLIFY** | Keep the data types as a starting vocabulary. Don't add more stubs. Replace each with a real module when it's built. Hide the "Disabled" rows from the Overview. |
| News integration | — | **REPRIORITISE** (after the deterministic pipeline) | Valuable context, but the source needs a licensing decision. The pipeline should work correctly without it. |
| Gemini integration | — | **REPRIORITISE** (after the deterministic pipeline) | Must be advisory: it may confirm, weaken or veto, never size up or override risk. Build it when there are setups for it to judge. |
| Technical indicators & strategies | — | **REPRIORITISE → next** | The core missing capability. |
| ML / meta-model | — (nowhere in the code) | **DEFER** | Needs baseline strategies, a backtest harness and labelled outcomes first. Not MVP. |
| Signal generation | — | **REPRIORITISE** | Core MVP, after indicators, strategies and risk. |
| Risk management & sizing | — | **REPRIORITISE** | Core MVP. A setup without a size and a monetary risk isn't actionable. |
| Execution | `domain/execution.py` (refusing) | **DEFER** | Keep refusing. Revisit only after forward-tested decisions. |
| Dashboard shell & navigation | `web/*` | **RETAIN** (refocus content) | The layout is good. The Overview should show live quotes, a candlestick chart, analysis and setups instead of infrastructure health. |
| API | `api.py` | **RETAIN** | Security and routing are solid. Add analysis and setup endpoints. |
| Browser smoke harness | `tests/browser_harness.py`, `tests/test_browser_smoke.py` | **RETAIN** (frozen) | Cheap regression protection. Extend only for new user-facing panels. |
| `mt5_smoke` diagnostic | `mt5_smoke.py` | **RETAIN** | Useful terminal check. Note that it writes symbol specs into `data/broker_specs/`. |
| Infrastructure acceptance cycles | `docs/phase-1-*` | **STOP** | No further infrastructure acceptance rounds. Future acceptance criteria are decision-quality criteria (§F). |

Nothing is classified **REMOVE**. Deleting tested, working code would save no effort now and
loses optionality. Deferring it costs nothing.

---

## D. Missing trading capabilities (most important first)

1. **Live market view from MT5:** current bid/ask, spread, last tick age, and candlestick bars on
   demand per pair and timeframe, with live / cached / stale / unavailable labels.
2. **Indicator and market-structure engine:** a small, verified set (e.g. EMA, ATR, RSI, ADX or
   an equivalent trend-strength measure, swing highs/lows, volatility regime) as pure functions
   over bar arrays.
3. **One or two transparent systematic strategies** that turn analysis into candidate setups
   (direction, entry, stop, target, invalidation, reasons), for example trend-pullback with ATR
   stops, or a range breakout.
4. **Backtest harness** that runs the *same* strategy code over broker history fetched on
   demand, with spread costs, reporting expectancy, win rate, drawdown and trade count. Without
   it, signals have no evidence behind them.
5. **Account-aware risk engine:** equity, balance, free margin and leverage from `account_info`;
   tick value, contract size and volume min/step/max from symbol specs. Computes position size
   for a configured risk %, monetary risk, R:R and margin required, and vetoes setups that break
   limits (per-trade risk, daily loss, open exposure).
6. **Trade setup output and "no trade" reasoning:** the complete setup from the brief, or an
   explicit "no trade justified" with reasons.
7. **Decision journal:** every evaluation stored in SQLite with its inputs, so recommendations
   can be reviewed and later scored against outcomes.
8. **Event-risk filter:** high-impact calendar events near entry block or flag the setup.
9. **Gemini context review:** reads the setup, the indicator summary, nearby events and (later)
   headlines; returns confirm, weaken or invalidate with reasons; can only reduce or veto.

---

## E. Proposed target architecture

```
                      ┌──────────────── MT5 terminal (read-only) ────────────────┐
                      │ quotes · bars on demand · symbol specs · account snapshot │
                      └──────────────────────────────┬────────────────────────────┘
                                                     │ ServerClock → UTC
                                         MarketData (live, short TTL cache,
                                          live/cached/stale/unavailable)
                                                     │ bars, quote, specs, account
                     ┌───────────────────────────────┼───────────────────────────────┐
                     ▼                               ▼                               ▼
          Analysis (pure functions)        Strategies (pure functions)      Calendar (6h cache)
          indicators · structure ·  ─────► candidate setups with  ◄──────── event-risk windows
          volatility regime                entry/SL/TP/invalidation
                                                     │
                                                     ▼
                                     Risk engine (pure, account-aware)
                         size · monetary risk · R:R · margin · limit checks · VETO
                                                     │ approved / rejected + reasons
                                                     ▼
                                Context review (optional, advisory only)
                       Gemini + headlines → confirm / weaken / invalidate  (may only
                       downgrade or veto; never resizes; cached; pipeline works without it)
                                                     │
                                                     ▼
                         Decision = trade setup  or  "no trade" + reasons
                                 │                              │
                        Decision journal (SQLite)        API → Dashboard
                                                     (later) Execution gate: explicit
                                                     permission + independent limits
```

Principles:
- **One data path for live use and backtests.** The analysis, strategy and risk modules take
  plain inputs (bars, quote, specs, account) and return plain outputs. A backtest replays
  historical bars through the same functions. This is also what makes decision quality testable.
- **MT5 is the source of truth for market data.** No continuous collection is required. Parquet
  becomes an optional cache or archive.
- **Risk is the last deterministic gate and cannot be overridden by AI.** Gemini sits after it
  and can only remove or weaken a setup.
- **Persistence holds what a decision workstation needs:** preferences and watchlist (existing),
  decisions and their inputs (new), AI reviews (new, cached), calendar snapshots (existing files).
- **The existing shell stays:** `JohannaApplication`, `ApplicationStore`, `BackendAPI`, and the
  dashboard navigation.

---

## F. Revised development roadmap

Changes from the proposed order, with the reasons:
- **Backtesting moves up**, right after strategies. Showing signals with no measured edge would
  invite trading on untested rules.
- **Risk comes before signal output.** The required setup fields (size, monetary risk) cannot
  exist without it.
- **The calendar filter joins the decision pipeline phase.** The adapter exists, so it's cheap.
- **News and Gemini come after the deterministic pipeline**, so correctness never depends on AI
  or on a news licence.

### Phase 1 — Live market workspace (MVP)
- **Objective:** see the market now, from MT5, with no collector running.
- **Depends on:** finishing the `ServerClock` conversion; configuring the server clock (evidence
  points to `America/New_York` + 7 h, to be confirmed against broker documentation).
- **Deliverables:** watchlist with live bid/ask, spread and quote age; candlestick chart for any
  pair and timeframe, fetched on demand; live/cached/stale/unavailable labels; Overview without
  infrastructure rows.
- **Acceptance:** collectors disabled → watchlist and chart still populate within 2 s; quote age
  and bar times match the terminal's server time converted to UTC (verified live); terminal
  closed → "unavailable" with no fabricated values.
- **Deferred:** collectors, tick store, Parquet extensions.

### Phase 2 — Indicators and market structure
- **Objective:** a quantified description of each pair's state.
- **Depends on:** Phase 1 bars.
- **Deliverables:** indicator module (EMA, ATR, RSI, trend strength, swings, volatility regime);
  chart overlays; per-pair analysis summary.
- **Acceptance:** each indicator matches hand-computed reference fixtures to a stated tolerance;
  outputs are identical for identical input bars (pure, deterministic).

### Phase 3 — Strategy evaluation and backtesting
- **Objective:** systematic setups with measured historical behaviour.
- **Depends on:** Phase 2.
- **Deliverables:** 1–2 documented strategies producing candidate setups; a backtest runner over
  on-demand MT5 history with spread costs; a report (trades, win rate, expectancy, max drawdown,
  profit factor).
- **Acceptance:** backtest and live evaluation share the same code; a no-lookahead test (results
  unchanged when future bars are removed); reports reproducible from a saved bar set.
- **Deferred:** ML, optimisation, multi-strategy portfolios.

### Phase 4 — Account-aware risk and position sizing
- **Objective:** turn a candidate into an exact, affordable size, or reject it.
- **Depends on:** MT5 account snapshot and symbol specs (adapter already reads both).
- **Deliverables:** risk settings (risk % per trade, max daily loss, max open risk); a sizing
  calculator (volume rounded to broker step, monetary risk, R:R, required margin); veto reasons.
- **Acceptance:** sizing matches worked examples for JPY and non-JPY pairs and account-currency
  conversion; setups exceeding limits or free margin are rejected with a reason; never sizes
  above the configured risk.

### Phase 5 — Decision pipeline, signals and event filter
- **Objective:** a complete, explained recommendation, or an explicit "no trade".
- **Depends on:** Phases 2–4; calendar adapter.
- **Deliverables:** setup card (pair, BUY/SELL, entry, SL, TP, size, R:R, monetary risk, reasons);
  "no trade" state with reasons; high-impact event filter; decision journal in SQLite; journal
  view.
- **Acceptance:** every evaluation is journaled with its inputs; the same inputs give the same
  decision; an event within the configured window blocks the setup; no setup is shown without a
  size and risk amount.

### Phase 6 — Gemini contextual review and news
- **Objective:** context that can weaken or invalidate a setup.
- **Depends on:** Phase 5; a decision on the news source and licence; a Gemini API key.
- **Deliverables:** an advisory review (confirm / weaken / invalidate + reasons) attached to each
  setup; cached responses; clear "AI unavailable" state.
- **Acceptance:** AI output can never raise size or remove a risk veto (enforced in code and
  tests); the pipeline runs unchanged with AI disabled; prompts and responses are journaled.

### Phase 7 — Forward evaluation
- **Objective:** measure recommendation quality on live markets without trading.
- **Deliverables:** outcome tracking for journaled setups (hit SL, hit TP, expired), live
  expectancy versus the backtest.
- **Acceptance:** at least N weeks of forward results reviewed before any execution work.

### Phase 8 — Controlled execution (later; demo account first)
- Explicit per-trade confirmation, independent hard limits outside the strategy code, kill
  switch, full audit log. Only after Phase 7 shows acceptable results.

**Explicitly deferred beyond the MVP (Phases 1–5):** continuous collectors, tick collection,
Twelve Data, FRED, file ingestion, quality reports, ML/meta-models, optimisation, multi-broker
support, execution.

---

## G. Existing work preservation

**Must be preserved (no change in any phase):**
- **`data/` as a whole:** `johanna.sqlite3` (preferences, watchlist), `collectors/mt5` (793 bars),
  `collectors/mt5_ticks`, `collector_state/`, `broker_specs/`, `twelve_data_credit_ledger.sqlite3`,
  the quality reports. Keep the existing Parquet as a read-only archive.
- **Working code paths:** collectors (CLI and backend worker), adapters, ingestion and quality
  CLIs all keep working. "Defer" means no new work, not removal.
- **Safety properties:** read-only MT5; execution refused; secret handling; Host/Origin checks;
  instance lock.

**On the historical timestamp migration:** under this direction it becomes **optional and
low-value**. Analysis and backtests should fetch bars from MT5 on demand, already converted
correctly at the adapter, so nothing would read the 793 archived bars. Recommendation: do not
migrate; label that archive as server-time and leave it untouched. Build the migration utility
only if you later decide to use those specific files.

**Risks of simplifying:**
1. *Fail-closed guard.* Until the server clock is configured, the new guard stops live MT5
   collection (intended, but it is a behaviour change you need to know about).
2. *Mixing time bases.* If collectors are re-enabled into the old store after the clock is
   configured, the new guard refuses. Point them at a new directory instead.
3. *Chart source switch.* Moving the chart from Parquet to live MT5 means a closed terminal shows
   "unavailable" where it used to show stale stored bars. Keep Parquet as a labelled "cached"
   fallback.
4. *Hidden status rows.* Removing the stub rows from the Overview must not hide real failures.
   Keep real service health visible.
5. *Lost broker history.* Broker history depth is finite (a year of H1 here). If long backtests
   are needed later, the deferred ingestion and collection tools are the way back. That is why
   they're deferred rather than deleted.

---

## H. Final recommendation

- **Should the direction change? Yes.** Stop treating data collection and infrastructure hardening
  as progress. The foundation is sufficient. Johanna's value is the decision pipeline, and none
  of it exists yet.
- **What should stop or be deferred now:**
  - further collector, lock, storage or quality work;
  - Twelve Data, FRED, tick and ingestion work;
  - historical migration;
  - more domain stubs;
  - further infrastructure acceptance rounds.
- **Priority:** live MT5 workspace → indicators → strategies + backtests → risk and sizing →
  decision pipeline with journal and event filter → Gemini and news → forward evaluation →
  execution.
- **Next implementation task (needs your approval): Phase 1, live MT5 market workspace.** Scope:
  1. Finish the in-progress `ServerClock` correction: update the one failing test; add
     conversion tests for UTC, NY+7 and both 2026 US DST transitions, independent of the machine
     timezone. Set your server clock in config only after you confirm it (evidence: New York + 7 h).
  2. Add a live MT5 market provider in the backend: on-demand quote and bars per pair and
     timeframe, a short in-memory cache, and live/cached/stale/unavailable labels. Parquet becomes
     an optional "cached" fallback.
  3. Dashboard: live bid/ask, spread and quote age in the watchlist; candlestick chart from live
     bars; remove the six placeholder rows from the Overview's health panel.
  4. Acceptance: works with all collectors disabled; live-verified against the terminal; no data
     under `data/` modified; no migration.

  Out of scope for that task: indicators, strategies, risk, AI, any collector changes, any change
  to stored data.

No code changes will be made until you approve.
