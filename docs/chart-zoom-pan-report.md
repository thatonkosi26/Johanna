# Chart zoom, pan and Go Live: report

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed**

**Scope:** frontend only. No backend, MT5, collector or `data/` change. No charting library was
added: the existing canvas renderer supports zoom and pan with a small viewport model, so a
dependency wasn't justified.

## 1. Files changed

| File | Change |
|---|---|
| `src/johanna/web/app.js` | **Viewport model:** `chartView()`, `defaultView()`, `resolveViewport()`, `setViewport()`, `lastIndexAtOrBefore()`, `goLive()`, `chartGeometry()`, `viewControlsMarkup()`, constants `CHART_PAD`, `MIN_VISIBLE_CANDLES`, `MIN_SLOT_PX`, `DEFAULT_SLOT_PX`, `PRICE_SCALE_MIN` / `MAX`, `BAR_HISTORY_LIMIT`. **Interaction:** `zoomChart()`, `candleAtX()`, `chartViewState()`, plus document-level `wheel`, `pointerdown`, `pointermove`, `pointerup`, `pointercancel`, `lostpointercapture` and `dblclick` handlers (each registered once). **Changed:** `drawChart()` (draws the viewport, applies the price scale, clips to the plot), `mergeBars()` (keeps candles older than the refreshed window), `chartMarkup()` (view-state overlay and hint line), the click handler (`#go-live`), `state` (`view`, `chartViewport`). |
| `src/johanna/web/styles.css` | Canvas `touch-action` / `user-select` / cursor; view-state badge; Go Live button (theme colours); hint line |
| `tests/test_browser_smoke.py` | 13 new tests (§7). One existing assertion updated: after a new candle the chart holds **301** candles instead of 300, because the oldest is now kept. That's stricter: it also proves no candle is lost. |

**Bug fixed along the way:**
- **What was wrong:** inside `drawChart()`'s candle loop, a local `width` (the candle-body
  width) shadowed the canvas width. The time-axis labels were positioned with it and ended up
  off-canvas. I introduced this in the earlier remediation round, when adding hollow forming
  candles.
- **Fix:** labels are now drawn in their own pass with the correct width.
- **Test:** `test_theme_and_timezone_changes_keep_the_viewport_and_time_labels` checks that
  labels are drawn.

## 2. Zoom

- **Wheel up** zooms in (fewer, wider candles); **wheel down** zooms out. Each step changes the
  visible candle count by a factor of 1.25. The page doesn't scroll while the cursor is over the
  chart.
- **Centred on the cursor:** the fractional candle position under the cursor is kept at the same
  screen position, give or take one candle from rounding (tested at 30 % and 70 % of the width).
- **Limits:**
  - **Most zoomed in:** 10 candles.
  - **Most zoomed out:** never more candles than are loaded, and never closer than 2 px between
    candle centres, so candles can't overlap.
  - **Body width:** stays between 1 and 12 px.
- **Data untouched:** zoom changes only which candles are drawn and how wide; the candles
  themselves are never changed.

## 3. Pan

- **Click and drag on the plot**, using pointer events with pointer capture:
  - **Dragging left** moves towards older candles, and **dragging right** towards newer ones, as
    the task specifies.
  - That's the "scroll" convention. Some charting tools use the opposite "grab" convention.
- **Smooth:** the movement follows the pointer in whole-candle steps (1 candle per candle-width
  of pointer movement).
- **Clamped:** panning stops at the oldest loaded candle and at the newest.
- **Safe input handling:**
  - text selection and native dragging are prevented (`preventDefault`, `touch-action: none`,
    `user-select: none`);
  - releasing the pointer outside the chart still ends the drag, thanks to pointer capture and
    `lostpointercapture`;
  - if the page re-renders mid-drag, the drag ends cleanly.
- **Cursors:** `grab` over the plot, `grabbing` while panning, `ns-resize` over the price axis.
- **No interference:** the pair and timeframe selectors, and every other control, sit outside the
  canvas.

## 4. Price-axis scale

