# Market data

The unified service uses the `data` folder and API port `8060`.

This package provides live outright prices, historical minute spreads, and daily
SEAC settlements.

## Data sources

| Source | Format | Use |
|---|---|---|
| Lightstreamer | Outrights such as `CL X26` and `CO Z26` | Latest live prices |
| Historical download | Spreads such as `CLX26-Z26` | Minute history |
| SEAC download | Daily outright settlements | Seasonality and backtests |

Live expressions are calculated from outright legs. Historical expressions are
reconstructed from stored spreads.

## Storage

`data/master_database.duckdb` contains:

```text
spread_prices(timestamp, instrument, price)
spread_instruments(instrument, first_seen, last_seen)
seac_settlements(symbol, contract_code, trading_date, price)
```

`data/live_price_cache.sqlite` keeps the latest three hours of
minute outright prices. Rows are unique by product, contract, and minute.

`-100` means unavailable and is not stored. Other negative prices are valid.

## Run

Start this one process manually:

```powershell
python -m market_data.live_service --products CL BRN NG DBI FCPO G GC HG HO RB SI
```

In another terminal, start the dashboard:

```powershell
python -m market_data.dashboard
```

Open `http://127.0.0.1:8062`. It shows service health, live outright prices,
the three-hour cache, and merged expression history.

It continuously receives live outrights, saves a minute snapshot to the
three-hour cache, updates historical spreads at startup and every 30 minutes,
and updates SEAC at startup and every six hours. Updates use a transaction and
never copy the full DuckDB file. The three-hour overlap recovers source data
that initially arrives 30-60 minutes late.

## Python access

```python
from market_data import MarketData

data = MarketData()
spread = data.spread("CLV26-X26", start="2026-09-01")
minute = data.synthetic("CL", "V26-2*X26+Z26")
candles = data.synthetic_ohlc("CL", "V26-2*X26+Z26", interval="30min")
settlements = data.settlement("CO", "Z26")
data.close()
```

Stored spread data has priority. Cache-derived points fill recent missing
minutes. Historical expressions must be zero-sum because they are built from
spreads. CO historical symbols use `LCO` and may use one-digit years.

## Live API

The service listens on `http://127.0.0.1:8060`.

```text
GET /health
GET /snapshot?product=CL&product=CO
GET /latest?product=CL&contract=X26
GET /expression/latest?product=CL&expression=X26-2*Z26+F27
GET /history?product=CL&contract=X26
GET /expression/history?product=CL&expression=X26-2*Z26+F27
GET /settlement/history?product=CL&contract=X26
```

```python
from market_data import LiveMarketDataClient

live = LiveMarketDataClient()
price = live.expression("CL", "X26-2*Z26+F27")
live.close()
```

Live values use `AdminPrice`, then `LastPrice`, then bid/ask midpoint. Stale
prices are rejected. `/history` returns cached outright minutes.
`/expression/history` returns merged stored and cached expression history.
`/health` includes the latest successful scheduled updates and any updater error.
