# Master Market Database

This package is the central data interface for the project. It downloads,
updates, stores, and queries minute spread prices and daily SEAC settlements.

## First-time dashboard setup

From the project root (`V2`), install all Python dependencies:

```powershell
python -m pip install -r requirments.txt
```

The tracked `data/contract_universe_config.json` selects CL, nine consecutive nearby
contracts, and March/June/September/December deferred contracts over three
calendar years. Edit these settings to choose a different universe.

Before starting the dashboard, restore an existing `data/master_database.duckdb`
or run the updater while connected to the corporate network:

```powershell
python -m market_data.watch
```

Wait for the update to finish; the command exits after one import. Run it
manually whenever you want to refresh downloaded history (for example, daily).
Then start the dashboard:

```powershell
python main.py
```

Open http://127.0.0.1:8050 in your browser.

To open the central dashboard manager from the project root, run:

```powershell
python run_all.py
```

Open http://127.0.0.1:8070. The manager shows each configured dashboard's
status and provides Start, Stop, and Open controls. Use the Add dashboard
fields to register another Python file; its configuration is saved in
`data/dashboard_manager.json`. If the file accepts a `--port` argument, tick
`pass --port` when adding it. Press Ctrl+C in the launcher terminal to stop the
manager and its child processes. Each row also displays the latest 30 lines
from that dashboard's terminal output, including errors from stderr.

## Running the live market-data service

From the project root, start the service with every product currently stored in
`data/master_database.duckdb`:

```powershell
$products = (& .venv\Scripts\python.exe -c "import duckdb; c=duckdb.connect(r'data\master_database.duckdb', read_only=True); print(' '.join(r[0] for r in c.execute('SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol').fetchall()))").Trim().Split(' ', [StringSplitOptions]::RemoveEmptyEntries); & .venv\Scripts\python.exe market_data\live_service.py --products $products
```

The service listens on `http://127.0.0.1:8060`. Press Ctrl+C to stop it. Run
the command while connected to the corporate network and after installing the
`lightstreamer-client-lib` dependency.

The live service also records the last valid received price within each UTC
minute. It buffers those closes in `data/live_minute_buffer.sqlite3` and publishes
completed minutes to the master database every 10 minutes. The feed callback
only updates a small in-memory buffer; a separate worker commits it to SQLite
about once per second. Restart the live service after upgrading to enable this.

A minute is labelled by its start: `10:05:00 UTC` stores the last valid update
received before `10:06:00 UTC`. Missing minutes remain missing. Initial subscription
snapshots are not treated as new price observations. Incomplete minutes are not
published, and prices retain their full precision.

The master stores spreads, so the recorder subtracts the two outright minute
closes for existing `spread_instruments`. Both legs must have a valid observation
in the same minute. No stale leg is carried forward. CO maps to the existing LCO
source symbols. The expiry calendar maps each compressed one-digit code to the
earliest unexpired full-year contract, so a far-decade quote cannot replace a
missing near-decade observation. Without a calendar match, ambiguous codes are
skipped rather than guessed.
Larger chart candles aggregate these minute prices, not every underlying tick.

Open main-dashboard minute charts check the master file signature on their live
refresh timer. They reload history only after a database publication; ordinary
ticks update the live markers without querying minute history. Closed popups
do not request chart data.

Published live minutes are provisional. Downloaded records always win for the
same instrument and UTC minute. A manual historical import replaces matching
provisional rows and retains newer live rows or live rows absent from the download.
Live writes cannot overwrite a downloaded minute. Successful publication removes
consumed closes from the SQLite buffer; failed or blocked publication retains
them and retries after about a minute. A graceful stop attempts a final publish
of completed minutes and retains the incomplete minute for recovery.

The `/health` response includes `minute_recording`: worker status, queued closes,
last publication time, accepted spread count and the latest recording error.

## Database

The database is stored at:

```text
data/master_database.duckdb
```

It contains:

- `spread_prices(timestamp, instrument, price)`
- `spread_instruments(instrument, first_seen, last_seen)`
- `seac_settlements(symbol, contract_code, trading_date, price)`
- `live_spread_keys(timestamp, instrument)` identifies provisional live rows

Exact `-100` spread values are treated as unavailable and are not stored.
Negative values other than `-100` are valid prices.

## Requirements

```powershell
pip install duckdb pandas requests ijson
```

The corporate Insight certificate is not available to Python, so both internal
downloads currently use `verify=False` and suppress the related warning.

## Running the updater

