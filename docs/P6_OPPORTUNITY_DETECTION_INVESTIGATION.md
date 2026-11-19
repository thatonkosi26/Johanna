# P6 — Opportunity Detection Investigation (read-only)

## 1. Executive summary

**Verdict: INSUFFICIENT EVIDENCE** — no software defect that would prevent valid candidates from
being generated, recorded or displayed was found in the source, but the central question (no
qualifying setups, or something blocking them?) cannot be settled without the production
decision-outcome distribution, which this phase was not permitted to read.

What the source and the available evidence do establish:

1. **The detection pipeline has barely run where opportunities are counted.** The opportunity
   funnel and shadow intents are produced only by the scheduled shadow evaluation, and the
   scheduler has been **OFF** since 2026-11-18 07:11 UTC. Aggregate counts gathered in the earlier
   authorised read-only verifications (P6.2b.3, 2026-11-19 04:42 UTC; P6.2b.6a, 10:24 UTC) show
   **6 evaluation cycles in total, 0 shadow intents**. Zero intents is the expected result of six
   windows, not evidence of a broken detector. (HIGH, DESIGN/OPERATIONAL — confirmed from those
   counts.)
2. **Request-time coverage is one pair and one timeframe.** Outside the scheduler, T6 runs only
   for the dashboard's selected pair/timeframe (default EURUSD H1), only on H1/H4/D1 and only while
   the dashboard is open (every 30–60 s, deduplicated per closed candle). Those results go to
   Decision history; they never reach the funnel or create intents.
3. **The setup is a strict conjunction, and the risk stage adds a structural tension.** A
   candidate (R7) needs ADX ≥ 25 with a non-flat EMA50 slope, **all four** bias votes aligned,
   EMA200 alignment, a pullback to within 0.25 ATR of EMA20 in the last five candles, a close
   beyond EMA20, ≥ 1 ATR clearance to the opposing level (or no level), and no V1/V2 veto. Phase 4
   then needs that opposing level as its take-profit and **rejects** a candidate with no level
   (TC6 *passes* that case) or with the level closer than 1:2 reward:risk from a stop beyond the
   last swing. Only a constructed trade becomes a shadow intent. (HIGH, DESIGN LIMITATION.)
4. **A fragile data gate.** T6 needs a **661-candle** window, and the live path evaluates without
   peer symbols, so any unexplained missing candle inside the window is a T4 `gap_unknown`
   **rejection (R1)** for as long as it stays in the window (≈ 4 weeks on H1, ≈ 2.6 years on D1).
   T0 found no unexplained gaps in the standard-symbol history it studied, so this is a risk,
   not an observed cause; the cent symbols' (`…c`) history depth is unverified. (MEDIUM,
   INSUFFICIENT EVIDENCE.)
5. **The "why not" is recorded but hard to see.** Every request-time decision stores its rule and
   reasons, but the UI shows only the outcome; there is no R1–R7 distribution for request-time
   decisions, and no test runs real candles through T6 *and* a natively constructed Phase 4 trade.
   (MEDIUM, MISSING OBSERVABILITY / TEST GAP.)

Highest-priority next step (needs your approval): a **read-only aggregate query** of production
decision outcomes and rules (counts by symbol, timeframe, outcome, rule and first failed check)
and of the six cycles' gate reasons — it would settle whether candidates have occurred at all and
which rule stops evaluations.

## 2. Actual architecture and pipeline

