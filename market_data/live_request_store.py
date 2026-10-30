"""Shared live demand and durable quoted-minute storage with atomic publication."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import math
import shutil
from uuid import uuid4
import pandas as pd
from .config import DATABASE_PATH, DATA_DIR, MINUTE_PRODUCT_NAMES, LIVE_MINUTE_PUBLISH_SECONDS, LIVE_SUBSCRIPTION_TTL_SECONDS
from .contracts import _stored_contract_code
from .database import MasterDatabase
from .publication import atomic_publish, writer_lock

DEFAULT_PATH=Path(__file__).parent/'runtime/structure_requests.sqlite3'
PUBLISH_SECONDS=LIVE_MINUTE_PUBLISH_SECONDS
SPOOL_PATH=DATA_DIR/'live_minute_buffer.sqlite3'
logger=logging.getLogger(__name__)


def load_contract_expiries():
    frame=pd.read_csv(DATA_DIR/'expiry_dates.csv',dtype=str)
    return {(MINUTE_PRODUCT_NAMES.get(row.symbol,row.symbol),row.contract_code):
            datetime.fromisoformat(row.expiry_date).date() for row in frame.itertuples(index=False)}


def quoted_spread_closes(records,instruments,expiries):
    """Translate full-year quoted calendars to the minute source's instrument codes."""
    known=set(instruments)
    aliases={}
    rows=[]
    for minute,product,contract,price,_observed in records:
        product=MINUTE_PRODUCT_NAMES.get(product,product)
        first,second=contract.split('-')
        day=datetime.fromtimestamp(minute,timezone.utc).date()
        if day not in aliases:
            candidates={}
            for (symbol,full_code),expiry in expiries.items():
                if expiry>=day:
                    key=(symbol,_stored_contract_code(symbol,full_code))
                    if key not in candidates or expiry<candidates[key][0]:
                        candidates[key]=(expiry,full_code)
            aliases[day]={key:code for key,(_expiry,code) in candidates.items()}
        compressed=[_stored_contract_code(product,code) for code in (first,second)]
        if any(short!=full and aliases[day].get((product,short))!=full
               for full,short in zip((first,second),compressed)):
            continue  # Never map a one-digit source code to the wrong decade.
        full_name=product+contract
        instrument=full_name if full_name in known else product+'-'.join(compressed)
        rows.append((datetime.fromtimestamp(minute,timezone.utc),instrument,price))
    return pd.DataFrame(rows,columns=['timestamp','instrument','price'])


