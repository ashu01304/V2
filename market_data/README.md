# Market data and dashboard guide

## Project layout

| Location | Purpose |
| --- | --- |
| `main.py`, `dashboard.py`, `dashboard_components/` | Main dashboard and chart components |
| `idea_dashboard.py` | Bid/ask curves and historical changes, port 8055 |
| `idea_dashboard_2.py`, `idea_dashboard_2_assets/` | SEAC seasonality, port 8056 |
| `seac_experiment.py`, `seac_experiment_assets/` | Experimental rolling statistics, port 8057 |
| `market_data/` | Production data access, resolvers, live manager and historical imports |
| `API/` | QH REST API client, authentication, cache and endpoint wrappers |
| `diagnostics/` | Optional API chart comparison and standalone direct-feed tester |
| `tests/` | Current regression checks; run manually |
| `archive/` | Retired live bridge, original structure lab and historical tests |
| `data/` | Master database, saved contract catalogue and other local data |
| `analysis/` | Shared chart calculations using the common data client |
| `Tools/` | Separate tools repository; unchanged |

Archived files are retained for reference, with their original imports and paths.
They are not maintained runnable entry points. The previous detailed README is
saved as `archive/legacy_live/previous_market_data_README.md`. Do not run archived
live servers alongside the production manager.

## Run the current system