```
                           ┌──────────────── request-time (dashboard open) ────────────────┐
 MT5 (read-only, workers)  │ GET /api/market/analysis?symbol&timeframe (selected only,      │
   │                       │   H1/H4/D1; analysis poller 30 s live / 60 s quiet)            │
   ▼                       │ GET /api/risk/proposal (Risk page)                             │
 T1 live_market.snapshot   └───────────────┬───────────────────────────────────────────────┘
   (662 bars, closure labels)               │                ┌──── scheduled (only if enabled) ────┐
   ▼                                        │                │ ShadowEvaluationService: 3 pairs ×  │
 T2 freshness (analysed_at)                 │                │ H1/H4/D1 per candle close; closure  │
 T3 continuity (sessions, peers=None)       │                │ must be tick_observed; retries;     │
 T4 validate_window ──rejected──► R1        │                │ market_closed → SKIPPED            │
   │ provisional ─────────────► R2          │                └───────────────┬─────────────────────┘
   ▼                                        │                                │
 T5 analyse_window (EMA20/50/200, RSI, ATR, ADX, swings, S/R)                │
   ▼                                        │                                │
 T6 decision: P1–P10 provenance, freshness, T5 status, structure, consistency ─fail─► R1
   ▼ classify_market (regime, bias, strength, volatility, momentum, location, swings)
   ├─ R3 core undetermined · R4 conflicting bias / V2 · R5 setup input unavailable
   ├─ R7 TRADE_CANDIDATE (6/6 checks, no veto)      R6 MARKET_CONDITION (otherwise)
   ▼                                        ▼                                ▼
 decision journal (every outcome, dedup per fingerprint; source analysis_request /
   risk_request / scheduled_evaluation)            evaluation_cycle + gate_outcome rows
   ▼ (risk_request / scheduled, candidates)        ▼ (R7 only)
 Phase 4 plan_trade/finalise: TP = TC6 level (none → REJECTED), SL beyond last swing,
   gross R:R ≥ 2, net costs (CostTerms empty → UNVERIFIED), sizing, margin
   ▼                                               ▼ constructed trade only
 risk_assessment row                               shadow_intent (HYPOTHETICAL)
   ▼                                               ▼
 API: /api/journal/decisions (all outcomes)        /api/shadow/{evaluations,intents,funnel,diagnostics}
   ▼                                               ▼
 UI: Decision history (outcome + rule chips)       UI: Shadow research (cycles, intents, funnel)
     Overview (latest 6)                               (funnel counts scheduled windows only)
```

| Stage | Source | Can reject / suppress | Observable |
|---|---|---|---|
| T1 snapshot | `live_market.snapshot`, `analysis/closure.py` | MarketUnavailable → no evaluation | request error; shadow retry/skip reasons |
| T2/T4 window | `analysis/window.py` `validate_window` | rejected → R1; provisional → R2 | decision reasons; shadow gate rows (T4 codes) |
| T3 continuity | `analysis/continuity.py`, `sessions.py` | unknown/symbol_missing gap → T4 FAIL | T4 reason `gap_unknown` |
| T5 | `analysis/engine.py` `analyse_window` | invalid status → R1 | decision reasons |
| T6 gate | `analysis/decision.py` `_evaluate` | provenance, freshness, structure, consistency → R1 | decision reasons |
| Conditions | `analysis/conditions.py` | undetermined → R3; conflicting → R4 | condition states stored |
| Setup | `analysis/setups.py` `evaluate_trend_continuation` | failed checks → R6; vetoes → R4/R6; inputs → R5 | checklist stored in decision evidence |
| Recording | `decision_journal.py` `capture_analysis`/`record_analysis` | capture failure is swallowed (counted) | `capture_failures` on the Decisions page |
| Phase 4 | `risk_engine.py` `plan_trade`/`finalise` | REJECTED / UNVERIFIED | risk_assessment rows; funnel primary reasons |
| Shadow | `shadow_evaluation.py` | skipped/missed/failed windows; intent only with constructed trade | cycles, gates, funnel, diagnostics |
| API | `api.py` | no outcome filter on decisions; intents/funnel filters optional | — |
| UI | `web/modules/decisions.js`, `shadow.js`, `overview.js` | shows outcome/rule, not failed checks | — |

## 3. Instrument and timeframe coverage

- **Configured:** EURUSD/GBPUSD/USDJPY mapped to the broker's `EURUSDc/GBPUSDc/USDJPYc`
  (`shadow_evaluation.PAIRS`; request-time resolution in `live_market`), timeframes H1/H4/D1
  (`analysis.engine.SUPPORTED_TIMEFRAMES`); M1–M30 are never analysed.
- **Scheduled:** all 9 combinations at every candle close **when enabled**; market closures are
  skipped without MT5 reads.
- **Request-time:** only the selected pair/timeframe (default EURUSD H1) while the dashboard is
  open; the Risk page analyses the same selection on request.
- **Actual history (from the earlier authorised aggregate counts, not re-read):** 6 scheduled
  cycles ever; the scheduler last set OFF by the user on 2026-11-18 07:11 UTC; 53 decisions and 19
  risk assessments recorded (consistent with roughly one EURUSD H1 decision per closed candle
  while the dashboard was open, plus Risk-page requests). The per-cycle statuses and the decision
  outcomes were not collected.
- **Symbol mapping:** tested (`test_shadow_evaluation`, live-market tests); no mismatch found.
- **Window requirement:** 661 closed candles + 1 (`features.required_candles`), i.e. ≈ 27.5
  trading days (H1), ≈ 110 days (H4), ≈ 2.6 years (D1). Whether the cent symbols' D1 history is
  that deep is unverified (T0 studied the standard symbols).

## 4. Candidate-generation analysis

