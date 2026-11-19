# B7.1 — Frontend Coverage, UX and Workflow Audit

Inspection only. Based on `src/johanna/web/{index.html,app.js,styles.css}`, `src/johanna/api.py`,
`src/johanna/application.py`, the service modules and the browser tests in `tests/`. No source,
data, configuration or process was changed.

## 1. Frontend architecture

| Item | Finding |
|---|---|
| Files | `index.html` (50 lines, static shell), `app.js` (3,725 lines, single vanilla-JS file, no framework or build step), `styles.css` (321 lines). Served by `BackendAPI` at `/`, `/assets/app.js`, `/assets/styles.css` (`api.py`) |
| Shell | `index.html`: sidebar (`#primary-nav`, brand, backend state), top bar (`#top-status`, mode label, settings shortcut), `#main-content`, footer clock, mobile menu + scrim |
| Routing | hash routes from `NAV_ITEMS` (`app.js:1`); `showPage()` (`app.js:3695`), `render()` → `overviewPage()`, `settingsPage()` or `dataPage(id)` (`app.js:3196`) |
| Rendering | string templates assigned to `innerHTML`; values escaped with `esc()`; partial re-render helpers (`setInner`) per page |
| Data loading | `loadDashboard()` (`GET /api/dashboard`, 30 s) plus one poller per stream (`startLivePolling()`, `app.js:3558`): quotes 3 s, bars 5 s, analysis 30/60 s, account 10/30 s, risk 30 s, decisions 30 s. Pollers never overlap, back off on failure, pause when the tab is hidden, and only run while their page is open (`POLL`, `app.js:19`) |
| Freshness model | responses expire (`QUOTES_EXPIRE_MS`, `BARS_EXPIRE_MS`, `ANALYSIS_EXPIRE_MS`, `ACCOUNT_EXPIRE_MS`, `DECISIONS_EXPIRE_MS`); expired data is downgraded to CACHED / "not current" rather than shown as live |
| Theme | CSS custom properties on `:root` (dark default) with a light variant; system / dark / light preference (`applyTheme`) |
| Tests | 146 browser tests over 11 files (`tests/test_browser_*.py`, headless Chromium harness `tests/browser_harness.py`), incl. one mobile-viewport scenario (`test_browser_smoke.py`, 400 × 900) |

## 2. Existing screens and navigation

Navigation (`NAV_ITEMS`, 13 entries, inline SVG icons, no emojis):

| Nav label (route) | Renderer | What it actually is | Status |
|---|---|---|---|
| Overview (`overview`) | `overviewPage()` `app.js:2094` | quote strip, chart, watchlist, market information, 4 calendar events, compact analysis panel | implemented — **market-only**; no account, risk, journal or system summary |
| Charts (`charts`) | `dataPage` | full candlestick chart (zoom/pan/price-scale, Go Live), analysis overlay with annotation toggles (V1 overlay `app.js:889`), full analysis panel | implemented |
| Markets (`markets`) | `dataPage` | watchlist table (bid/ask/spread) and connection/time-base info | implemented |
| Account & Broker (`account`) | `accountPage()` `app.js:2235` | account cards with validation chips, provenance and freshness, permissions/margin rules, consistency checks, instrument specification table with live quote | implemented, read-only |
| Economic Calendar (`calendar`) | `dataPage` | provider snapshot status and events | implemented (provider-dependent) |
| News (`news`) | `dataPage` → `newsMarkup()` | headline list from provider cache; backend news service is `UnimplementedNewsService` (`application.py`) | **shell only** in practice |
| Decisions (`signals`) | `decisionsPage()` `app.js:2499` | decision journal table (analysis outcome, direction, risk, eligibility, execution as separate chips), coverage disclosure, journal status line, "link to trade" | implemented, list only |
| Trading Rules (`rules`) | `placeholderPage()` `app.js:3234` | "Rule evaluation has not been implemented" | placeholder |
| Risk Management (`risk`) | `riskPage()` `app.js:2412`, `riskContent()` `app.js:2314` | proposal for the selected pair/timeframe: entry/SL/TP, gross and net reward:risk, cost components and their status, sizing (theoretical → rounded volume, loss/reward incl. costs), daily risk, validations, rejection reasons, net-unverified banner | implemented, read-only |
| Positions (`positions`) | `placeholderPage()` | "Broker position synchronization is not connected" | placeholder (backend P6.3 monitoring exists, inactive) |
| Trade History (`history`) | `tradesPage()` `app.js:2684` | manual trade logbook: list, detail, fills (entry/exit, void with reason), protections, notes, decision links (create/void), trade void; per-trade P&L and costs (`tradeAccountingMarkup`) | implemented, full CRUD-by-append |
| Analytics (`analytics`) | `shadowPage()` `app.js:3107` | shadow scheduler status and **enable/disable button**, diagnostics, shadow intents (filter + detail), funnel, **operations readiness**, recent evaluation cycles | implemented; mixes research and operations |
| Settings (`settings`) | `settingsPage()` `app.js:3244` | theme, timezone, timeframe, operating mode, **autonomous-execution preference toggle**, background collector toggle, application services health (`healthMarkup()` `app.js:2081`) | implemented |

