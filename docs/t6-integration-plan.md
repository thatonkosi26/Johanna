# T6 integration plan: backend endpoint and dashboard (revision 2)

Date: 2026-11-12 · Claude Code (Opus 5.5) · **Plan only.** No source, test, CSS, API,
configuration or data change, no backend or MT5 contact, and nothing committed. Builds on the
accepted T6 (`docs/t6-implementation-report.md`, design `docs/t6-market-conditions-design.md`).
Revision 2 applies the independent review; the changes are listed in §13.

**Recommendation in one paragraph.** Add one backend operation, `GET /api/market/analysis`,
implemented by a small new application-layer service.
- **What every independent request does:**
  1. fetches **a new** coordinated T2 snapshot from the existing `LiveMarketService`;
  2. reads the backend's own clock *after* the snapshot is returned, as `analysed_at`;
  3. calls the accepted `evaluate_market_snapshot`, so T4 → T5 → T6 run on one deep copy;
  4. returns the T6 result **unchanged** inside a thin envelope.
- **Concurrency:** requests that arrive *while* the same symbol/timeframe is being computed join
  that computation (single-flight).
- **No reuse:** completed results are **never** reused.
- **Frontend:** polls the endpoint with the existing `createPoller` and renders a read-only
  panel from the verified T6 schema (§1.5). Nothing is calculated in JavaScript, and the chart
  viewport is never touched by an analysis update.

---

## 1. Existing architecture findings

### 1.1 Backend

| Component | File / function | Behaviour relevant to T6 |
|---|---|---|
| HTTP server | `src/johanna/__main__.py`, `ExclusiveWSGIServer(ThreadingMixIn, WSGIServer)` | One thread per request (`daemon_threads = True`). Concurrent API calls are possible, so the new endpoint must be thread-safe. |
| Routing | `src/johanna/api.py`, `_ROUTES`, `BackendAPI.__call__` | Static route table, GET-only market routes. `_host_allowed` rejects foreign `Host` / `Origin` (421). Unhandled exceptions become `500 {"error": "internal_error"}`. |
| Bars / snapshot parameters | `api.py`, `BackendAPI._market_bars(..., snapshot=False)` | Validates `symbol` (`persistence.SYMBOL_PATTERN`), `timeframe` (`live_market.TIMEFRAMES` = M1…D1) and `10 ≤ count ≤ 1000`, otherwise `400 invalid_bar_request`. |
| Live data | `src/johanna/live_market.py`, `LiveMarketService.bars` | Cache keyed by `(pair, timeframe, count)`, **`bars_ttl` = 5 s**. A cached entry keeps its original MT5 `retrieved_at_utc` and is marked `served_from_cache`. On `MarketUnavailable` it returns the previous entry as `status: CACHED`, or `_archived_bars` (`source: parquet_archive`), or `UNAVAILABLE` with no bars. |
| Snapshot | `LiveMarketService.snapshot` | `bars()`, then `analysis.snapshot.build_snapshot(..., snapshot_id=uuid4().hex, assembled_at=self._clock())`. Snapshot keys used by this plan: `upstream.{status, reason}`, `source`, `bars.{retrieved_at_utc, served_at_utc, served_from_cache}`, `eligibility.{state, evaluated_at_utc}`. |
| Clock | `LiveMarketService.__init__(clock=...)`, `self._clock` (default `datetime.now(UTC)`) | The same clock stamps `tick.retrieved_at_utc` / `bars.retrieved_at_utc` inside `MT5Session.bars`, at the moment of each MT5 read. It's a private attribute. |
| MT5 workers | `ProcessSessionRunner.run` / `ThreadSessionRunner.run` (both hold `self._lock`); `_refuse_during_backoff` | One bars job at a time per runner. After failures, exponential backoff (2–60 s), during which requests are refused immediately. Quotes and bars use separate workers. |
| Wiring | `src/johanna/application.py`, `JohannaApplication.__init__` | `self.live_market = live_market or LiveMarketService(...)`, injectable in tests. |
| T2 freshness | `analysis/snapshot.py`, `SnapshotLimits.max_snapshot_age_seconds = 5.0`, `evaluate_snapshot`, `_seconds` (rounded to ms) | `age = analysed_at − bars.retrieved_at_utc`; above 5.0 s or negative is `refused`. Non-`mt5` source, `CACHED`, `UNAVAILABLE` or no bars is `refused`. A stale snapshot tick (`clock_stale_ticks`) is `provisional`. |
| T4 | `analysis/window.py`, `validate_window(snapshot, *, required_count, analysed_at, excluded_opens, peers, ...)` | Re-evaluates T2 at `analysed_at`. Refused → `rejected`; provisional → `provisional`. Excludes the forming newest candle. |
| T6 | `analysis/decision.py`, `evaluate_market_snapshot(snapshot, *, analysed_at, required_count=None, excluded_opens=(), peers=None, parameters=...)` | `analysed_at` is required and must be timezone-aware; otherwise R1 with nothing evaluated. Default window: `required_candles()` = 661. H1/H4/D1 only. |

### 1.2 Frontend (`src/johanna/web/app.js`)

| Concern | Where | Behaviour |
|---|---|---|
| Live state | `state` (`quotes`, `bars`, `barsKey`, `barsSeq`, `barsReceivedAt`, `barsError`, `view`, ...) | Last successful responses only. `quoteView()` / `chartData()` downgrade them on error or expiry (`QUOTES_EXPIRE_MS` 10 s, `BARS_EXPIRE_MS` 65 s), measured from the **browser's receipt time** (`Date.now() − barsReceivedAt`), never by comparing browser and backend clocks. |
| Polling | `createPoller(name, run, interval)` | Never overlaps (`inFlight` / `pending`). Backs off on failure up to `POLL.maxBackoffMs`. Suspended while the tab is hidden. `restart()` aborts a request made for an old selection. |
| Requests | `fetchJson(url, poller, timeoutMs)` | `AbortController`, a timeout, and `{payload}` / `{error}` / `{aborted}` results. |
| Late responses | `refreshBars` | `barsSeq` plus `key !== chartKey()` discard older answers. |
| Chart | `BAR_COUNT = 300`, `mergeBars`, `drawChart`, `resolveViewport` / `setViewport` / `goLive`, `chartView()` (`follow`, `endT`, `count`, `priceScale` per `chartKey()`) | The viewport is anchored by time and reset only when the pair/timeframe changes. |
| Analysis slot | `analysisPlaceholder()` in `overviewPage()` | The natural place for a compact T6 panel. |
| Signal pages | `PAGE_INFO.signals` | Stay unchanged and accurate; T6 isn't a signal service. |

### 1.3 Tests available for reuse

