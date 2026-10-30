"""Lab-only request leases and minute cache. Never changes feed subscriptions."""
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import threading

RUNTIME=Path(__file__).parent/'runtime/live_runtime.sqlite3'


class LiveManager:
    def __init__(self,path=RUNTIME):
        self.path=Path(path)
        self.lock=threading.RLock()
        self.history=defaultdict(lambda:deque(maxlen=300))

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        connection=sqlite3.connect(self.path,timeout=10)
        connection.executescript('''
          CREATE TABLE IF NOT EXISTS requests (
            request_key TEXT PRIMARY KEY, expression TEXT, profile TEXT, named_components TEXT,
            last_seen REAL, expires_at REAL);
          CREATE TABLE IF NOT EXISTS minute_quotes (
            request_key TEXT, minute TEXT, profile TEXT, bid REAL, ask REAL, mid REAL,
            PRIMARY KEY(request_key,minute));
        ''')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self,key,request,profile,quote,recipe):
        now=datetime.now(timezone.utc)
        sample=dict(time=quote['asof'],bid=quote['bid'],ask=quote['ask'],mid=quote['mid'])
        with self.lock:
            self.history[key].append(sample)
            with self.connection() as connection:
                connection.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?)',
                    [key,request.text,profile,json.dumps(recipe),now.timestamp(),now.timestamp()+90])
                # Lab observations only: do not treat this as exchange tick-complete OHLC.
                connection.execute('INSERT OR REPLACE INTO minute_quotes VALUES (?,?,?,?,?,?)',
                    [key,now.replace(second=0,microsecond=0).isoformat(),profile,quote['bid'],quote['ask'],quote['mid']])
                connection.execute('DELETE FROM requests WHERE expires_at<?',[now.timestamp()])
                connection.execute("DELETE FROM minute_quotes WHERE minute<?",[
                    datetime.fromtimestamp(now.timestamp()-86400,timezone.utc).isoformat()])
            return list(self.history[key])

    def status(self):
        if not self.path.exists():
            return dict(active_requests=0,minute_rows=0,subscription_mode='No subscriptions controlled by this lab')
        with self.lock, self.connection() as connection:
            now=datetime.now(timezone.utc).timestamp()
            active=connection.execute('SELECT expression,profile,expires_at FROM requests WHERE expires_at>?',[now]).fetchall()
            count=connection.execute('SELECT COUNT(*) FROM minute_quotes').fetchone()[0]
        return dict(active_requests=len(active),minute_rows=count,requests=[dict(expression=e,profile=p,expires_at=x) for e,p,x in active],
                    subscription_mode='Demo components or read-only queries; production subscriptions are unchanged')
