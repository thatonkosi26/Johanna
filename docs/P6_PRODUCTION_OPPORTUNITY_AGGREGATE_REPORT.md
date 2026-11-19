# P6.1 — Production Decision and Opportunity Aggregate Investigation (read-only)

## 1. Executive summary

**Verdict: CANDIDATES GENERATED — RISK CONSTRUCTION BLOCKED** (with secondary bottlenecks).

- Johanna **has generated trade candidates**: **2 R7 `TRADE_CANDIDATE` decisions** (EURUSD H1,
  bearish, 2 distinct candles, 87 observations while the dashboard was open), both from
  request-time analysis. They are recorded and visible in Decision history as candidates.
- **Both reached Phase 4 and both were rejected for the same reason:
  `construction/target_structure`** — no qualifying opposing (support) level to serve as the
  take-profit — while their **TC6 clearance check passed**. No trade was constructed, so no
  proposal was approvable and no intent could exist. This is the TC6/Phase 4 contradiction
  predicted from the source, now observed in production in **2 of 2 candidates (100 %)**.
- Upstream, most evaluations end earlier: **R6 62.3 %** (setup not qualified; TC1 regime and
  TC2 bias fail together in 30 of 45 evaluated setups), **R4 18.9 %** (conflicting direction
  votes), **R1 11.3 %** (rejected data window), **R2 3.8 %**. Of the 14 evaluations with a clear
  direction, **12 failed TC4 (no pullback to EMA20)**.
- The scheduled detector ran **6 windows** (H1 only, in two short activations on 17–18 Nov), all
  completed, **0 candidates, 0 intents**; it has been OFF since 2026-11-18 07:11 UTC (user).
- The single scheduled R1 was a T4 rejection for **`closed_history_gap, gap_unknown`** — the
  missing-candle / no-peers fragility identified in the source investigation does occur.

## 2. Database access method and safeguards

- Exact path only: `C:\Users\thato\Downloads\Johanna\data\trading_journal.sqlite3`; the `-wal`
  and `-shm` siblings checked by exact path (no directory listing).
- At 11:50 UTC: journal 2,494,464 bytes, last written 11:32:35 UTC; **`-wal` and `-shm` absent**
  — every committed transaction was in the main file.
- Connection: `file:…?mode=ro&immutable=1` with `PRAGMA query_only = ON`. With no WAL file,
  `immutable=1` cannot omit committed WAL content, and it creates no sidecar. The read was
  accepted only because the `-wal` stayed absent and the journal's size and modification time were
  identical before and after (first attempt; retry logic unused). A second read (data-stage gate
  reason codes) used the same guard. Afterwards `-wal` and `-shm` were still absent.
- No write, checkpoint, journal-mode change, vacuum or maintenance; no other `data/` content
  accessed; no backend API called.

## 3. Schema and fields used (journal schema v6)

| Table (rows) | Fields used |
|---|---|
| `decision` (53) | `symbol, timeframe, analysis_outcome, t6_rule, market_direction, capture_source, as_of_utc, recorded_utc, t6_evidence` (JSON: `decision.reasons`, `gate`, `validation_verdict`, `analysis_status`, `market_condition.states`, `opportunity.status/failed_conditions/triggered_vetoes/checklist[id,result]`) |
| `decision_observation` (53) | `observations, first_seen_utc, last_seen_utc` |
| `risk_assessment` (19) | `decision_id` (linkage only), `risk_status, phase4_status_raw, risk_evidence` (JSON: `rejections[area,id]`, `unverified[area,id]`, `gross_reward_risk`) |
| `risk_assessment_observation` (19) | `observations` |
| `evaluation_cycle` (6) | `symbol, timeframe, window_close_utc, status, reason_code, t6_outcome, t6_rule, risk_status, decision_id` (linkage only) |
| `gate_outcome` (60) | `stage, gate, result, reason_code, detail` (T4 codes only), `recorded_utc` |
| `shadow_intent` (0) | count |
| `shadow_scheduler_setting` (4) | `enabled, changed_utc, actor` |

Reason texts were normalised (numbers and timestamps removed) before counting. No identifiers,
prices, amounts or account fields are reported.

## 4. Decision outcome distribution

53 decision records = **41 distinct candles** (symbol, timeframe, candle); **4,529 observations**
(repeated identical results while the dashboard polled); first recorded 2026-11-17 13:26 UTC,
last seen 2026-11-19 11:32 UTC.

| Rule | Outcome | Records | % | Distinct candles | Observations |
|---|---|---:|---:|---:|---:|
| R6 | MARKET_CONDITION | 33 | 62.3 | 26 | 3,460 |
| R4 | NO_TRADE | 10 | 18.9 | 10 | 963 |
| R1 | NO_TRADE | 6 | 11.3 | 3 | 6 |
| R7 | TRADE_CANDIDATE | 2 | 3.8 | 2 | 87 |
| R2 | NO_TRADE | 2 | 3.8 | 2 | 13 |
| R3 / R5 | — | 0 | 0 | 0 | 0 |

