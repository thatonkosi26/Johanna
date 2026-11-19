# P6.2b.6a.1 — Backend Identity and Migration Target Verification

Evidence only. The production journal was **not** opened, its key was not read, no file was
created, changed or removed, and the backend was not touched. Checked 2026-11-19 ≈10:28 UTC.

## Results

| Check | Result |
|---|---|
| Backend process exists (PID 26716) | **PASS** |
| Process identity (executable, command line, working directory, start time) | **PASS** |
| `JOHANNA_DATA_DIR` of PID 26716 | **PASS** — unset |
| Running code location | **PASS** — the project's `src/johanna` (editable install), with one limitation below |
| Source configuration defaults | **PASS** — `storage.data_dir = "data"` |
| Resolved journal path of the running backend | **PASS** — `C:\Users\thato\Downloads\Johanna\data\trading_journal.sqlite3` = intended production journal |
| Intended next-start target (documented command, project directory, `JOHANNA_DATA_DIR` unset) | **PASS** — resolves to the same journal |
| Exact environment of the future manual start | **NOT VERIFIED** — a start that has not happened cannot be observed |
| Existing SQLite sidecars | **NOT APPLICABLE** (not re-checked by design; status as previously reported) |

## 1. Running backend identity

| Item | Value | Method |
|---|---|---|
| PID | 26716 (exists) | Windows process list (`Win32_Process`) |
| Start time | 2026-11-19 06:30:21 UTC (08:30:21 SAST) | `Win32_Process.CreationDate` |
| Executable | `C:\Users\thato\AppData\Local\Programs\Python\Python313\python.exe` — the **global** Python 3.13.3 install, not the project `.venv` | `Win32_Process` and the process's own `ImagePathName` |
| Command line | `python  -m johanna` (no `--config`) | `Win32_Process` and the process parameters |
| Working directory | `C:\Users\thato\Downloads\Johanna\` | the process's `CurrentDirectory` |
| Parent | `cmd.exe`, started 06:25:59 UTC (a console window) | `Win32_Process` |

The working directory and the single environment variable were read from the process's own
parameter block with `OpenProcess` (query + memory-read rights only) and `ReadProcessMemory`;
nothing was written to or injected into the process. The environment block was parsed in memory
and **only** `JOHANNA_DATA_DIR` was looked up; no other variable was printed or stored.

**Code location.** The global Python 3.13.3 has an editable install
(`__editable__.johanna_forex_assistant-0.1.0.pth` in its `site-packages`);
`importlib.util.find_spec("johanna")` (which locates without executing the package), run with
that interpreter from the project directory, resolves to
`C:\Users\thato\Downloads\Johanna\src\johanna`. So the backend runs this working tree's code as
it stood when it started. Source files modified **after** 06:30:21 UTC (metadata of `src/` only)
— i.e. not in the running process and loaded by the next start:

| Modified (UTC) | File |
|---|---|
| 06:39 – 06:43 | `backups.py`, `reconciliation.py`, `api.py` (late P6.2b.4 edits) |
| 08:41 | `journal_store.py` (**v6 registration**), `operations.py`, `application.py`, `broker_monitoring.py`, `evidence.py`, `notifications.py`, `trade_links.py` (P6.2b.5) |

The running backend therefore holds a mid-P6.2b.4 build with v6 unregistered — consistent
with the journal still being v5.

## 2. Configuration

- `JOHANNA_DATA_DIR` in PID 26716: **unset**. Also unset at user level and machine level
  (persistent environment, so a new console would not inherit it by default). Only this one
  variable was read; Claude's own shell environment was not used to infer it.
- Source defaults (`src/johanna/config.py`, read without executing startup): `AppConfig.load`
  takes `--config` (optional TOML) then environment overrides; `data_dir` =
  `JOHANNA_DATA_DIR` if set, else `[storage] data_dir`, else `"data"`, used as a path relative
  to the working directory. `JournalStore(data_dir)` uses `trading_journal.sqlite3` and
  `trading_journal.key` in it.
- `johanna.example.toml` also sets `[storage] data_dir = "data"`. Starting with the README's
  `python -m johanna --config .\johanna.example.toml` gives the **same journal path**; it may
  differ from the current start in its `[integrations]` settings (not relevant to the journal).

## 3. Next-start target

- Intended command: `python -m johanna`, from `C:\Users\thato\Downloads\Johanna` (runbook:
  "Start: `python -m johanna` from the project directory"), as for the current process; the
  same global Python 3.13.3 resolves `johanna` to this working tree.
- Resolved journal: `C:\Users\thato\Downloads\Johanna\data\trading_journal.sqlite3` —
  **matches** the intended production journal `data/trading_journal.sqlite3`.
- **NOT VERIFIED:** the actual future start. It will match only if it is run from the project
  directory, with an interpreter that resolves `johanna` to this tree (the global 3.13 or the
  project `.venv`), and without `JOHANNA_DATA_DIR` set in that console. The `.venv`
  interpreters have the same editable install, so they would load the same code.

## 4. Existing SQLite sidecars

Not re-checked (by instruction). As reported in P6.2b.6a: `trading_journal.sqlite3-wal`
(0 bytes) and `trading_journal.sqlite3-shm` (32 KB) were created at 10:24:47 UTC by that phase's
read-only connection; they were not deleted or modified, and remain SQLite-managed.

## Limitations

- `PYTHONPATH` of PID 26716 was not read (only `JOHANNA_DATA_DIR` was in scope). The code
  location is established for this interpreter without `PYTHONPATH`; a `PYTHONPATH` in that
  console could in principle change it (no evidence of one).
- The exact file contents the backend loaded at 06:30 UTC cannot be reconstructed (no Git); only
  which files changed since is known.

## Confirmation

No journal, key, backup or sidecar was opened, created, changed or removed; no directory in
`data/` was listed; the backend process was only queried (not signalled, stopped or
restarted); no migration, startup code, service, MT5 or broker operation; no Git; no source,
test or configuration change. The only file written is this report.
