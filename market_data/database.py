from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

from .config import BATCH_SIZE, DUCKDB_MEMORY_LIMIT


class MasterDatabase:
    def __init__(self, path, read_only=False):
        self.connection = duckdb.connect(str(Path(path)), read_only=read_only)
        if not read_only:
            self.connection.execute("SET memory_limit = ?", [DUCKDB_MEMORY_LIMIT])
            self.connection.execute("PRAGMA threads=2")
            self._initialize()
            self._initialize_live_provenance()

    def _initialize(self):
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS spread_prices (
                timestamp TIMESTAMPTZ NOT NULL,
                instrument VARCHAR NOT NULL,
                price DOUBLE NOT NULL
            )
        """)
        self.connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS spread_price_key
            ON spread_prices (instrument, timestamp)
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS spread_instruments (
                instrument VARCHAR PRIMARY KEY,
                first_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMPTZ
            )
        """)
        self.connection.execute("""
            INSERT INTO spread_instruments (instrument)
            SELECT DISTINCT instrument FROM spread_prices
            ON CONFLICT DO NOTHING
        """)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS seac_settlements (
                symbol VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL,
                trading_date DATE NOT NULL,
                price DOUBLE,
                PRIMARY KEY (symbol, contract_code, trading_date)
            )
        """)

    def _initialize_live_provenance(self):
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS live_spread_keys (
                timestamp TIMESTAMPTZ NOT NULL,
                instrument VARCHAR NOT NULL,
                PRIMARY KEY (instrument, timestamp)
            )
        """)

    def spread_state(self, overlap_minutes):
        known = {row[0] for row in self.connection.execute(
            "SELECT instrument FROM spread_instruments").fetchall()}
        latest = self.connection.execute("""
            SELECT MAX(p.timestamp) FROM spread_prices p
            WHERE NOT EXISTS (
                SELECT 1 FROM live_spread_keys l
                WHERE l.instrument=p.instrument AND l.timestamp=p.timestamp
            )
        """).fetchone()[0]
        cutoff = latest - timedelta(minutes=overlap_minutes) if latest else None
        provisional = self.connection.execute(
            "SELECT MIN(timestamp) FROM live_spread_keys").fetchone()[0]
        if cutoff is not None and provisional is not None:
            cutoff = min(cutoff, provisional)
        return known, cutoff

    def register_spreads(self, instruments):
        frame = pd.DataFrame({"instrument": list(instruments)})
        self._register_insert("new_instruments", frame, """
            INSERT INTO spread_instruments (instrument)
            SELECT instrument FROM new_instruments ON CONFLICT DO NOTHING
        """)

    def save_spreads(self, frame):
        if frame.empty:
            return 0
        self._register_insert("new_spreads", frame, """
            INSERT INTO spread_prices
            SELECT timestamp, instrument, price FROM new_spreads
            ON CONFLICT (instrument, timestamp)
            DO UPDATE SET price=excluded.price;
            DELETE FROM live_spread_keys
            USING new_spreads
            WHERE live_spread_keys.instrument=new_spreads.instrument
              AND live_spread_keys.timestamp=new_spreads.timestamp
        """)
        return len(frame)

    def save_live_spreads(self, frame):
        """Fill missing/provisional minutes; never overwrite downloaded history."""
        if frame.empty:
            return 0
        self.connection.register("new_live_spreads", frame)
        try:
            self.connection.execute("""
                INSERT INTO live_spread_keys
                SELECT n.timestamp, n.instrument FROM new_live_spreads n
                WHERE NOT EXISTS (
                    SELECT 1 FROM spread_prices p
                    WHERE p.instrument=n.instrument AND p.timestamp=n.timestamp
                )
                ON CONFLICT DO NOTHING
            """)
            self.connection.execute("""
                INSERT INTO spread_prices
                SELECT n.timestamp, n.instrument, n.price
                FROM new_live_spreads n JOIN live_spread_keys l
                  ON l.instrument=n.instrument AND l.timestamp=n.timestamp
                ON CONFLICT (instrument, timestamp) DO UPDATE SET price=excluded.price
            """)
            return self.connection.execute("""
                SELECT COUNT(*) FROM new_live_spreads n JOIN live_spread_keys l
                ON l.instrument=n.instrument AND l.timestamp=n.timestamp
            """).fetchone()[0]
        finally:
            self.connection.unregister("new_live_spreads")

    def save_settlements(self, frame):
        if frame.empty:
            return 0
        self._register_insert("new_settlements", frame, """
            INSERT INTO seac_settlements
            SELECT symbol, contract_code, trading_date, price FROM new_settlements
            ON CONFLICT (symbol, contract_code, trading_date)
            DO UPDATE SET price=excluded.price
        """)
        return len(frame)

    def _register_insert(self, name, frame, statement):
        if frame.empty:
            return
        self.connection.register(name, frame)
        try:
            self.connection.execute(statement)
        finally:
            self.connection.unregister(name)

    def finish(self):
        self.connection.execute(
            "UPDATE spread_instruments SET last_seen=CURRENT_TIMESTAMP"
        )
        self.connection.execute("CHECKPOINT")

    def row_counts(self):
        spread_count = self.connection.execute(
            "SELECT COUNT(*) FROM spread_prices"
        ).fetchone()[0]
        settlement_count = self.connection.execute(
            "SELECT COUNT(*) FROM seac_settlements"
        ).fetchone()[0]
        return spread_count, settlement_count

    def close(self):
        self.connection.close()


def spread_frames(source_rows, known, cutoff):
    headers = []
    new_instruments = set()
    timestamps, instruments, prices = [], [], []
    for first, values in source_rows:
        if values is None:
            headers = first
            new_instruments = set(headers) - known
            yield "headers", headers
            continue
        recent = cutoff is None or first >= cutoff
        for instrument, price in zip(headers, values):
            if price is None or price == -100:
                continue
            if recent or instrument in new_instruments:
                timestamps.append(first)
                instruments.append(instrument)
                prices.append(float(price))
                if len(prices) >= BATCH_SIZE:
                    frame = pd.DataFrame({
                        "timestamp": timestamps,
                        "instrument": instruments,
                        "price": prices,
                    })
                    timestamps, instruments, prices = [], [], []
                    yield "prices", frame
                    del frame
    if prices:
        yield "prices", pd.DataFrame({
            "timestamp": timestamps,
            "instrument": instruments,
            "price": prices,
        })
