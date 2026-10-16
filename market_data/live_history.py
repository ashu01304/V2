"""Buffer received minute closes durably; publish provisional spreads every ten minutes."""

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from uuid import uuid4

import pandas as pd

from .config import DATABASE_PATH, DATA_DIR
from .contracts import _stored_contract_code
from .database import MasterDatabase
from .publication import atomic_publish, writer_lock

PUBLISH_SECONDS = 10 * 60
SPOOL_PATH = DATA_DIR / "live_minute_buffer.sqlite3"
CONTRACT_PAIR = re.compile(r"([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})")
logger = logging.getLogger(__name__)


def load_contract_expiries():
    """Use explicit full-year expiry dates to disambiguate compressed source codes."""
    frame = pd.read_csv(DATA_DIR / "expiry_dates.csv", dtype=str)
    return {
        ("LCO" if row.symbol == "CO" else row.symbol, row.contract_code):
            datetime.fromisoformat(row.expiry_date).date()
        for row in frame.itertuples(index=False)
    }


def spread_closes(records, instruments, expiries=None):
    """Use both outright closes from the same minute; never carry a leg forward."""
    groups = defaultdict(dict)
    aliases = {}
    for minute, product, contract, price, _observed in records:
        stored_product = "LCO" if product == "CO" else product
        code = _stored_contract_code(stored_product, contract)
        if expiries is not None and code != contract:
            day = datetime.fromtimestamp(minute, timezone.utc).date()
            if day not in aliases:
                candidates = {}
                for (expiry_product, full_code), expiry in expiries.items():
                    if expiry >= day:
                        alias = (expiry_product, _stored_contract_code(expiry_product, full_code))
                        previous = candidates.get(alias)
                        if previous is None or expiry < previous[0]:
                            candidates[alias] = (expiry, full_code)
                aliases[day] = {alias: full_code for alias, (_, full_code) in candidates.items()}
            expected = aliases[day].get((stored_product, code))
            if expected is not None and contract != expected:
                continue
        group = groups[(minute, stored_product)]
        # One-digit source years must not silently choose between two decades.
        if code in group and (group[code] is None or group[code][0] != contract):
            group[code] = None
        else:
            group[code] = (contract, price)
    rows = []
    for (minute, product), closes in groups.items():
        for instrument in instruments:
            if not instrument.startswith(product):
                continue
            pair = CONTRACT_PAIR.fullmatch(instrument[len(product):])
            if not pair:
                continue
            first, second = (closes.get(code) for code in pair.groups())
            if first is None or second is None:
                continue
            price = first[1] - second[1]
            if math.isfinite(price):
                rows.append((datetime.fromtimestamp(minute, timezone.utc), instrument, price))
    return pd.DataFrame(rows, columns=["timestamp", "instrument", "price"])


