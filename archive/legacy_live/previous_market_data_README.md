# Master Market Database

This package is the central data interface for the project. It downloads,
updates, stores, and queries minute spread prices and daily SEAC settlements.

## Current workflow: Phase 3

Production dashboards now obtain reconstructed prices through `DataClient` and the
three separate resolvers. Source-specific expiry/seasonality/rollover presentation
continues to use existing analysis utilities, without changes inside `analysis/`.
This migration has been reviewed by reading code only; no tests, imports of the
new application, database queries or servers were run by the coding agent.

Stop the previous **lab manager on 8071** and any old **8060 bridge** with Ctrl+C before starting their
replacement below. In one terminal:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.live_server
```

In another terminal, restart the main dashboard:

```powershell
.\.venv\Scripts\python.exe -B main.py
```

- Main dashboard: **http://127.0.0.1:8050**
- Shared live manager and subscription monitor: **http://127.0.0.1:8071**
- Optional lab watchlist on 8070 can still connect to this same manager.

Restart whichever dashboards you use after replacing the manager:

```powershell
.\.venv\Scripts\python.exe -B idea_dashboard.py
.\.venv\Scripts\python.exe -B idea_dashboard_2.py
.\.venv\Scripts\python.exe -B seac_experiment.py
```

Run each desired dashboard in its own terminal (8055, 8056 and 8057 respectively).
They all share the same 8071 manager, in addition to the main dashboard on 8050.
**Do not also run `structure_lab.manager_server`.** Its separate implementation
remains only as the earlier experiment. The old startup command
`python market_data/live_service.py` now delegates to the same new manager.

The manager URL is configured once as `LIVE_MANAGER_URL` in
`market_data/config.py`. Restart processes after changing it; the manager's
default listening port follows that URL. The old `live_subscription_config.json`
belongs to the archived bridge, not the new demand-driven subscription selection.
Normal dashboards never open independent streamer connections. Diagnostic tools
`standalone_live_quote_tester.py` and `live_stream_testing_dashboard.py` remain
explicit direct-feed diagnostics and can create their own connections when run.

The new backend is entirely under `market_data/`, with no `structure_lab` imports.
It uses a separate temporary registry at
`market_data/runtime/structure_requests.sqlite3`, not the master DuckDB database
or the old lab registry. There is one record, calculation stream and published
quote per normalized named structure, shared by every caller. Dashboard ownership
is not recorded. Registration or quote reads renew its expiry to 30 minutes from
that demand; opening the monitor does not renew it. Stopping or changing a
dashboard selection does not delete shared quotes. Expired structures are removed,
and TT IDs are unsubscribed only when no remaining structure needs them.
The new shared tables ignore the old owner-based registry rows; after this upgrade,
restart dashboards to register their selections again. Polling renews demand and
subscription changes reuse existing streamer sessions. Credentials remain in
memory. No master data is written or new minute-history recording enabled by
this manager. Historical import/update processes remain as before.

| File | Responsibility |
| --- | --- |
| `data_client.py` | Shared request interface for SEAC, minute and live |
| `structure_parser.py` | Named structures plus centralized legacy-expression conversion |
| `seac_resolver.py` | Whole-structure aligned settlement prices |
| `minute_resolver.py` | Actual stored-spread recipes, aligned minute prices and candles |
| `live_resolver.py` | Normalized whole live requests and compact shared quote reads |
| `live_structures.py` | Pure quoted-component decomposition |
| `live_requirements.py`, `quote_catalogue.py` | Resolve original requests to saved candidate TT IDs |
| `live_request_store.py` | Temporary requirements, leases and latest publications |
| `live_subscriptions.py`, `live_feed.py` | Deduplicated demand and shared MDS sessions |
| `live_manager.py`, `live_server.py` | Price publication worker, HTTP endpoints and monitor |
| `available_quotes.py`, `structure_best_quotes.py` | Fresh fallback recipes and bounded best-side optimization |
| `workflow_client.py` | Main-workflow compatibility helpers, batching and local HTTP clients |
| `structure_seasonality.py`, `structure_curve.py`, `structure_rollover.py` | Existing chart/calendar behavior with whole-structure data requests |

Migrated paths include the main grid and seasonality chart, rollover data,
forward curves, popup minute charts/session overlays and bookmark ranges;
8055 live curves and historical changes; both SEAC builders and their live
markers; and the API testing dashboard's database minute comparison. The QH
API OHLCV/volume/VAP requests remain separate API diagnostics. The previously
removed conditional-frequency tab is not reintroduced; its retained helper now
routes through the minute resolver if called.
Existing weighted expressions such as `X27-3*Z27+3*F28-G28` are interpreted
centrally as `CL X27 1MDF` when the product is CL. Arbitrary zero-sum weighted
expressions can be represented by named spread components. They never cause
outright live subscriptions. Unbalanced/outright live requests remain unavailable.
`value` means `(bid + ask) / 2`; quote timestamps are the oldest received time
among the components used, not the time the manager recalculated the price.

The manager publishes available fresh prices for every active request before
spending a bounded, rotating optimization budget. Missing catalogue coverage for
one structure does not reject every other request in a batch. Forward curves
register shifted complete structures in batches and refresh on the main
dashboard's 10-second timer. Settlement curve data is cached briefly and
invalidated by publication signature; chart patches preserve interaction state.

Manual verification, to be run by the user:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.manual_check --structure "CL X27 1MS" --structure "CO X27 1MS"
```