| By | Distribution |
|---|---|
| Source | `analysis_request` 46 (86.8 %), `scheduled_evaluation` 5 (9.4 %), `risk_request` 2 (3.8 %) |
| Symbol | USDJPY 26 (49.1 %), EURUSD 25 (47.2 %), GBPUSD 2 (3.8 %) |
| Timeframe | H1 42 (79.2 %), H4 11 (20.8 %), **D1 0** |
| Direction | NONE 39, SELL 14 (12 R6 + 2 R7), BUY 0 |

Symbol × timeframe × rule: USDJPY H1 R6 18; EURUSD H4 R6 8; EURUSD H1 R6 6; USDJPY H1 R4 5;
EURUSD H1 R1 4; EURUSD H1 R4 3; **EURUSD H1 R7 2**; USDJPY H1 R1 1; USDJPY H1 R2 1; GBPUSD H1 R4 1;
GBPUSD H1 R6 1; USDJPY H4 R4 1; EURUSD H4 R2 1; EURUSD H4 R1 1.

## 5. Rejection reason distribution

**Data / gate (R1, 6; R2, 2):** 5 × "validated window was rejected; nothing was calculated" (T4
verdict `rejected`, T5 `rejected`); 1 × provenance P-check failed because the snapshot carried no
`broker_symbol`; 2 × provisional window (R2). Decision evidence stores the T4 *verdict* but not
T4's reason codes, so the cause of the 5 request-time rejections is **not recorded**; the one
scheduled rejection's gate row records `closed_history_gap, gap_unknown`.

**Direction (R4, 10):** all "directional evidence conflicts" — most often price vs EMA50 bullish
while EMA20 vs EMA50 and the DI balance are bearish (4), or price and DI bullish with EMA20 vs
EMA50 bearish (3). No V2 swing-conflict veto.

**Setup (45 evaluations past the gate with a trusted window; 47 incl. 2 provisional):**

| Check | Pass | Fail | Not applicable |
|---|---:|---:|---:|
| TC1 regime (trending) | 15 | **30** | — |
| TC2 bias (bullish/bearish) | 14 | **31** | — |
| TC3 long term | 14 | 0 | 31 |
| TC4 pullback | 2 | **12** | 31 |
| TC5 resumption | 10 | 4 | 31 |
| TC6 clearance | 14 | 0 | 31 |

Failure combinations: TC1 + TC2 together **30**; TC4 alone 8; TC4 + TC5 4; TC2 alone 1; none
failed (R7) 2 (+ 2 not evaluable). Vetoes triggered: none. Market states over the 47: regime
ranging 19 / trending 16 / transitional 12; bias neutral 22 / bearish 15 / conflicting 10;
swing structure mixed 24 / HH-HL 17 / LH-LL 6; volatility normal 22 / low 16 / high 9.

## 6. R7 candidate count

**2 R7 decisions** — EURUSD H1, bearish (SELL), 2 distinct candles, both `analysis_request`,
87 observations in total; reasons "6 of 6 required checks passed, no veto triggered" (one with the
caution `volatility_high`). TC6 result: **pass** on both. No scheduled window produced a candidate.

## 7. Candidate-to-risk conversion

| | Count |
|---|---:|
| R7 decisions | 2 |
| with a linked risk assessment | **2** (100 %) |
| risk status | **REJECTED 2** |
| rejection ids | **`construction/target_structure` 2** |
| unverified ids | none (construction failed before cost checks) |
| gross reward:risk recorded | 0 (no trade constructed) |
| constructed trades / approved proposals / intents | 0 / 0 / 0 |

Linkage is unambiguous: every risk assessment has a `decision_id` that exists (19/19); the two
candidates' assessments were made when a Risk-page request re-evaluated the same candle (the
identical analysis is deduplicated onto the existing decision). The other 17 assessments are
`NOT_EVALUATED` (R6 14, R4 3): Phase 4 preconditions — not a candidate — not risk rejections.

## 8. TC6 / Phase 4 structural-target analysis