- `tests/test_snapshot.py`: `SteppingClock`, `_fake` (`FakeMT5`), `_service`.
- `tests/fake_mt5.py`: `FakeMT5.rates_hook`, to supply arbitrary bars.
- `tests/t6_fixtures.py`: `trend_bars` (known R7 histories), `pipeline`, `FRESH`.
- `tests/browser_harness.py` and `tests/test_browser_smoke.py` (50 tests).
- WSGI `BackendAPI` tests in `test_application.py` and `test_live_market*.py`.

### 1.4 Conflicts and constraints found (none requires changing T2–T6)

| # | Finding | Consequence |
|---|---|---|
| C1 | `bars_ttl` (5 s) equals T2's maximum age (5 s). Back-to-back requests within 5 s can be served the **same MT5 observation** from the live service's cache. | That's acceptable, because T2 judges that observation's real age at the new `analysed_at`. A cached observation can never look fresher than it is: a request may be refused (R1, `snapshot_too_old`), but never falsely accepted. The envelope exposes `served_from_cache` and the data age (§3.3). The analysis uses its own `count` (662), so it never shares the chart's cache entry. |
| C2 | The chart fetches 300 candles; T6 needs 661 closed candles. | The analysis requests `required_candles() + 1` = **662**, derived from the T6 parameters. |
| C3 | The chart supports M1–D1; T6 supports only H1/H4/D1. | Below H1, `400 analysis_unsupported_timeframe` without contacting MT5. |
| C4 | `LiveMarketService._clock` is private. | `JohannaApplication` passes the same clock callable to both services (wiring change only). |
| C5 | A closed or quiet market makes the T2 snapshot provisional, so T6 returns R2. | Expected. Shown as diagnostic. |
| C6 | MT5 unavailable or in backoff: `CACHED` / parquet archive / `UNAVAILABLE`. T2 refuses all of them, so T6 returns R1. | No special path; the envelope repeats the upstream status. |
| C7 | Sunday D1 candles are partial days; exclusion is caller-controlled. | Default: no exclusions (decision D5). |
| C8 | Measured T6 result size: about **21 KB** of JSON (R7 example, 700-candle history). | Acceptable at the cadence. |
| C9 | `johanna/analysis/__init__.py` docstring is outdated. | Cosmetic fix in I1. |
| C10 | **The 662-candle MT5 request has not been verified against the real Exness connection.** The 40–60 ms pipeline figure is synthetic and says nothing about live MT5 latency. | Timing is exposed in the envelope (§3.3) and measured in L1. No latency assumption is built into the design. |

### 1.5 Actual T6 output schema (verified)

I checked these against `analysis/decision.py`, `setups.py` and `conditions.py`, and against
real outputs produced by a read-only script: an R7 candidate, R1 with MT5 `CACHED`, R2
provisional and R4. Every field the panel renders is listed with its real shape.

| Path | Shape (actual) | Notes for rendering |
|---|---|---|
| `decision` | `{outcome, rule, reasons: [str], actionable: false}` | `outcome` is one of `NO_TRADE` / `MARKET_CONDITION` / `TRADE_CANDIDATE`; `rule` is one of `R1`…`R7` |
| `symbol`, `broker_symbol`, `timeframe` | strings | |
| `as_of_utc` | ISO string, e.g. `"2026-11-11T13:00:00Z"` (close of the last window candle) | **`null` for R1** |
| `evaluated_at_utc` | ISO string as produced by T4's `_iso`: whole seconds (`"2026-11-11T14:00:31Z"`) or with microseconds (`"2026-11-11T14:00:32.200000Z"`) | It's T5 `analysed_at_utc` = the T4 evaluation time = `analysed_at`. Present for R1 when an analysis ran; `null` when the evaluation time was missing. |
| `inputs.snapshot_id`, `analysis_version`, `analysis_status`, `validation_verdict` | scalars | |
| `inputs.window` | `{count, first_t, last_t}` | `last_t` is the open time of the last candle |
| `inputs.price_reference` | `{source: "last_closed_candle_close", t, value}` | `null` for R1 |
| `inputs.provenance` | `{mode, passed, checks: [{id, passed, detail}], content_binding}` | constructed / by_construction for this endpoint |
| `inputs.evaluation_time` | `{passed, evaluated_at_utc, re_evaluated, problems: [str]}` | |
| `inputs.consistency_check` | `{passed, compared, mismatches}` or `null` | |
| `gate` | `{passed, reasons: [str]}` | |
| `market_condition` | `{trusted, understood, summary, dimensions}` | **`null` for R1** |
| `market_condition.summary` | `{trend_regime, directional_bias, trend_strength, volatility, momentum: str; location: [str]; swing_structure: str}` | `location` is a **list** |
| `market_condition.dimensions.<name>` | `{state, role, trusted, supporting: [Evidence], contradicting: [Evidence], unavailable: [{input, reason}]}` | Extras: `directional_bias.votes` (Evidence + `vote`); `location.{zones, zones_containing_price, overlaps, nearest_below, nearest_above, non_qualifying_levels}`, plus `tolerance` when levels exist; `swing_structure.{pivots, pending_ignored}` |
| Evidence | `{id, observed, threshold, comparison, source, t, detail}` (all keys always present; values may be `null`) | `observed` / `threshold` may be a number, string, list or object (TC3 `observed` is `{close, ema50, ema200}`) |
| `opportunity` | `{setup, direction, status, required_inputs, checklist, failed_conditions, vetoes, triggered_vetoes, cautions, unavailable_context}` | **`null` for R1.** For **R2**: `status: "not_evaluable"`, `checklist: []`, `vetoes: []`, and `required_inputs.unavailable` = `[{input: "validation", reason: "provisional window: diagnostic only"}]` |
| `opportunity.required_inputs` | `{all_available, unavailable: [{input, reason}]}` | |
| `opportunity.checklist[]` | Evidence + `{result, evidence_id}`; `result` is one of `pass` / `fail` / `unavailable` / `not_applicable` | Extras: TC4 `candles: [{t, d, low or high, ema20_j, atr_j}]`. TC6 `selected_level` (object or `null`), `price_inside_zone` (bool or `null`), `t5: {t5_status, t5_trusted, t5_reason, side_reason}`, and `nearest_non_qualifying_level` **only in TC6 cases 1–3**. With no direction, TC3–TC6 are plain Evidence + `result: "not_applicable"` |
| `opportunity.vetoes[]` | Evidence + `{triggered: true \| false \| null}` | V2 always has `pivots: [{role, type, price, pivot_t, confirmed_at_utc}]`. V1 has none. |
| `opportunity.cautions[]` | Evidence (`id`: `volatility_high` / `momentum_overbought` / `momentum_oversold` / `trend_strength_extended`) | Present only when a direction exists |
| `opportunity.unavailable_context` | `[{input, reason}]` | |
| `features` | `{as_of_utc, price, latest, slope50, pullback, structure_break, swings, levels, volatility}` | Additive T6 field; `null` for R1. Not rendered in I2 except where noted. |
| `limitations` | `[str]` | |
| `note` | str | Rendered verbatim |