Then inspect the main grid, select a known-covered spread, compare seasonality
and minute candles, inspect the live marker and forward curve, change selection,
and check requested structures/IDs on the 8071 monitor. Initially pending prices
and unconfirmed optimality can be normal while subscriptions/optimization catch
up. Missing minute history (for example the previously checked G28 legs) still
produces an explicit no-data result rather than a fabricated price.

The old bridge is archived in `market_data/legacy_live_service.py`; its fixed
subscription resolver/feed/recording helpers are no longer used by normal
startup. `market_data/live_client.py` is now a compatibility adapter to the common
manager, not a second HTTP protocol or streamer. Legacy outright snapshots are
not supplied, and raw tick-history methods explicitly report that recording is
not part of this manager. Metadata/expiry database helpers remain available;
price reconstruction for production dashboards goes through the three resolvers.
No stored market data is deleted or rewritten by this migration.

Phase 3 manual checks (not executed by the coding agent): rerun the manual-check
command above, then open two dashboards together, select a known-covered spread
or fly, and inspect 8071. The same structure requested by both dashboards should
appear only once, with shared required IDs subscribed once. Change one selection
and confirm the other dashboard continues updating. Check SEAC historical plots,
the live marker, 8055 bid/ask curves, historical change cells, and the API DB
candle comparison. Closing a dashboard stops renewals; unused demand expires
after 30 minutes. Missing minute history and unsupported outright live requests
remain explicit missing results. Older bridge commands/options described later
in this file are legacy reference; use the startup commands in this section.

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

### Live contract monitor

Run `python live_stream_testing_dashboard.py` and open http://127.0.0.1:8058.
The former tester is now a live monitor: discovery and direct subscriptions
start automatically, with no request editor or testing controls.
Use the Product and Type filters to narrow the searchable Contracts dropdown.
Select individual contracts, or leave Contracts empty for every matching item,
then click **Apply subscriptions**. This replaces the actual shared stream
subscription, not just the table view. Leave all three controls empty and Apply
to return to every discovered contract. Filters do not issue more catalogue requests;
applying an unchanged selection does not reconnect. Empty matches leave the old
subscription intact. Table-column filters affect only the display.
**Subscribe by ID** accepts a full `TT-<instid>` or a numeric instrument ID;
separate multiple IDs with commas, semicolons or whitespace. **Subscribe to IDs
only** (or Enter) replaces the current subscription and bypasses the catalogue
filters. IDs need not exist in the discovered catalogue. Known IDs retain their
contract metadata; others display their TT item with type ID. Instrument IDs
remain strings so large 64-bit values are not rounded. Invalid input leaves the
existing subscription unchanged.

It discovers every returned instrument using these two catalogue requests:

```json
{"products":["CL","BRN","G","HO","NG","RB"],"strategies":["OUT","MS","MF"]}
{"products":["+1xCL"],"strategies":[""]}
```

The known box product is +1xCL and supplies box spreads only. Box names and exact
TT IDs are retained even when qh_code is empty. The monitor preserves all returned
instruments and removes duplicate TT IDs; it does not impose the previous
50-item testing limit. One Lightstreamer connection uses subscriptions of up to
250 items each, using the current AdminPriceAdapter / AdminPrice_DataAdapter,
MERGE mode, user Test, snapshots yes and maximum frequency 1.

