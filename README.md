# Johanna

Johanna is an educational research project for a Forex trading assistant. The
repository includes read-only MT5 market-data access and local data storage; it does
not include order execution.

## Persistent backend foundation

Johanna now has a central backend application entry point, validated TOML and
environment configuration, structured JSON logs, service lifecycle management,
and a small HTTP API for a separately built frontend. Start it with:

```powershell
python -m johanna --config .\johanna.example.toml
```

The backend serves a dark, responsive workstation at `/`, with the Overview,
Charts, Markets, Economic Calendar, News, Trading Signals, Trading Rules, Risk
Management, Positions, Trade History, Analytics, and Settings workspaces. It
exposes `GET /health`, `GET /status`, and `GET /api/dashboard`; workstation
preferences are saved through `POST /api/preferences`. By default it binds to
`127.0.0.1:8765`; override settings with `JOHANNA_SERVER_HOST`,
`JOHANNA_SERVER_PORT`, `JOHANNA_DATA_DIR`, and `JOHANNA_LOG_LEVEL`. Integration
settings live in `[integrations]` in the TOML example. Secrets remain in process
environment variables such as `TWELVE_DATA_API_KEY`, `FRED_API_KEY`, and future
`GEMINI_API_KEY`; the backend does not serve or log them.

The application composition root defines separate lifecycle boundaries for
market data, calendar, news, Gemini intelligence, signals, rules, risk, broker
execution, and persistence. Persistence starts first and stops last. Market data
and the economic calendar are real services (`johanna.data_services`) over the
existing stores: the dashboard reads the same Parquet bars, manifests, collector
progress files and weekly calendar snapshots that the collectors write, and
reports `fresh`/`stale`/`no_data` from the last successful collection (market data
is stale 15 minutes after the last success; calendar snapshots follow the
adapter's 6-hour rule). News, Gemini, signals, rules, risk and execution remain
explicit not-implemented placeholders.

Market monitoring is off unless you point the backend at the existing collector
configuration with `integrations.collectors_config = "collectors.toml"` (or
`JOHANNA_COLLECTORS_CONFIG`). Then the persisted *Market monitoring* preference
starts and stops the read-only MT5 collector as a supervised background worker,
using that file's symbols, timeframes and output directories. Without it, the
dashboard shows *Monitoring: Not configured* and serves stored data only. If
`calendar_enabled` and `forex_factory_authorized` are both true, the calendar
collector runs the same way. Worker failures are logged with a traceback, shown
in the dashboard, and make `GET /health` return `503`.

Only one collector runs per source at a time. The standalone CLI and the backend
monitoring worker both take an OS-level lock on `<state_dir>/<source>.lock`
(`mt5.lock`, `calendar.lock`) for the whole run. A second attempt for the same
source is refused: the CLI exits with code `3`, and the backend shows
*Monitoring: Failed* with `CollectorBusyError` until you stop the other collector
and switch monitoring off and on. Different sources never block each other. The
operating system releases the lock if the holder crashes or is killed, so a
leftover `.lock` file never blocks later runs.

### Live market workspace

The dashboard reads quotes and candlestick bars **directly from the running MT5
terminal on demand**; no collector is needed. Quotes are cached for 1 s and bars
for 5 s. Each value is labelled `LIVE`, `STALE` (market closed or quiet),
`CACHED` (MT5 unavailable; shown earlier or from the Parquet archive) or
`UNAVAILABLE`. MT5 runs in separate worker processes, because the MetaTrader5
package blocks every Python thread while a call runs. Johanna never starts the
terminal, never trades, and reads only quotes, bars, symbol properties and the
server identity (company, server name, account type). Endpoints:
`GET /api/market/quotes[?symbols=EURUSD,...]` and
`GET /api/market/bars?symbol=EURUSD&timeframe=H1&count=300`.

MT5 timestamps are the trade server's wall clock. Set it with
`[integrations] mt5_server_time_zone` (IANA) and `mt5_server_time_shift_hours`
(default `UTC`/`0`, correct for GMT+0 servers such as Exness). Johanna checks the
configured clock against every live tick. A tick that lands in the future is
refused as `server_clock_mismatch`, so shifted times are never shown. See
`docs/live-market-workspace-report.md`.

> **Archived bars:** the 793 bars under `data/collectors/mt5` were collected from
> the MetaQuotes-Demo server and are stamped in that server's time (UTC+3 during
> US daylight saving, which covers their whole range, 23–30 Oct 2026). They're
> kept unmodified. When shown as a CACHED fallback, the chart says their times
> are server time, not UTC.