The panel must treat every "extra" field above as optional, and must handle the R1 / R2 nulls
and empty lists without errors (tested in I2).

## 2. Recommended backend design

### 2.1 Components

- **New module `src/johanna/market_analysis.py`**, class `MarketAnalysisService`. It sits in
  the application layer; the analysis package stays pure.
  - `__init__(live_market, *, clock, parameters=DEFAULT_DECISION_PARAMETERS)`.
  - `analyse(symbol, timeframe) -> dict` returns the envelope (§3).
- **`application.py`:** passes one clock callable to both services and exposes
  `self.market_analysis`.
- **`api.py`:** route `"/api/market/analysis": frozenset({"GET"})` and handler
  `_market_analysis`, which reuses the symbol and timeframe validation plus the H1/H4/D1
  restriction.

### 2.2 The analysis operation

```
analyse(symbol, timeframe):
  key = (symbol, timeframe)
  with lock:
      record = in_flight.get(key)
      if record is None:                        # this request computes
          record = InFlight(event=Event()); in_flight[key] = record; leader = True
      else:
          leader = False                        # join the running computation
  if leader:
      try:
          t0 = monotonic()
          snapshot = live_market.snapshot(symbol, timeframe, required_candles() + 1)
          t1 = monotonic()
          analysed_at = clock()                 # backend clock, tz-aware UTC, read AFTER the snapshot
          result = evaluate_market_snapshot(snapshot, analysed_at=analysed_at)
          t2 = monotonic()
          record.envelope = envelope(result, snapshot, analysed_at, timings(t0, t1, t2))
      except BaseException as exc:
          record.error = exc
      finally:
          with lock: in_flight.pop(key, None)   # cleared on success AND failure
          record.event.set()
  else:
      record.event.wait()
  if record.error: raise record.error           # every waiter sees the same failure (500)
  return mark_joined(record.envelope, joined=not leader)
```

**Rules:**
- **No completed-result reuse.** Once the leader finishes, the record is removed. The next
  request, even a millisecond later, starts a new snapshot and evaluation.
- **Joined requests receive the same envelope,** with `service.joined_in_flight: true`, and
  the **original** `result.evaluated_at_utc` and snapshot times. Nothing is restamped.
- **The record is removed in `finally`,** so a failure never leaves a key permanently busy.
  The exception is delivered to every waiter and never cached.
