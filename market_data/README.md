# Master Market Database

This package is the central data interface for the project. It downloads,
updates, stores, and queries minute spread prices and daily SEAC settlements.

## First-time dashboard setup

### Imported product selection

Future historical and live writes use the selection in `market_data/config.py`:

| SEAC product | Minute prefix |
| --- | --- |
| CL | CL |
| CO | LCO |
| G | LGO |
| HO | HO |
| NG | NG |
| RB | RB |
| — | WTCL-LCO (retained direct CL–CO data) |

SEAC parsing skips other products. Minute parsing selects the original source
columns for the allowed prefixes and skips all others before creating batches.
Product matching is exact: NG does not include NGBF or NG-HH, and HO/RB do not
include HOBF/RBBF. Both compressed and two-digit source years are recognized.
The database writer also enforces this selection on registrations, historical
prices, live prices/provenance, and settlements. Existing unwanted rows are not
deleted or updated by these filters. Database cleanup remains a separate step.

The source URLs return bulk files, so the full files still download. Filtering
limits saved data rather than requesting smaller downloads. Restart existing
live services/importers to load the new code. No import is triggered by editing
these settings. Offline checks (no API calls or master-database writes):

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_import_filters
```

### Existing-data cleanup

The one-off maintenance command is `python -m market_data.cleanup`. It takes the
shared writer lock, creates a full backup in `data/backups/`, checks its SHA-256,
and rebuilds the four master tables into a new compact database. Table schemas,
indexes, retained registry timestamps and live provenance are preserved. Each
table is verified against the filtered backup using row count and two data-hash
aggregates, and the completed file is reopened before atomic publication.
The JSON cleanup report is saved alongside the backup. No source API is called.

This command changes the active database and should only be run when cleanup is
intended. It needs free disk space for the backup and working copy; its full-index
rebuild uses a separate 2 GB memory limit and one thread. Incremental imports keep
their usual memory limit. `--resume-backup PATH` reuses a prior cleanup backup only
if it still matches the master; otherwise it takes a fresh backup. Restart running
services/importers after code changes to ensure they use the import filters.

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

## Independent ideas canvas

Run `python idea_dashboard.py` and open http://127.0.0.1:8055 (or use `--port`).
This standalone chart plots one live reference-price line for
CL, CO, G, HO, NG, RB or CL-CO. Choose **Outright / single month**, **Calendar spread** (1:-1) or **Butterfly**
(1:-2:1), and a leg spacing of 1-24 calendar months. Each point rolls the starting
month forward by one month. For example, a 2-month spread is Z26-G27; a 2-month
fly is Z26-2*G27+J27. For CL-CO each month is itself CL minus CO, creating
four-leg spreads or six-leg butterflies. CL-CO remains the default. Outright mode
plots each contract's own server reference value; CL-CO single-month mode is the
calculated CL-minus-CO difference. Spacing is disabled for outright mode.
The server reference is admin price, falling back to last trade, then midpoint.
Spreads and flies are calculated from these reference values, as in the original
single-line chart; this view does not subscribe to directly quoted structures.
HO and RB show four decimals, NG three, and other products two. Product, structure,
spacing, starting-month count and "Skip first N months" persist in the browser.
Contract generation uses the selected product's expiry calendar; CL-CO uses
months unexpired in both calendars. Missing later months leave gaps rather than
shifting a structure to different calendar months.
The plot updates every 10 seconds from one shared live snapshot for the selected
product (CL and CO together for CL-CO), with missing or
stale prices left as gaps. Snapshots older than 120 seconds, future-dated
receipts and invalid reference values are excluded. Drag a box to zoom; mouse-wheel zoom is disabled.
Zoom stays in place during live refreshes. Series details appear on hover.
The collapsible price-change table and four direction boxes compare live prices
with stored prices 15 minutes, 30 minutes, 1 hour, and 2 hours earlier. This section
ignores the skip setting and follows the selected product, structure and spacing.
Up or Down requires strictly more than 50% of structures
with available data; unchanged spreads count in the denominator. History is fetched
in one cached read-only query per reference minute, month range, or database version.
The master database stores minute spreads rather than outright minute prices,
so outright mode shows its live values but leaves historical changes blank.
Missing data stays blank. Changing product, structure or spacing resets chart zoom;
timed refreshes preserve it. The chart has no historical lines, error plots, or entry signals. A single
multi-select dropdown optionally overlays linear, quadratic, cubic least-squares
fits or a smooth spline with fixed smoothing strength, using the same live
reference prices as the plotted line. Curves and table values use the selected
product's display precision; there are no extra fitting controls, and selections persist.

Offline checks: `python -m unittest tests.test_idea_structures tests.test_intraday_data.IdeaLivePlotTests`.
Restart the live service if it is still running the older three-product selection
so quotes for all six selected products are subscribed.

## SEAC seasonality dashboard

Run `python idea_dashboard_2.py` and open http://127.0.0.1:8056 (or use `--port`).
The dashboard opens directly to SEAC settlement seasonality using a product selector
and monthly contract cells, initially all empty. Click a cell's value to open Count and Structure (outright,
calendar spread, fly, or defly) in a small popup. Click outside, close it, or select
another month to save automatically. Each cell keeps its own structure label
(for example `1 Fly`); legs are expanded and combined only for the calculation.
Drag a filled value to another month to move its count and structure. The source
is cleared and an existing selection in the destination is replaced.
Fly weights are `1,-2,1`; Defly uses `1,-3,3,-1` across consecutive months.
Set Count to zero to clear that cell. Hover over Count and
scroll up/down to increase/decrease it without clicking. Click a month label
(such as H27) to clear only that block; click its value to open the editor.
Scroll over Structure to cycle through
Outright, Spread, Fly, and Defly. Enter the number of latest contract years
to display, ending with the selected expression's first net leg (for example
H27 with `4` years means 2024–2027). Cross-year structures keep this anchor year;
Z26–F27 ends at 2026. The exclusion dropdown follows the same contract-year range.
Check any years to exclude in the **Exclude years** dropdown, and enter a numeric lookback in
working days, then press **Plot**. All years align to expiry. Average and zero-base
options apply to the year overlays, which are always used for the chart.
The builder in `dashboard_components/seac_builder.py` reuses the existing
seasonality engine and plotter in `analysis/`. Historical lines use daily settlements.
A small green dot shows the selected expression's live price from the local
server on port 8060, refreshed every 10 seconds without reloading historical lines.
The dot follows the expiry axis and zero-base setting. Missing quotes,
quotes older than two minutes, and excluded anchor years have no live dot.
Settlement volume and open interest are unavailable.

## Running the live market-data service

From the project root, start the service for the six selected products:

```powershell
.\.venv\Scripts\python.exe market_data\live_service.py
```

The service listens on `http://127.0.0.1:8060`. Press Ctrl+C to stop it. Run
the command while connected to the corporate network and after installing the
`lightstreamer-client-lib` dependency.