There is **one** strategy path: T6 `trend_continuation` (`analysis/setups.py`), reached only
after R1–R5 pass. Defaults are documented as "experimental, not tuned or validated"
(`features.DecisionParameters`).

| Requirement | Rule |
|---|---|
| Data | accepted T4 window (fresh at `analysed_at`, no forming/unknown candle, no unexplained gap), T5 status available/partial, provenance P1–P10, consistency guard |
| TC1 regime | ADX ≥ 25 **and** |EMA50 slope over 5 candles| > 0.25 ATR (`conditions.trend_regime`) |
| TC2 bias | all four votes agree: close vs EMA50, EMA20 vs EMA50, +DI vs −DI, slope vs ±0.25 ATR (`conditions.directional_bias`); any split → neutral; opposing votes → R4 |
| TC3 long term | close and EMA50 beyond EMA200 on the bias side |
| TC4 pullback | a candle's low (high) within 0.25 ATR of EMA20 among the last 5 closed candles, all `tick_observed` |
| TC5 resumption | latest close beyond EMA20 |
| TC6 clearance | nearest qualifying opposing level (strength ≥ 2) ≥ 1 ATR away; no qualifying level → pass |
| V1 / V2 vetoes | EMA50 structure break in the pullback window; confirmed swing structure opposing the bias (→ R4) |
| Required inputs | EMA20/50/200, ATR14, slope50, pullback window, structure-break window, support/resistance usable — else R5 |

Cooldown / duplicate suppression: none in T6; the journal deduplicates identical decisions (an
observation count increases) and the scheduler deduplicates windows; neither hides a new
candidate. Candidate expiry: Phase 4 proposals are valid until the next candle close.

Classification of the candidate stage: **reached** (every evaluation past R1–R5 runs the
checklist); **successful in tests** (synthetic drift-plus-oscillation histories give R7:
`test_t6_decision.py::test_bullish_trend_history_is_a_trade_candidate`, `test_market_analysis.py`);
**recorded** for every outcome (`record_analysis`, source-tagged); **exposed** through
`/api/journal/decisions` without an outcome filter. Whether it has been successful in production
is **unknown** (not accessed).

## 5. Rejection taxonomy

| Rejection condition | Stage | Purpose | Inputs | Observability | Tests | Potential issue |
|---|---|---|---|---|---|---|
| no valid `analysed_at` | T6 R1 | no clock-free evaluation | caller time | reason | `test_t6_freshness` | none |
| provenance P1–P10 | T6 R1 | inputs belong together | snapshot/validation/analysis | reason per check | `test_t6_decision` | none |
| stale snapshot (T2) | T4 → R1 | never evaluate stale data | tick/bar ages | reason | `test_t6_freshness` | historical defect fixed (R7 on stale data) |
| forming/unknown candle in window | T4 | only closed candles | closure labels | T4 reason | T4 tests | forming newest candle excluded correctly |
| `clock_only` candle | T4 provisional → R2 | closure unproven | latest tick | reason | `test_r2_provisional…` | rare outside quiet periods |
| unexplained gap (`gap_unknown`, `gap_symbol_missing`) | T3/T4 → R1 | fail closed on data loss | 661-candle window, session calendar, **no peers** | T4 reason; shadow `T4.history_slots` rows | T3/T4 tests | **fragile**: one gap blocks a timeframe for the whole window length (finding F4) |
| market-wide unverified gap | T4 provisional → R2 | possible unconfigured holiday | peers (none supplied live) | reason | T4 tests | cannot occur live without peers; such gaps become `unknown` (F4) |
| insufficient history (< 661) | T4/T5 → R1/R5 | EMA200 convergence | bar count | reason | `test_r5_ema200…661` | cent-symbol D1 depth unverified (F5) |
| T5 status invalid | R1 | — | T5 | reason | yes | none |
| structure / consistency guard | R1 | tamper/defect guard | recomputed indicators | reason | yes | none |
| core condition undetermined | R3 | — | ADX, EMAs, DI | reasons | yes | none |
| conflicting bias / V2 | R4 | — | votes, swings | reasons | yes | strictness, not a defect |
| setup input unavailable (pullback window not tick_observed, S/R untrusted, EMA invalid) | R5 | — | features | reasons | yes | none found |
| TC1–TC6 failed / V1 | R6 | strategy definition | features | failed checks in evidence (not shown in UI) | `test_t6_setups` | strict conjunction (F3) |
| no opposing level for TP | Phase 4 REJECTED (`target_structure`) | TP must be structural | TC6 selection | risk reasons; funnel primary reasons | fixture `CANDIDATE_NO_TARGET` | contradicts TC6's pass for "no level" → candidate can never become an intent (F3) |
| gross R:R < 2 | Phase 4 REJECTED | 1:2 policy | SL beyond last swing, TP at level | risk reasons | risk tests | combined with TC6's 1 ATR threshold many candidates fail here (F3) |
| costs not established | Phase 4 UNVERIFIED | no unknown costs | CostTerms (empty) | risk status | risk tests | expected: every proposal UNVERIFIED today; intents still recorded as UNVERIFIED |
| closure unconfirmed / candle not available / worker busy | shadow SKIPPED | never evaluate an unconfirmed candle | ticks, MT5 | cycle reason + gates | shadow tests | none |
| market closed | shadow SKIPPED | no MT5 read when closed | session calendar | reason | yes | none |
| scheduler disabled | — (no windows) | user control | journal setting | Shadow research / Control Centre | yes | **the dominant reason for zero intents** (F1) |