Run commands from the project root, using a separate terminal for each process.
Start exactly one shared manager:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.live_server
```

Its quote API and subscription monitor are at **http://127.0.0.1:8071**.
Use `market_data.live_server` as the single service startup command; the redundant
`live_service.py` launcher has been removed.
The single-instance lock prevents duplicate managers using its registry.

Then start whichever dashboards you need:

| Dashboard | Command after `.\.venv\Scripts\python.exe -B` | Port |
| --- | --- | --- |
| Main | `main.py` | 8050 |
| Bid/ask curves | `idea_dashboard.py` | 8055 |
| SEAC seasonality | `idea_dashboard_2.py` | 8056 |
| SEAC experiment | `seac_experiment.py` | 8057 |
| Live structure watchlist | `-m market_data.live_dashboard` | 8070 |

Restart running dashboards after this cleanup. The old
`python -m structure_lab.dashboard` command is replaced by
`python -m market_data.live_dashboard`.

Optional diagnostics:

```powershell
.\.venv\Scripts\python.exe -B -m diagnostics.api_dashboard
.\.venv\Scripts\python.exe -B -m diagnostics.standalone_live_quote_tester
```

The API chart tester uses **8058**, avoiding the SEAC experiment's 8057 port.
The standalone quote tester uses **8059** and explicitly opens its own direct
streamer connections when Subscribe is clicked. It is independent of the shared
manager. Its saved catalogue remains in `data/`; nothing is fetched on startup.
For QH login, endpoint probes and dependencies, see `API/README.md`.

## Data flow

Dashboards send complete structures through `DataClient` in `data_client.py`:

- `seac_resolver.py` reconstructs aligned settlement histories.
- `minute_resolver.py` finds available stored spread recipes and returns minute
  prices or candles. Missing observations remain missing.
- `live_resolver.py` requests shared published quotes from the HTTP manager.

`structure_parser.py` parses named structures and converts legacy expressions.
`workflow_client.py` contains shared dashboard batching, session windows,
minute-history helpers and compatibility calls to the common data client.
`analysis/seasonality.py`, `analysis/forward_curve.py` and `analysis/rollover.py`
now request complete structures themselves while retaining chart/calendar logic.
Their duplicate `market_data/structure_*` adapter classes have been removed;
dashboards import these analysis classes directly. Live prices always come from
the shared manager; these classes do not reconstruct live prices from outrights
or substitute stored minute observations for a live quote.

The live manager stores **one normalized structure, one calculation stream and
one published quote per unique structure**. Dashboard ownership is not tracked.
Every registration or quote read renews its expiry to 8 hours from that demand.
Monitor/status reads do not renew it. Closing a dashboard or removing a selection
leaves its shared quote until inactivity expiry. Required TT IDs are deduplicated
across all structures and removed only when no active structure needs them.

`live_resolver.py` contains live component recipes and the HTTP quote client;
`quote_catalogue.py` indexes saved candidate IDs;
`live_request_store.py` maintains temporary shared demand;
`live_manager.py` resolves subscription requirements, coordinates shared
subscriptions and publishes results; `live_feed.py` handles the streamer;
`live_pricing.py` calculates fallback prices and optimizes bid/ask sides;
`live_server.py` serves HTTP quotes and the monitor.
Chart refresh helpers live in `dashboard_components/chart_updates.py`.
On the 8055 chart, bid/ask point hovers show the signed quoted components,
book side and component price used. Only callers requesting `include_recipes`
receive this compact breakdown; other quote responses stay unchanged.

The manager also publishes `alternatives`: unranked exact constructions, each
with paired bid/ask, midpoint, sizes, component IDs/names/weights and side timestamps.
The same component quantities are used for both sides of each construction.
Request `include_alternatives: true` on `POST /quotes` for compact responses,
or call `DataClient.live(structure, include_alternatives=True)` (also supported
by `live_many`). Full responses include the list automatically.
Routes cover direct, named (such as quoted flies) and spread reconstructions,
plus mixes discovered by the existing search. Exact recipes are deduplicated;
enumeration is capped at 64 combinations per route and reports
`alternatives_truncated`. The list is not an exhaustive search of every possible mix.
Existing single-quote fields remain for compatibility with other dashboards.

On 8057, blue and yellow bands show the minimum-to-maximum available bid and ask
separately, with price labels and construction details on hover. A band can span
different constructions; its endpoints are not one paired quote. A single value
collapses the band to a line. Empty/stale alternatives hide the corresponding band.
The existing reference midpoint still supplies the current-year observation and
rolling statistics. Range endpoints are not inserted as historical observations.

Available prices are published even when best-side optimization is unconfirmed.
`average`/`mid` is `(bid + ask) / 2`. Timestamps reflect the oldest received
component used, not the recalculation time. Outright live subscriptions are
intentionally unsupported. Subscription changes reuse streamer sessions.

## Configuration and stored data

`market_data/config.py` contains `LIVE_MANAGER_URL`, the database path and import
product filters. Restart processes after changing configuration.
The retired `live_subscription_config.json` is now under `archive/legacy_live/`;
it does not control the demand-driven manager.

The master remains `data/master_database.duckdb`. Shared live requests use
`market_data/runtime/structure_requests.sqlite3`. The manager also records actual
post-snapshot updates of directly quoted calendar spreads: the last valid bid/ask
midpoint received within each UTC minute, without forward-filling quiet minutes.
Only subscribed spreads for the retained products are recorded; flies, boxes and
dashboard-computed alternatives are not written as calendar-spread observations.
No extra subscriptions or outright prices are needed.

The durable buffer is `data/live_minute_buffer.sqlite3`, using a new
`quoted_minute_closes` table. Older outright-buffer rows are left untouched.
The recorder is implemented in `live_request_store.py`, alongside the temporary
live storage, and starts/stops with the manager. It flushes memory to SQLite each
second, then publishes completed minutes every **10 minutes** by default.
`LIVE_MINUTE_PUBLISH_SECONDS` in `config.py` controls that interval (minimum 60s).
Publication copies the master, takes the same writer lock used by `watch.py`,
registers retained-product spread instruments and atomically replaces the master.
Dashboards see these points after publication, not immediately on receipt.

`live_spread_keys` marks these observations as provisional. Live recording never
overwrites imported historical values. The existing `watch.py` importer includes
the provisional period in its overlap, replaces matching points with downloaded
history and removes their provisional keys. If the importer holds the writer lock
or publication fails, buffered closes stay queued for retry. The buffer is
acknowledged only after successful publication (or when history already has the
points). Quote midpoints are provisional observations, not executed trade prices.
The 8071 monitor and `/status` show received/queued counts, publication time,
published point count and recorder errors. Credentials remain in memory and `.env` stays local.
The quoted contract catalogue remains
`data/quoted_live_probe_full_20261029_131318.json`.

Historical imports retain CL, CO, G, HO, NG and RB, with minute aliases LCO and
LGO, plus the retained WTCL-LCO intermarket spread. Import filters are in
`config.py`. `database.py`, `spreads.py`, `settlements.py` and `sync.py` handle
storage/imports; `queries.py` and `reader_snapshots.py` handle dashboard reads;
`publication.py` handles locking and publication.

The existing historical updater command is unchanged:

```powershell
.\.venv\Scripts\python.exe -B -m market_data.watch
```

It is separate from the live manager. `diagnostics.cleanup_database` is an
explicit master-data maintenance tool, not part of source cleanup or normal
startup. The old `market_data.cleanup` command has been replaced by that module.

## Manual verification

No tests, dashboards, application imports or database queries were run by the
coding agent during this cleanup. With the manager running, use:

```powershell
.\.venv\Scripts\python.exe -B -m diagnostics.manual_check --structure "CL X27 1MS" --structure "CO X27 1MS"
```

Open two dashboards and request the same structure: the 8071 monitor should show
one row. Change one dashboard selection and confirm the other still updates.
Check the main curve, SEAC live marker, minute candles and 8055 bid/ask lines.
Missing historical coverage remains an explicit no-data result. Archived tests
refer to retired interfaces and are excluded from the current `tests/` folder.