Top bar (`renderTopbar()`, `app.js:281`): application Online/Degraded/Offline, market-data
status badge with server name, Execution (`Disabled` / `Preference on · inactive`), last quote
time; mode label (MANUAL). Sidebar footer mirrors application state.

## 3. API integrations (actual calls in `app.js`)

| Endpoint | Used by |
|---|---|
| `GET /api/dashboard` | `loadDashboard()` (status, preferences, market/calendar/news, services, cached data) |
| `POST /api/preferences` | `savePreferences()` (Settings, watchlist, pair/timeframe) |
| `GET /api/market/quotes`, `GET /api/market/bars` | quote/bars pollers |
| `GET /api/market/analysis` | analysis poller (Overview compact, Charts full, overlay) |
| `GET /api/account/summary`, `GET /api/account/instruments` | Account & Broker poller |
| `GET /api/risk/proposal` | Risk poller |
| `GET /api/journal/decisions?limit=50` | Decisions poller |
| `GET/POST /api/trades`, `GET /api/trades/{id}`, `POST …/fills`, `…/fills/{id}/void`, `…/protection`, `…/notes`, `…/links`, `…/links/{id}/void`, `…/void` | Trade History (`tradeRequest()`, `app.js:2787`; idempotent request ids) |
| `GET /api/shadow/status`, `/diagnostics`, `/evaluations?limit=20`, `/intents`, `/intents/{id}`, `/funnel`; `POST /api/shadow/scheduler` | Analytics |
| `GET /api/operations/readiness` | Analytics (`readinessMarkup()`, `app.js:3084`) |

**Backend routes with no frontend caller:** `GET /status`, `GET /health` (covered indirectly by
the dashboard), `GET /api/market/snapshot`, `GET /api/account/instruments/{symbol}`,
`GET /api/journal/decisions/{id}` (decision detail), `GET /api/shadow/evaluations/{id}` (cycle
detail), `GET /api/costs/readiness`, `GET /api/monitoring/status`,
`GET /api/monitoring/reconciliation`, `GET /api/notifications`,
`GET /api/reconciliation/links` (and its inactive `POST`).

**Backend capabilities with no API at all:** backup inventory/status (`BackupManager`,
`backups.py`), evidence registry (`EvidenceRegistry`, `evidence.py`), cost-policy registry
history/decisions (`CostPolicyRegistry`, `cost_policy.py`; only its summary appears inside
`/api/costs/readiness`), audit-log browsing, notification lifecycle actions
(present/acknowledge).

## 4. Functional coverage matrix

Legend: Complete · Partial · Display-only · Missing · Unavailable/inactive · Not applicable.

### A. Overview dashboard

| Feature | Coverage | Evidence / gap |
|---|---|---|
| Account and portfolio overview | **Missing** on Overview | exists only on Account & Broker |
| Balance and equity | **Partial** | Account page cards with validation chips; not on Overview |
| Daily performance and risk usage | **Missing** on Overview | daily risk shown inside Risk page only (`riskContent`, "Realised losses today") |
| System health and readiness | **Partial** | top-bar application health; readiness only inside Analytics |
| Current operational status | **Partial** | Execution label in top bar; scheduler state only in Analytics; monitoring/notifications/backups nowhere |

### B. Market analysis

| Feature | Coverage | Evidence / gap |
|---|---|---|
| Instruments EURUSDc / GBPUSDc / USDJPYc | **Complete** | watchlist and pair select (watchlist is user-editable up to 200, analysis limited to the three) |
| Price charts and timeframes | **Complete** | canvas chart, M1–D1, zoom/pan, forming candle, Go Live |
| Trend and market structure | **Complete** | analysis panel + overlay (pivots, pullback, zones) |
| Technical indicators | **Complete** | EMA20/50/200 overlay tags, checks |
| Support and resistance | **Partial** | zones in overlay; no separate level list |
| Market conditions | **Complete** | `conditionRows()` `app.js:1875` |
| Detected opportunities | **Complete** (current) | opportunity checklist + vetoes in analysis panel |
| Candidate history | **Partial** | Decisions table (list) and shadow intents; no filter by outcome, no detail |