Every invocation runs one update and exits. The older `--once` option remains
accepted for compatibility. A failed update exits with code 1 and prints the
traceback. The final database appears only after the complete import succeeds.

There is one operational entry point:

```powershell
python -m market_data.watch
```

Running the command downloads minute spreads and SEAC settlements once. It does
not schedule further downloads; continuous minute recording belongs to the live
service.

Contract expressions are generated at runtime from `data/expiry_dates.csv` and
`data/contract_universe_config.json`; no generated contract-list file is needed.
Add custom structures to `custom_structures` in the config, for example:

```json
{"name": "C6", "coefficients": [1, 0, -1, -1, 0, 1], "steps": [1]}
```

The coefficients use consecutive expiry positions; zeroes skip legs. The
coefficient sum must be zero for minute data reconstructed from stored spreads.

Only one watcher can run at a time. Progress and errors are written to
`data/market_data_sync.log`.

The full source files are downloaded because neither source provides a known
incremental endpoint. New/recent spread records, outstanding provisional live
minutes, and new or revised SEAC settlements are processed. Downloaded files are deleted after processing.

Updates never modify the master database directly. A temporary copy is updated,
checkpointed, and closed first. Only a completely successful copy replaces the
master. If the process stops or fails, the existing master remains unchanged.

## Dashboard reads during updates

Dashboards use read-only, per-process database snapshots in the system temporary
folder. A snapshot is reused until the master database changes; new `MarketData`
connections then receive a new snapshot. Existing connections finish against
their original snapshot and must be reopened to see refreshed data.

Each dashboard process needs disk space for its own database copy, and its first
connection after an update incurs the copy cost. Unused older snapshots are
removed when their readers close. Reader DuckDB buffers are configured for
512 MB and two threads. The historical importer and live-minute publisher share
an OS writer lock for the entire copy/update/publish operation. A live publish
waits for the next retry when a manual import owns that lock; recording continues
in SQLite. Both publish a checkpointed, closed copy atomically and retry file
replacement for up to 30 seconds during brief reader copies. Reopen a reader to
see the latest completed publication.

## Reading spread data

```python
from market_data import MarketData

data = MarketData()
frame = data.spread("CLV26-X26")
data.close()
```

With an optional time range:

```python
frame = data.spread(
    "CLV26-X26",
    start="2026-03-01",
    end="2026-04-01",
)
```

List available spreads:

```python
cl_spreads = data.spreads("CL%")
```

## Reading SEAC settlements

```python
from market_data import MarketData

data = MarketData()
frame = data.settlement("CO", "Z26")
data.close()
```

## Building contracts from stored spreads

Complex zero-sum contracts can be requested directly without loading each
spread in application code:

```python
from market_data import MarketData

data = MarketData()

# CLV26 - 3*CLX26 + 3*CLZ26 - CLF27
minute = data.synthetic("CL", "V26-3*X26+3*Z26-F27")
candles = data.synthetic_ohlc(
    "CL", "V26-3*X26+3*Z26-F27", interval="30min"
)

data.close()
```

The example is decomposed automatically into:

```text
1 * CLV26-X26 - 2 * CLX26-Z26 + 1 * CLZ26-F27
```

Butterflies work through the same interface:

```python
butterfly = data.synthetic("CL", "V26-2*X26+Z26")
```

Only zero-sum expressions can be created from spreads alone. An expression
whose coefficients do not sum to zero requires an outright price and is
rejected.

Always close the read-only connection when finished, preferably using `try` /
`finally`, especially before publishing an update on Windows.

## One-digit expiry years

Some source symbols use two-digit years, such as `CLV26-X26`, while others use
one digit, such as `LCOV6-X6` or `LCOZ9-F0`. The database deliberately preserves
the raw source symbol and does not guess the decade. For example, in the current
contract cycle `Z9-F0` likely represents December 2029 to January 2030, but that
interpretation depends on the reference decade. Any future normalization must
store the raw symbol and apply an explicit reference-year rule.

## Operational notes

- DuckDB supports one writer; project applications should open read-only queries.
- Close readers before the final database replacement if Windows reports that
  the file is in use.
- Do not delete `.tmp` or `.incoming` files while an update is running.
- A retained temporary database after failure can be inspected, while the master
  remains safe.
- Contract construction and synthetic structures consume the existing stored
  spread format. Outright closes exist only in the recoverable SQLite buffer
  until their completed-minute batch is consumed.
- Keep free disk space for one working master copy during publication and the
  per-process reader snapshots. A live publication creates a new master version
  at most every 10 minutes, plus a final attempt during graceful shutdown.
