# Week 1–2 setup and operations

This guide documents the implemented research foundation. Johanna currently
collects and validates data; it does not contain trading strategies, forecasts,
machine-learning models, or order execution.

## Windows development setup

Install Python 3.11 or newer and Git. In PowerShell, change to the repository
directory and create an environment using the selected Python interpreter:

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The first command should report Python 3.11 or newer. If activation is blocked by
PowerShell policy, run the environment's interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

With the environment activated, the regular checks are:

```powershell
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

The GitHub Actions workflow uses Python 3.11 and runs these lint, format, and test
checks. Automated tests use synthetic fixtures, mock HTTP transports, and a fake
MT5 module. They do not need a broker terminal, credentials, external network
access, or paid API requests.

## Environment variables and credentials

`.env.example` contains variable names with blank values; it is not a credentials
file. Johanna does not load `.env` files automatically. The active API clients read
these process variables:

| Variable | Used by | Notes |
| --- | --- | --- |
| `TWELVE_DATA_API_KEY` | `TwelveDataAdapter` | Sent in the HTTPS `Authorization` header. |
| `FRED_API_KEY` | `FREDAdapter` | FRED API v1 requires it as a query parameter; do not share request URLs or diagnostic captures containing it. |

`MT5_LOGIN`, `MT5_PASSWORD`, and `MT5_SERVER` are not read by the MT5 adapter. It
connects to the terminal account that the user has already opened and signed into.
`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` are placeholders for
possible future integrations; no current module uses them.

Set credentials through Windows user environment settings or in the PowerShell
process that will run the client. Do not type a real key into a command that will be
saved in shell history, commit `.env`, or paste secrets into logs or support output.
`.gitignore` excludes `.env`, common credential files, and the full `data/` directory.

## Read-only MetaTrader 5

The adapter requires Windows, the locally installed MetaTrader 5 desktop terminal,
and the optional official Python package. Install the package into the project
environment with:

```powershell
python -m pip install -e ".[dev,mt5]"
```

Open MT5 yourself and sign into the intended demo account. The adapter does not
accept a login or password and does not change account state. Find the exact symbol
name in the terminal's symbol list, including broker suffixes, then run:

```powershell
python -m johanna.mt5_smoke --symbol EURUSD.a
```

Replace `EURUSD.a` with the exact name shown by your broker. If `--symbol` is
omitted, the command chooses `EURUSD` when available, otherwise the first visible
symbol. The smoke test prints terminal/account connection booleans, visible symbol
names, and sample tick/M5 data when available. A successful connection is reported
only after the adapter observes both terminal and account connectivity.

Symbol specifications are saved as JSON under `data/broker_specs/` (for example
`data/broker_specs/EURUSD.a.json`). The capture includes the exact broker symbol,
UTC capture time, provenance, and available contract, price-step, volume,
execution/filling, stop, and freeze fields. Unavailable fields remain `null`; the
broker server timezone remains unknown unless independently established. The local
file may include a broker server name, so keep it private. The smoke command does
not print login or password fields. See `python -m johanna.mt5_smoke --help` for
`--terminal-path` and `--metadata-dir` options.

The MT5 adapter is read-only. It exposes market-data methods, not order placement,
modification, or closing methods. Automated tests use a fake MT5 module. Run the
manual smoke test against a demo terminal before relying on live broker data. On
October 25, 2026, the repository smoke command reported terminal and account
connected, listed 12,260 symbols, returned an EURUSD tick and five M5 bars, and
saved broker metadata. The sample tick timestamp was October 23 UTC, so this
verified connectivity but not a quote updated during the Sunday check. The broker
server timezone remains unknown.

## Twelve Data

Set `TWELVE_DATA_API_KEY` in the process environment, then use
`johanna.adapters.twelve_data.TwelveDataAdapter`. For example, with the key already
available in the process:

```powershell
python -c "from johanna.adapters.twelve_data import TwelveDataAdapter; print(TwelveDataAdapter().get_time_series('EUR/USD', '5min', outputsize=5))"
```

The default local limiter permits at most **7 credits in any rolling 60 seconds**
and **790 credits in its configured UTC daily window**, leaving safety margins below
the configured 8/minute and 800/day caps. Costs are configurable by endpoint. Each
network attempt reserves credits before sending, including attempts that fail; the
limiter does not refund a reservation. This rolling-minute model is deliberately
conservative and is not an exact emulation of the provider's minute refresh. Check
the plan and provider policy for your account before use. The provider's current
published daily reset information and the policy-check date are documented in the
[README Twelve Data section](../README.md#twelve-data-client-and-credit-limits).

Credit reservations persist in `data/twelve_data_credit_ledger.sqlite3`. Separate
processes launched from the same project directory share this file, including
after restart. Configure a common `RequestCreditLimiter(state_path=...)` for
processes launched from different working directories. Ledger access errors reject
the request before HTTP. Separate computers are not coordinated by this local
SQLite ledger.

Automated tests inject mocked HTTP responses and a deterministic clock. They never
use the user's key or spend credits. A user-run EUR/USD price request earlier in
this project conversation returned a price; that manual check was not repeated
during this documentation pass.

## Economic calendar and FRED

### Calendar

The calendar adapter normalizes events, converts explicitly zoned timestamps to
UTC, deduplicates events, and caches weekly JSON snapshots. The Forex Factory
provider requests only the publicly linked weekly JSON export. The adapter is
disabled unless constructed with `authorized_for_local_cache=True`; only enable it
when written permission specifically covers automated retrieval and local caching.
The repository's example collector configuration leaves calendar collection
disabled and authorization false.

To exercise the mock without network access, first copy the example config and set
`[calendar] enabled = true` (leave authorization false for dry-run):

```powershell
Copy-Item .\collectors.example.toml .\collectors.toml
python -m johanna.collectors --config .\collectors.toml --source calendar --dry-run --once
```

The dry-run uses synthetic events. To use the live provider, set
`authorized_for_local_cache = true` only after checking the written permission,
then run the collector without `--dry-run`. The client has a 10-second default
timeout, conditional request support, and a six-hour minimum fetch interval by
default; it stops on HTTP 403/429. No live calendar request has been verified in
this repository. The MT5 Python adapter does not expose MQL5 calendar functions;
an authorized MQL5 CSV exporter with an established timezone would be a separate
integration.

### FRED

Set `FRED_API_KEY` in the process environment. The adapter accepts explicit series
IDs and inclusive date ranges, validates metadata/observations, preserves provider
missing markers, and records retrieval time and provenance. FRED observation dates
are not release timestamps. The current observations endpoint does not supply an
exact publication instant, so `publication_timestamp_utc` remains unknown and
must not be used as intraday availability evidence.

FRED and calendar tests are mocked and offline. No live FRED request was made in
this documentation pass. The adapter has bounded retries and a 10-second default
timeout.

## Historical files, Parquet, and reports

`python -m johanna.ingestion --help` lists the actual options. Supported formats
are documented MT5 text-bar exports and HistData Generic ASCII M1. Dukascopy bar
files are unsupported because a stable, verified OHLC schema and timestamp
convention are not available. Import only files you are authorized to use; the
importer does not download or copy source files.

Try the checked-in synthetic MT5 fixture (synthetic prices only):

```powershell
python -m johanna.ingestion --file .\tests\fixtures\synthetic_mt5_bars.csv --format mt5 --symbol EURUSD --timeframe M1 --source-timezone UTC --acquired-from "synthetic test fixture" --license-note "synthetic data" --root .\data\synthetic_demo
```

For a real MT5 export, replace the path, symbol, timezone, and acquisition details
with verified information. Use `--start-date`/`--end-date` for inclusive UTC date
filters. HistData requires an explicit `--timestamp-convention bar_open` operator
assertion because its file format does not establish the bar convention. Its volume
column is not mapped to tick or real volume.

The canonical Parquet schema stores UTC bar-open timestamps, OHLC, separate nullable
tick/real volume fields, source provenance, and source-time metadata. Clean data is
written under `<root>\clean\`, partitioned by symbol/timeframe/year/month. A
manifest records status, ingestion source/time, date range, schema version, row
count, and acquisition information where supplied. Re-ingestion is idempotent;
partition writes are atomic and an `in_progress` manifest requires replaying the
same batch to recover. Original input files remain where the user supplied them;
`<root>\raw\` is reserved but automatic raw archival is not implemented.

Run the market-data quality report against the same store root. For historical
ingestion using `--root .\data`, use:

```powershell
python -m johanna.quality --root .\data --json-out .\data\quality_report.json
```

The command prints a readable summary and writes JSON. It returns exit code 1 when
confirmed problems are present, including an empty dataset or missing manifest.
There is no configured FX session/holiday calendar, so gaps remain unresolved
unless the operator supplies explicit UTC closure evidence using `--closure-evidence`.
The canonical bar schema does not retain the original source filename per row, so
historical-file boundary attribution is limited. No user-supplied real-market OHLC
dataset was available during this documentation pass; only synthetic fixtures were
used. Real-data quality validation remains blocked.

## Collectors, shutdown, and troubleshooting

Copy and edit the validated TOML example:

```powershell
Copy-Item .\collectors.example.toml .\collectors.toml
```

Run one offline MT5 mock cycle or one offline calendar mock cycle:

```powershell
python -m johanna.collectors --config .\collectors.example.toml --source mt5 --dry-run --once
python -m johanna.collectors --config .\collectors.toml --source calendar --dry-run --once
```

The example config disables calendar collection, so enable `[calendar] enabled = true`
in the local copy before its dry run. For live MT5 polling, open the terminal and
run:

```powershell
python -m johanna.collectors --config .\collectors.toml --source mt5
```

For an authorized live calendar collector, set both `enabled = true` and the
permission assertion in the local config, then run:

```powershell
python -m johanna.collectors --config .\collectors.toml --source calendar
```

With `mt5.collect_ticks = true` (the default), each poll also writes the latest
quote snapshot for each configured symbol to a separate Parquet dataset under
`data/collectors/mt5_ticks/`. This is sampled polling, not a lossless tick stream.
Tick writes have their own manifest, idempotent storage, progress state, and
pending-write recovery. A tick failure does not prevent that symbol's bar write.

Each collector is a separate process. Logs go to the console; structured progress
is written to `data/collector_state/mt5.json` or `calendar.json`. MT5 bars and
tick snapshots and calendar snapshots go to separate subdirectories under
`data/collectors/`. Press
**Ctrl+C** in the relevant PowerShell window for graceful shutdown. MT5 finishes
the current poll and closes the terminal connection. On restart, pending bars and
tick snapshots are replayed before new retrievals; storage deduplication makes this
safe. A source failure does not write into the other source's store.

Troubleshooting:

- **MT5 package unavailable:** install with `python -m pip install -e ".[dev,mt5]"`.
- **MT5 reports disconnected:** open the desktop terminal, sign in to the demo
  account, verify the exact broker symbol, and rerun the smoke test.
- **API key is reported missing:** confirm it is set in the same PowerShell process;
  editing `.env` alone does not load it.
- **Calendar collector refuses to start:** it is disabled by default; live use also
  requires the local written-permission assertion.
- **Import reports invalid rows:** review the reported row numbers and timezone/
  convention parameters; invalid rows are recorded in the manifest validation
  errors, not silently treated as valid bars.
- **Quality command exits 1:** inspect the terminal summary and JSON; this indicates
  confirmed report findings, not a Python test failure.

## Unverified acceptance items and limitations

- Automated tests and CI are offline; they do not establish live service availability
  or broker connectivity. The current documentation run did not make live requests.
- No local clean OHLC dataset/manifest was available for real-data validation.
- Collector unattended reliability is not established; the required three
  consecutive-day run has not been observed.
- No FX session/holiday schedule is configured, so schedule-related bar gaps cannot
  be explained automatically.
- Source input filenames are not retained per canonical bar, limiting attribution
  across historical-file boundaries.
- No MQL5 calendar exporter is included; Dukascopy OHLC imports, strategies,
  forecasts, ML models, and all order execution remain out of scope.
