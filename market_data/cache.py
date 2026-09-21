"""Small persistent cache for recent live outright prices."""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import CACHE_HOURS, CACHE_PATH


class LivePriceCache:
    def __init__(self, path=CACHE_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS live_prices (
                minute TEXT NOT NULL,
                product TEXT NOT NULL,
                contract TEXT NOT NULL,
                price REAL NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY (minute, product, contract)
            )
        """)
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS live_product_contract "
            "ON live_prices(product, contract, minute)"
        )
        self.connection.commit()

    def save_snapshot(self, rows, now=None):
        now = now or datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0).isoformat()
        values = [(minute, row["product"], row["contract"], row["value"],
                   row["received_at"]) for row in rows if row.get("value") is not None]
        with self.lock, self.connection:
            self.connection.executemany("""
                    INSERT INTO live_prices VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(minute, product, contract) DO UPDATE SET
                        price=excluded.price, received_at=excluded.received_at
                """, values)
            self._prune(now)
        return len(values)

    def _prune(self, now=None):
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=CACHE_HOURS)
        self.connection.execute("DELETE FROM live_prices WHERE minute < ?", (cutoff.isoformat(),))

    def history(self, product, contract, start=None, end=None):
        conditions, parameters = ["product=?", "contract=?"], [product, contract]
        if start:
            conditions.append("minute>=?")
            parameters.append(self._utc(start))
        if end:
            conditions.append("minute<=?")
            parameters.append(self._utc(end))
        with self.lock:
            return pd.read_sql_query(
                f"SELECT minute AS timestamp, price FROM live_prices "
                f"WHERE {' AND '.join(conditions)} ORDER BY minute",
                self.connection, params=parameters, parse_dates=["timestamp"])

    @staticmethod
    def _utc(value):
        stamp = pd.Timestamp(value)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        return stamp.isoformat()

    def expression_history(self, product, legs, start=None, end=None):
        frames = []
        for coefficient, contract in legs:
            frame = self.history(product, contract, start, end)
            if frame.empty:
                return pd.DataFrame(columns=["timestamp", "price"])
            frames.append((coefficient, frame.set_index("timestamp")["price"]))
        aligned = pd.concat([series for _, series in frames], axis=1, join="inner").dropna()
        if aligned.empty:
            return pd.DataFrame(columns=["timestamp", "price"])
        aligned.columns = range(len(aligned.columns))
        price = sum(coefficient * aligned[index]
                    for index, (coefficient, _) in enumerate(frames))
        return price.rename("price").reset_index()

    def close(self):
        with self.lock:
            self.connection.close()
