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

    def get_contract_history(self, symbol, codes):
        """Return the settlement format expected by seasonality/backtesting."""
        histories = {}
        for code in codes:
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
        """Calculate a minute synthetic contract from stored spread series."""
        from .contracts import minute_series
        return minute_series(self.spread, product, expression, start, end)

    def synthetic_ohlc(self, product, expression, interval="30min",
                       start=None, end=None):
        """Calculate synthetic minute values and resample them to OHLC."""
        from .contracts import ohlc
        return ohlc(self.spread, product, expression, interval, start, end)

    def spreads(self, pattern="%"):
        return [row[0] for row in self.connection.execute("""
            SELECT instrument FROM spread_instruments
            WHERE instrument ILIKE ? ORDER BY instrument
        """, [pattern]).fetchall()]

    def close(self):
        if not self._closed:
            self.connection.close()
            self._closed = True