- **Why the clock is read after the snapshot:** T2's age, `analysed_at −
  bars.retrieved_at_utc`, then includes every delay since the MT5 read.
- **A browser-supplied time is never accepted:** the endpoint takes no time parameter.

### 2.3 Why single-flight is enough without a result cache

**Within one tab, `createPoller` never overlaps requests** (`inFlight` / `pending`). At the
proposed cadence (§4.5: 30 s while LIVE, 60 s otherwise), a tab sends at most one analysis
request per 30 s per selection. That's at least six times T2's 5 s freshness budget, so every
request needs fresh data anyway.

**Concurrent requests for one key** come only from several tabs or clients, or from a trigger
coinciding with a poll. Single-flight collapses those into one MT5 read and one evaluation:
- **Joiners don't get stale data:** the shared result was evaluated during their own wait, at
  most one computation (one MT5 read plus about 60 ms) before they receive it.
- **No time is misrepresented:** they're told its true `evaluated_at_utc`.

**A result cache would add nothing but risk.** Reusing a completed result means serving data
whose age keeps growing after evaluation. As the review showed (bars at 10:00:00, evaluated at
10:00:04, reused at 10:00:08), that's eight-second-old data. It's removed from the design.

**Load:** the worst case is N independent clients making N fresh evaluations per cadence
period. That's negligible for this local, single-user application.

### 2.4 Alternatives considered

| Option | Why not |
|---|---|
| Extend `/api/market/snapshot` | Changes an accepted T2 contract and payload. |
| Extend `/api/market/bars` | Bars are polled every 5 s; adding a 662-candle fetch and a T6 evaluation would slow the chart and couple two cadences. |
| Frontend orchestration | Puts the evaluation time in the browser, so provenance would only be "supplied"; it would also need new T4/T5 endpoints. |
| Server loop over every pair | MT5 contention for unseen selections; results age between views. |
| Completed-result cache (revision 1) | **Removed:** a reused result's data ages past T2's 5 s budget (§2.3). |
| **Dedicated endpoint, single-flight, no reuse (recommended)** | One coherent operation, provenance by construction, an authoritative backend clock, T2 freshness applied to every independent request. |

## 3. Endpoint contract

### 3.1 Request

`GET /api/market/analysis?symbol=EURUSD&timeframe=H1`

- **Parameters:** `symbol` must match `SYMBOL_PATTERN`; `timeframe` must be one of H1, H4, D1.
- **No other parameters:** no `count` and no time.
- **Existing security checks apply:** Host/Origin check, GET only.

### 3.2 Responses

| HTTP | When | Body |
|---|---|---|
| 200 | An evaluation happened (or was joined), whatever its outcome, including R1 for MT5 unavailable or stale | Envelope |
| 400 | Invalid symbol or timeframe; timeframe below H1 | `{"error": "invalid_analysis_request" \| "analysis_unsupported_timeframe", "message": ...}` |
| 404 / 405 / 421 | Existing generic behaviour | unchanged |
| 500 | Unexpected exception (logged; shared by joined waiters) | `{"error": "internal_error"}` |

### 3.3 Envelope (200)

```json
{
  "symbol": "EURUSD",
  "timeframe": "H1",
  "served_at_utc": "2026-11-11T14:00:31.090000Z",
  "service": {
    "joined_in_flight": false,
    "requested_count": 662,
    "timing_ms": {
      "snapshot": 812,
      "evaluation": 58,
      "total": 871
    },
    "data_age_at_evaluation_ms": 900
  },
  "snapshot": {
    "snapshot_id": "4f0c…",
    "upstream_status": "LIVE",
    "upstream_reason": null,
    "source": "mt5",
    "bars_retrieved_at_utc": "2026-11-11T14:00:30.100000Z",
    "bars_served_from_cache": false,
    "eligibility_at_assembly": "live"
  },
  "result": { "…": "the accepted T6 output, exactly as evaluate_market_snapshot returns it" }
}
```

**Field sources:**
- `snapshot.*`: copied from the T6 input snapshot (`upstream.status`, `upstream.reason`,
  `source`, `bars.retrieved_at_utc`, `bars.served_from_cache`, `eligibility.state`).
- `data_age_at_evaluation_ms`: `analysed_at − bars.retrieved_at_utc`, for the L1 measurements.
  It's the same quantity T2 judges, reported but never used for any decision.
- `timing_ms`: monotonic durations. The 812 ms above is **illustrative only**; live latency is
  unverified (C10).
- `served_at_utc`: transport only. The panel never shows it as an evaluation time.

**Example A: `TRADE_CANDIDATE`.** `result` abridged, shapes exactly as §1.5:

```json
"result": {
  "decision_version": 1, "symbol": "EURUSD", "broker_symbol": "EURUSD…", "timeframe": "H1",
  "as_of_utc": "2026-11-11T14:00:00Z", "evaluated_at_utc": "2026-11-11T14:00:31Z",
  "inputs": {
    "snapshot_id": "4f0c…", "analysis_version": 1, "analysis_status": "available",
    "validation_verdict": "accepted",
    "window": {"count": 661, "first_t": "…", "last_t": "2026-11-11T13:00:00Z"},
    "price_reference": {"source": "last_closed_candle_close", "t": "2026-11-11T13:00:00Z", "value": 1.0854},
    "provenance": {"mode": "constructed", "passed": true, "content_binding": "by_construction",
                   "checks": [{"id": "P1", "passed": true, "detail": "…"}, "… P2–P10 …"]},
    "evaluation_time": {"passed": true, "evaluated_at_utc": "2026-11-11T14:00:31Z",
                        "re_evaluated": true, "problems": []},
    "consistency_check": {"passed": true, "compared": ["EMA20", "EMA50", "EMA200", "ATR14"], "mismatches": []}
  },
  "gate": {"passed": true, "reasons": []},
  "market_condition": {
    "trusted": true, "understood": true,
    "summary": {"trend_regime": "trending", "directional_bias": "bullish", "trend_strength": "strong",
                "volatility": "high", "momentum": "neutral", "location": ["between_levels"],
                "swing_structure": "higher_highs_higher_lows"},
    "dimensions": {"trend_regime": {"state": "trending", "role": "core", "trusted": true,
                                    "supporting": [{"id": "adx_at_or_above_trend_min", "observed": 29.4,
                                                    "threshold": 25.0, "comparison": ">=",
                                                    "source": "indicators.adx.value", "t": "2026-11-11T13:00:00Z",
                                                    "detail": null}, "…"],
                                    "contradicting": [], "unavailable": []}, "…": "six more dimensions"}
  },
  "opportunity": {
    "setup": "trend_continuation", "direction": "bullish", "status": "qualified",
    "required_inputs": {"all_available": true, "unavailable": []},
    "checklist": [{"id": "TC1_regime", "result": "pass", "observed": "trending", "threshold": "trending",
                   "comparison": null, "source": null, "t": null, "detail": null, "evidence_id": null},
                  "… TC2–TC5 …",
                  {"id": "TC6_clearance", "result": "pass", "observed": 2.0, "threshold": 1.0, "comparison": ">=",
                   "evidence_id": "resistance_clearance_sufficient", "price_inside_zone": false,
                   "selected_level": {"low": 1.0876, "high": 1.0879, "level": 1.08775, "strength": 2,
                                      "latest_confirmed_at_utc": "…"},
                   "t5": {"t5_status": "available", "t5_trusted": true, "t5_reason": null, "side_reason": null},
                   "source": "structure.support_resistance", "t": null, "detail": "…"}],
    "failed_conditions": [],
    "vetoes": [{"id": "V1_ema50_structure_break", "triggered": false, "…": "Evidence fields"},
               {"id": "V2_swing_context_conflict", "triggered": false, "pivots": ["… four pivots with times …"]}],
    "triggered_vetoes": [],
    "cautions": [{"id": "volatility_high", "observed": "high", "threshold": null, "comparison": null,
                  "source": "conditions.volatility", "t": null,
                  "detail": "caution only: not a veto, not supporting evidence"}],
    "unavailable_context": []
  },
  "features": {"…": "additive T6 field"},
  "decision": {"outcome": "TRADE_CANDIDATE", "rule": "R7", "actionable": false,
               "reasons": ["trend_continuation (bullish): 6 of 6 required checks passed, no veto triggered",
                           "caution: volatility_high"]},
  "parameters": {"decision": {"…": "…"}, "t5": {"…": "…"}},
  "limitations": ["thresholds are experimental defaults, not validated against outcomes", "…"],
  "note": "Analytical classification only. Not a recommendation, instruction or authorisation to trade."
}
```

**Example B: MT5 unavailable.** HTTP 200; actual R1 shape, verified:

```json
{"symbol": "EURUSD", "timeframe": "H1",
 "snapshot": {"upstream_status": "CACHED", "upstream_reason": "mt5_not_running", "source": "mt5",
              "eligibility_at_assembly": "refused", "bars_served_from_cache": true},
 "result": {"as_of_utc": null, "evaluated_at_utc": "2026-11-11T14:00:31Z",
            "market_condition": null, "opportunity": null, "features": null,
            "decision": {"outcome": "NO_TRADE", "rule": "R1", "actionable": false,
                         "reasons": ["T5 analysis status 'rejected'",
                                     "the validated window was rejected; nothing was calculated"]}}}