- **Dragging vertically on the price axis** (the right-hand 70 px) scales the visible price
  range around its centre:
  - **down** widens the range;
  - **up** narrows it.
  - **Limits:** 0.1× to 10× the automatic fit.
- **Double-click** on the axis returns to the automatic fit.
- **Centre:** the automatic fit still follows the visible candles, so panning keeps them
  vertically in view.
- **Prices unchanged:** only the price-to-pixel mapping changes, so prices and positions stay
  consistent, and OHLC values never change (tested by comparing all candles before and after).
- **Clipping:** candles and the live line are clipped to the plot area, which only matters when
  the user zooms the price range in. The live price tag is drawn only while the line is in view.
  Price labels stay at 5 evenly spaced levels.

## 5. Go Live

- **Following:** while the chart follows the newest candle, an unobtrusive **"Following latest"**
  badge sits in the chart's top-right corner.
- **Viewing history:** panning or zooming away from the newest candle switches to **"Viewing
  history"** and shows an accent-coloured **Go Live** button.
- **Go Live:** jumps to the newest candles and resumes following. The zoom level is kept.
  Panning back to the newest candle by hand also resumes following.
- **Separate from the feed status:** this view state is distinct from the market-data status. The
  chart can show "Viewing history" while its LIVE badge (MT5 feed) stays LIVE; both are tested.

## 6. Live updates and a manually adjusted view

- **Anchored by time:** the viewport is stored as the **open time of its right-most candle**, a
  candle count and a price scale, not as array positions.
- **No duplicates, nothing invented:** each refresh is still merged by open time via
  `mergeBars()`, so there are no duplicates and no interpolation.
- **Kept history:** candles that drop out of the backend's 300-candle window are kept, up to
  1,000, so a historical view never loses its candles.
- **Following:** the right edge stays on the newest candle. The forming candle updates, and a new
  candle scrolls into view.
- **Viewing history:** new and updated candles are merged into the data, but the visible window
  doesn't move. Data keeps updating; Go Live then shows the latest.
- **Repaints:** the canvas repaints only when something *visible* changes, so a new candle outside
  a historical view costs no repaint.
- **Preserved through:** quote refreshes, candle refreshes, dashboard snapshot refreshes,
  forming-candle updates, new candles and other dashboard updates.
- **Resize, theme and timezone:** the position and zoom level are kept. The candle count is kept
  unless the new width can't fit it.
- **Pair or timeframe change:** the view resets to the latest candles, following, with the
  automatic fit, so no viewport carries over to a different dataset.

## 7. Tests added

All in `tests/test_browser_smoke.py`, headless Edge against the fake MT5. Results come from the
verbose run in §8.

| # | Test | Covers | Result |
|---|---|---|---|
| 1, 2, 7 | `test_wheel_zooms_in_and_out_within_limits` | Zoom in narrows the range; zoom out widens it; limits: 10 candles minimum, never more than loaded, range always inside history | PASSED |
| 3 | `test_zoom_keeps_the_candle_under_the_cursor` | The candle under the cursor stays within one candle of its position (at 30 % and 70 % of the width) | PASSED |
| 4, 5 | `test_dragging_pans_to_older_and_back_to_newer_candles` | Drag left: older, Go Live shown; drag right: newer; back at the newest: following again, Go Live hidden; pan doesn't change zoom | PASSED |
| 7 | `test_pan_is_clamped_to_the_available_history` | Huge drags stop exactly at the oldest and newest loaded candles | PASSED |
| 6 | `test_price_axis_drag_changes_the_price_scale_not_the_prices` | Axis drag down widens and up narrows the price range; candle range unchanged; all OHLC identical; bounded at 0.1×; double-click resets | PASSED |
| 8 | `test_new_candles_do_not_move_a_panned_view` | Next M1 candle arrives while panned back: view unchanged, data grows by 1, no duplicates, Go Live still shown | PASSED |
| 9 | `test_forming_candle_keeps_updating_while_zoomed_in` | Zoomed and following: forming candle takes the new price; zoom kept; next candle comes into view | PASSED |
| 10 | `test_go_live_returns_to_the_latest_candles_and_follows` | Go Live returns to the newest candle and follows; zoom kept; feed LIVE badge independent of view state | PASSED |
| 11 | `test_switching_pair_resets_the_viewport` | After pan and price scale, switching pair gives the latest candles, following, automatic fit and default zoom | PASSED |
| 12 | `test_switching_timeframe_resets_the_viewport` | Same for switching timeframe | PASSED |
| 13 | `test_resizing_keeps_the_viewing_position_and_zoom` | Narrower window: same right-most candle, candle count, history mode and price scale | PASSED |
| 14 | `test_automatic_refreshes_keep_canvas_and_viewport` | Two or more quote and two or more candle refreshes plus a snapshot refresh: canvas not recreated, view unchanged | PASSED |
| 7 (theme/tz) | `test_theme_and_timezone_changes_keep_the_viewport_and_time_labels` | Theme and timezone changes rebuild the page, but the view is kept; time-axis labels are drawn (regression) | PASSED |

