# QH API integration

This package is independent of the dashboards and existing `market_data` ingestion.
It uses the existing `requests` dependency. DataFrame views additionally use the
existing `pandas` dependency. Automatic browser login additionally uses the
optional dependency in `API/requirements.txt`.

## Authentication

To read and save the token automatically, install the optional login dependency
once, then run the auth module:

```powershell
.\.venv\Scripts\python.exe -m pip install -r API/requirements.txt
.\.venv\Scripts\python.exe -m API.login
```

This opens your installed Microsoft Edge browser with a dedicated persistent
profile at git-ignored `data/qh_api/edge_profile/`. Complete Microsoft sign-in/MFA
if requested. Within five minutes, the script reads the JSON `access` field from
the successful QH `/apis/auth/callback/` response (also accepts `access_token`),
and saves it as `QH_ACCESS_TOKEN` in the root `.env`, preserving other entries.
It never prints the token or the callback URL. The URL's `code` parameter is an
OAuth authorization code, not the bearer token. Only the QH origin's auth JSON
responses are accepted.

The dedicated profile remembers its own Microsoft session between runs; your
already-open everyday browser session is separate. The first run may need sign-in.
Edge closes after capture. Run only one automatic login at a time. Corporate
browser policies may prevent automation; the manual `--login` flow remains available.
This uses Playwright's [persistent browser context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)
and [response events](https://playwright.dev/python/docs/network#network-events).

To capture automatically and immediately run a probe:

```powershell
.\.venv\Scripts\python.exe -m API.probe --auto-login --endpoint ohlc
```

Subsequent probes can omit both login flags to use the saved token. Run the auth
module again when it expires. An existing process-level `QH_ACCESS_TOKEN` takes
precedence over `.env`; clear/update that variable if it contains an older token.

For manual copy/paste instead:

From the V2 project root, in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m API.probe --login
```

This opens https://qh-api.corp.hertshtengroup.com/apis/auth/ in your browser.
Complete Microsoft sign-in, copy the returned **access_token**, then paste it
into the terminal's hidden prompt. An existing Microsoft browser session may
complete sign-in without another password prompt. Python requests do not inherit
your browser login. The script does not capture browser cookies or automate MFA.
Tokens entered this way stay in memory for this process and are not saved.

For repeated use, optionally add `QH_ACCESS_TOKEN=<your token>` to the existing
git-ignored root `.env`, then omit `--login`. `QH_USERNAME` and `QH_PASSWORD`
are not used by this authentication flow. A process environment variable takes
precedence over `.env`; an explicit `QHClient(token=...)` takes highest precedence.
Either the raw token or `Bearer <token>` is accepted. On 401/403, get a fresh token
from the auth page. Automatic refresh is not implemented because no refresh flow
has been provided.

## Manual response probes

These are live, read-only data queries, including the two POST endpoints. They
consume API quota. No probes have been run by the implementation agent.

```powershell
# One endpoint, small response:
.\.venv\Scripts\python.exe -m API.probe --login --endpoint ohlc --count 3

# All five endpoints, sequentially:
.\.venv\Scripts\python.exe -m API.probe --login --endpoint all --tas-payload API/examples/tas.json

# Individual POST endpoint:
.\.venv\Scripts\python.exe -m API.probe --login --endpoint tas --tas-payload API/examples/tas.json
```

The examples reproduce the supplied screenshot inputs with smaller counts.
Edit instruments, dates, and time bounds to a period where you expect data.
The TAS request time zone remains unconfirmed; returned
trade timestamps include UTC ISO strings. GET start/end
parameters use Unix seconds as documented in the screenshots.
`--instruments` accepts comma-separated QH codes and defaults to `CLZ26-F27-G27`.
`--endpoint all` skips POST endpoints whose payload files are not supplied,
and prints each skip explicitly.

Paste the probe output into the conversation so endpoint-specific reshaping can
be completed. It prints status, rate-limit headers, top-level structure and a
bounded JSON sample. It does not print request authorization headers or tokens;
authentication-shaped response fields are redacted. Errors omit response bodies.

## Python use

### Local testing dashboard

```powershell
.\.venv\Scripts\python.exe -m diagnostics.api_dashboard
```

Open http://127.0.0.1:8058. Select a stored contract or enter another QH code,
choose a common interval (1m, 5m, 1h, 1d) and candle count, then click **Plot / refresh**.
The optional end time is UTC; blank uses now. The charts use the same completed
UTC buckets, excluding the currently forming bucket. There is no automatic polling.
The selector includes stored two-digit-year spreads and a few sample contracts.

Two panels show OHLCV candles and database candlesticks built from minute prices in the selected interval,
with the already-fetched OHLCV volume. Each UTC bucket uses the first price as
open, maximum as high, minimum as low, and last as close. Empty buckets are
omitted; buckets with some missing minutes use the available observations.
At 1m, a single minute sample gives equal open/high/low/close. Database
minute samples cannot reproduce tick-level candle highs/lows. Missing periods
are compressed on the time axis, and missing volume is not replaced with zero. Each plot makes at most
one API request before retries; identical requests use the shared 60-second cache.
The button respects the cache, so a repeated click within a minute may reuse data.

The database treats three-month QH shorthand `CLZ26-F27-G27` as the butterfly
`Z26-2*F27+G27`; the effective expression is displayed in the status. Use the DB
expression override if the API instrument uses a different weighting. Two-month
codes use the ordinary calendar spread. The database must contain the required
legs in the chosen period; choose an older end time for expired contracts.
API panels remain usable when the database has no matching history. Price/volume
panels have no range-slider candle preview. Their UTC timestamps are equally
spaced categories, collapsing weekends and any other periods without observations;
spacing therefore represents observations, not elapsed wall-clock time.
Current OHLCV queries use count and interval, matching the successful manual probe;
an explicit historical end adds only `end` (Unix seconds). Results are clipped to
the common comparison window, and errors/empty-response details appear in the
corresponding chart and status.
The OHLCV request includes `extraFields=buyvolume,sellvolume`. Both the OHLCV
and database panels show separate green buy-volume and red sell-volume bars,
not combined volume. Missing side fields are reported instead of being inferred.

The dedicated **Volume at price** panel below the comparison charts has its own
Load VAP button, multi-instrument input, 5-minute/daily interval, bucket count
(1-200 per instrument), optional UTC end, and cache bypass. After loading, choose
an instrument and a single time bucket (latest by default), or aggregate all loaded
buckets. The horizontal profile keeps Volume 1 and Volume 2 as separate bars and
marks the price with the largest combined volume. Summary cards show bucket/price
level counts and volume totals. These two fields retain the API order; their
buy/sell mapping is not inferred.

The heatmap shows price against all loaded timestamps for the selected instrument,
with a choice of Volume 1, Volume 2, or their sum. Missing cells stay blank and
periods without returned buckets take no horizontal space. The bucket selector
changes only the profile and summary; the heatmap retains the full loaded history.
A collapsible price ladder and raw sample support inspection. Changing an
instrument, bucket or heatmap metric uses loaded browser-memory data with no API
calls; only Load VAP fetches. The cache still expires after 60 seconds, but loaded
charts remain a snapshot until loaded again.

The **Other endpoints** section lets you independently query latest fairvalue,
historical fairvalue (including open/high/low), and TAS.
Enter comma-separated instruments or leave blank to use the contract chosen
above. Each click makes one endpoint call before retries; changing inputs alone
does not fetch. The 60-second cache applies unless **Fresh request** is checked.
History uses its own interval, count and optional UTC end controls.
TAS uses dates and start/end clock times in its separate query section.
Latest fairvalue ignores historical controls.

Results include a chart, sortable/filterable table (first 1,000 rows), and bounded
raw/metadata preview. TAS plots individual trade prices,
retains numeric side values, and reports whether more pages are available without
silently fetching them. The explorer does not execute queries on page load.

```python
from API import QHClient
from API.endpoints import fairvalue, fairvalue_historical, ohlc, vap, tas

with QHClient() as client:
    result = ohlc.fetch(client, ["CLZ26-F27-G27"], interval="1H", count=50)
    raw_json = result.raw
    records = result.records
    frame = result.to_frame()
```

Each endpoint module exports `fetch(client, ...)`:

| Module | HTTP path | Rate limits (minute / hour / day) |
| --- | --- | --- |
| `fairvalue` | GET `/fairvalue/` | 7 / 420 / 10,080 |
| `fairvalue_historical` | GET `/fairvalue/historical/` | 7 / 420 / 10,080 |
| `ohlc` | GET `/ohlc/` | 30 / 1,800 / 43,200 |
| `tas` | POST `/tas/` | 10 / 600 / 14,400 |
| `vap` | GET `/vap/` | 7 / 420 / 10,080 |

The manual probe returned HTTP 200 for all five supported endpoints. Result views follow
those observed response structures:

- `raw`: complete decoded JSON with original names and metadata.
- `source_records`: rows with server field names, unwrapping top-level `data` where present.
- `records`: normalized names, including `instrument`, `timestamp`, `price`,
  `open`, `high`, `low`, `close`, `volume`, `buy_volume`, `sell_volume`, and
  `quantity` where present. All five samples use Unix milliseconds for returned
  timestamps. This view retains milliseconds as numbers and preserves row order.
- `to_frame()`: normalized pandas DataFrame with `timestamp` converted to a
  timezone-aware UTC datetime. Historical endpoints returned newest-first rows;
  call `.sort_values(["instrument", "timestamp"])` if you need chronological order.
- `metadata`: non-row fields, including TAS status and pagination.
- `has_more`: TAS pagination indicator based on `next` and page counts. The sample
  returned 40 trades on one page. More pages are reported, not automatically fetched;
  the pagination request contract needs confirmation before automatic traversal.
- `levels` / `levels_frame()`: VAP expanded into one row per instrument, time,
  and price. For `-0.14:10-2`, values are `price=-0.14`, `volume_1=10`, and
  `volume_2=2`. Their buy/sell meaning is unconfirmed, so no side labels are assigned.
  `levels_frame()` also converts timestamps to UTC. The original `vap` strings
  remain in `raw`, `source_records`, and `records`.

TAS `side` values remain the numeric values returned by the API (observed `1`
and `-1`); their meaning is not inferred. Its original `timestamp_iso` is retained
alongside the normalized timestamp. Unknown row fields are retained.

```python
with QHClient() as client:
    profile = vap.fetch(client, ["CLZ26-F27-G27"], count=3)
    price_levels = profile.levels_frame()

```

The probe prints normalized record samples, parsed VAP levels, and
TAS metadata as well as the raw preview. Rerun it when you want to inspect
these changes; no automatic or live tests were run by the implementation agent.

## Request coordination

Every request includes Bearer authorization and uses a 60-second timeout. HTTP
429 uses `Retry-After` seconds (60-second fallback); HTTP 5xx, connection failures
and timeouts use 1s, 2s, 4s, ... backoff, with at most five total attempts.
Other HTTP failures do not retry. Redirects are rejected so an expired login
cannot silently return an HTML sign-in page.

Calls use conservative spacing plus rolling minute/hour/day budgets per token
and endpoint. Every attempted request is recorded before sending, including
retries. Remaining=0 defers the next request until the server's reset time;
long waits happen in 30-second sleep chunks. A final failed attempt still saves
its cooldown for the next invocation. Pacing can make waits longer than the
minimum retry backoff. Successful data returns immediately even when the next
request must wait.

OS file locks serialize the same endpoint/token across cooperating local
processes, and client locks protect threads and the requests session. State lives
in git-ignored `data/qh_api/`, keyed by a SHA-256 digest; it contains timestamps,
not tokens or response data. Keep this directory intact while quotas are active.
All processes must share this directory. This does not coordinate another
computer, browser Swagger calls, or scripts outside this package; server headers
are still honored when those other callers consume quota. Use a single owner
for each token/endpoint where possible.

Successful identical GET and read-only POST calls are cached on disk for **60
seconds** by default, including fairvalue snapshots. A repeated call can therefore
return a snapshot up to one minute old. Every call checks the shared cache before
reserving quota or sending a request; after acquiring the endpoint lock it checks
again, preventing two waiting processes from fetching the same response twice.

The cache persists across clients and script restarts in git-ignored
`data/qh_api/responses.sqlite3`. Cache keys include a hash of the token, base URL,
method, endpoint, query parameters, and POST body. Tokens are not stored in the
cache. A new token uses a separate cache scope. Dictionary key order does not
affect matching, but different instrument lists, counts, bounds, or request bodies
are separate entries. It does not yet extract a smaller request from a cached
larger batch or combine overlapping date ranges.

Expiry runs from the time the response is cached; reads do not extend it. Expired
entries are never returned. They are deleted during the next cache lookup/write
for any endpoint, including calls that bypass the cache. No background process
runs while the app is closed; expired rows may remain on disk until its next use.
SQLite reuses freed storage, so the database file may not shrink immediately.

Set `QHClient(cache_ttl=...)` or an endpoint's `cache_ttl=...` in seconds to change
the freshness requirement. A shorter TTL will not accept an older cached value.
`cache_ttl=0` bypasses both cache reads and writes, without removing existing
unexpired entries. HTTP failures and invalid JSON are not cached. Cached JSON is
decoded independently for each call so modifying a result does not alter the cache.

The probe uses the 60-second cache by default and reports `cached`, cache age,
and expiry on hits. To deliberately obtain fresh data:

```powershell
.\.venv\Scripts\python.exe -m API.probe --endpoint ohlc --no-cache
```

Offline cache checks are provided for you to run; they make no API calls:

```powershell
.\.venv\Scripts\python.exe -m API.check_cache
```

Batch up to 50 instruments where documented; larger lists are rejected so callers
choose explicit batches rather than triggering hidden requests.

The private Swagger URL could not be accessed during implementation. Paths,
parameters, limits and examples here follow the supplied screenshots. No live
API requests or automated tests were executed.
