# Johanna — Current Status and Development Roadmap

**Authoritative central status document.**
**Last updated:** 2026-11-19 (status as of the P6.2b.8 implementation, full validation in progress)
**Maintainer rule:** see §25, *Document maintenance*.

> **Status vocabulary used throughout.** Every item is labelled with exactly one of the following. A higher status never implies a lower one was skipped, and a lower status never implies a higher one.
>
> | Label | Meaning |
> |---|---|
> | **PROPOSED** | Future work; not approved, not built |
> | **APPROVED — NOT IMPLEMENTED** | The user has approved the work; no code exists yet |
> | **IMPLEMENTED (source)** | The code exists in the working tree |
> | **TESTED (offline)** | Verified by automated tests on synthetic data |
> | **REGISTERED (source)** | A schema migration is registered in code but not applied to production |
> | **MIGRATED (production)** | Applied to the production journal, with evidence |
> | **DEPLOYED** | The running backend serves this code (after a user-controlled restart) |
> | **OPERATIONAL / ACTIVE** | Running in production |
> | **INACTIVE / DISABLED** | Present but deliberately not running |

---

## Contents

1. [Project overview and objectives](#1-project-overview-and-objectives)
2. [Current production state at a glance](#2-current-production-state-at-a-glance)
3. [System architecture](#3-system-architecture)
4. [T0–T6 analysis pipeline](#4-t0t6-analysis-pipeline)
5. [R1–R7 decision gates](#5-r1r7-decision-gates)
6. [Trend-continuation checklist and V1/V2 vetoes](#6-trend-continuation-checklist-tc1tc6-and-v1v2-vetoes)
7. [Account and broker intelligence (Phase 3)](#7-account-and-broker-intelligence-phase-3)
8. [Phase 4 risk construction and risk policy](#8-phase-4-risk-construction-and-risk-policy)
9. [P5 journal and audit infrastructure](#9-p5-journal-and-audit-infrastructure)
10. [P6 shadow evaluation infrastructure](#10-p6-shadow-evaluation-infrastructure)
11. [Cost-policy infrastructure](#11-cost-policy-infrastructure)
12. [Monitoring, reconciliation and notifications](#12-monitoring-reconciliation-and-notifications)
13. [Journal schema history and the v6 production migration](#13-journal-schema-history-and-the-v6-production-migration)
14. [Production opportunity investigation findings](#14-production-opportunity-investigation-findings)
15. [P6.2b.7 consistency investigation and P6.2b.8 shared structural-target contract](#15-p62b7-consistency-investigation-and-p62b8-shared-structural-target-contract)
16. [Frontend and control centre (B7)](#16-frontend-and-control-centre-b7)
17. [Future development roadmap (overview)](#17-future-development-roadmap-overview)
18. [The four trading strategies](#18-the-four-trading-strategies)
19. [Historical backtesting and validation](#19-historical-backtesting-and-validation)
20. [ML meta-model improvements](#20-ml-meta-model-improvements)
21. [Economic news and contextual analysis](#21-economic-news-and-contextual-analysis)
22. [Multi-pair and multi-timeframe scanning](#22-multi-pair-and-multi-timeframe-scanning)
23. [Development and operational principles](#23-development-and-operational-principles)
24. [Outstanding issues and known limitations](#24-outstanding-issues-and-known-limitations)
25. [Document maintenance](#25-document-maintenance)
26. [Long-term vision](#26-long-term-vision)
27. [Phase history and sources](#27-phase-history-and-sources)

---

## 1. Project overview and objectives

Johanna is a personal forex decision-support system for an Exness MetaTrader 5 cent account. It covers three instruments, **EURUSDc, GBPUSDc and USDJPYc** (requested as EURUSD, GBPUSD, USDJPY), on three timeframes, **H1, H4 and D1**.

**Purpose:** to watch the market, analyse it with transparent and deterministic rules, and say clearly either "no trade is justified, because …" or "this is a qualified candidate". A qualified candidate is then turned into fully risk-checked, *proposed* trade parameters for a human to review and, if they choose, execute **manually**.

**Objectives:**
1. Live market monitoring from MT5 (read-only).
2. Transparent technical analysis: indicators, market structure and market conditions.
3. Systematic strategy detection with explicit pass, fail and veto reasons.
4. Account-aware risk management and position sizing against real broker constraints.
5. A tamper-evident decision journal and audit trail.
6. Forward (shadow) evaluation of decision quality without trading.
7. Over time: multiple strategies, historical validation, ML-assisted ranking, contextual news analysis and multi-market scanning (all **PROPOSED**, §17–§22).

**What Johanna is not:**
- an automated trading robot;
- a source of guaranteed profit.

Johanna never places, modifies or closes orders (§2).

---

## 2. Current production state at a glance

| Area | State | Label |
|---|---|---|
| Backend | Runs locally (`127.0.0.1:8765`); started and stopped **only by the user**. Last known user restart: 2026-11-19 10:34:48 UTC, for the v6 migration | OPERATIONAL |
| MT5 connection | **Read-only** (quotes, bars, account, symbols, positions, history). No order functions are used | OPERATIONAL (read-only) |
| Production journal schema | **v6 installed** (2026-11-19, P6.2b.6b) | MIGRATED (production) |
| Shadow scheduler | **OFF** since 2026-11-18 07:11 UTC (user) | INACTIVE |
| Monitoring and reconciliation | Implemented, **not active** | INACTIVE |
| Notifications | Local inbox infrastructure only; **not active**; no external delivery | INACTIVE |
| Automatic backups | **Disabled**: no backup schedule exists. Pre-migration backups are made automatically during migrations only | DISABLED |
| Production `CostTerms` | **Empty**: no verified commission, slippage or holding assumption. Every proposal's net reward:risk is therefore UNVERIFIED and no proposal can be approved | EMPTY (by design until evidence is approved) |
| Autonomous execution | **Disabled by design**; manual execution only | DISABLED |
| Unattended operation | **Not permitted** (readiness reports blocking reasons) | NOT PERMITTED |
| P6.2b.8 shared structural-target contract | **Approved. Implemented in source, uncommitted; full-suite validation in progress; NOT deployed** (§15) | IMPLEMENTED (source); validation pending |
| B7.2 frontend shell | Implemented in source (uncommitted); **not deployed** until the user restarts the backend (§16) | IMPLEMENTED (source) |

**Source control:**
- The working tree is on branch `phase-2.1-live-market-analysis`.
- The latest commit is `dea1208` ("V1 overlay, Phase 3 account intelligence, Phase 4 risk, P5.0 design, P5.1 journal").
- Everything from P6 onwards is **uncommitted** (per the session-start git status). Committing is done only when the user asks.

---

## 3. System architecture

**Layers and modules** (`src/johanna/`):

| Layer | Modules | Responsibility |
|---|---|---|
| Broker adapter | `adapters/mt5.py` | Read-only MT5 access, server clock verification |
| Live market | `live_market.py` | Quotes, bars and snapshots on demand; LIVE / CACHED / STALE / UNAVAILABLE labels |
| Analysis (pure) | `analysis/` (`snapshot`, `closure`, `continuity`, `sessions`, `window`, `indicators`, `structure`, `volatility`, `engine`, `features`, `conditions`, `setups`, `decision`, `recency`, `targets`) | Deterministic T1–T6 pipeline. No clock, no I/O, no broker imports (test-enforced) |
| Market analysis service | `market_analysis.py` | Runs the pipeline on a fresh snapshot and returns the analysis envelope |
| Account intelligence | `account_intelligence.py` | Account, instruments, exposure, money conversion |
| Risk | `risk_engine.py` (pure), `risk_service.py` | Phase 4 construction, validation, sizing and the cost gate |
| Costs | `cost_evidence.py`, `cost_policy.py` | Structured cost evidence and broker-terms fingerprints |
| Journal | `journal_store.py`, `decision_journal.py`, `trade_logbook.py`, `trade_links.py`, `evidence.py`, `backups.py` | Encrypted SQLite journal (HMAC audit chain), decisions, risk assessments, trade logbook, backups |
| Shadow | `shadow_evaluation.py`, `shadow_intents.py` | Scheduled closed-candle evaluation and shadow trade intents (research only) |
| Operations | `operations.py`, `broker_monitoring.py`, `reconciliation.py`, `notifications.py` | Readiness, monitoring, reconciliation and the notification inbox (inactive) |
| Application and API | `application.py`, `api.py` | Lifecycle, read-only JSON API, static assets |
| Frontend | `web/index.html`, `web/app.js`, `web/modules/*.js`, `web/styles.css` | Native ES-module dashboard (B7.2 architecture) |

**Principles built into the architecture:**
- **Purity:** analysis and risk computation are pure and deterministic. All I/O sits at the edges.
- **Read-only:** MT5 is never written to. No order code path exists.
- **Fail-closed:** any missing, stale or unverifiable input produces `NO_TRADE`, `UNVERIFIED` or `REJECTED`, never an approval.

---

## 4. T0–T6 analysis pipeline

| Stage | What it does | Label |
|---|---|---|
| **T0** | Evidence study of Exness session and closure behaviour and historical gaps (`docs/evidence/t0-exness-sessions/`). It found no unexplained gaps in the *standard*-symbol H1 history studied. Cent symbols were not studied | Evidence only |
| **T1** | Bar data quality and closure labels: `tick_observed`, `clock_only`, `forming` | IMPLEMENTED, TESTED |
| **T2** | Coordinated market snapshot (661 required candles + 1), with a freshness requirement (≤ 5 s) | IMPLEMENTED, TESTED |
| **T3** | Session schedule and continuity: expected candles, closures and gap classification | IMPLEMENTED, TESTED |
| **T4** | Window validation (`validate_window`): accepted or rejected, with reason codes | IMPLEMENTED, TESTED |
| **T5** | Indicators and structure (`analyse_window`): EMA20/50/200, ATR14, RSI14, ADX14, swings, support/resistance levels, volatility regime | IMPLEMENTED, TESTED |
| **T6** | Market conditions (regime, bias, location, swing context, volatility) plus the trend-continuation setup and the R1–R7 decision table | IMPLEMENTED, TESTED, DEPLOYED (decision version 1). Version 2 (P6.2b.8) is in source only |

**Notes:**
- T6 parameters (`DecisionParameters`) are **experimental defaults, not tuned or validated** (§19).
- The live path evaluates the window **without peer symbols**. An unexplained missing candle is therefore classified `gap_unknown`, which rejects the whole window (T4) and gives R1 (§24, F4).
- The T6 design specification is `docs/t6-market-conditions-design.md` (revision 2.6).

---

## 5. R1–R7 decision gates

| Rule | Outcome | Meaning |
|---|---|---|
| **R1** | `NO_TRADE` | Provenance, data or window failure (rejected T4 window, invalid input, stale snapshot) |
| **R2** | `NO_TRADE` | Provisional or undetermined data (for example a `clock_only` last candle) |
| **R3** | `NO_TRADE` | Market state cannot be determined |
| **R4** | `NO_TRADE` | Conflicting directional evidence, or V2 swing-context conflict |
| **R5** | `NO_TRADE` | A required setup input is unavailable (including untrusted support/resistance) |
| **R6** | `MARKET_CONDITION` | Market understood; setup not qualified (lists failed checks and vetoes) |
| **R7** | `TRADE_CANDIDATE` | Trend continuation qualified: 6 of 6 checks passed, no veto |

**Key facts:**
- Every outcome is **non-actionable** (`actionable: false`). A candidate is not a trade.
- An R7 must still pass Phase 4 (§8) before parameters are proposed.

---

## 6. Trend-continuation checklist (TC1–TC6) and V1/V2 vetoes

**Checks:**
- **TC1 regime:** ADX ≥ 25 and |slope50| > 0.25 ATR.
- **TC2 bias:** all four bias votes agree.
- **TC3 long-term:** close and EMA50 on the trend side of EMA200 (EMA200 mandatory).
- **TC4 pullback:** within 0.25 ATR of EMA20 in the last 5 candles, all `tick_observed`.
- **TC5 resumption:** close beyond EMA20.
- **TC6 clearance:** the nearest qualifying (strength ≥ 2) opposing level is at least 1.0 ATR from the close.
  - **Deployed behaviour (decision v1):** TC6 *passes* when no qualifying opposing level exists.
  - **P6.2b.8 (source only, decision v2):** TC6 **fails** when no qualifying opposing level exists, and validates the level with the shared structural-target contract (§15).

**Vetoes:**
- **V1 EMA50 structure break:** a pullback candle closes beyond its own EMA50.
- **V2 swing-context conflict:** a complete opposite swing sequence.

**Cautions** (reported, never vetoing): high volatility, overbought or oversold momentum, extended trend strength.

**Strategy status:**
- Trend continuation is the **only implemented strategy**.
- Its parameters are experimental and have not been historically validated.
- It **requires improvement** (§18.1).

---

## 7. Account and broker intelligence (Phase 3)

**IMPLEMENTED, TESTED, DEPLOYED (read-only):**
- Account snapshot: balance, equity, margin, free margin, leverage, currency (USC), with freshness and unit-consistency checks.
- Instrument specifications: contract size, tick size and value, volume min/step/max, digits, swap rates and modes, rollover.
- Exposure: open positions and pending orders, today's deals, realised losses.
- Money conversion to USC, including non-USD profit currencies (USDJPYc).
- Broker server clock verified against live ticks; the trading day is the broker server's calendar day.
- Broker-terms fingerprints, used to detect changes in instrument terms (§11).

---

## 8. Phase 4 risk construction and risk policy

### 8.1 Current risk policy (unchanged; must not be weakened)

| Rule | Value |
|---|---|
| Per-trade risk | **0.5 % of current equity** |
| Daily risk limit | **2 %** of **min(balance, equity)**; floating profit never raises it |
| Minimum reward:risk | **1:2 net**, after commission, swap and slippage. Gross ≥ 1:2 is a prerequisite |
| Equity freshness | Fresh account data is required; stale data gives UNVERIFIED |
| Execution | **Manual only**. Proposals are "proposed trade parameters (not an order)" |
| Unknown costs | **Never treated as zero**. Any unestablished cost makes net reward:risk UNVERIFIED and blocks approval |

Strategy expansion (§18) must **not** weaken any of these safeguards.

### 8.2 Construction methodology

**IMPLEMENTED, TESTED, DEPLOYED (engine version 3). Version 4 is in source only (P6.2b.8).**
- **Entry:** market; BUY at the ask, SELL at the bid.
- **Stop-loss:** beyond the last confirmed swing (`L_last` / `H_last`) by 0.25 × ATR14, rounded away from the entry to the tick.
- **Take-profit:**
  - It is the near edge of TC6's selected structural level, rounded towards the entry.
  - **No level means rejection.**
  - From engine v4 (P6.2b.8, source only), Phase 4 also revalidates that level independently through the shared contract.
- **Checks, in order:**
  - stop direction and target direction;
  - gross reward:risk ≥ 2;
  - costs gate (net ≥ 2 with every cost established);
  - money per lot, computed two ways, plus the MT5 calculator;
  - volume rounded **down** to the step;
  - margin;
  - daily risk;
  - a final budget recheck.
- **Statuses:**
  - `APPROVED_FOR_MANUAL_REVIEW`: every check passed. This is not an instruction to trade.
  - `REJECTED`: a verified rule breach.
  - `UNVERIFIED`: a required input can't be established.
- **Phase 4 remains the final authority for trade construction.** T6 never guarantees a 1:2 net outcome.

---

## 9. P5 journal and audit infrastructure

**IMPLEMENTED, TESTED, DEPLOYED.**
- **Storage:** an encrypted SQLite decision journal (`trading_journal.sqlite3` plus its key file), with an HMAC audit chain, integrity checks and fail-closed startup states. These include `corrupt_database`, `orphaned_key` and `restore_required` (see `docs/operations-runbook.md`).
- **Decisions:**
  - Analyses are recorded as decisions.
  - Identical analyses are deduplicated by fingerprint into **observations**.
  - Risk assessments are linked to their decision.
  - Dimensions are kept separate: analysis outcome, risk status, trade eligibility and execution status.
- **Trade logbook and links:** manual trade records linked to decisions (P5.1 and later).
- **Backups:**
  - A mandatory, verified **pre-migration backup pair** is created on every schema migration.
  - There are **no automatic periodic backups** (§2).
  - Restore is a documented manual procedure.
- **Persisted evidence gap:**
  - Decision evidence stores each check's `{id, result}` only.
  - TC6's `evidence_id` and selected level are **not** journaled (§24).

---

## 10. P6 shadow evaluation infrastructure

**IMPLEMENTED, TESTED, DEPLOYED; scheduler INACTIVE.**
- **What it does:**
  - It evaluates each closed candle (all pairs and timeframes), about 25 s after close on the broker clock.
  - It records evaluation cycles, gate outcomes, risk results and **shadow trade intents**. These are for research only, and are created only for verified approved proposals.
- **Deduplication:** windows are deduplicated by `symbol|timeframe|close|a{analysis}|d{decision}|r{risk}`.
- **Activation:** only by explicit user approval. The dashboard has **no** control for it (read-only since B7.2).
- **History:**
  - ON 17 Nov 22:07 → OFF 22:16 UTC.
  - ON 18 Nov 06:52 → OFF 07:11 UTC.
  - **OFF since then.**

---

## 11. Cost-policy infrastructure

**IMPLEMENTED, TESTED, DEPLOYED (P6.2b.1); production terms EMPTY.**
- **Structured cost evidence:** commission, slippage and holding assumptions per timeframe. Each component carries currency, unit, basis, source, VERIFIED/APPROVED status, approval time, a 30-day maximum age and a broker-terms fingerprint.
- **Swap:** live MT5 rates, an approved holding assumption, and broker rollover events. Credits are never counted in Johanna's favour.
- **Production `COST_TERMS` is empty.** All live proposals are therefore UNVERIFIED (net reward:risk unverified), and no proposal can be approved.
- **Populating cost terms** requires user-approved evidence. That is a separate, explicitly authorised step (§17).

---

## 12. Monitoring, reconciliation and notifications

**IMPLEMENTED, TESTED (P6.2b.4 operational reliability work); INACTIVE in production.**
- **Broker monitoring and reconciliation:** compare broker positions and deals with the journal and trade logbook, and report discrepancies.
- **Notifications:** a local inbox only. Nothing is created or delivered automatically, and there is no external delivery channel.
- **Readiness** (`operations.py`):
  - It reports checks and unattended-operation blocking reasons, for example `scheduler_disabled`, `costs_unverified` and `execution_disabled`.
  - Unattended operation is **not permitted**.

---

## 13. Journal schema history and the v6 production migration

| Schema | Status |
|---|---|
| v5 | REGISTERED (Phase 2), MIGRATED to production (P6.2b.3) |
| v6 | REGISTERED and offline-verified (P6.2b.5); preflight read-only (P6.2b.6a, P6.2b.6a.1); **MIGRATED to production (P6.2b.6b)** |

**P6.2b.6b (2026-11-19):**
- The user restarted the backend at 10:34:48 UTC.
- A pre-migration backup pair (v5) was created during startup and verified: `integrity_check` ok, 0 foreign-key violations, audit chain valid.
- The final schema is **6** (`PRAGMA user_version` and the backend journal status).
- Report: `docs/P626B_PRODUCTION_MIGRATION_REPORT.md`.

P6.2b.8 requires **no** schema change.

---

## 14. Production opportunity investigation findings

**Sources:** `docs/P6_OPPORTUNITY_DETECTION_INVESTIGATION.md` (source investigation) and `docs/P6_PRODUCTION_OPPORTUNITY_AGGREGATE_REPORT.md` (P6.1, read-only aggregate). The P6.1 verdict was **CANDIDATES GENERATED — RISK CONSTRUCTION BLOCKED**.

**Decisions:**
- **53 decisions over 41 distinct candles** (4,529 observations).
- Rule split: R6 62.3 %, R4 18.9 %, R1 11.3 %, R7 3.8 % (2), R2 3.8 %, R3/R5 0.
- Sources: `analysis_request` 46, `scheduled_evaluation` 5, `risk_request` 2.
- Timeframes: H1 42, H4 11, D1 0.

**The two R7 candidates:**
- Both were **EURUSD H1, bearish (SELL)**, from request-time analysis, with 87 observations.
- **Both were rejected by Phase 4 with `construction/target_structure`**, a structural-target issue: TC6 passed without a qualifying opposing level, and Phase 4 requires that level for the take-profit.
- No gross reward:risk was recorded, and no cost check was reached.

**Upstream bottlenecks:**
- TC1 and TC2 failed together in 30 of 45 evaluated setups.
- TC4 (no pullback) failed in 12 of 14 directional setups.

**Scheduled shadow evaluation:**
- **6 cycles** (H1 only, two short activations), all completed.
- **0 candidates, 0 intents.**
- One cycle was R1 with `closed_history_gap, gap_unknown`.

**Anomalies:**
- Three T4 gate rows from 17 Nov 22:00 show FAIL with no reason (old code).
- One R1 provenance failure: missing `broker_symbol`.

---

## 15. P6.2b.7 consistency investigation and P6.2b.8 shared structural-target contract

### 15.1 P6.2b.7 (offline investigation) — COMPLETE

`docs/P627_OFFLINE_CANDIDATE_RISK_CONSISTENCY_REPORT.md`. Verdict: **DESIGN INCONSISTENCY CONFIRMED — SAFE RESOLUTION IDENTIFIED**.
- TC6 passed in three no-target cases, while Phase 4 rejects every one of them.
- The recommended fix was Option C: a shared structural-target contract.

### 15.2 P6.2b.8 — APPROVED; IMPLEMENTED (source); validation in progress; NOT DEPLOYED

> **Accuracy note.** The task brief for this document described P6.2b.8 as "approved but implementation pending". At the time of writing, the implementation **already exists in the working tree** (uncommitted). It was carried out under the user's P6.2b.8 brief on 2026-11-19. Its validation is **not complete**: the focused and regression runs passed (712 tests on each of Python 3.11 and 3.13), and the three full-suite runs per Python version were still running when this document was written. It has **not** been reviewed or accepted by the user, **not** committed and **not** deployed. Report: `docs/P628_SHARED_STRUCTURAL_TARGET_IMPLEMENTATION_REPORT.md` (its verdict and full-run results are pending).

**What is implemented in source:**
- **Shared contract** `johanna.analysis.targets.validate_structural_target`:
  - pure, with no exceptions on malformed input;
  - stable codes: `structural_target_valid`, `no_structural_target`, `target_strength_below_minimum`, `target_wrong_side`, `invalid_target_evidence`, `invalid_direction`, `target_clearance_insufficient`.
- **TC6:** no qualifying opposing level now **fails**. `DECISION_VERSION` 2.
- **Phase 4:** independent revalidation. A malformed target is a structured REJECTED (it previously caused an unhandled `AttributeError` and HTTP 500). `RISK_ENGINE_VERSION` 4.
- **Unchanged:** all risk safeguards and thresholds. No reward:risk filtering was added to T6, and no alternative target methods were introduced.

**Still required before it is operational:**
- the full-suite results;
- user review and acceptance;
- a commit, when the user asks;
- a user-controlled restart (deployment).

---

## 16. Frontend and control centre (B7)

| Phase | Scope | Label |
|---|---|---|
| B7.1 | Frontend coverage, UX and workflow audit (`docs/B71_*.md`) | COMPLETE (documentation) |
| B7.2 | Application shell, grouped navigation, Overview, native ES-module architecture, read-only scheduler display, System Control Centre, Positions and Evidence pages | IMPLEMENTED (source), TESTED (focused runs); full runs folded into P6.2b.8 validation; **NOT DEPLOYED** (the running backend serves the old asset allowlist, so reloading the dashboard before a restart can break it) |
| B7.3 | Market Analysis, Decisions & Risk, cost readiness | PROPOSED (roadmap) |
| B7.4 | Trade Journal | PROPOSED |
| B7.5 | Positions & Reconciliation, Notifications (read-only) | PROPOSED |
| B7.6 | Backups & recovery, Evidence, System Control Centre completion (needs new read-only backend APIs) | PROPOSED |
| B7.7 | Integration, accessibility, responsive design, regression | PROPOSED |

**Rules for the control centre roadmap:**
- It stays **read-only for operational capabilities**.
- No UI control may activate the scheduler, execution or any unattended service without separate approval.

The `docs/B72_IMPLEMENTATION_REPORT.md` placeholders are still unfilled (§24).

---

## 17. Future development roadmap (overview)

All items below are **PROPOSED** unless stated otherwise. They remain standing objectives until completed or explicitly removed with the user's approval (§25).

| # | Workstream | Depends on | Label |
|---|---|---|---|
| 1 | Complete and deploy P6.2b.8 | full validation, user review, restart | IMPLEMENTED (source); pending |
| 2 | TC6 / T4 observability: journal TC6 `evidence_id` and level, T4 reason codes | — | PROPOSED |
| 3 | Approved cost evidence for production `CostTerms` (commission, slippage bound, holding assumptions) | user-approved evidence | PROPOSED |
| 4 | Peer-aware T4 continuity (reduce `gap_unknown` R1); cent-symbol history depth (D1) | investigation | PROPOSED |
| 5 | Trend-continuation improvements (§18.1) | backtesting (§19) | PROPOSED (standing objective) |
| 6 | Breakout and retest strategy (§18.2) | common risk engine, backtesting | PROPOSED (standing objective) |
| 7 | Range reversal strategy (§18.3) | same | PROPOSED (standing objective) |
| 8 | Liquidity sweep and reversal strategy (§18.4) | same | PROPOSED (standing objective) |
| 9 | Historical backtesting and validation harness (§19) | — | PROPOSED |
| 10 | ML meta-model (§20) | backtest and forward evidence | PROPOSED |
| 11 | Economic news and contextual analysis (§21) | news source decision | PROPOSED |
| 12 | Multi-pair and multi-timeframe scanning (§22) | strategies, scheduler approval | PROPOSED |
| 13 | Frontend B7.3–B7.7 (§16) | B7.2 deployment | PROPOSED |
| 14 | Forward evaluation: shadow outcome tracking against backtests | scheduler activation approval | PROPOSED |
| 15 | Monitoring, reconciliation and notification activation | explicit approval | INACTIVE — activation PROPOSED |
| 16 | Automatic backup schedule | design and approval | PROPOSED |
| 17 | Controlled execution, demo account first, with per-trade confirmation and independent hard limits | only after forward evaluation | PROPOSED (long-term; not authorised) |

---

## 18. The four trading strategies

These four strategies are **standing future development objectives**, not optional suggestions.

**Requirements for every strategy:**
- independent **detection conditions**;
- independent **rejection conditions** (fail and veto reasons);
- its own **structural risk construction** (structural stop and structural target);
- **historical validation** (§19);
- **performance evaluation** (backtest and forward).

**Rules shared by all strategies:**
- All strategies must use the **common Phase 4 risk engine**, with the unchanged risk policy (§8.1).
- No strategy may introduce weaker risk rules or manufactured targets.

### 18.1 Trend continuation — IMPLEMENTED (T6); requires improvements

- It is the existing T6 setup (§6), with experimental, unvalidated parameters.
- **Improvements needed (PROPOSED):**
  - historical validation of the TC1–TC6 thresholds;
  - TC4 pullback sensitivity (12 of 14 directional setups failed it);
  - TC1/TC2 co-failure analysis;
  - target availability under the P6.2b.8 rule;
  - D1 coverage.
- Any parameter change needs offline validation and explicit approval.

### 18.2 Breakout and retest — PROPOSED

- **Detection:** a confirmed break of a qualifying structural level, followed by a retest that holds.
- **Rejections:** false breaks, no retest, or opposing structure too close.
- **Risk construction:** stop beyond the retest structure; target at the next structural level.
- Not implemented.

### 18.3 Range reversal — PROPOSED

- **Detection:** a confirmed range (low trend strength, bounded by qualifying support and resistance) with a rejection at a boundary.
- **Rejections:** trending regime, or the boundary already broken.
- **Risk construction:** stop beyond the boundary; target at the opposite boundary or the mid-range structure.
- Not implemented.

### 18.4 Liquidity sweep and reversal — PROPOSED

- **Detection:** price sweeps a prior swing extreme or a cluster of equal highs or lows, then closes back inside, with confirmation.
- **Rejections:** acceptance beyond the swept level, or no confirmation.
- **Risk construction:** stop beyond the sweep extreme; target at the opposing structure.
- Not implemented.

---

## 19. Historical backtesting and validation

**PROPOSED.**
- **Approach:**
  - A backtest harness that runs the **same** strategy and risk code over broker history.
  - It includes spread and the established cost model, with no lookahead (results unchanged when future bars are removed).
  - It must be reproducible from saved bar sets.
- **Reports:** trade count, win rate, expectancy, profit factor and maximum drawdown, per strategy, pair and timeframe.
- **Uses:**
  - validating the experimental T6 parameters;
  - measuring candidate frequency, the `target_structure` rate and the 1:2 achievability rate.
- **Required before:** parameter tuning, ML work or any execution discussion.

---

## 20. ML meta-model improvements

**PROPOSED (deferred until backtest and forward evidence exist).**
- **Role:** a meta-model that ranks or filters candidates already produced by the rule-based strategies, for example by estimating the probability of reaching the target before the stop.
- **Constraints:**
  - It may only **reduce** or **veto**.
  - It may never create candidates, raise size, or bypass any risk rule.
  - It must be trained and evaluated on out-of-sample data, with the journal and backtests as sources.

---

## 21. Economic news and contextual analysis

**PROPOSED.**
- **Event-risk filter:** high-impact calendar events near the entry block or flag a setup. A calendar adapter exists in source; no filter is implemented.
- **Contextual review:** an advisory review (for example Gemini, which needs a news source and licence decision plus an API key) attached to each setup: confirm, weaken or invalidate.
- **Constraints:**
  - AI output can never raise size or remove a risk veto.
  - The pipeline must run unchanged with AI disabled.
  - Prompts and responses are journaled.

---

## 22. Multi-pair and multi-timeframe scanning

**PROPOSED.**
- **Approach:**
  - Scan all configured pairs and timeframes, H1, H4 and D1 (and later more pairs), for all strategies.
  - Rank candidates.
  - Respect daily and open-exposure risk across simultaneous candidates.
- **Prerequisites:** the existing shadow scheduler covers closed-candle evaluation of 3 pairs × 3 timeframes for trend continuation, but is **INACTIVE**. Activation needs explicit approval.
- **Open data issues:** D1 history depth for cent symbols (§24).

---

## 23. Development and operational principles

1. **Fail closed.** Missing, stale or unverifiable input gives NO_TRADE, UNVERIFIED or REJECTED, never an approval.
2. **MT5 is read-only.** No order placement, modification or closing; execution is disabled by design.
3. **Risk safeguards are never relaxed** to increase frequency (§8.1). Unknown costs are never zero.
4. **Phase 4 is authoritative** for trade construction. A candidate is not a trade, and a proposal is not an order.
5. **Determinism and purity** in analysis and risk code, with no lookahead.
6. **The user controls the system:**
   - restarts, migrations, scheduler activation and cost evidence are user-controlled;
   - silence is never approval;
   - each phase stops for review.
7. **Production data is protected:**
   - no access to `data/`, the journal, keys, backups or WAL/SHM without explicit authorisation;
   - never expose account IDs, logins, tickets, position IDs, keys or HMAC values.
8. **Honest reporting:**
   - test failures and intermittents are reported, not hidden;
   - assertions are never weakened to pass.
9. **Phased delivery:** design, then offline implementation and tests, then a preflight, then a user-controlled deployment, then separately approved activation.
10. **Git only when asked.**

---

## 24. Outstanding issues and known limitations

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | TC6/Phase 4 structural-target inconsistency (2/2 production candidates lost) | HIGH | Fixed in source (P6.2b.8); not yet validated in full or deployed |
| 2 | Production `CostTerms` empty, so no approvable proposal | HIGH (by design) | Awaiting approved cost evidence |
| 3 | `gap_unknown` from peerless T4 continuity can block whole windows (R1) | MEDIUM | Open (F4) |
| 4 | Cent-symbol D1 history depth for 661 candles unverified; D1 has 0 production decisions | MEDIUM | Open (F5) |
| 5 | TC6 `evidence_id`/level and T4 reason codes not journaled | MEDIUM (audit) | Open |
| 6 | T6 parameters experimental and unvalidated | MEDIUM | Open (§19) |
| 7 | TC4 pullback and TC1/TC2 bottlenecks | INFO | Under analysis |
| 8 | Old-code T4 FAIL rows without a reason (17 Nov) | LOW | Historical anomaly |
| 9 | One R1 provenance failure (missing `broker_symbol`) | LOW | Open |
| 10 | No automatic backups | MEDIUM | Proposed |
| 11 | B7.2 not deployed; `docs/B72_IMPLEMENTATION_REPORT.md` placeholders unfilled | LOW | Pending |
| 12 | Intermittent browser test `test_browser_account::test_zero_values_are_distinct_from_unavailable` (timed out once; 5/5 in isolation) | LOW | Recorded; unexplained |
| 13 | P6.2b.8 and B7.2 changes uncommitted | INFO | Commit on user request |

---

## 25. Document maintenance

1. This document **must be updated after every completed and reviewed development phase**.
2. **Approval alone does not mean implementation is complete.** Record APPROVED — NOT IMPLEMENTED until code exists.
3. **Implementation alone does not mean production deployment is complete.** Record IMPLEMENTED or TESTED until a user-controlled restart or migration is evidenced.
4. **Operational activation is recorded separately** from deployment. Examples: scheduler ON, monitoring active, cost terms populated.
5. **Historical statuses are updated only when supported by evidence**: a report, test output or verified production read.
6. **Future roadmap items remain visible** until completed or explicitly removed with user approval. This explicitly includes the four strategies (§18).
7. Each update records the date and the phase that caused it (§27).

---

## 26. Long-term vision

Johanna becomes a trustworthy, evidence-driven trading co-pilot:
- it continuously and transparently analyses the configured markets with **four validated strategies**: trend continuation, breakout and retest, range reversal, and liquidity sweep and reversal;
- every candidate passes through one **common, uncompromising risk engine** (0.5 % per trade, 2 % daily, net 1:2 minimum, verified costs);
- decisions are ranked with **historically validated** evidence and, later, an ML meta-model that can only make the system more conservative;
- news and context can only weaken or veto;
- everything is journaled with a tamper-evident audit trail, and decision quality is measured forward against backtests.

Any form of execution remains a distant, separately approved step: demo account first, explicit per-trade confirmation, independent hard limits and a kill switch. The human stays in control.

---

## 27. Phase history and sources

| Phase | Summary | Status |
|---|---|---|
| Week 1–2 / Phase 1 | Data foundation, lifecycle, acceptance | Complete (`docs/phase-1-acceptance-report.md`) |
| T0–T6, Phase 2.1 | Live market analysis pipeline and dashboard panels | Complete, committed |
| E1 | Market-data recency integrity (T4 recency) | Complete, committed |
| Phase 3 | Account intelligence | Complete, committed |
| Phase 4 | Risk construction | Complete, committed |
| P5.0 / P5.1 | Journal design and implementation | Complete, committed (`docs/phase-5-architecture.md`) |
| P6 / P6.2a / P6.2b.1 | Shadow evaluation, risk corrections, cost evidence | Implemented, deployed (uncommitted) |
| Phase 2 (schema v5) / P6.2b.3 | v5 registration and production migration | Migrated |
| P6.2b.4 | Operational reliability and reconciliation | Implemented (inactive) |
| P6.2b.5 / 6a / 6a.1 / 6b | v6 registration, preflight, identity check, production migration | **v6 migrated 2026-11-19** |
| B7.1 / B7.2 | Frontend audit; shell and architecture | B7.1 complete; B7.2 implemented, not deployed |
| P6 investigation / P6.1 | Opportunity detection (source and production aggregate) | Complete |
| P6.2b.7 | Offline candidate-to-risk consistency investigation | Complete |
| P6.2b.8 | Shared structural-target contract | Approved; implemented in source; validation pending; not deployed |
| — | This status document created | 2026-11-19 |

**Status vocabulary: what is implemented vs. operational.** Phases P6 onwards are uncommitted per the session-start git status. "Deployed" for them means the running backend loaded that working-tree code at the last user restart. It does not mean the code is committed.