`GET /health` returns `200 {"status":"ok"}` only while the application is running
with persistence available and no service in an error state. Only one backend can
use a data directory (`data/johanna.lock`) or port at a time. No strategy or
order execution is implemented, and autonomous execution is reported disabled
regardless of configuration. Use a loopback bind unless a separate authenticated
deployment boundary is provided; the foundational API has no remote-user
authentication. It rejects requests whose `Host` is not a loopback name or the
configured host (DNS rebinding), cross-origin browser requests, and preference
writes that are not `application/json`.

The backend stores mutable workstation preferences, ordered watchlists, the
effective non-secret configuration snapshot, service settings, provider update
times, and JSON cache entries in `data/johanna.sqlite3`. A preference update is
validated in full and written in one transaction, so a rejected request changes
nothing. Market-data and calendar update times are not copied into SQLite: their
system of record is the Parquet manifest, the collector progress files under
`data/collector_state/`, and the calendar snapshot files. SQLite schema changes are
versioned with `PRAGMA user_version`; corrupt databases are preserved under a
`.corrupt-<UTC timestamp>` name and recreated with safe defaults. Existing
historical bars remain in Parquet, and the economic-calendar file cache remains
available to its current adapter. API keys and broker credentials are read from
the process environment and are rejected from persisted preferences/configuration.

## Week 1–2 scope

This foundation covers timezone-safe timestamps, a canonical OHLC bar schema and
Parquet store, read-only market-data adapters, authorized calendar and FRED clients,
local historical-file ingestion, collection commands, and data-quality reporting.
It does not implement strategies, forecasting, machine-learning models, or trading
orders. See [the Week 1–2 operations guide](docs/week-1-2-operations.md) for the
full setup, configuration, commands, and current limitations.

## Windows setup

Install Python 3.11 or newer and Git. From PowerShell, open the repository directory
and run:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

If PowerShell blocks activation, use the venv interpreter directly instead:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

`.env.example` documents environment-variable names only; the project does not load
`.env` files automatically. The Twelve Data and FRED adapters read credentials from
the process environment. Configure those variables in the current PowerShell session
or Windows user environment when you use the corresponding adapter. Keep real
credentials out of source control.

## Checks

Run the tests and lint checks from the repository root:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

The current tests are local and offline. They do not connect to a broker or call any
external API.

`tests/test_browser_smoke.py` drives the real dashboard in a headless Edge or
Chrome through the DevTools protocol (standard library only; no Playwright or
Selenium). It covers loading, navigation, settings persistence, monitoring
status, market and calendar rendering, empty, stale and failed states, and
400px/1600px layouts. It is skipped automatically when no Chromium-family browser
is found. Point `JOHANNA_BROWSER` at a browser executable to choose one, or run
`python -m pytest -m "not browser"` to exclude these tests.

## Read-only MT5 adapter (Windows)

Install the optional official MetaTrader 5 Python package in the project environment:

```powershell
python -m pip install -e ".[dev,mt5]"
```

Open the locally installed MetaTrader 5 terminal and sign in to the intended demo
account yourself. Then run the read-only smoke test with the exact symbol name shown
by that broker (including any suffix):

```powershell
python -m johanna.mt5_smoke --symbol EURUSD.a
```

The smoke test reports terminal and account connection status, lists visible
symbols, and requests a tick, completed M5 bars, and broker symbol specifications.
Captured specifications are saved under `data/broker_specs/` with UTC capture time
and provenance. Missing fields are recorded as `null`; broker server timezone is
left unknown unless established independently. The adapter does not accept account
credentials or expose order placement, modification, or closing methods.

