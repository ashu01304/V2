from datetime import timedelta
from pathlib import Path

import duckdb
import pandas as pd

from .config import BATCH_SIZE, DUCKDB_MEMORY_LIMIT, SEAC_PRODUCTS, minute_instrument_allowed


class MasterDatabase:
    def __init__(self, path, read_only=False):
        self._connection = duckdb.connect(str(Path(path)), read_only=read_only)
        if not read_only:
            self._connection.execute("SET memory_limit = ?", [DUCKDB_MEMORY_LIMIT])
            self._connection.execute("PRAGMA threads=2")
            self._initialize()
            self._initialize_live_provenance()

    def _initialize(self):
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS spread_prices (
                timestamp TIMESTAMPTZ NOT NULL,
                instrument VARCHAR NOT NULL,
                price DOUBLE NOT NULL
            )
        """)
        self._connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS spread_price_key
            ON spread_prices (instrument, timestamp)
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS spread_instruments (
                instrument VARCHAR PRIMARY KEY,
                first_seen TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMPTZ
            )
        """)
        self._connection.execute("""
            INSERT INTO spread_instruments (instrument)
            SELECT DISTINCT instrument FROM spread_prices
            ON CONFLICT DO NOTHING
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS seac_settlements (
                symbol VARCHAR NOT NULL,
                contract_code VARCHAR NOT NULL,
                trading_date DATE NOT NULL,
                price DOUBLE,
                PRIMARY KEY (symbol, contract_code, trading_date)
            )
        """)

    def _initialize_live_provenance(self):
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS live_spread_keys (
                timestamp TIMESTAMPTZ NOT NULL,
                instrument VARCHAR NOT NULL,
                PRIMARY KEY (instrument, timestamp)
            )
        """)

    def spread_state(self, overlap_minutes):
        known = {row[0] for row in self._connection.execute(
            "SELECT instrument FROM spread_instruments").fetchall()
            if minute_instrument_allowed(row[0])}
        latest = self._connection.execute("""
            SELECT MAX(p.timestamp) FROM spread_prices p
            WHERE p.instrument IN (SELECT UNNEST(?)) AND NOT EXISTS (
                SELECT 1 FROM live_spread_keys l
                WHERE l.instrument=p.instrument AND l.timestamp=p.timestamp
            )
        """, [sorted(known)]).fetchone()[0]
        cutoff = latest - timedelta(minutes=overlap_minutes) if latest else None
        provisional = self._connection.execute(
            "SELECT MIN(timestamp) FROM live_spread_keys WHERE instrument IN (SELECT UNNEST(?))",
            [sorted(known)]).fetchone()[0]
        if cutoff is not None and provisional is not None:
            cutoff = min(cutoff, provisional)
        return known, cutoff

    def registered_spreads(self):
        """Instruments available to a publisher inside its writer transaction."""
        return [row[0] for row in self._connection.execute(
            "SELECT instrument FROM spread_instruments"
        ).fetchall() if minute_instrument_allowed(row[0])]

    def register_spreads(self, instruments):
        frame = pd.DataFrame({"instrument": [item for item in instruments if minute_instrument_allowed(item)]})
        self._register_insert("new_instruments", frame, """
            INSERT INTO spread_instruments (instrument)
            SELECT instrument FROM new_instruments ON CONFLICT DO NOTHING
        """)

    def save_spreads(self, frame):
        if frame.empty:
            return 0
        frame = frame.loc[frame["instrument"].map(minute_instrument_allowed)]
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

    def fill_live_spread_gaps(self):
        """Replace provisional values with the next fetched price, if available.

        Keep live_spread_keys: these replacements remain estimates, and future
        imports must revisit them even outside the normal overlap window.
        """
        return self._connection.execute('''
            WITH observations AS (
                SELECT p.timestamp,p.instrument,
                    CASE WHEN l.instrument IS NULL AND isfinite(p.price)
                        THEN p.price END AS fetched_price
                FROM spread_prices p LEFT JOIN live_spread_keys l
                    USING(instrument,timestamp)
                WHERE p.instrument IN (SELECT DISTINCT instrument FROM live_spread_keys)
            ), filled AS (
                SELECT timestamp,instrument,
                    FIRST_VALUE(fetched_price IGNORE NULLS) OVER (
                        PARTITION BY instrument ORDER BY timestamp
                        ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS price
                FROM observations
            )
            UPDATE spread_prices p SET price=f.price
            FROM filled f, live_spread_keys l
            WHERE p.instrument=f.instrument AND p.timestamp=f.timestamp
                AND l.instrument=p.instrument AND l.timestamp=p.timestamp
                AND isfinite(f.price) AND p.price IS DISTINCT FROM f.price
        ''').fetchone()[0]

    def minute_cleanup_frames(self, start, end):
        """Read each retained spread separately for bounded maintenance work."""
        instruments = [row[0] for row in self._connection.execute('''
            SELECT DISTINCT instrument FROM spread_prices
            WHERE timestamp>=? AND timestamp<? ORDER BY instrument
        ''', [start, end]).fetchall() if minute_instrument_allowed(row[0])]
        for instrument in instruments:
            frame = self._connection.execute('''
                SELECT timestamp,price FROM spread_prices
                WHERE instrument=? AND timestamp>=? AND timestamp<?
                    AND isfinite(price) ORDER BY timestamp
            ''', [instrument, start, end]).fetchdf()
            yield instrument, frame

    def replace_minute_spikes(self, corrections):
        """Update existing observations without changing their source markers."""
        if corrections.empty:
            return 0
        self._connection.register('minute_corrections', corrections)
        try:
            return self._connection.execute('''
                UPDATE spread_prices p SET price=c.replacement
                FROM minute_corrections c
                WHERE p.instrument=c.instrument AND p.timestamp=c.timestamp
                    AND p.price=c.original
            ''').fetchone()[0]
        finally:
            self._connection.unregister('minute_corrections')

    def save_live_spreads(self, frame):
        """Fill missing/provisional minutes; never overwrite downloaded history."""
        if frame.empty:
            return 0
        frame = frame.loc[frame["instrument"].map(minute_instrument_allowed)]
        if frame.empty:
            return 0
        self._connection.register("new_live_spreads", frame)
        try:
            self._connection.execute("""
                INSERT INTO live_spread_keys
                SELECT n.timestamp, n.instrument FROM new_live_spreads n
                WHERE NOT EXISTS (
                    SELECT 1 FROM spread_prices p
                    WHERE p.instrument=n.instrument AND p.timestamp=n.timestamp
                )
                ON CONFLICT DO NOTHING
            """)
            self._connection.execute("""
                INSERT INTO spread_prices
                SELECT n.timestamp, n.instrument, n.price
                FROM new_live_spreads n JOIN live_spread_keys l
                  ON l.instrument=n.instrument AND l.timestamp=n.timestamp
                ON CONFLICT (instrument, timestamp) DO UPDATE SET price=excluded.price
            """)
            return self._connection.execute("""
                SELECT COUNT(*) FROM new_live_spreads n JOIN live_spread_keys l
                ON l.instrument=n.instrument AND l.timestamp=n.timestamp
            """).fetchone()[0]
        finally:
            self._connection.unregister("new_live_spreads")

    def save_settlements(self, frame):
        if frame.empty:
            return 0
        frame = frame.loc[frame["symbol"].isin(SEAC_PRODUCTS)]
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
        self._connection.register(name, frame)
        try:
            self._connection.execute(statement)
        finally:
            self._connection.unregister(name)

    def finish(self):
        self._connection.execute(
            "UPDATE spread_instruments SET last_seen=CURRENT_TIMESTAMP"
        )
        self._connection.execute("CHECKPOINT")

    def row_counts(self):
        spread_count = self._connection.execute(
            "SELECT COUNT(*) FROM spread_prices"
        ).fetchone()[0]
        settlement_count = self._connection.execute(
            "SELECT COUNT(*) FROM seac_settlements"
        ).fetchone()[0]
        return spread_count, settlement_count

    def close(self):
        self._connection.close()


def spread_frames(source_rows, known, cutoff):
    headers = []
    positions = []
    new_instruments = set()
    timestamps, instruments, prices = [], [], []
    for first, values in source_rows:
        if values is None:
            positions = [(index, instrument) for index, instrument in enumerate(first)
                         if minute_instrument_allowed(instrument)]
            headers = [instrument for _, instrument in positions]
            new_instruments = set(headers) - known
            yield "headers", headers
            continue
        recent = cutoff is None or first >= cutoff
        for index, instrument in positions:
            if index >= len(values):
                continue
            price = values[index]
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


def build_filtered_database(source, destination, progress=print):
    """Rebuild an immutable source into a compact, verified filtered database."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or source == destination:
        raise ValueError("Cleanup destination must be a new database file")
    # Building the full ART price index needs more memory than incremental writes.
    connection = duckdb.connect(str(destination), config={"memory_limit": "2GB", "threads": "1",
                                                         "preserve_insertion_order": "false"})
    try:
        connection.execute("ATTACH '" + str(source).replace("'", "''") + "' AS original (READ_ONLY)")
        schemas = dict(connection.execute("""SELECT table_name, sql FROM duckdb_tables()
            WHERE database_name='original' AND schema_name='main' AND NOT internal""").fetchall())
        expected = {"spread_prices", "spread_instruments", "live_spread_keys", "seac_settlements"}
        if set(schemas) != expected:
            raise ValueError("Unexpected master tables; cleanup stopped to avoid losing unrecognized data")
        indexes = connection.execute("SELECT sql FROM duckdb_indexes() WHERE database_name='original'").fetchall()
        instruments = connection.execute("""SELECT instrument FROM original.spread_instruments
            UNION SELECT DISTINCT instrument FROM original.spread_prices
            UNION SELECT DISTINCT instrument FROM original.live_spread_keys""").fetchall()
        selected = [(code,) for (code,) in instruments if minute_instrument_allowed(code)]
        connection.execute("CREATE TEMP TABLE allowed_minutes(instrument VARCHAR PRIMARY KEY)")
        if selected:
            connection.executemany("INSERT INTO allowed_minutes VALUES (?)", selected)
        for sql in schemas.values():
            connection.execute(sql)
        report = {}
        for table in sorted(expected):
            progress(f"Copying and verifying {table}...", flush=True)
            condition = ("symbol IN (SELECT UNNEST(?))" if table == "seac_settlements" else
                         "instrument IN (SELECT instrument FROM allowed_minutes)")
            parameters = [list(SEAC_PRODUCTS)] if table == "seac_settlements" else []
            before = connection.execute(f'SELECT COUNT(*) FROM original."{table}"').fetchone()[0]
            connection.execute(f'INSERT INTO main."{table}" SELECT * FROM original."{table}" WHERE {condition}', parameters)
            columns = [row[0] for row in connection.execute(f'DESCRIBE main."{table}"').fetchall()]
            names = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
            aggregate = f"COUNT(*), BIT_XOR(HASH({names})), SUM(HASH({names}))"
            retained = connection.execute(f'SELECT {aggregate} FROM original."{table}" WHERE {condition}', parameters).fetchone()
            actual = connection.execute(f'SELECT {aggregate} FROM main."{table}"').fetchone()
            if actual != retained:
                raise RuntimeError(f"Validation failed for {table}; original database was not changed")
            report[table] = {"before": before, "kept": actual[0], "removed": before - actual[0]}
        progress("Rebuilding price index...", flush=True)
        for (sql,) in indexes:
            connection.execute(sql)
        report["minute_symbols"] = [row[0] for row in connection.execute("""
            SELECT DISTINCT regexp_extract(instrument, '^([^0-9]+)[FGHJKMNQUVXZ][0-9]', 1)
            FROM main.spread_prices ORDER BY 1""").fetchall()]
        report["seac_symbols"] = [row[0] for row in connection.execute(
            "SELECT DISTINCT symbol FROM main.seac_settlements ORDER BY symbol").fetchall()]
        report["cl_co_instruments"] = connection.execute("""SELECT COUNT(DISTINCT instrument)
            FROM main.spread_prices WHERE instrument LIKE 'WTCL-LCO%'""").fetchone()[0]
        connection.execute("CHECKPOINT")
        return report
    finally:
        connection.close()