## 6. Historical and operational evidence

| Evidence | Source | Status |
|---|---|---|
| Scheduler OFF since 2026-11-18 07:11 UTC; 6 cycles; 60 gate rows; 0 shadow intents; 53 decisions; 19 risk assessments | aggregate counts from P6.2b.3 / P6.2b.6a read-only verifications (this session) | available, aggregate only |
| Per-cycle statuses, decision outcomes and rules, failed checks, risk outcomes | production journal | **not accessed** (prohibited) |
| Synthetic histories produce R7 | `tests/t6_fixtures.trend_bars`, `test_t6_decision`, `test_market_analysis` | available |
| No unexplained gaps in the studied history (H1, standard symbols) | `docs/evidence/t0-exness-sessions/T0-findings.md` | available; cent symbols not studied |
| A historical defect (R7 on a stale snapshot) was found and fixed | `docs/t6-implementation-report.md` | resolved |

The funnel distinguishes "no windows" from "no opportunities" (`windows_scheduled`,
`not_evaluated`), and shadow cycles distinguish skipped / missed / failed from completed
NO_TRADE. Request-time decisions are never filtered by outcome in the API; the UI shows the
outcome and rule (TRADE_CANDIDATE · R7 would be visible in Decision history and on Overview).
**No claim is made here that Johanna has generated zero candidates in production.**

## 7. Test-quality assessment

| Area | Tested? | Notes |
|---|---|---|
| Valid data → candidate | **Yes, synthetic** | sinusoid-with-drift histories through the real T1-T6 path (`_real()` in `risk_fixtures`) |
| Invalid / stale data → no candidate | Yes | freshness, provenance, T4 tests |
| Different market conditions | Yes | R1–R7 each (`test_t6_decision`, `test_t6_conditions`, `test_t6_setups`) |
| Instruments / timeframes | Partial | mapping and scheduling tested; real-pipeline candidates only on EURUSD H1 synthetic data |
| Valid setups rejected incorrectly | Partial | rules tested individually; no realistic-history replay |
| Recording and retrieval | Yes | decision journal, shadow cycles, intents, API tests |
| Funnel accounting | Yes | funnel tests |
| API / UI visibility | Yes | browser tests for Decisions, Shadow research, intents |
| **Complete natural path** (real candles → T6 R7 → Phase 4 constructs the trade → intent → API) | **No** | the real R7 fixtures have no qualifying opposing level (risk rejects: `CANDIDATE_NO_TARGET`); constructed trades in tests rely on a hand-inserted TC6 level (`envelope(level=BUY_LEVEL)`) or a stub risk service |
| Rule frequency on realistic data | **No** | no replay or calibration test on archived history |

## 8. Findings (ranked)