The adapter follows the [MetaQuotes Python integration reference](https://www.mql5.com/en/docs/python_metatrader5).
MT5 rate timestamps are treated as UTC bar-open times, as documented for
[`copy_rates_from`](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py);
specification names correspond to [MetaQuotes symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants).

## Economic calendar status

The research plan names Forex Factory's weekly export, fetched a few times per
day and cached, with an MT5 calendar export as fallback. Forex Factory currently
links to `https://nfs.faireconomy.media/ff_calendar_thisweek.json` from the
calendar's Weekly Export controls, but its [notices and terms](https://www.forexfactory.com/notices)
prohibit copying calendar schedules without prior written consent and restrict
access to provided interfaces and instructions. The plan's existence of a
public JSON feed does not establish permission to collect and locally cache its
contents for this application. You have since confirmed that written permission
covers automated retrieval and local caching. The client can therefore be used
within that permission's scope; retain any limits stated in the permission.

`johanna.adapters.economic_calendar` implements a provider protocol, a
normalized event schema, duplicate removal, UTC conversion, and atomic weekly
JSON snapshots under `data/calendar/`. Missing event times and actual/forecast/
previous values remain `null`; naive event times require a provider-declared
IANA timezone. Cached snapshots report `fresh` or `stale`; when refresh fails,
an existing snapshot is returned as stale rather than being represented as
current. `ForexFactoryWeeklyJsonProvider` requests only the linked JSON file,
uses ETag/Last-Modified validators, has a 10-second default timeout, and stops
on 403/429 without retrying or bypassing the response. Automatic re-fetching is
limited to once per six hours by default.

The Forex Factory provider remains **disabled by default**. To use it, construct
it with `authorized_for_local_cache=True`; this is an explicit assertion by the
caller that written permission for retrieval and caching is in place. You have
confirmed that permission now covers both. The provider and HTTP handling tests
use synthetic mock responses only; no live request has been made, so live
connectivity and the current feed response have not yet been verified.

The suitable fallback is an authorized local CSV export produced by an MQL5
script using MetaTrader's built-in economic calendar. MetaQuotes documents
calendar functions such as `CalendarValueHistory`, but these APIs use broker
trade-server time (including its timezone and DST), not the computer's local
time. Any exporter must therefore include an established UTC offset or emit
offset-aware ISO timestamps before a Python provider can normalize events.
MetaTrader's official [economic calendar reference](https://www.mql5.com/en/docs/calendar)
documents those functions and their time basis. The current MetaTrader5 Python
package adapter does not expose the MQL5 calendar functions, so an MQL5 exporter
is still required; no calendar feed or exporter has been tested as an active
integration. Offline fixtures are synthetic and are never presented as real
events.

## Twelve Data client and credit limits

Set `TWELVE_DATA_API_KEY` in the process environment. This project does not load a
local `.env` file automatically. The key is sent in the HTTP
`Authorization` header and is not put in the URL or error messages. Tests inject
mock transports and never read a real key or send requests.

`johanna.adapters.twelve_data.TwelveDataAdapter` provides a timeout-protected,
bounded-retry JSON client and a validated `get_time_series` helper. Credit costs
are configured by endpoint; built-in defaults cover `time_series`, `quote`, and
`price` at one credit each. Add an explicit cost for any other endpoint. Every
network attempt, including a failed or retried attempt, reserves credits first;
the limiter does not refund a failed attempt because the provider may have
counted it.

The local limiter uses a rolling 60-second window and a UTC calendar-day
accounting window. Its defaults reserve at most 7 credits in any rolling minute
(8 cap minus 1 safety credit) and 790 per UTC day (800 cap minus 10 safety
credits). Both caps, margins, the UTC daily reset hour, and the clock/sleeper are configurable. The
rolling-minute rule is stricter than Twelve Data's documented credit refresh at
the start of each provider minute, so it is conservative rather than an exact
simulation of that provider window. Twelve Data currently documents the Basic
plan's 800-credit daily quota as resetting at 00:00 UTC; paid plans have no
daily cap. Configure the UTC reset hour to match a different accounting window or policy
change. These details are based on the [Twelve Data credits policy](https://support.twelvedata.com/en/articles/5615854-credits)
and [current pricing limits](https://twelvedata.com/pricing), checked on
October 25, 2026.

Reservations are persisted before each HTTP attempt in
`data/twelve_data_credit_ledger.sqlite3`. Processes on the same machine share this
ledger when they run from the same project directory. To share a different local
ledger, pass the same `state_path` to each `RequestCreditLimiter`. Requests fail
closed if the ledger cannot be read or written. Separate machines do not coordinate;
reliable locking on a network share is not assumed.

Example (after configuring the environment key):

```powershell
python -c "from johanna.adapters.twelve_data import TwelveDataAdapter; print(TwelveDataAdapter().get_time_series('EUR/USD', '5min', outputsize=5))"
```

## FRED macroeconomic observations

Create an API key in your [FRED account](https://fredaccount.stlouisfed.org/apikeys).
The client reads `FRED_API_KEY` from the process environment. `.env.example`
lists the variable name, but this project does not automatically load `.env`;
configure the value in your shell, Windows environment settings, or a local
secrets manager. Never commit the real key. The FRED API v1 requires the key as
a request parameter; the adapter does not include request URLs or response bodies
in its errors.

`johanna.adapters.fred.FREDAdapter` takes explicit series IDs and inclusive
observation date ranges. It requests series metadata for units and validates the
JSON response. Provider value `.` is preserved in `provider_value` and represented
as `value=None` with `missing_value_marker="."`. Other values are parsed as
`Decimal`. Retrieval time is UTC.

Observation dates identify the period being measured. They are not publication
times. FRED's [observations endpoint](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
returns observation dates and real-time vintage date fields, but not an exact
publication timestamp. Accordingly, the adapter records `publication_timestamp_utc`
as unknown and does not treat a vintage date as an intraday availability time.
Use a separate, authoritative release source to establish when a value became
available before using it in time-sensitive analysis. The client has bounded
retries and a configurable timeout; automated tests inject mocked responses and
do not need an API key.

Example from PowerShell after `FRED_API_KEY` is configured in the process:

```powershell
python -c "from datetime import date; from johanna.adapters.fred import FREDAdapter; rows=FREDAdapter().get_observations('UNRATE', date(2024,1,1), date(2024,12,31)); print(f'{len(rows)} observations')"
```

## Historical file ingestion

`python -m johanna.ingestion` imports a local, user-supplied OHLC file into the
existing `ParquetBarStore` under `data/clean/`. It never downloads or copies the
input; the original remains at its supplied path. The manifest records the
source file name and SHA-256, acquisition details when supplied, UTC date filter,
ingestion time, row count, and up to 100 row validation errors. Re-running the
same input is idempotent through the existing store's deterministic upsert and
interrupted-ingestion recovery. A UTC date filter is inclusive by UTC calendar
date.

Supported inputs:

- **MT5 text bars:** MetaQuotes documents the columns `Date Time Open High Low
  Close TickVolume Volume Spread`; timestamps are period-start times per its
  `MqlRates` definition. Pass the exact symbol, timeframe, and an explicitly
  established IANA source timezone. Tick and real volume are stored separately;
  spread is validated but not stored in the current canonical schema.
- **HistData Generic ASCII M1:** semicolon-delimited rows with
  `YYYYMMDD HHMMSS;Open;High;Low;Close;Volume`. HistData documents its clock as
  fixed EST (UTC-05:00) without daylight-saving adjustment. Its timestamp
  convention is not explicit in the file specification, so the importer requires
  `--timestamp-convention bar_open` as an operator assertion. HistData says it
  removes volume information from delivered data; the column is validated as a
  non-negative integer placeholder and mapped to neither canonical volume field.
- **Dukascopy:** not currently supported. Dukascopy's official documentation
  describes its CSV/Bid/Ask/tick-data exports and a `.bi5` tick decoder, but we do
  not have a verified, stable OHLC bar-file column and timestamp contract for a
  direct mapping to this bar schema. No importer guesses a schema or converts
  ticks to bars.

These formats and timestamp/volume details are based on the [MetaTrader 5 custom
history import specification](https://www.metatrader5.com/en/terminal/help/trading_advanced/custom_instruments),
[MqlRates reference](https://www.mql5.com/en/docs/constants/structures/mqlrates),
[HistData Generic ASCII specification](https://www.histdata.com/f-a-q/data-files-detailed-specification/)
and [HistData volume FAQ](https://www.histdata.com/f-a-q/), and [Dukascopy's
historical-data guide](https://www.dukascopy.com/wiki/en/development/data-export/).
Check your own download or broker terms; the importer cannot verify that a file
was legally obtained or that its timezone declaration is correct.

The checked-in `tests/fixtures/synthetic_*.csv` files contain synthetic values
only; they are not market observations. To try the MT5 importer against that
synthetic fixture without touching the normal dataset:

```powershell
python -m johanna.ingestion --file .\tests\fixtures\synthetic_mt5_bars.csv --format mt5 --symbol EURUSD --timeframe M1 --source-timezone UTC --acquired-from "synthetic test fixture" --license-note "synthetic data" --root .\data\synthetic_demo
```

For a real local MT5 export, substitute your file and use its verified timezone
and acquisition details; select a date range if needed:

```powershell
python -m johanna.ingestion --file .\input\EURUSD_M1.csv --format mt5 --symbol EURUSD --timeframe M1 --source-timezone UTC --start-date 2024-01-01 --end-date 2024-12-31 --acquired-from "broker history export" --acquired-at 2025-01-05T12:00:00Z --license-note "personal research permission" --root .\data
```

HistData files require the timestamp convention to have been independently
established before passing the explicit flag:

```powershell
python -m johanna.ingestion --file .\input\DAT_ASCII_EURUSD_M1_202401.csv --format histdata-ascii-m1 --symbol EURUSD --timeframe M1 --timestamp-convention bar_open --start-date 2024-01-01 --end-date 2024-01-31 --acquired-from "authorized HistData download" --root .\data
```

## Data collectors

Create a local config from the example, then edit its symbols, timeframes,
intervals, retry bounds, and output/state directories:

```powershell
Copy-Item .\collectors.example.toml .\collectors.toml
```

Unknown settings and unsupported MT5 timeframes are rejected. The MT5 collector
uses the existing read-only adapter and `ParquetBarStore`; each source writes to
its own output subtree and progress file, so run them independently if one source
is unavailable. Progress includes per-target status, received rows, last attempt,
last successful retrieval, and the last error. Repeated bar windows are safe to
re-ingest because the store de-duplicates by symbol, timeframe, timestamp, and
source. Before a write, the collector checkpoints the exact pending bar batch in
its local progress file; after restart it replays that batch before requesting
new bars, allowing the store's interrupted-write recovery to finish it.

When `mt5.collect_ticks = true` (the default), each poll also saves one snapshot
of the latest quote per configured symbol to a separate UTC Parquet store under
`data/collectors/mt5_ticks/`. This is sampled polling, not a lossless tick stream;
quotes arriving between polls are not captured. Tick snapshots have their own
schema, manifest, idempotent writes, progress status, and pending-write recovery.
A tick failure is reported per symbol and does not prevent bar collection.

The calendar collector uses `CachedEconomicCalendar` and the weekly JSON snapshot
cache. Its example polls every 15 minutes but will not fetch the feed more often
than the configured six-hour minimum. It is disabled by default. Enable it only
with `calendar.enabled = true`; the Forex Factory provider additionally requires
`calendar.authorized_for_local_cache = true`, which asserts that written permission
covers automated retrieval and caching. Calendar failures leave the existing
snapshot stale and visible in state; they do not affect MT5 storage. No Twelve
Data polling loop is included, so the collector cannot generate Twelve Data API
calls; any future Twelve Data collector must use its existing credit-limited
adapter.

Run one offline synthetic cycle, or one live read-only MT5 cycle:

```powershell
python -m johanna.collectors --config .\collectors.example.toml --source mt5 --dry-run --once
python -m johanna.collectors --config .\collectors.toml --source calendar --dry-run --once
python -m johanna.collectors --config .\collectors.toml --source mt5 --once
```

For the calendar dry run, set `calendar.enabled = true` in the copied local config;
the synthetic provider does not require Forex Factory permission or make a network
request.

For continuous MT5 collection, leave this command running in its own PowerShell
window. Run the calendar separately in another window after enabling it in the
config:

```powershell
python -m johanna.collectors --config .\collectors.toml --source mt5
python -m johanna.collectors --config .\collectors.toml --source calendar
```

Press **Ctrl+C** in that collector's PowerShell window to request graceful
shutdown. The MT5 adapter closes its terminal connection after finishing the
current poll. Outputs go under `data/collectors/mt5/`,
`data/collectors/mt5_ticks/` and `data/collectors/calendar/snapshots/`; progress goes under
`data/collector_state/`. Dry-run events and bars are clearly marked synthetic and
do not access MT5, Forex Factory, or Twelve Data.

These are new collectors, not a claim of unattended reliability. They have not
been run unattended for three consecutive days; reliability remains unverified
until that duration has actually been observed.

## Market-data storage

The clean-bar schema is versioned in `johanna.market_data.BAR_SCHEMA`. It stores
symbol, timeframe, timezone-aware UTC bar-open timestamp, OHLC prices, nullable
`tick_volume` and `real_volume`, source, source timezone, and the declared source
timestamp convention. Tick volume counts price updates; real volume records traded
quantity when supplied. One is never substituted for the other. The clean store
validates timestamps and OHLC consistency, sorts deterministically, and writes
Zstandard-compressed Parquet partitions under `data/clean/` by
symbol/timeframe/year/month. `data/raw/` is separate for future immutable source
captures; raw captures are not produced until source adapters are implemented.

Each clean-store snapshot has `data/clean/manifest.json`, recording the latest
ingestion source and UTC time, the dataset's UTC date range, schema version, row
count, and whether an ingestion completed. A repeated identical ingestion does not
add duplicate rows. Temporary writes are replaced atomically; an interrupted
multi-partition write is marked `in_progress` with a digest of its input. Retry that
exact batch before starting another ingestion.

After a collector writes data, inspect the manifest and Parquet schema from
PowerShell:

```powershell
Get-Content .\data\clean\manifest.json
python -c "import pyarrow.dataset as ds; d=ds.dataset('data/clean', format='parquet', exclude_invalid_files=True); print(d.schema); print(d.to_table().slice(0, 5).to_pandas())"
```

For sampled MT5 ticks, inspect `data/collectors/mt5_ticks/clean/manifest.json`
and its Parquet schema with:

```powershell
Get-Content .\data\collectors\mt5_ticks\clean\manifest.json
python -c "import pyarrow.dataset as ds; d=ds.dataset('data/collectors/mt5_ticks/clean', format='parquet', exclude_invalid_files=True); print(d.schema); print(d.to_table().slice(0, 5).to_pandas())"
```

## Market-data quality report

Run a read-only audit of every clean Parquet partition and write both a terminal
summary and JSON report:

```powershell
python -m johanna.quality --root .\data --json-out .\data\quality_report.json
```

The report includes manifest status/provenance, observed symbols, timeframes and
date range, schema and OHLC violations, duplicate/non-monotonic timestamps,
optional-volume null counts, and gaps. It records unreadable files and invalid
rows with locations and reasons; it never edits or silently removes them. The
canonical schema has no spread field, so suspicious-spread checks are reported as
not applicable unless a source file actually contains that field; no arbitrary
spread threshold is assumed.

Johanna does not currently have a configured FX session/holiday calendar. Gaps
therefore remain unresolved unless an operator supplies explicit, UTC-aware
closure intervals with an evidence description in a JSON list, for example:

```json
[
  {"start":"2025-01-06T10:01:00Z","end":"2025-01-06T10:02:00Z","evidence":"documented venue maintenance window"}
]
```

Pass that file with `--closure-evidence .\closures.json`. Only a gap fully covered
by a supplied interval is classified as an expected closure. The current schema
does not preserve a source filename per bar, so attribution of a gap to a
historical-file boundary or collection interruption remains limited and is
explicitly called out in the JSON. Ingestion freshness measures manifest age; it
does not claim that historical bars should be current. The default threshold is
48 hours and can be changed with `--stale-after-hours`.

If no local dataset exists, the report says so. To exercise the reproducible
synthetic test path only, run `pytest tests/test_quality.py`; test records are not
real market data and are not stored under `data/`.

The current tests use small synthetic bars only. They are not market observations
and are not written into the repository's data directory.