class LiveMinuteRecorder:
    def __init__(self, database_path=DATABASE_PATH, spool_path=SPOOL_PATH,
                 publish_seconds=PUBLISH_SECONDS):
        self.database_path = Path(database_path).resolve()
        self.spool_path = Path(spool_path).resolve()
        self.publish_seconds = publish_seconds
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._owner = None
        self._status = {"last_publish_at": None, "last_error": None,
                        "queued_closes": 0, "last_published_spreads": 0}

    def record(self, product, contract, price, received_at):
        if price is None or not math.isfinite(price) or price == -100:
            return
        if received_at.tzinfo is None:
            raise ValueError("Minute recording requires a timezone-aware receipt time.")
        observed = received_at.timestamp()
        minute = int(observed // 60) * 60
        key = (minute, product, contract)
        with self._lock:
            previous = self._pending.get(key)
            if previous is None or observed >= previous[1]:
                self._pending[key] = (float(price), observed)

    def health(self):
        with self._lock:
            return {**self._status, "pending_updates": len(self._pending),
                    "publish_interval_seconds": self.publish_seconds,
                    "running": self._thread is not None and self._thread.is_alive()}

    def _connect(self):
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.spool_path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS minute_closes (
                    minute INTEGER NOT NULL, product TEXT NOT NULL,
                    contract TEXT NOT NULL, price REAL NOT NULL,
                    observed_at REAL NOT NULL,
                    PRIMARY KEY (minute, product, contract)
                )
            """)
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    def _persist(self, connection):
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return
        records = [(*key, *value) for key, value in pending.items()]
        try:
            with connection:
                connection.executemany("""
                    INSERT INTO minute_closes VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(minute, product, contract) DO UPDATE
                    SET price=excluded.price, observed_at=excluded.observed_at
                    WHERE excluded.observed_at >= minute_closes.observed_at
                """, records)
        except Exception:
            with self._lock:
                for key, value in pending.items():
                    current = self._pending.get(key)
                    if current is None or value[1] > current[1]:
                        self._pending[key] = value
            raise
        with self._lock:
            self._status["queued_closes"] = connection.execute(
                "SELECT COUNT(*) FROM minute_closes").fetchone()[0]

    def _publish(self, connection, now):
        # Allow in-flight callbacks to finish before sealing a minute.
        cutoff = int((now - timedelta(seconds=2)).timestamp() // 60) * 60
        records = connection.execute(
            "SELECT * FROM minute_closes WHERE minute < ? ORDER BY minute, product, contract",
            (cutoff,),
        ).fetchall()
        if not records:
            return
        if not self.database_path.exists():
            raise FileNotFoundError("Run market_data.watch once to create the master database.")
        temporary = self.database_path.parent / f".live_{uuid4().hex}.duckdb"
        try:
            with writer_lock(self.database_path, timeout=0):
                shutil.copy2(self.database_path, temporary)
                database = MasterDatabase(temporary)
                try:
                    instruments = [row[0] for row in database.connection.execute(
                        "SELECT instrument FROM spread_instruments").fetchall()]
                    frame = spread_closes(records, instruments, load_contract_expiries())
                    accepted = database.save_live_spreads(frame)
                    if accepted:
                        database.finish()
                finally:
                    database.close()
                if accepted:
                    atomic_publish(temporary, self.database_path)
                # Only acknowledge the durable buffer after publication succeeds.
                with connection:
                    connection.execute("DELETE FROM minute_closes WHERE minute < ?", (cutoff,))
                with self._lock:
                    self._status.update(
                        last_publish_at=now.isoformat(), last_error=None,
                        last_published_spreads=accepted,
                        queued_closes=connection.execute(
                            "SELECT COUNT(*) FROM minute_closes").fetchone()[0],
                    )
                logger.info("Minute history published: %s provisional spreads; %s outright closes consumed",
                            accepted, len(records))
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + ".wal").unlink(missing_ok=True)

    def start(self):
        self._owner = writer_lock(self.spool_path, timeout=0)
        self._owner.__enter__()
        try:
            with closing(self._connect()) as connection:
                with self._lock:
                    self._status["queued_closes"] = connection.execute(
                        "SELECT COUNT(*) FROM minute_closes").fetchone()[0]
            self._thread = threading.Thread(target=self._run, name="live-minute-recorder",
                                            daemon=True)
            self._thread.start()
            logger.info("Minute-close recorder ready | buffer: %s | publish interval: %s seconds",
                        self.spool_path, self.publish_seconds)
        except BaseException:
            self._owner.__exit__(None, None, None)
            self._owner = None
            raise

    def _run(self):
        try:
            with closing(self._connect()) as connection:
                next_publish = time.monotonic() + self.publish_seconds
                while not self._stop.is_set():
                    try:
                        self._persist(connection)
                        if time.monotonic() >= next_publish:
                            self._publish(connection, datetime.now(timezone.utc))
                            next_publish = time.monotonic() + self.publish_seconds
                    except Exception as error:
                        with self._lock:
                            self._status["last_error"] = str(error)
                        if isinstance(error, TimeoutError):
                            logger.info("Minute publication deferred while the manual importer is writing.")
                        else:
                            logger.exception("Minute history update failed; buffered rows retained.")
                        next_publish = time.monotonic() + 60
                        self._stop.wait(5)
                    self._stop.wait(1)
                self._persist(connection)
                # The current incomplete minute stays buffered for the next run.
                try:
                    self._publish(connection, datetime.now(timezone.utc))
                except Exception:
                    logger.exception("Final minute publication deferred; durable buffer retained.")
        except Exception as error:
            with self._lock:
                self._status["last_error"] = str(error)
            logger.exception("Minute recorder stopped unexpectedly.")

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._owner is not None:
            self._owner.__exit__(None, None, None)
            self._owner = None