### C. Decisions and risk

| Feature | Coverage | Evidence / gap |
|---|---|---|
| Candidate decisions | **Partial** | list with separate dimensions; no detail view (`/api/journal/decisions/{id}` unused), no filters (API supports outcome/eligibility/execution filters) |
| Gate outcomes / rejection reasons | **Partial** | current proposal's rejection reasons on Risk; shadow gate outcomes in Analytics; historical per-decision reasons not reachable |
| ML assessment | **Not applicable** | no ML in the backend |
| Context checks | **Not applicable** | not a backend concept (analysis checks/vetoes exist and are shown) |
| Risk assessments | **Complete** (current) / **Missing** (history) | Risk page shows the live proposal; stored risk assessments only as chips in Decisions |
| SL/TP reasoning | **Complete** | Risk page rows and validations |
| Reward-to-risk | **Complete** | gross and net, with assumptions and "not established" states |
| Trading costs and verification | **Partial** | per-proposal cost status on Risk; **cost readiness (`/api/costs/readiness`) not shown anywhere** |
| Margin and volume calculations | **Complete** | broker volume limits, theoretical → rounded volume, below-minimum |
| Candidate vs proposal vs actionable trade | **Complete in wording** | decisions note, risk status chip, net-unverified banner; but no single visual "lifecycle" |

### D. Trade journal

| Feature | Coverage | Evidence / gap |
|---|---|---|
| Manual trade creation | **Complete** | create form, idempotent request ids, outcome-unknown message |
| Fills | **Complete** | entry/exit, void with reason |
| SL/TP protections | **Complete** | append-only protections with effective time |
| Notes and corrections | **Complete** | notes; corrections via void + new fill (`corrects_fill_id` is backend-side) |
| Realised / unrealised performance | **Complete per trade** | calculated vs user-entered realised; unrealised labelled ESTIMATE |
| Costs and net P&L | **Complete per trade** | unknown costs never treated as zero |
| Unknown / incomplete information | **Complete** | "unknown", "not entered", "Not calculated · reason" |
| Portfolio / aggregate performance | **Missing** | no totals, no expected-vs-actual, no period summary |

### E. Reconciliation

| Feature | Coverage |
|---|---|
| Broker observations, candidate matches, ambiguous matches, discrepancies | **Unavailable/inactive** in backend operation and **Missing** in the UI (APIs exist: `/api/monitoring/status`, `/api/monitoring/reconciliation`) |
| User-confirmed links | **Unavailable/inactive** (confirmation endpoint answers 403; `GET /api/reconciliation/links` unused) |
| Positions page | **Display-only placeholder** with outdated copy ("not connected") |

### F. Notifications

| Feature | Coverage |
|---|---|
| History, delivery status, failed/pending, claim/retry | **Missing** in UI; **inactive** in backend (`GET /api/notifications` exists and returns `delivery` per item since P6.2b.4) |

### G. Evidence and costs

| Feature | Coverage |
|---|---|
| Evidence registry and verification | **Missing** (no API) |
| Cost policy records, approval/expiry | **Missing** (registry summary only inside `/api/costs/readiness`, unused) |
| Missing commission / slippage / swap / holding assumptions | **Partial** — visible per proposal on Risk; the per-instrument readiness report with blocking reasons is not shown |

### H. Backups and recovery

| Feature | Coverage |
|---|---|
| Backup history, verification, recovery points, rehearsal, retention | **Missing** in UI and API; readiness shows only `valid_backup_pairs` count (not rendered) |

### I. System control centre

| Feature | Coverage | Evidence / gap |
|---|---|---|
| Journal health and schema version | **Partial** | readiness panel (Analytics); Decisions journal line |
| Scheduler state | **Complete** but misplaced | Analytics; one-click enable |
| Monitoring / reconciliation / notification / backup state | **Missing** | readiness `services` block not rendered; no API for backups |
| Readiness assessment | **Partial** | rendered inside Analytics, not a primary destination |
| Audit information | **Missing** | audit chain validity only as a readiness check |
| Configuration visibility | **Partial** | Settings: preferences, collector, services health |

## 5. Missing and partially implemented workflows

1. **No system home**: health, readiness, service states and restrictions are scattered (top
   bar, Analytics, Settings, Decisions footnote) or absent.
2. **Decision drill-down**: no way to open one decision with its analysis, risk assessment, gate
   outcomes and linked trades, although the API exists.
3. **Cost readiness**: the most important blocker for actionable proposals (empty CostTerms) has
   a diagnostic API but no screen.