## 8. Complete test results

| Command | Result |
|---|---|
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8), first run | 308 passed, **1 failed**: `tests/test_collector_locking.py::test_cli_is_refused_while_another_collector_runs_same_source` (see below) |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8), re-run | **309 passed**, 0 skipped |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **309 passed**, 0 skipped |
| `tests/test_browser_smoke.py -v` (3.11.8) | **49 passed**: all 13 new tests and all 10 automatic-refresh tests |
| The new zoom/pan tests plus the automatic-refresh tests, repeated twice (3.13.3) | 23 passed, 23 passed |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 69 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |

There were 296 tests before this task; 13 are new.

**The one failure.** `test_cli_is_refused_while_another_collector_runs_same_source` failed once,
in the first full 3.11 run.
- **What the test does:** it gives a background dry-run collector thread 5 s to take a file lock
  (`tests/test_collector_locking.py:105`).
- **Unrelated to this task:** the test is from earlier work. It exercises only collector locking,
  which this task didn't touch, and it involves no frontend code.
- **Couldn't reproduce:** it passed 5 times out of 5 in isolation, and in the 3.13 full run and
  the 3.11 re-run.
- **Likely cause (not confirmed):** under full-suite load the thread missed the 5 s window.
- **Not changed:** I didn't modify the test, since widening its window would weaken it.

## 9. MT5 remains read-only

`grep -rnE "order_send|order_check|order_calc|positions_get|positions_total|orders_get|
history_orders|history_deals|TRADE_ACTION|\.login\(" src/ scripts/` found **no matches**.
No backend, MT5 adapter, worker or terminal-verification code changed. The new code is entirely
browser-side and only draws candles already received. No step of this task used the real
Exness account.

## 10. Collectors remain disabled

No collector code or configuration changed and no collector was run.

## 11. `data/` unchanged

- **Unchanged:** SHA-256, size and modification time of the 11 readable files under `data/` were
  identical before and after this task, and `data/` still holds 12 files.
- **Not hashed:** `data/johanna.lock` couldn't be hashed because your running backend holds it
  open. Its size and modification time were unchanged.
- **Your backend:** your running Johanna backend wasn't stopped, restarted or contacted. All tests
  used temporary directories and their own ports.

## 12. Known limitations

- **Loaded history only.** Panning and zooming cover the candles loaded so far: the 300 fetched
  plus older ones kept while the page is open, up to 1,000. Fetching deeper history would need a
  backend or API change, which is out of scope.
- **Whole-candle pan steps.** Panning moves in whole-candle steps. At the most zoomed-in level
  (10 candles) each step is wide; that's deliberate, so no partial candle is ever drawn.
- **Opposite convention.** Drag direction follows the task (left means older), which is the
  reverse of some charting tools' "grab" convention.
- **Mouse only.** There's no touch pinch-zoom; pointer dragging works for touch, but wheel zoom
  is mouse or trackpad only.
- **View not saved.** It's kept in memory only and returns to "Following latest" after a page
  reload.
- **Not verified live.** I haven't checked this against the live Exness feed. Your running
  backend serves the previous frontend until it's restarted and the page reloaded.