class RequestStore:
    def __init__(self,path=DEFAULT_PATH,clock=time.time):
        self.path=Path(path)
        self.clock=clock
        self.lock=threading.RLock()
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS shared_structures (
                id TEXT PRIMARY KEY, expression TEXT NOT NULL UNIQUE,
                plan TEXT NOT NULL, expires_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS shared_required_contracts (
                request_id TEXT REFERENCES shared_structures(id) ON DELETE CASCADE,
                tt_id TEXT, metadata TEXT NOT NULL, PRIMARY KEY(request_id,tt_id));
              CREATE TABLE IF NOT EXISTS shared_published_quotes (
                request_id TEXT PRIMARY KEY REFERENCES shared_structures(id) ON DELETE CASCADE,
                payload TEXT NOT NULL, computed_at REAL NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            db=sqlite3.connect(self.path,timeout=10)
            db.execute('PRAGMA foreign_keys=ON')
            db.row_factory=sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def register(self,owner,plans,ttl=LIVE_SUBSCRIPTION_TTL_SECONDS):
        if not 30<=ttl<=LIVE_SUBSCRIPTION_TTL_SECONDS:
            raise ValueError(f'Lease must be between 30 and {LIVE_SUBSCRIPTION_TTL_SECONDS} seconds')
        expires=self.clock()+ttl
        result=[]
        with self.connection() as db:
            db.execute('DELETE FROM shared_structures WHERE expires_at<=?',[self.clock()])
            for plan in plans:
                key=hashlib.sha256(plan['expression'].encode()).hexdigest()[:32]
                db.execute('''INSERT INTO shared_structures VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET plan=excluded.plan,expires_at=excluded.expires_at''',
                    [key,plan['expression'],json.dumps(plan),expires])
                db.execute('DELETE FROM shared_required_contracts WHERE request_id=?',[key])
                db.executemany('INSERT INTO shared_required_contracts VALUES (?,?,?)',
                    [(key,tt,json.dumps(meta)) for tt,meta in plan['contracts'].items()])
                result.append(dict(request_id=key,expression=plan['expression'],expires_at=expires,
                                   required_ids=list(plan['contracts'])))
        return result

    def renew(self,owner,ids,ttl=LIVE_SUBSCRIPTION_TTL_SECONDS):
        if not 30<=ttl<=LIVE_SUBSCRIPTION_TTL_SECONDS:
            raise ValueError(f'Lease must be between 30 and {LIVE_SUBSCRIPTION_TTL_SECONDS} seconds')
        count=0
        with self.connection() as db:
            for key in ids:
                count+=db.execute('UPDATE shared_structures SET expires_at=? WHERE id=? AND expires_at>?',
                    [self.clock()+ttl,key,self.clock()]).rowcount
        return count

    def release(self,owner,key):
        # A caller cannot delete a quote another caller may still need.
        # Shared demand is removed only by inactivity expiry.
        return False

    def active(self):
        with self.connection() as db:
            db.execute('DELETE FROM shared_structures WHERE expires_at<=?',[self.clock()])
            return [dict(request_id=row['id'],expires_at=row['expires_at'],
                         **json.loads(row['plan'])) for row in db.execute('SELECT * FROM shared_structures ORDER BY expression')]

    def desired(self):
        return {tt:meta for row in self.active() for tt,meta in row['contracts'].items()}

    def publish(self,key,payload):
        with self.connection() as db:
            db.execute('''INSERT INTO shared_published_quotes SELECT id,?,? FROM shared_structures
                WHERE id=? AND expires_at>? ON CONFLICT(request_id) DO UPDATE
                SET payload=excluded.payload,computed_at=excluded.computed_at''',
                [json.dumps(payload,allow_nan=False),self.clock(),key,self.clock()])

    def read(self,key):
        with self.connection() as db:
            row=db.execute('''SELECT r.*,q.payload,q.computed_at FROM shared_structures r
                LEFT JOIN shared_published_quotes q ON r.id=q.request_id WHERE r.id=? AND r.expires_at>?''',
                [key,self.clock()]).fetchone()
            if row is None:
                return None
            expires=self.clock()+LIVE_SUBSCRIPTION_TTL_SECONDS
            db.execute('UPDATE shared_structures SET expires_at=? WHERE id=?',[expires,key])
            return dict(request_id=key,expression=row['expression'],expires_at=expires,
                        computed_at=row['computed_at'],quote=json.loads(row['payload']) if row['payload'] else None)

    def clear_quotes(self):
        with self.connection() as db:
            db.execute('DELETE FROM shared_published_quotes')




class LiveMinuteRecorder:
    def __init__(self, database_path=DATABASE_PATH, spool_path=SPOOL_PATH,
                 publish_seconds=PUBLISH_SECONDS):
        self.database_path = Path(database_path).resolve()
        self.spool_path = Path(spool_path).resolve()
        self.publish_seconds = publish_seconds
        if not isinstance(publish_seconds,(int,float)) or not math.isfinite(publish_seconds) or publish_seconds<60:
            raise ValueError('Minute publication interval must be at least 60 seconds')
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._owner = None
        self._status = {"last_publish_at": None, "last_error": None,
                        "queued_closes": 0, "last_published_spreads": 0,
                        "last_received_at": None, "received_updates": 0}

    def record(self, metadata, bid, ask, received_at):
        """Capture actual post-snapshot calendar books, without outright synthesis."""
        if bid is None or ask is None or not math.isfinite(bid) or not math.isfinite(ask) or bid > ask:
            return
        if bid == -100 or ask == -100:
            return
        weights = metadata.get('weights', {})
        products = {key.split(':', 1)[0] for key in weights}
        if len(products) != 1 or len(weights) != 2:
            return  # No flies, boxes, outrights or arbitrary dashboard structures.
        product = next(iter(products))
        if product not in MINUTE_PRODUCT_NAMES and product not in {'CL', 'HO', 'NG', 'RB'}:
            return
        legs = sorted(((key.split(':', 1)[1], weight) for key, weight in weights.items()),
                      key=lambda item: (int(item[0][1:]), 'FGHJKMNQUVXZ'.index(item[0][0])))
        if abs(legs[0][1]) != 1 or legs[1][1] != -legs[0][1]:
            return
        if received_at.tzinfo is None:
            raise ValueError('Minute recording requires a timezone-aware receipt time.')
        price = (bid / 2 + ask / 2) / legs[0][1]
        if not math.isfinite(price):
            return
        observed = received_at.timestamp()
        minute = int(observed // 60) * 60
        key = (minute, product, legs[0][0] + '-' + legs[1][0])
        with self._lock:
            previous = self._pending.get(key)
            if previous is None or observed >= previous[1]:
                self._pending[key] = (float(price), observed)
                self._status.update(last_received_at=received_at.isoformat(),
                                    received_updates=self._status['received_updates']+1)

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
                CREATE TABLE IF NOT EXISTS quoted_minute_closes (
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
                    INSERT INTO quoted_minute_closes VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(minute, product, contract) DO UPDATE
                    SET price=excluded.price, observed_at=excluded.observed_at
                    WHERE excluded.observed_at >= quoted_minute_closes.observed_at
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
                "SELECT COUNT(*) FROM quoted_minute_closes").fetchone()[0]

    def _publish(self, connection, now):
        # Allow in-flight callbacks to finish before sealing a minute.
        cutoff = int((now - timedelta(seconds=2)).timestamp() // 60) * 60
        records = connection.execute(
            "SELECT * FROM quoted_minute_closes WHERE minute < ? ORDER BY minute, product, contract",
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
                    frame = quoted_spread_closes(records, database.registered_spreads(), load_contract_expiries())
                    database.register_spreads(frame["instrument"].unique().tolist())
                    accepted = database.save_live_spreads(frame)
                    if accepted:
                        database.finish()
                finally:
                    database.close()
                if accepted:
                    atomic_publish(temporary, self.database_path)
                # Only acknowledge the durable buffer after publication succeeds.
                with connection:
                    connection.execute("DELETE FROM quoted_minute_closes WHERE minute < ?", (cutoff,))
                with self._lock:
                    self._status.update(
                        last_publish_at=now.isoformat(), last_error=None,
                        last_published_spreads=accepted,
                        queued_closes=connection.execute(
                            "SELECT COUNT(*) FROM quoted_minute_closes").fetchone()[0],
                    )
                logger.info("Minute history published: %s provisional spreads; %s quoted calendar closes consumed",
                            accepted, len(records))
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + ".wal").unlink(missing_ok=True)

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._owner = writer_lock(self.spool_path, timeout=0)
        self._owner.__enter__()
        try:
            with closing(self._connect()) as connection:
                with self._lock:
                    self._status["queued_closes"] = connection.execute(
                        "SELECT COUNT(*) FROM quoted_minute_closes").fetchone()[0]
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