| # | Finding | Class | Severity | Evidence | Why it matters / how it explains no opportunities | Reproducible | Needed evidence |
|---|---|---|---|---|---|---|---|
| F1 | Scheduled detection (the only source of funnel counts and intents) has run 6 windows ever; scheduler OFF since 2026-11-18 | DESIGN LIMITATION (operational) | HIGH | aggregate counts (P6.2b.3/6a); `shadow_evaluation` docstring; funnel counts scheduled windows only | zero intents is expected; Shadow research cannot show opportunities that were never evaluated | source + prior counts | cycle statuses (read-only query) |
| F2 | Request-time evaluation covers only the selected pair/timeframe (default EURUSD H1) while the dashboard is open | DESIGN LIMITATION | HIGH | `web/modules/analysis.js` poller, `api._market_analysis`, preference default | 8 of 9 combinations are rarely or never evaluated outside the scheduler | source | decision counts by symbol/timeframe |
| F3 | Strict setup conjunction plus Phase 4 TP rule: a qualified candidate with no opposing level is always rejected (TC6 passes it), and a level must also satisfy 1:2 from a swing-based stop | DESIGN LIMITATION | HIGH | `setups.clearance` (`no_*_levels` → PASS), `risk_engine` docstring "No such level: rejected", fixture `CANDIDATE_NO_TARGET` | even real candidates rarely become constructed trades / intents | source + fixtures | risk outcome distribution for R7 decisions |
| F4 | 661-candle window evaluated without peers: any unexplained missing candle → `gap_unknown` → R1 for the whole window span | INSUFFICIENT EVIDENCE (likely limitation) | MEDIUM | `market_analysis` calls `evaluate_market_snapshot_with_validation` without `peers`; `window.py` FAIL for unknown gaps | could silently block a timeframe for weeks (H1) or years (D1) | source; T0 found no such gaps in studied history | R1 reason counts by timeframe |
| F5 | Cent-symbol D1 history depth for 661 candles unverified | INSUFFICIENT EVIDENCE | MEDIUM | T0 studied standard symbols | D1 may be R1/R5 permanently | — | R1/R5 reasons for D1 decisions |
| F6 | Failed checks are recorded but not shown; no R1–R7 distribution for request-time decisions | MISSING OBSERVABILITY | MEDIUM | `decisions.js` shows outcome/rule only; shadow diagnostics cover scheduled windows | the user cannot see *why* nothing qualifies | source | — |
| F7 | No test of the complete natural path with a natively constructed trade | MISSING OBSERVABILITY (test gap) | MEDIUM | section 7 | a regression between T6 and Phase 4 construction would go unnoticed | source | — |
| F8 | Parameters are experimental, never calibrated on market data | DESIGN LIMITATION | LOW | `DecisionParameters` docstring | candidate frequency is unknown, not a defect | source | offline replay |
| F9 | Costs unverified → every proposal UNVERIFIED | EXPECTED SAFETY REJECTION | LOW | CostTerms empty | prevents approval, not detection or intents | source | — |

No CONFIRMED DEFECT was found.

## 9. Prioritised remediation plan (not implemented)

**A. Correctness (only if evidence confirms)**
1. If the read-only query shows R1 `gap_unknown` dominating: review whether request-time analysis
   should pass peer symbols (as T3 supports) so market-wide gaps are classified provisional rather
   than unknown — a design change for separate approval, keeping fail-closed behaviour.
2. If D1 is permanently R1/R5 for lack of history: document D1 as unsupported for the cent
   symbols or source sufficient history — not by shortening EMA200 silently.
3. Resolve the TC6/Phase 4 tension by design review (e.g. TC6 reporting "no target available"
   explicitly, or Phase 4 documenting why open-space candidates are never actionable) — no
   threshold change without evidence.

**B. Observability**
1. Approve a read-only aggregate query of production decisions (outcome, rule, first failed
   check, by symbol/timeframe) and the six cycles' gates — the decisive next step.
2. Show each decision's rule reasons and failed checks in Decision history (B7.3 detail drawer).
3. Add an R1–R7 / failed-check distribution for request-time decisions alongside the shadow
   diagnostics.

**C. Strategy evaluation (offline, no production rule change)**
1. Replay archived historical bars (the Parquet archive) through the unchanged T1–T6 + Phase 4
   pipeline offline for all nine combinations; report R1–R7 frequencies, per-check failure rates
   and how often a candidate yields a constructed trade.
2. Only then consider parameter research, separately approved and versioned.
3. Operationally, enabling the shadow scheduler (a separate, existing user decision) is what
   produces funnel and intent evidence.

**D. Tests**
1. A positive-path test where realistic candles produce R7 **and** Phase 4 natively constructs
   the trade (a qualifying opposing level ≥ 1:2), through the shadow service to `/api/shadow/intents`.
2. Tests showing a single unexplained gap's effect on the 661-candle window (H1/D1) to make the
   F4 behaviour explicit.
3. A request-time decision distribution test (API exposes rule and reasons for every outcome).

## 10. Access statement

**Accessed:** `src/` (analysis, decision, setups, features, conditions, window, continuity,
sessions, closure, market_analysis, shadow_evaluation, shadow_intents, decision_journal,
risk_engine, api, application, persistence, web modules), `tests/` (fixtures and T6, risk,
shadow, market-analysis tests), `docs/` (T0 findings, T6 report). Aggregate production counts
are quoted from earlier authorised verifications in this session, with their provenance.

**Not accessed:** production `data/`, journal, key or backups; MT5; the broker; the running
backend; Git. No source code, test, configuration or preference was changed; no migration,
service or scheduler activation; nothing was restarted.