Only connected instruments with a post-snapshot update received within 120 seconds
and at least one populated LTP/bid/ask/admin field appear in the table. Empty
snapshots and stale contracts are hidden. LTP means LastPrice, never a calculated
midpoint. Prices, sizes, trade volume, WAP, settlements, update counts and receipt
age are shown as supplied, with missing fields left empty. Product/type coverage
counts show live contracts versus all discovered contracts. Filter/sort the table
to inspect individual contracts. BRN/LCO display as CO and LGO as G.

The UI refreshes local in-memory state every second. It does not rediscover the
catalogue or resubscribe each second. Multiple tabs share the same connection.
Discovery runs at process startup (or first page load when imported); restart to
reload the catalogue. Partial discovery failures and subscription errors are
shown explicitly. Connection details remain available in a collapsed panel.

This monitor does not calculate synthetic structures, write prices to a database,
or change the production live bridge on port 8060 or the SEAC dashboards.
Ctrl+C stops the monitor and disconnects its subscriptions.

Offline verification: `python -m unittest tests.test_live_probe tests.test_live_monitor`.

### Production service

For a separate experimental copy, run `python seac_experiment.py` and open
http://127.0.0.1:8057. Its code and assets are isolated in
`dashboard_components/seac_experiment.py` and `seac_experiment_assets/`.
The chart shows only −90 to +90 calendar days around today, with the x-axis
labelled in days to expiry (negative before expiry, zero at expiry).
Historical years stay aligned by distance to expiry, retaining the existing
engine's last-available-date fallback when an old official expiry is missing. Future-side
observations appear only where historical data exists; prices are never projected.
The live dot uses today's days-to-expiry position and still updates independently of the lines.
The original dashboard on port 8056 is unchanged.
The experiment also shows a rolling mean line and a lightly shaded rolling mean ± SD band. Use the
Mean/SD checkboxes to hide either, and edit **SD ×** (default 2) to change the
band width immediately. **Roll days** sets N (default 20): statistics pool the
displayed years' observations over N calendar days ending at each expiry
position, including that position itself, after exclusions and zero-base adjustments.
Earlier history warms up the visible window. Sample SD needs at least two
observations; missing values are not filled. These controls update only the
statistical traces and do not reload history or redraw the year lines.
The darker blue band uses all displayed years. An orange **Current SD** band
uses only the selected expression's latest contract year, with its own
**Current SD ×** multiplier in 0.25 steps and visibility checkbox. Both bands
share **Roll days**; the current-year band stops at available current-year data
and disappears if that year is excluded.
Fresh live quotes replace today's observation in the latest-year line and both
rolling bands. The green dot sits on the connected price line. Each refresh
replaces the same day's value rather than adding duplicate points, and updates
only the latest-year line, live dot and statistical traces. Historical year
lines and chart zoom remain unchanged. A missing/stale quote restores settlement
data and removes the live observation from the calculations.

From the project root, start the service for the six selected products:

```powershell
.\.venv\Scripts\python.exe market_data\live_service.py
```

The service listens on `http://127.0.0.1:8060`. Press Ctrl+C to stop it. Run
the command while connected to the corporate network and after installing the
`lightstreamer-client-lib` dependency.

For a live connection test without writing minute observations, run
`.\.venv\Scripts\python.exe market_data\live_service.py --no-record`.
Open `http://127.0.0.1:8060/health` to check connection state and received counts,
and `http://127.0.0.1:8060/snapshot` to inspect the received quotes.
Press Ctrl+C after testing; omit `--no-record` for normal recording.

Edit `market_data/live_subscription_config.json` to select live `products` and `structures`.
Products are CL, CO, G, HO, NG, RB and `+1xCL` (CL–BZ boxes). Structure names
are `spreads`, `flies`, `boxes`. Outright subscriptions are rejected. Remove entries you do not need,
then restart the service. `--products CL CO` overrides the product selection
for one run; `--config PATH` selects another configuration file.

IDs come only from `data/quoted_live_probe_full_20261029_131318.json`: currently
1,292 saved contracts, every leg before January 2029, adjacent gaps at most
six months. Editing this JSON changes the available contracts after restart.
No catalogue requests are sent. On startup the service fetches product-specific
credentials and connects to `https://ls.prod-live.hertshtengroup.com`, adapter
set `MARKET_DATA`, adapter `MDS`, item `<ID>-2`. Subscriptions are grouped by
credentials and batched at 250 IDs; this is a batch size, not a total limit.
Credentials are kept in memory. All batches remain active concurrently.

