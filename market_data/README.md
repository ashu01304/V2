# Master Market Database

This package is the central data interface for the project. It downloads,
updates, stores, and queries minute spread prices and daily SEAC settlements.

## Database

The database is stored at:

```text
data/master_database.duckdb
```

It contains:

- `spread_prices(timestamp, instrument, price)`
- `spread_instruments(instrument, first_seen, last_seen)`
- `seac_settlements(symbol, contract_code, trading_date, price)`

Exact `-100` spread values are treated as unavailable and are not stored.
Negative values other than `-100` are valid prices.

## Requirements

```powershell
pip install duckdb pandas requests ijson
```

The corporate Insight certificate is not available to Python, so both internal
downloads currently use `verify=False` and suppress the related warning.

## Updating the database

The completed spread database is already stored as `master_database.duckdb`.
Run the same command for the first SEAC update and every later update:

```powershell
python -m market_data
```

### Automatic hourly updates

Double-click `start_market_data_sync.bat` in the project root. It starts a
background process that updates immediately and then once every hour. Only one
copy can run at a time. Progress and errors are written to
`data/market_data_sync.log`.

The full source files are downloaded because neither source provides a known
incremental endpoint. Only new/recent spread records and new or revised SEAC
settlements are written. Downloaded files are deleted after processing.

Updates never modify the master database directly. A temporary copy is updated,
checkpointed, and closed first. Only a completely successful copy replaces the
master. If the process stops or fails, the existing master remains unchanged.

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
- Contract construction and synthetic structures should consume stored spread
  prices; outright prices are not stored.
