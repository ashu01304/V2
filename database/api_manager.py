import sqlite3
from pathlib import Path

import pandas as pd


API_DB_PATH = Path(__file__).resolve().parents[1] / "api_market_data.db"


class APIDatabaseManager:
    def __init__(self, path=API_DB_PATH):
        self.conn = sqlite3.connect(path)
        self.cursor = self.conn.cursor()
        self._initialize_tables()

    def _initialize_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS ohlc (
                instrument TEXT NOT NULL,
                interval   TEXT NOT NULL,
                time       INTEGER NOT NULL,
                open       REAL,
                high       REAL,
                low        REAL,
                close      REAL,
                volume     REAL,
                PRIMARY KEY (instrument, interval, time)
            )
        """)
        self.conn.commit()

    def save_ohlc(self, data, interval):
        rows = [
            (
                instrument,
                interval.upper(),
                candle["time"],
                candle.get("open"),
                candle.get("high"),
                candle.get("low"),
                candle.get("close"),
                candle.get("volume"),
            )
            for instrument, candles in data.items()
            for candle in candles
        ]
        self.cursor.executemany(
            "INSERT OR REPLACE INTO ohlc VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        self.conn.commit()
        return len(rows)

    def has_instrument(self, instrument, interval="1D"):
        return self.cursor.execute(
            "SELECT 1 FROM ohlc WHERE instrument = ? AND interval = ? LIMIT 1",
            (instrument, interval.upper()),
        ).fetchone() is not None

    def has_coverage(self, instrument, interval="1D", count=None, start=None, end=None):
        size, first, last = self.cursor.execute(
            "SELECT COUNT(*), MIN(time), MAX(time) FROM ohlc WHERE instrument = ? AND interval = ?",
            (instrument, interval.upper()),
        ).fetchone()
        if not size or (count is not None and size < count):
            return False
        scale = 1000 if last >= 10**11 else 1
        return not ((start is not None and first > start * scale) or
                    (end is not None and last < end * scale))

    def load_ohlc(self, instruments, interval="1D"):
        if isinstance(instruments, str):
            instruments = [instruments]
        result = {}
        for instrument in instruments:
            frame = pd.read_sql_query(
                """SELECT time, open, high, low, close, volume FROM ohlc
                   WHERE instrument = ? AND interval = ? ORDER BY time""",
                self.conn,
                params=(instrument, interval.upper()),
            )
            if frame.empty:
                continue
            unit = "ms" if frame["time"].abs().max() >= 10**11 else "s"
            frame["Date"] = pd.to_datetime(frame.pop("time"), unit=unit)
            frame.rename(columns={c: c.title() for c in frame.columns}, inplace=True)
            frame.set_index("Date", inplace=True)
            result[instrument] = frame
        return result

    def close(self):
        self.conn.close()