4. **Journal performance**: per-trade accounting is strong; there is no aggregate view.
5. **Reconciliation / notifications / evidence / backups**: backend infrastructure without UI;
   all inactive, so the UI needs read-only status views before any workflow.
6. **Positions**: placeholder copy no longer matches the backend (monitoring exists but is
   inactive).

## 6. UI/UX weaknesses

- **Information architecture**: Overview is a market screen; "Analytics" mixes research (shadow
  intents, funnel) with operations (scheduler control, readiness); route id `signals` is labelled
  "Decisions"; two placeholders (Rules, Positions) and a near-empty News page occupy navigation.
- **Hierarchy**: long single-column stacks of panels on Risk and Analytics; the most decisive
  facts (status, blocking reason) compete with detail rows.
- **Vocabulary consistency**: readiness checks use shadow-cycle chips (`COMPLETED` / `FAILED`) for
  `ok` / `attention` (`readinessMarkup()`), and "FAILED" overstates an attention state.
- **Stale backend label**: the Decisions journal line renders
  `monitoring and reconciliation not_implemented` from `JournalStore.status()`
  (`journal_store.py:1103`); since P6.3/P6.2b.4 they are implemented but inactive.
- **Monolithic `app.js`** (3,725 lines): every new screen increases coupling; there is no
  component or module boundary.
- **Responsive**: breakpoints at 1180/900/760/650 px and table scrollers exist; only one mobile
  scenario is tested; dense tables (instruments, risk rows, decisions) rely on horizontal scroll.
- **Strengths to keep**: explicit freshness and CACHED states, validation chips instead of zeros,
  separate decision dimensions, "not an order" banners, idempotent trade writes, careful P&L
  labelling, non-overlapping pollers, dark/light themes.

## 7. Safety and operational presentation gaps

| Requirement | Current presentation | Gap |
|---|---|---|
| Scheduler OFF | Analytics shows state; **one-click Enable** (`shadowSchedulerMarkup()` `app.js:2899`), no confirmation | activation without confirmation; not visible globally |
| Monitoring inactive | not shown | needs an explicit INACTIVE status |
| Reconciliation inactive | not shown; Positions says "not connected" | wording and status |
| Notifications inactive | not shown | status missing |
| Automatic backups disabled | not shown | no API |
| CostTerms empty / unverified | per-proposal "costs unverified" banner on Risk | no global indicator; cost readiness unused |
| Proposals without actionable volume | Risk sizing row "no actionable volume" | good; should also be summarised where proposals are listed |
| Autonomous execution disabled | top bar `Execution Disabled`; **Settings has a working toggle** that saves `autonomous_execution_enabled` (inactive; top bar then shows "Preference on · inactive") | a live switch for a non-existent capability invites misreading |
| Unattended operation not permitted | readiness panel (Analytics) with blockers | not prominent |
| Candidate ≠ approved trade | consistently worded | keep |
| Estimated ≠ realised P&L | consistently labelled | keep |
| Unknown costs ≠ zero | consistently "unknown" / "not established" | keep |

## 8. Source references

`src/johanna/web/index.html`; `src/johanna/web/app.js`: `NAV_ITEMS` 1, `POLL` 19, `statusBadge`
175, `renderTopbar` 281, `emptyState` 310, `quoteStripMarkup` 315, `watchlistMarkup` 353,
`marketInfoMarkup` 369, `chartMarkup` 414, analysis overlay 889, `conditionRows` 1875,
`calendarMarkup` 2055, `newsMarkup` 2070, `healthMarkup` 2081, `overviewPage` 2094,
`accountPage` 2235, `riskStatusChip` 2306, `riskContent` 2314, `riskPage` 2412, `decisionsPage`
2499, `TRADE_BANNER` 2529, `tradeDetailMarkup` 2635, `tradesContent` 2674, `tradesPage` 2684,
`tradeRequest` 2787, `decisionLinkMarkup` 2833, `SHADOW_BANNER` 2889, `shadowSchedulerMarkup`
2899, `shadowIntentsMarkup` 3037, `shadowFunnelMarkup` 3056, `readinessMarkup` 3084,
`shadowPage` 3107, `dataPage` 3196, `placeholderPage` 3234, `settingsPage` 3244,
`startLivePolling` 3558, `showPage` 3695; `src/johanna/web/styles.css` (tokens lines 1–34,
breakpoints 101–311); `src/johanna/api.py` (`_ROUTES`, `_TRADE_ROUTES`);
`src/johanna/application.py` (`status`, `dashboard_data`, `cost_readiness`);
`src/johanna/journal_store.py:1103`.