Both candidates passed TC6 and both were rejected by Phase 4 with `target_structure`. TC6 passes
when no qualifying opposing level exists; Phase 4 requires that level as its take-profit ("no such
level: rejected"). The decision evidence stores TC6's result but not its evidence id, so whether
each case was "no levels" or "no qualifying level below" cannot be told apart from the journal —
both lead to the same Phase 4 rejection. **Impact observed: 2 of 2 candidates (100 %) lost at
this step.** The 1:2 reward:risk check and the (empty) cost evidence were never reached.

## 9. Shadow evaluation and gates

| Item | Result |
|---|---|
| Scheduler settings (user) | ON 2026-11-17 22:07 → OFF 22:16; ON 2026-11-18 06:52 → OFF **07:11**; OFF since |
| Cycles | 6 — all **COMPLETED**; 0 skipped / missed / failed |
| Windows | H1 only: EURUSD, GBPUSD, USDJPY × closes 2026-11-17 22:00 and 2026-11-18 07:00 UTC (no H4/D1 close fell inside the activations) |
| T6 results | R6 4, R4 1, R1 1 (T4 rejected: `closed_history_gap, gap_unknown`) |
| Candidates / risk / intents | 0 / 0 / **0** |
| Gate rows | 60 (11 per cycle × 5, 5 for the R1 cycle): session open 6, closure tick_observed 6, T4 accepted 2, T4 FAIL (no reason) 3, T4 rejected 1, T6 gate pass 5 / fail 1, TC1 fail 5, TC2 fail 5, TC3–TC6 not applicable 5 each |

## 10. Cross-table consistency

- Candidates without risk assessments: **0**. Risk assessments without a decision: **0**.
- Cycles referencing missing decisions: **0** (6 cycles → 5 `scheduled_evaluation` decisions; one
  cycle's identical analysis was deduplicated onto an existing request-time decision).
- Scheduled R7 without an intent: **0** (there was no scheduled R7).
- Repeated observations are counted once per decision above (records and distinct candles
  reported separately from observations).
- **Historical recording anomaly:** the 3 T4 gate rows of the 17 Nov 22:00 run are `FAIL` with no
  reason code although those windows were accepted (their cycles completed R6/R4). The 18 Nov
  07:00 run records the state correctly; the current code always writes the T4 state as the
  reason. Old rows are wrong but harmless; they were not changed.
- **Single anomaly:** one R1 decision failed provenance because the snapshot had no
  `broker_symbol` — cause not determinable from the journal.
- API-visible counts were not compared (no backend request was authorised).

## 11. Limitations

- Decision evidence keeps the T4 verdict, not T4's reason codes: the 5 request-time R1
  rejections' causes are unknown; TC6's evidence id (no levels vs. no qualifying level) is not
  stored; free-text risk details are deliberately not persisted.
- The data cover what the dashboard and two short scheduler activations evaluated (≈ 2 days,
  mostly EURUSD/USDJPY H1); **D1 was never evaluated**; GBPUSD only twice. This is not a
  representative sample of strategy frequency.
- Aggregates reflect the journal at 11:32:35 UTC on 2026-11-19.

## 12. Ranked findings and next steps

| # | Finding | Severity | Evidence |
|---|---|---|---|
| 1 | Every candidate generated so far was rejected by Phase 4 for a missing structural target while TC6 passed (design contradiction) | **HIGH** | 2/2 R7 → `construction/target_structure` |
| 2 | Opportunity coverage is minimal: scheduler OFF since 18 Nov; 6 H1 windows ever; request-time analysis follows the chart selection (no D1, GBPUSD twice) | HIGH | settings, cycles, decision distribution |
| 3 | Strict direction/regime gate: TC1 + TC2 fail together in 30/45 setups; R4 conflicts 10/53 | MEDIUM (strategy design, not a defect) | checklist, R4 reasons |
| 4 | Pullback rarely present: TC4 fails 12 of 14 directional setups | MEDIUM (strategy design) | checklist |
| 5 | Unexplained-gap rejections occur (`gap_unknown`), and request-time R1 causes are not recorded | MEDIUM | scheduled R1 gate detail; decision evidence lacks T4 codes |
| 6 | Historical T4 gate rows without reason (17 Nov) | LOW (fixed in current code) | gate rows |
| 7 | One provenance failure (snapshot without broker symbol) | LOW | R1 reason |

Recommended next steps (each needs separate approval; none implemented):
1. **Design decision on finding 1**: either TC6 should not pass when no opposing target exists for
   Phase 4, or Phase 4 needs a documented alternative target method — to be designed and tested
   offline, never by silently relaxing the 1:2 rule.
2. **Offline replay** of archived history through the unchanged pipeline (all nine combinations)
   to measure R1–R7 frequencies and the target_structure rate on a representative sample.
3. **Observability**: store T4 reason codes and TC6's evidence id in decision evidence; show
   rule reasons and failed checks in Decision history (planned B7.3 detail drawer).
4. **Coverage** is an operational choice: enabling the shadow scheduler (your decision) would
   evaluate all nine combinations every candle.
5. Investigate the single missing-`broker_symbol` provenance failure (source-side, read-only).