```

**Example C: market closed (R2).**
- `result.decision`: `{"outcome": "NO_TRADE", "rule": "R2", ...}`.
- `market_condition.trusted`: `false`.
- `opportunity`: `{"status": "not_evaluable", "checklist": [], "vetoes": [], "required_inputs":
  {"all_available": false, "unavailable": [{"input": "validation", "reason": "provisional window:
  diagnostic only"}]}, ...}`.

**Example D: unsupported timeframe.** `400 {"error": "analysis_unsupported_timeframe",
"message": "Analysis supports H1, H4 and D1."}`

**Envelope rules:** `result` is never modified, filtered or re-keyed, and the envelope never
includes account identity.

### 3.4 `window_validation` (implemented; final diagnostic contract)

**What it is:** an additive envelope field, a **verbatim projection of the exact T4 validation
object** the evaluation used.
- **Source:** the object comes from `decision.evaluate_market_snapshot_with_validation`, the
  same single T4 run that T5/T6 consumed.
- **No second run:** T4 is never run again for it.
- **Nothing derived:** nothing is taken from T6 messages, candle counts or the browser.

**When T4 ran:**

```json
"window_validation": {
  "available": true,
  "window_version": 1,
  "snapshot_id": "…",
  "evaluated_at_utc": "2026-11-11T14:00:31Z",
  "requested": {"required": 661, "warmup": 0, "expected": 661},
  "window": {"count": 660, "first_t": "…", "last_t": "…"},
  "excluded_candles": [],
  "verdict": {"state": "rejected",
              "reasons": [{"dimension": "completeness", "code": "insufficient_history",
                           "severity": "fail", "detail": "660 closed candle(s) available, 661 required"}]},
  "reason_codes": ["insufficient_history"],
  "dimensions": {"price": {"status": "pass", "reasons": []}, "…": "all seven, incl. session_candles"},
  "d1_convention": "… only when T4 includes it (D1)"
}
```

**Projection rules:**
- **Names and values:** field names and values are copied (deep copies) from T4.
- **`reason_codes`:** `[r["code"] for r in verdict.reasons]`, in T4's order.
- **Omitted:** `window.candles` and each dimension's `evidence`.
- **`d1_convention`:** present only if T4 supplied it.
- **Joined requests:** receive equal content without sharing nested objects.

**When T4 didn't run** (refuse-before-T4 paths): `{"available": false, "reason": "T4 did not
run: <the T6 R1 reason>"}`.

**Correspondence (tested):** `window_validation.snapshot_id`, `evaluated_at_utc`, `window` and
`verdict.state` equal the T6 result's `inputs.snapshot_id`, `evaluated_at_utc`, `inputs.window`
and `inputs.validation_verdict`. `evaluated_at_utc` is never `served_at_utc`.

## 4. Freshness, latency and scheduling

### 4.1 Evaluation time

- **Source:** the injected backend clock, read immediately after the snapshot returns.
- **Never** from the browser, and **never** read inside T6.

### 4.2 T2's 5-second threshold

- **Applied unchanged** by T2/T4 inside `evaluate_market_snapshot`, to **every** independent
  request, because there's no completed-result reuse.
- **Not reimplemented, relaxed or extended** by any backend or frontend value.

### 4.3 Fail-closed behaviour and no retries

- **Too old at evaluation time:** a snapshot older than 5 s at `analysed_at` (slow MT5 read,
  cached observation, queueing) gives T6 R1 with the T4 reason `snapshot_refused` /
  `snapshot_too_old`. This is returned as an explained `NO_TRADE`.
- **No retry:** the service makes **no automatic retry**. The next attempt is the frontend's
  next scheduled poll (≥ 30 s), so there's no retry loop.
- **One instant:** the T6 verdict concerns data age at `analysed_at`. The roughly 60 ms of
  evaluation after that instant doesn't change it.

### 4.4 MT5 latency, the bars worker and backoff

- **Live latency is unverified (C10):** the 662-candle request hasn't been run against the real
  Exness connection. Synthetic timings prove nothing about live MT5 latency.
- **Existing worker behaviour, unchanged:**
  - the service calls only `LiveMarketService.snapshot`, never a runner directly, so the
    existing timeout (`bars_timeout`), worker lock and backoff (`_refuse_during_backoff`, 2–60 s)
    all apply;
  - during backoff the call returns at once with a refused snapshot, giving R1, so the worker
    isn't occupied.
- **Worker occupancy:**
  - at most one MT5 read per key at a time (single-flight);
  - at most one request per tab per 30–60 s (cadence);
  - failures back off further in the frontend (`createPoller`).
  - Analysis therefore can't repeatedly occupy the bars worker beyond one 662-candle read per
    period per selection. Contention with the chart's 5 s, 300-candle polls is expected to be
    small but is **to be measured in L1**.
- **Timing for verification:** the envelope reports `timing_ms.snapshot`,
  `timing_ms.evaluation`, `timing_ms.total`, `data_age_at_evaluation_ms` and
  `bars_served_from_cache`. The service also logs one INFO line per computation (key, outcome,
  rule, timings), with no prices and no account data.

### 4.5 When analysis runs (frontend)

T6 depends on closed candles plus freshness.

**Triggers:**
1. **Selection change** (H1/H4/D1): `analysisPoller.restart()`.
2. **New closed candle:** when `refreshBars` sees the latest closed candle's open time change
   for the current key, call `analysisPoller.now()`.
3. **Periodic re-evaluation:** `POLL.analysisMs` = **30 s** while the chart is LIVE, **60 s**
   otherwise (D2).
4. **Backoff:** existing `createPoller` backoff on failure.

**Suspended** while the tab is hidden, below H1, and on pages without the panel.

### 4.6 Late-response protection

`refreshAnalysis` mirrors `refreshBars`: `analysisSeq` plus a key check. An answer for an older
request or another selection is discarded. On error the previous result stays visible but
becomes **outdated** (§5.6).

## 5. Frontend plan

### 5.1 State

`state.analysis` (last good envelope), `analysisKey`, `analysisReceivedAt` (browser
`Date.now()` at receipt), `analysisError`, `analysisSeq`, and `analysisPoller` in
`livePolling`.

### 5.2 Placement

- **Overview:** a compact panel replaces `analysisPlaceholder()`.
- **Charts page:** the full panel goes under the chart.
- **No change elsewhere:** the Trading Signals page stays unchanged.

### 5.3 Sections, mapped to the verified schema (§1.5)

**A. Market overview.**
- `result.symbol`, `result.broker_symbol`, `result.timeframe`.
- **Data observed:** `envelope.snapshot.bars_retrieved_at_utc`, plus "(repeat of a cached MT5
  read)" when `bars_served_from_cache`.
- **Evaluated:** `result.evaluated_at_utc`, always this, never `served_at_utc`, and labelled
  "(shared computation)" when `joined_in_flight`.
- **Covers candles up to:** `result.as_of_utc`.
- **Received:** "n s ago", from `analysisReceivedAt`.
- **Validation:** `result.inputs.validation_verdict` and `analysis_status`.
- **Provenance:** `result.inputs.provenance.mode` / `content_binding`.

When `as_of_utc` is `null` (R1), the coverage line reads "No analysis window".

**B. Market conditions** (only when `market_condition` isn't `null`).
- **Chips:** one per `summary` key. `location` renders one chip per list element.
- **Expanded evidence:** each chip opens `dimensions.<name>.supporting / contradicting /
  unavailable`. Each Evidence item shows `id`, `observed` vs `threshold` with `comparison`, and
  `t`, using a generic formatter for numbers, strings, lists and objects (TC3-style
  `{close, ema50, ema200}`).
- **Extras:**
  - `directional_bias.votes` as four rows with their `vote`;
  - `location.zones` / `overlaps` / `nearest_*` in a small table;
  - `swing_structure.pivots` with `pivot_t` / `confirmed_at_utc`.
- **Untrusted** (`market_condition.trusted === false`, R2): muted, "diagnostic only, not
  trusted".

**C. Opportunity** (only when `opportunity` isn't `null`).
- `setup`, `direction` (or "no direction"), and `status`.
- **`checklist`:** one row per check, with a `result` icon and Evidence.
  - `evidence_id` is shown when present.
  - **TC4:** an expandable list of `candles` (`t`, `d`, `ema20_j`, `atr_j`).
  - **TC6:** `selected_level` (raw interval `low`–`high`, strength) when not null;
    `price_inside_zone`; `t5.*`; and `nearest_non_qualifying_level` only if present.
- **`vetoes`:** `triggered` true / false / null (null renders as "not evaluated"). V2 lists its
  `pivots`.
- **`cautions`:** labelled "caution only, not a veto".
- **`required_inputs.unavailable` and `unavailable_context`:** `input` with `reason`.
- **R2:** the empty `checklist` / `vetoes` render as "Setup not evaluated: provisional window
  (diagnostic only)", from `required_inputs.unavailable[0].reason`.

**D. Decision.**
- **Badge:** `decision.outcome` (grey `NO_TRADE`, blue `MARKET_CONDITION`, amber outline
  `TRADE_CANDIDATE`), with `decision.rule` and `decision.reasons`.
- **Text:** `result.note` verbatim, plus the fixed caption "Analytical finding only, not an
  instruction or recommendation to trade (actionable: false)".
- **Limitations:** collapsible.
- **Never rendered:** no prices for opening, protecting or closing a position, no sizes, no
  order buttons.

### 5.4 Rendering mechanics

- **Own renderer, no chart work:** `renderAnalysis()` updates only the analysis panel and never
  calls `drawChart()`. `state.view` isn't read or written by analysis code.
- **Escaping:** all values go through `esc()`.
- **No calculation:** nothing is computed from bars in JavaScript.

### 5.5 Optional chart marker (I3, D4)

- **What it draws:** a vertical marker at `as_of_utc` and a faint band over `inputs.window`.
- **How:** drawn from `state.analysis` only for a matching key; it never changes `view.*`.

### 5.6 Freshness model in the panel

**Five times, kept distinct** and never substituted for one another:

| Quantity | Source | Meaning |
|---|---|---|
| Data observed | `envelope.snapshot.bars_retrieved_at_utc` | When MT5 delivered these bars (backend clock) |
| Evaluated | `result.evaluated_at_utc` | When T2/T4 judged freshness and T6 evaluated (backend clock) |
| Covers up to | `result.as_of_utc` | Close of the last candle in the analysed window |
| Received | `Date.now() − analysisReceivedAt` | Browser-side elapsed time since this result arrived |
| Newer candle? | `as_of_utc` vs the close of the latest **closed** candle in the live chart data (both backend timestamps) | Whether the market has moved on by a closed candle |

**Clocks aren't mixed:** browser and backend clocks are never compared. Display expiry uses the
browser-side receipt time, as the existing `BARS_EXPIRE_MS` does.

**Panel states, evaluated in this order:**

| State | Exact condition | Presentation |
|---|---|---|
| **Unsupported** | Selected timeframe below H1 | "Analysis is available for H1, H4 and D1"; no request |
| **Loading** | No result yet for `chartKey()` | Skeleton |
| **Unavailable** | `rule == "R1"`, or the last request failed and no result exists for the key | Reason category: "MT5 unavailable" when `snapshot.upstream_status` is `CACHED` / `UNAVAILABLE` or `source` isn't `mt5`; "data not fresh enough at evaluation time" when the gate reasons cite a refused snapshot; otherwise "analysis not possible: <first reason>" |
| **Diagnostic** | `rule == "R2"` | "Market closed or quiet / data provisional: diagnostic only, not trusted" |
| **Outdated** | A result exists for the key (R3–R7), **and** any of: the last analysis request failed (`analysisError`); `Date.now() − analysisReceivedAt > ANALYSIS_EXPIRE_MS`; a newer closed candle exists than `as_of_utc`; the chart data isn't LIVE | Muted, "Not current: <which condition>", still showing its own **evaluated** time and age |
| **Current** | A result exists for the key (R3–R7) and **none** of the Outdated conditions hold | Normal |

**Rules:**
- **The expiry is display policy only:** `ANALYSIS_EXPIRE_MS` (proposed 65 s, the existing
  `BARS_EXPIRE_MS`) decides only when the panel stops calling a displayed result current. It
  **doesn't** extend or relax T2's 5 s freshness. Every result shown was judged fresh by T2 at
  its own `evaluated_at_utc`; the expiry just stops an old judgement being shown as current.
- **No result is shown as newly evaluated unless it is:** the "Evaluated" time always comes
  from `result.evaluated_at_utc`. A joined result keeps the leader's time and is labelled
  shared. The backend never reuses a completed result, so no "reused" state exists.
- **Errors don't hide the last result:** on a failed refresh the previous result stays, but in
  the Outdated state.

## 6. Historical chart interaction

- **One reference:** the analysis always refers to its window ending at `as_of_utc`. Panning or
  zooming (`view.follow = false`) doesn't change the reference and doesn't trigger analysis.
- **While viewing history,** the panel adds: "You are viewing history ending <view end>. This
  analysis covers candles up to <as_of_utc>." It's omitted when the visible end equals
  `inputs.window.last_t`.
- **Go Live:** unchanged; only the notice is removed.
- **Analysing a historical viewport** is out of scope. T2 freshness would refuse a past
  snapshot.

## 7. Errors and unavailable states

| Situation | Backend | Frontend state |
|---|---|---|
| Invalid symbol / timeframe | 400 `invalid_analysis_request` | Error text; no result for another key is shown |
| Timeframe below H1 | 400 `analysis_unsupported_timeframe` (no MT5 call) | Unsupported |
| MT5 not running / backoff / timeout | 200, R1 (snapshot refused) | Unavailable: "MT5 unavailable" |
| Market closed / quiet | 200, R2 | Diagnostic |
| Stale at `analysed_at` | 200, R1 (`snapshot_too_old`), no retry | Unavailable: "data not fresh enough", next poll in ≥ 30 s |
| Insufficient history (< 661) | 200, **R1**: T4 rejects the window first (completeness, `insufficient_history`). R3/R5 aren't reachable through this endpoint. | Unavailable: "Not enough history", from `window_validation.reason_codes` (§3.4) |
| Exception | 500, logged; shared by joined waiters; record cleared | Previous result Outdated (or Unavailable if none); backoff |
| Request timeout | — | `fetchJson` timeout (`POLL.analysisTimeoutMs` 45 s), then backoff; previous result Outdated |

## 8. Testing strategy

**Synthetic tests** use `FakeMT5` with `rates_hook` fed from `t6_fixtures.trend_bars`,
`SteppingClock`, injected services, the WSGI `BackendAPI`, and the browser harness. **None
contacts MT5.**

| Area | Tests |
|---|---|
| Invocation | `analysed_at` is the clock read **after** the snapshot (call-order spy); count = `required_candles() + 1`; `result` is deep-equal to a direct `evaluate_market_snapshot(snapshot, analysed_at=...)` on the same snapshot and time |
| **No completed-result reuse** | Two sequential requests, even with no time elapsed, make **two** `snapshot()` calls and give two distinct `inputs.snapshot_id`s; a second request after the first finished never returns the first envelope |
| **Single-flight** | N concurrent requests for one key (released together with a barrier, the FakeMT5 read held open) make exactly **one** `snapshot()` call; all N get identical `result`s with the same `evaluated_at_utc` and `snapshot_id`; exactly one has `joined_in_flight: false` |
| **In-flight cleanup** | After success the key's record is gone and the next request computes; after an exception (injected) all waiters get 500, the record is gone, the next request computes, and nothing is cached |
| Different keys | Concurrent requests for different keys each compute (no false sharing) |
| **Freshness boundary** | With `SteppingClock`, the clock read for `analysed_at` is placed 5.000 s after `bars.retrieved_at_utc` (evaluated, R7 with trend fixtures) and 5.001 s after (R1, `snapshot_too_old`); a live-service cached bars entry served at 4.9 s then evaluated past 5.0 s gives R1, never acceptance; no client time is accepted (extra query parameters are ignored) |
| Outcomes | R7 bullish and bearish; R6; R5; R3; R4; R2 (stale snapshot tick); R1 for `CACHED`, `UNAVAILABLE` and parquet archive |
| Worker / backoff | During `ProcessSessionRunner` backoff (fake), the endpoint returns R1 promptly without a worker call; no retry happens inside one request (FakeMT5 call count = 1 on failure) |
| Timing fields | `timing_ms.*` non-negative and consistent (`total ≥ snapshot + evaluation`); `data_age_at_evaluation_ms` equals `analysed_at − bars.retrieved_at_utc` |
| Endpoint | Envelope keys exactly as §3.3; 400s; 405; 421 |
| Read-only | `market_analysis.py` imports only `live_market`, `analysis.decision` and the standard library; the order-function grep extended to it |
| **Exact-schema frontend tests** | Fixtures are **real T6 outputs** generated by the accepted code (R1 `CACHED`, R2, R3, R4, R5, R6, R7 bullish and bearish; TC6 cases 0–5), not hand-written JSON. The panel renders all of them without errors, including R1/R2 nulls and empty lists, optional TC4/TC6 extras, `null` veto results, and the list-valued `location`. A test fails if the renderer reads a path absent from §1.5. |
| **Timestamps shown** | "Evaluated" equals `result.evaluated_at_utc` (formatted), never `served_at_utc`; joined results show the leader's time with "shared computation"; "Data observed" equals `bars_retrieved_at_utc` |
| **States** | Each §5.6 state reached; an Outdated result is never labelled current (expiry, failed refresh, newer closed candle, chart not LIVE each tested alone) |
| Decisions | All three outcomes always show the fixed caption and `note`; forbidden words (buy, sell, entry, stop, take profit, size, order) absent from the panel; escaping of hostile strings |
| Concurrency (frontend) | Late answer for the old pair discarded; `restart()` aborts; no overlapping analysis requests |
| **Viewport** | After zooming and panning into history, analysis updates leave `chartViewState()` unchanged and don't increase `chartDraws` (unless I3); the history notice appears and is removed by Go Live |
| Regression | Existing 50 browser tests and the full suite on Python 3.11 and 3.13 |

**Live verification (L1 only, separate approval, never in CI):**
- 662-candle latency on H1/H4/D1;
- `data_age_at_evaluation_ms` distribution;
- T2 verdicts in market hours versus the weekend;
- bars-worker contention with chart polling;
- a first read-only T6 run on live data.

## 9. Security and read-only guarantees

- **Route and checks:** one new GET route; the existing Host/Origin checks, CSP and error
  handling apply.
- **No order functions:** the service calls only `LiveMarketService.snapshot` and the pure T6
  function; no order, position or account function is reachable.
- **No new data or providers:** no collectors, no writes to `data/`, no persisted results, no
  new providers, ML, AI or news.
- **Response contents:** no account identity; `actionable: false` and `note` rendered verbatim.
- **Contracts unchanged:** T2–T6 calculation rules and output contracts are unchanged. The
  envelope wraps but never edits the result.

## 10. Dependencies and prerequisites

1. **T1/T2 live verification (L1), a separate workstream that is still outstanding.**
   Accepting T6 doesn't establish live-data correctness. Before any live T6 result is shown to
   users, verify read-only against the running terminal, with your permission for any backend
   restart:
   - per-bar `closure` evidence at candle boundaries;
   - duplicate and order checks, and spreads;
   - UTC conversion at a real close and across the weekly reopen;
   - the tick and bars retrieval gap stays within T2's 2 s, with clock checks `verified` in
     market hours and `stale_ticks` at weekends;
   - snapshot verdicts under live, quiet and MT5-down conditions;
   - at least 662 closed candles on H1/H4/D1 for each watchlist pair (D1 about 824, about 688
     without Sundays);
   - **real 662-candle latency** and `data_age_at_evaluation_ms` against the 5 s budget.
2. **Your decisions D1–D6** (§12).
3. **No change** to the T2–T6 modules or tests.

## 11. Implementation stages and acceptance criteria

| Stage | Scope | Acceptance criteria |
|---|---|---|
| **I1: backend service and endpoint** | `market_analysis.py`; application wiring (shared clock); `api.py` route and handler; docstring fix (C9); backend tests | (a) **no completed-result reuse:** sequential requests always make new snapshots (tested); (b) **single-flight:** N concurrent requests give one `snapshot()` call and identical results with the original `evaluated_at_utc` (tested); (c) in-flight record cleared after success and after failure (tested); (d) **freshness boundary** 5.000 s evaluated / 5.001 s R1 through the endpoint (tested); (e) no retry within a request; backoff passes through to R1 (tested); (f) `result` deep-equal to a direct T6 call; envelope exactly §3.3, including timing fields; (g) **no file under `src/johanna/analysis/` and no T2–T6 test changed** (`git diff`); (h) full suite green on 3.11 and 3.13; Ruff clean |
| **I2: dashboard panel** | `app.js` state, poller, renderers; `styles.css` in the existing palette; browser tests | (a) **exact-schema tests** on real T6 outputs for R1–R7 and TC6 cases 0–5, with no renderer access to paths absent from §1.5; (b) the **original `evaluated_at_utc`** is shown as the evaluation time and never `served_at_utc`; joined results labelled shared; (c) **no false representation:** every Outdated / Diagnostic / Unavailable condition tested, and an outdated result is never labelled current; (d) the fixed caption and `note` on every outcome; no forbidden words; escaping; (e) **no chart viewport change** and no extra chart draws from analysis updates (tested); (f) late-response and overlap protection (tested); (g) existing 50 browser tests unchanged and passing; full suite green; (h) **no T2–T6 change** |
| **I3 (optional)** | Chart marker; history notice | Marker only for a matching key; viewport unchanged; notice tested |
| **L1 (separate, needs explicit approval)** | §10 item 1, then a first read-only live T6 run | Evidence report like T0; read-only proxy; no trades; any backend restart only with your permission |

Each stage ends with a report and a stop.

## 12. Risks, open questions, decisions for approval

**Risks:**
- **R1. Perception:** a candidate may be read as advice. Mitigated by neutral styling, the
  fixed caption and no order affordances.
- **R2. Unverified live latency (C10):** if a real 662-candle read approaches 5 s, many results
  will be R1 (stale). That fails closed, but would make the feature unusable until addressed
  (for example a smaller window on H4/D1, which would need a T6 parameter decision). It's
  measured in L1.
- **R3. MT5 contention:** one extra 662-candle read per 30–60 s per selection, alongside
  chart polling. Measured in L1.
- **R4. Payload:** about 21 KB per result. Acceptable.
- **R5. D1 margin:** about 688 Sunday-excluded candles against the 662 needed.
- **R6. Live data unverified:** T6 results on live data mean nothing until L1.
- **R7. Historical analysis expectations:** out of scope, and stated in the UI.

**Decisions requiring approval:**

| # | Decision | Proposal |
|---|---|---|
| D1 | Dedicated `GET /api/market/analysis`, envelope as in §3.3 | Yes |
| D2 | Frontend cadence 30 s LIVE / 60 s otherwise, plus selection and new-candle triggers; `ANALYSIS_EXPIRE_MS` = 65 s as **display policy only** | Yes |
| D3 | Single-flight per key, **no completed-result reuse** | Yes (revised) |
| D4 | Optional chart marker (I3) | Your choice |
| D5 | Sunday D1 candles: keep raw (default) or exclude via `excluded_opens` | Keep raw; revisit after L1 |
| D6 | Order: I1 → I2 on synthetic data; L1 before any live use | Yes |

**Open technical questions:**
- **Panel placement:** Overview (compact) and Charts (full), as proposed, or only one?
- **Raw JSON view:** whether to offer a read-only "copy JSON" view of `result` for auditing.
- **Logging:** whether the INFO timing line should be kept after L1 or reduced to DEBUG.

## 13. Changes in revision 2

| # | Change | Review item |
|---|---|---|
| 1 | **Completed-result reuse removed** (`reuse_seconds` and `reused` deleted). Single-flight kept: concurrent requests share one computation and keep its original `evaluated_at_utc`. In-flight records are cleared in `finally` after success and failure, and exceptions go to every waiter without being cached. §2.3 explains why single-flight suffices at the cadence. | 1 |
| 2 | C1 reworded: the live service's 5 s bars cache can serve the same MT5 observation, but T2 judges its real age, so it can only be refused, never falsely accepted | 1 |
| 3 | Freshness model rewritten (§5.6): five distinct quantities; exact state conditions; `ANALYSIS_EXPIRE_MS` defined as display policy only; browser and backend clocks never compared; the evaluation time always from `result.evaluated_at_utc` | 2 |
| 4 | Latency and failure (§4.3, §4.4, C10): 662-candle latency stated as unverified; fail closed; no retries; existing timeout, worker lock and backoff preserved; envelope `timing_ms`, `data_age_at_evaluation_ms`, `bars_served_from_cache`; one INFO log line | 3 |
| 5 | **Actual T6 schema verified** (§1.5) by source inspection and real outputs. Corrected: the `evaluated_at_utc` format (whole seconds or microseconds; the earlier `…04.25Z` was a format the code never emits); R1 nulls (`as_of_utc`, `market_condition`, `opportunity`, `features`); R2 opportunity (`not_evaluable`, empty checklist and vetoes, `validation` unavailable entry); TC4 `candles`; TC6 extras, with `nearest_non_qualifying_level` only in cases 1–3; V2 `pivots`; cautions only with a direction; payload about 21 KB (not 30–60 KB). Examples A–C and the rendering plan (§5.3) updated to match. | 4 |
| 6 | I1 and I2 acceptance criteria extended: no reuse, single-flight, cleanup, freshness boundary, exact-schema tests on real outputs, original evaluation time shown, no false current state, no viewport change, no T2–T6 change. L1 kept separate and subject to explicit approval. | 5 |
| 7 | **(after I1)** Final `window_validation` contract recorded (§3.4), implemented via the approved additive `decision.evaluate_market_snapshot_with_validation`. §7 corrected: insufficient history is R1 (T4 completeness), not R3/R5, through this endpoint. | T4 diagnostic exposure |

## 14. I2 implementation addendum (dashboard; implemented)

**Files changed:** `src/johanna/web/app.js` and `src/johanna/web/styles.css`, plus the browser
tests. No backend, analysis, API, wiring or test of T2–T6 changed.

**Panels:**
- **Overview, compact:** the existing "Analysis & setups" slot (`#analysis-compact-body`).
- **Charts, full:** under the chart (`#analysis-full-body`).
- **Trading Signals page:** unchanged.

**Polling:**
- **Infrastructure:** the existing `createPoller` gains two backward-compatible features:
  - an optional `active()` predicate (when inactive it neither requests nor schedules);
  - `retime()`, which brings a pending poll forward when the interval shrinks.
- **Cadence:** 30 s while the chart is LIVE, 60 s otherwise.
- **Immediate:** on a symbol or supported-timeframe change (old answers aborted or discarded).
- **Early:** when a newly closed chart candle is seen.
- **Suspended:** while the tab is hidden, on pages without a panel, and for unsupported
  timeframes. Nothing is ever requested for M1–M30.

**Status:**
- **Order:** Unsupported, Loading, Error, then R1 Unavailable, R2 Provisional, then Current or
  Outdated for R3–R7. R1 and R2 are never shown as Current.
- **Outdated when any of:** a failed refresh, more than 65 s since browser receipt
  (presentation policy only), a newer closed chart candle than `inputs.window.last_t`, or the
  chart not being LIVE.

**Isolation:** analysis rendering writes only the two panel containers. It never calls
`drawChart` or touches viewport or chart state; this is verified in a browser test with a
`drawChart` spy.

**Scope note:**
- **Why:** the Charts page now scrolls, which narrows the chart canvas. That exposed a latent
  rounding drift in the existing `zoomChart`: the cursor anchor could move 2 candles after 3
  steps at some widths.
- **Fix:** one line. `zoomChart` now uses the same floor convention as `candleAtX`.
- **Result:** the existing zoom test passes unchanged.
