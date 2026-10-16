"""The read-only interface for all master-database queries."""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

import duckdb
import pandas as pd

from .config import DATABASE_PATH
from .contracts import price_recipe
from .reader_snapshots import acquire_snapshot, release_snapshot


TIMEFRAMES = (15, 30, 60)
MAX_CANDLES = 1_500


@dataclass(frozen=True)
class DatabaseStatus:
    """File metadata for cache invalidation and update labels; no SQL is opened."""

    modified_ns: int
    size: int
    inode: int

    @property
    def signature(self):
        # Browser numbers lose precision above 2**53. Keep signatures exact.
        return [str(self.modified_ns), str(self.size), str(self.inode)]

    @property
    def updated_at(self):
        return datetime.fromtimestamp(self.modified_ns / 1_000_000_000, timezone.utc)


def database_status(path=None):
    """Return the published master file's status, or None if it is missing."""
    try:
        target = DATABASE_PATH if path is None else Path(path)
        stat = target.stat()
    except FileNotFoundError:
        return None
    return DatabaseStatus(stat.st_mtime_ns, stat.st_size, stat.st_ino)


def database_signature(path=None):
    """Return a browser-safe version token, or None when unavailable."""
    try:
        status = database_status(path)
        return status.signature if status is not None else None
    except OSError:
        return None


def _minute_query(recipe):
    """Align all recipe legs to the same minute before combining their prices."""
    placeholders = ", ".join("(?, ?)" for _ in recipe)
    parameters = [value for item in recipe.items() for value in item]
    sql = f"""
        WITH recipe(instrument, weight) AS (VALUES {placeholders})
        SELECT p.timestamp, SUM(p.price * r.weight) AS price
        FROM spread_prices p JOIN recipe r USING (instrument)
        WHERE p.timestamp >= ? AND p.timestamp <= ? AND isfinite(p.price)
        GROUP BY p.timestamp
        HAVING COUNT(DISTINCT p.instrument) = ?
    """
    return sql, parameters


