from pathlib import Path

import duckdb
import pandas as pd

from .config import DATABASE_PATH


class MarketData:
    def __init__(self, path=DATABASE_PATH):
        self.connection = duckdb.connect(str(Path(path)), read_only=True)
        # Compatibility for existing analysis code that executes settlement SQL.
        self.cursor = self.connection
        self.conn = self.connection
        self._closed = False
        self._live_cache = None

    def get_contract_history(self, symbol, codes):
        """Return the settlement format expected by seasonality/backtesting."""
        histories = {}
        for code in codes:
            if symbol == "CL-CO":
                frame = self.connection.execute("""
                    SELECT cl.trading_date AS Date, cl.price - co.price AS Close
                    FROM seac_settlements cl
                    JOIN seac_settlements co
                      ON co.trading_date=cl.trading_date
                     AND co.contract_code=cl.contract_code
                    WHERE cl.symbol='CL' AND co.symbol='CO'
                      AND cl.contract_code=?
                    ORDER BY cl.trading_date
                """, [code]).fetchdf()
            else:
                frame = self.connection.execute("""
                    SELECT trading_date AS Date, price AS Close
                    FROM seac_settlements
                    WHERE symbol=? AND contract_code=?
                    ORDER BY trading_date
                """, [symbol, code]).fetchdf()
            if frame.empty:
                continue
            frame["Date"] = pd.to_datetime(frame["Date"])
            frame.set_index("Date", inplace=True)
            histories[code] = frame
        return histories

    def spread(self, instrument, start=None, end=None):
        conditions = ["instrument = ?"]
        parameters = [instrument]
        if start is not None:
            conditions.append("timestamp >= ?")
            parameters.append(start)
        if end is not None:
            conditions.append("timestamp <= ?")
            parameters.append(end)
        return self.connection.execute(f"""
            SELECT timestamp, price FROM spread_prices
            WHERE {' AND '.join(conditions)} ORDER BY timestamp
        """, parameters).fetchdf()

    def settlement(self, symbol, contract_code):
        return self.connection.execute("""
            SELECT trading_date, price FROM seac_settlements
            WHERE symbol=? AND contract_code=? ORDER BY trading_date
        """, [symbol, contract_code]).fetchdf()

    def synthetic(self, product, expression, start=None, end=None):
        """Return stored history with recent live-cache gaps filled."""
        from .cache import LivePriceCache
        from .contracts import minute_series, parse

        if self._live_cache is None:
            self._live_cache = LivePriceCache()
        cache_product = "CO" if product.upper() in ("CO", "LCO", "BRN") else product.upper()
        cached = self._live_cache.expression_history(
            cache_product, parse(expression), start, end)
        try:
            official = minute_series(self.spread, product, expression, start, end)
        except ValueError:
            if not cached.empty:
                return cached
            raise
        if cached.empty:
            return official
        official = official.copy()
        official["timestamp"] = pd.to_datetime(official["timestamp"], utc=True).dt.floor("min")
        cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True).dt.floor("min")
        return (pd.concat([cached, official]).drop_duplicates("timestamp", keep="last")
                .sort_values("timestamp").reset_index(drop=True))

    def synthetic_ohlc(self, product, expression, interval="30min",
                       start=None, end=None):
        """Calculate synthetic minute values and resample them to OHLC."""
        minute = self.synthetic(product, expression, start, end)
        return minute.set_index("timestamp")["price"].resample(interval).ohlc().dropna()

    def spreads(self, pattern="%"):
        return [row[0] for row in self.connection.execute("""
            SELECT instrument FROM spread_instruments
            WHERE instrument ILIKE ? ORDER BY instrument
        """, [pattern]).fetchall()]

    def close(self):
        if not self._closed:
            if self._live_cache is not None:
                self._live_cache.close()
            self.connection.close()
            self._closed = True