All dashboards request a complete expression through
`LiveMarketDataClient.expression(product, expression)` or batch
`LiveMarketDataClient.quotes(product, expressions)`. The central resolver is
`market_data/quote_resolver.py`; it never uses outright prices. Resolution order:

1. Fresh direct quote for the exact structure (including inverse/scaled forms).
2. An exact combination of fresh quoted flies.
3. An exact combination of fresh quoted spreads or boxes.
4. An exact mix of those quoted structures, if needed.

For example, `F27-3*G27+3*H27-J27` is the difference of the two quoted flies
`F27-2*G27+H27` and `G27-2*H27+J27`. If those are unavailable, the resolver
tries `F27-G27 - 2*(G27-H27) + H27-J27` using quoted spreads. It checks exact
leg replication and uses the least gross component quantity within each tier.
Plans are cached; prices and freshness are checked on each request. Missing,
stale and disconnected components are excluded, with a default age limit of
120 seconds. Single-month or other nonzero-net exposure is unavailable.

Dashboard 8055 plots separate bid and ask lines. Its batch requests set
`best_sides=True`. The shared resolver compares the direct book with exact
combinations of up to three independent quoted instruments, including mixed
flies/spreads and cross-product structures. Bid maximizes sale proceeds; ask
minimizes purchase cost, using opposite sides for negative coefficients. They
can use different recipes. Zero-size sides, stale or disconnected books and
missing sides are excluded. Redundant arbitrage cycles are excluded; crossed
results from different valid recipes are displayed without clamping.

Best-side searches have a 0.35-second budget per side. `bid_optimal` and
`ask_optimal` are true only when the solver certifies the optimum within the
three-instrument model; otherwise the values are the best valid candidates
found so far, and the dashboard says optimality is unconfirmed. Results are
briefly cached against the actual bid/ask/size inputs. `bid_components`,
`ask_components` and side timestamps describe each side without sending leg
arrays. Quantities may be fractional: these are indicative structure prices,
not guaranteed exchange fills. Fits, if selected, use the bid/ask midpoint.

Price-only dashboard requests keep the fast reference-resolution path;
`client.quotes(product, expressions, best_sides=True)` or
`client.expression(product, expression, best_sides=True)` explicitly requests
optimized sides. Single HTTP `/expression/latest` requests optimize by default;
`best_sides=0` skips that work. Reference `value`/`source` keep their original
meaning; optimized side recipes may differ from the reference-value recipe.

The small quote response includes `value`, `last`, `bid`, `ask`, `bid_size`,
`ask_size`, `timestamp`, `source` and `component_count`, plus product/expression
and a direct instrument ID when applicable. No raw feed fields or component
arrays are sent. Composite bid/ask use the correct side of each component;
they are indicative combinations, not a quote for an exchange-listed order.
Sizes are bottleneck quantities; missing side data remains null. Composite
value combines each component's last price (or midpoint when last is missing),
and timestamp is the oldest component receipt time. Unrelated volumes and
settlement fields are not combined.

HTTP endpoints remain on port 8060: `/expression/latest` and `/latest` for one
quote, `POST /quotes` for up to 128 expressions per request, `/curve` for a
resolved forward curve, `/health` for connection status and one latest-price
timestamp, and `/snapshot` for diagnostics. Dashboards use batch requests and
the health timestamp rather than transferring whole snapshots. Responses have
source `direct`, `quoted_flies`, `quoted_spreads`, `quoted_mixed` or
`quoted_cross_product`. CL-CO first uses quoted boxes, then quoted CL/CO
structures if necessary. Full names determine fly legs because some catalogue
codes repeat the wrong months.

The main forward-curve popup uses `market_data/structure_curve.py` to request
resolved live structures; historical settlement comparisons still use the
existing historical data. The main dashboard's initial live overlay uses
`market_data/structure_seasonality.py`, including direct box requests for CL-CO.
The 8055 price curve and SEAC helpers also request complete structures. Files
inside `analysis/` are unchanged.

With outright subscriptions disabled, the old outright-based minute recorder
has no new observations; this upgrade changes live quotes, not historical
minute generation. Existing master history is retained. The following minute
recording details describe the previous outright-based recording path.

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

## Phase 1: common structure client and separate source resolvers

This additive interface is available for manual evaluation. Existing dashboards,
the production live service on 8060 and `analysis/` have not been migrated.

```text
Dashboard -> data_client.py -> seac_resolver.py -> MarketData settlement queries
                           -> minute_resolver.py -> MarketData spread queries
                           -> live_resolver.py -> shared manager on 8071
```