class MarketData:
    def __init__(self, path=DATABASE_PATH):
        self._snapshot_key, snapshot = acquire_snapshot(path)
        try:
            self._connection = duckdb.connect(
                str(snapshot), read_only=True,
                config={"memory_limit": "512MB", "threads": "2"},
            )
        except BaseException:
            if self._snapshot_key is not None:
                release_snapshot(self._snapshot_key)
            raise
        self._closed = False

    def get_contract_history(self, symbol, codes):
        """Return the settlement format expected by seasonality/backtesting."""
        histories = {}
        for code in codes:
            if symbol == "CL-CO":
                frame = self._connection.execute("""
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
                frame = self._connection.execute("""
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
        return self._connection.execute(f"""
            SELECT timestamp, price FROM spread_prices
            WHERE {' AND '.join(conditions)} ORDER BY timestamp
        """, parameters).fetchdf()

    def settlement(self, symbol, contract_code):
        return self._connection.execute("""
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
        return [row[0] for row in self._connection.execute("""
            SELECT instrument FROM spread_instruments
            WHERE instrument ILIKE ? ORDER BY instrument
        """, [pattern]).fetchall()]

    def symbols(self):
        """List products present in daily settlements."""
        return [row[0] for row in self._connection.execute(
            "SELECT DISTINCT symbol FROM seac_settlements ORDER BY symbol"
        ).fetchall()]

    def latest_settlement_date(self, symbol=None):
        sql = "SELECT MAX(trading_date) FROM seac_settlements"
        parameters = []
        if symbol is not None:
            sql += " WHERE symbol = ?"
            parameters.append("CL" if symbol == "CL-CO" else symbol)
        return self._connection.execute(sql, parameters).fetchone()[0]

    def contract_codes(self, symbol, pattern="%"):
        """Return settlement contract codes, optionally matching a SQL LIKE pattern."""
        query_symbol = "CL" if symbol == "CL-CO" else symbol
        return [row[0] for row in self._connection.execute(
            "SELECT DISTINCT contract_code FROM seac_settlements "
            "WHERE symbol = ? AND contract_code LIKE ? ORDER BY contract_code",
            [query_symbol, pattern],
        ).fetchall()]

    def settlement_dates(self, symbol, limit=2):
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Settlement date limit must be a positive integer.")
        query_symbol = "CL" if symbol == "CL-CO" else symbol
        return [row[0] for row in self._connection.execute(
            "SELECT DISTINCT trading_date FROM seac_settlements "
            "WHERE symbol = ? ORDER BY trading_date DESC LIMIT ?",
            [query_symbol, limit],
        ).fetchall()]

    def settlements_on(self, symbol, date):
        """Get valid outright settlement prices for one day, including CL-CO."""
        if symbol == "CL-CO":
            return self._connection.execute("""
                SELECT cl.contract_code, cl.price - co.price AS price
                FROM seac_settlements cl JOIN seac_settlements co
                  ON co.trading_date=cl.trading_date AND co.contract_code=cl.contract_code
                WHERE cl.symbol='CL' AND co.symbol='CO' AND cl.trading_date=?
                  AND cl.price IS NOT NULL AND co.price IS NOT NULL
            """, [date]).fetchdf()
        return self._connection.execute(
            "SELECT contract_code, price FROM seac_settlements "
            "WHERE symbol=? AND trading_date=? AND price IS NOT NULL",
            [symbol, date],
        ).fetchdf()

    def expression_minutes(self, product, expression, start, end):
        """Combine only finite, matching-minute observations for every expression leg."""
        recipe = price_recipe(product, expression)
        sql, parameters = _minute_query(recipe)
        return self._connection.execute(
            sql + " ORDER BY p.timestamp",
            [*parameters, start, end, len(recipe)],
        ).fetchdf()

    def expression_ranges(self, selections, start, end):
        """Fetch multiple expressions' lows/highs in one bounded database query."""
        if not selections:
            return {}
        recipes, parameters = [], []
        for index, (_, product, expression) in enumerate(selections):
            recipe = price_recipe(product, expression)
            for instrument, weight in recipe.items():
                recipes.append("(?, ?, ?, ?)")
                parameters.extend((index, instrument, weight, len(recipe)))
        rows = self._connection.execute(f"""
            WITH recipe(cell, instrument, weight, legs) AS (VALUES {", ".join(recipes)}),
            minutes AS (
                SELECT r.cell, p.timestamp, SUM(p.price * r.weight) AS price
                FROM spread_prices p JOIN recipe r USING (instrument)
                WHERE p.timestamp >= ? AND p.timestamp <= ? AND isfinite(p.price)
                GROUP BY r.cell, p.timestamp
                HAVING COUNT(DISTINCT p.instrument) = MAX(r.legs)
            )
            SELECT cell, MIN(price), MAX(price) FROM minutes GROUP BY cell
        """, [*parameters, start, end]).fetchall()
        bounds = {selections[index][0]: (low, high) for index, low, high in rows}
        return {key: bounds.get(key) for key, _, _ in selections}

    def expression_candles(self, product, expression, timeframe=15, max_candles=MAX_CANDLES):
        """Align minute legs before OHLC aggregation; return newest weekday bars."""
        if timeframe not in TIMEFRAMES:
            raise ValueError("Choose a 15-, 30- or 60-minute timeframe.")
        if not 1 <= max_candles <= MAX_CANDLES:
            raise ValueError(f"Request between 1 and {MAX_CANDLES:,} candles.")
        recipe = price_recipe(product, expression)
        connection = self._connection
        placeholders = ", ".join("?" for _ in recipe)
        bounds = connection.execute(f"""
            SELECT instrument, MIN(timestamp), MAX(timestamp)
            FROM spread_prices WHERE instrument IN ({placeholders})
            GROUP BY instrument
        """, list(recipe)).fetchall()
        missing = set(recipe) - {row[0] for row in bounds}
        if missing:
            raise ValueError("No stored minute data for " + ", ".join(sorted(missing)))
        earliest = max(pd.Timestamp(row[1]) for row in bounds)
        latest = min(pd.Timestamp(row[2]) for row in bounds)
        if earliest > latest:
            raise ValueError("The required spreads have no overlapping history.")
        minute_sql, weights = _minute_query(recipe)
        # Start with a recent window, extending it only when bars are sparse.
        days = max(7, ceil(max_candles * timeframe / 1440 * 7 / 5 * 1.5))
        sql = f"""
            WITH minutes AS ({minute_sql}),
            candles AS (
                SELECT time_bucket(INTERVAL '{timeframe} minutes',
                                   timezone('UTC', timestamp)) AS candle_time,
                       arg_min(price, timestamp) AS open,
                       MAX(price) AS high, MIN(price) AS low,
                       arg_max(price, timestamp) AS close
                FROM minutes
                WHERE isodow(timezone('UTC', timestamp)) BETWEEN 1 AND 5
                GROUP BY candle_time
            )
            SELECT * FROM candles ORDER BY candle_time DESC LIMIT ?
        """
        while True:
            start = max(earliest, latest - pd.Timedelta(days=days))
            candles = connection.execute(
                sql, [*weights, start.to_pydatetime(), latest.to_pydatetime(),
                      len(recipe), max_candles],
            ).fetchdf()
            if len(candles) >= max_candles or start == earliest:
                break
            days *= 2
        if candles.empty:
            raise ValueError("No common weekday minute prices for this expression.")
        candles["candle_time"] = pd.to_datetime(candles["candle_time"], utc=True)
        return candles.set_index("candle_time").sort_index()

    def close(self):
        if not self._closed:
            self._connection.close()
            self._closed = True
            if self._snapshot_key is not None:
                release_snapshot(self._snapshot_key)