The default and allowed live products are CL, CO, G, HO, NG and RB, configured
in `market_data/config.py`. Use `--products CL CO` to request a smaller subset.
Other products are rejected before any instrument request. BRN/LCO are accepted
as aliases for CO, and LGO for G. The corporate instrument request uses BRN for
CO; incoming BRN/LCO map to CO and incoming LGO maps to G. Minute recording maps
CO to the stored LCO prefix and G to LGO, including compressed one-digit years.
The service no longer discovers subscriptions from every SEAC symbol. Restart
an already-running service for the new subscription selection to take effect.
This change does not prune or rewrite existing master data, including WTCL-LCO.

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

Main table analysis is cached by product, expression, and selected years. Ordinary
10-second refreshes reuse settlement baselines and historical-year series, request
live expression quotes, and replace the current live point without opening a
master-database connection. Database publications and UTC date changes invalidate
that cache; changed year selections load the appropriate analysis. If the live
service is unavailable, the last cached values remain visible until recovery.

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

## Code responsibilities and regression checks

- market_data/queries.py is the read-only master database interface. It owns all
  read SQL, including product/contract lists, settlement snapshots, matching-minute
  expression prices, bounded candle aggregation, and batched expression ranges.
  database_status() and database_signature() expose file status without opening SQL
  connections or copying snapshots; dashboards use them for update labels and caches.
- market_data/database.py is the master writer interface. Historical imports and
  live-minute publication use its named methods; writer metadata queries stay here.
- market_data/intraday.py handles session windows and convenient reader lifetimes,
  delegating data requests to MarketData.
- market_data/contracts.py owns expression-to-spread recipes.
- market_data/live_client.py owns live-price requests and optional quote fetching.
- analysis/intraday_chart.py and analysis/session_overlay.py build reusable figures.
- dashboard_components/ owns Dash layout, callbacks, browser stores, and endpoints.
- tests/ contains automated regression checks. testing/ contains the older
  interactive range-testing app, which still requires the removed product_config.py.
  The retained backtesting execution chain also requires the removed feature module.

Run regression checks from the project root:

~~~powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p "test_*.py"
node tests/bookmark_indicator_check.js
node tests/live_marker_check.js
~~~

## Database access boundary

Application code should call named methods on MarketData or MasterDatabase rather
than access a DuckDB connection or cursor. Both interfaces keep their connections
private. Examples: symbols(), contract_codes(), latest_settlement_date(),
settlements_on(), expression_minutes(), expression_candles(), expression_ranges(),
save_spreads(), save_settlements(), and save_live_spreads().

Dashboard and analysis modules do not import DATABASE_PATH or inspect the master
file directly. Use database_status() for its UTC update time or existence and
database_signature() for cache invalidation. Signatures contain strings so browser
number rounding cannot trigger repeated history reloads.

Database copying, publication locks, and reader snapshots remain internal to
market_data; updater orchestration coordinates these helpers and the writer.
The live recorder's private SQLite spool is separate from the master DuckDB file;
its buffer SQL remains internal to live_history.py.