`structure_parser.py` understands named expressions including `MS`, `MF`,
`MDF`, `MDDF`, signed counts and cross-product combinations. Named terms and
their expanded exposure remain available separately. No production module in
this interface imports code from `structure_lab`. The existing lab manager
remains a separate HTTP backend during Phase 1; promoting that backend and
adapting legacy month-weight expressions belong to Phase 2.

```python
from market_data.data_client import DataClient

# Keep this instance for the dashboard session, or reuse a stable owner ID.
client = DataClient(owner="my-dashboard-session")
structure = "CL X27 1MDF - CO X27 3MS"

# Pure recipe inspection: no database connection or network request.
print(client.plan(structure, "seac"))
print(client.plan(structure, "minute"))
print(client.plan(structure, "live"))

# Daily settlement dates: both endpoints inclusive.
settlements = client.seac(structure, start="2026-10-01", end="2026-10-29")
print(settlements["status"], settlements["data"].tail())

# Minute timestamps: UTC; start inclusive, end exclusive.
candles = client.minute(structure, start="2026-10-27", end="2026-10-30",
                        interval="15min")
print(candles["status"], candles["data"].tail())
# interval=None returns aligned raw minute prices instead of OHLC.

# Requires the lab manager to be running on 8071. First result may be pending.
live = client.live(structure)
print(live)
# Call again on the dashboard's refresh tick; do not busy-loop.
batch = client.live_many(["CL X27 1MDF", "CO X27 3MS"])

client.disconnect()  # Release this client's registered live requests when done.
client.close()
```

The result envelope has `structure`, `source`, `status`, `data` and `recipe`.
SEAC data is a DataFrame with `timestamp, price`; minute candles contain
`timestamp, open, high, low, close`. Missing common observations produce
`status="no_data"`; missing legs are never silently omitted or forward-filled.
These adapters expose prices only: no synthetic volume or open interest.

Live data contains `bid`, `ask`, `mid`, `value`, `timestamp` and optimality flags.
`value` is the bid/ask midpoint, not last traded price. Status distinguishes
pending, available, partial and unavailable results, with a message where supplied
by the manager. A manager outage returns unavailable data rather than an old
cached price. Up to 64 structures are fetched together in compact responses.
Only requests polled by this client are renewed, every 15 seconds as needed,
with a 30-minute lease. Use `disconnect()` to release demand; `close()` only
closes HTTP resources, allowing outstanding leases to expire naturally.

All historical SQL stays in `queries.py`, using existing read-only reader
snapshots. Aliases and stored minute symbols reuse `contracts.price_recipe`
(CO -> LCO, G -> LGO). The existing one-digit year convention for those stored
minute symbols is preserved; its decade ambiguity is not changed in Phase 1.
No existing dashboard calls the new client until the later migration phase.

The new minute resolver chooses actual stored calendar spreads with finite
observations in the requested window. It accepts one- and two-digit year codes
and can construct a longer spread from a path of shorter stored spreads, rather
than requiring the theoretical direct symbol. Intermediate contracts are limited
to six months either side of the requested span. Ambiguous compressed years are
excluded. The pure `plan()` remains a theoretical recipe; a resolved result's
`recipe` identifies the actual stored IDs used. Combination still requires exact
common timestamps, with no forward fill. If no path/data exists, `message` and
`diagnostics` identify the missing coverage. The manual script also prints actual
stored date ranges when minute checks cannot run.

Manual checks for Phase 1 (not executed by the coding agent): inspect all three
plans for the example above; compare settlement and minute results against known
stored values using dates with coverage; then poll a live result with the 8071
manager running. Check missing-data handling and explicit disconnect as well.

For a single user-run check of all three paths, keep the lab manager on 8071
running, then execute from the project root:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.manual_check
```

This checks parser cases, prints all three recipes for three complete structures,
compares sample reconstructed settlements/minutes against individual stored
components, verifies OHLC against raw minute points, and polls live prices for
up to 30 seconds. It releases only its own live requests afterward. It starts no
servers or data importers and does not write master data. Output distinguishes
PASS, WARN (missing coverage/live availability) and FAIL, with a final summary.

Defaults use the last 14 UTC days. Override dates or structures when needed:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.manual_check --start 2026-10-15 --end 2026-10-30 --structure "CL X27 1MDF"
```

Use `--skip-live` for historical-only checks or `--wait 60` for a longer initial
live snapshot wait. Paste the terminal output for review. This script has not
been executed by the coding agent.
