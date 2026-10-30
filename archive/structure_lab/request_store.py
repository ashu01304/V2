"""Temporary lab demand registry. No access to the master database."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

DEFAULT_PATH=Path(__file__).parent/'runtime/structure_requests.sqlite3'


class RequestStore:
    def __init__(self,path=DEFAULT_PATH,clock=time.time):
        self.path=Path(path)
        self.clock=clock
        self.lock=threading.RLock()
        with self.connection() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS structure_requests (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, expression TEXT NOT NULL,
                plan TEXT NOT NULL, expires_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS required_contracts (
                request_id TEXT REFERENCES structure_requests(id) ON DELETE CASCADE,
                tt_id TEXT, metadata TEXT NOT NULL, PRIMARY KEY(request_id,tt_id));
              CREATE TABLE IF NOT EXISTS published_quotes (
                request_id TEXT PRIMARY KEY REFERENCES structure_requests(id) ON DELETE CASCADE,
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

    def register(self,owner,plans,ttl=1800):
        if not isinstance(owner,str) or not 1<=len(owner)<=128:
            raise ValueError('Provide a dashboard owner ID of at most 128 characters')
        if not 30<=ttl<=1800:
            raise ValueError('Lease must be between 30 and 1800 seconds')
        expires=self.clock()+ttl
        result=[]
        with self.connection() as db:
            db.execute('DELETE FROM structure_requests WHERE expires_at<=?',[self.clock()])
            for plan in plans:
                key=hashlib.sha256((owner+'\0'+plan['expression']).encode()).hexdigest()[:32]
                db.execute('''INSERT INTO structure_requests VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET plan=excluded.plan,expires_at=excluded.expires_at''',
                    [key,owner,plan['expression'],json.dumps(plan),expires])
                db.execute('DELETE FROM required_contracts WHERE request_id=?',[key])
                db.executemany('INSERT INTO required_contracts VALUES (?,?,?)',
                    [(key,tt,json.dumps(meta)) for tt,meta in plan['contracts'].items()])
                result.append(dict(request_id=key,expression=plan['expression'],expires_at=expires,
                                   required_ids=list(plan['contracts'])))
        return result

    def renew(self,owner,ids,ttl=1800):
        if not 30<=ttl<=1800:
            raise ValueError('Lease must be between 30 and 1800 seconds')
        count=0
        with self.connection() as db:
            for key in ids:
                count+=db.execute('UPDATE structure_requests SET expires_at=? WHERE id=? AND owner=? AND expires_at>?',
                    [self.clock()+ttl,key,owner,self.clock()]).rowcount
        return count

    def release(self,owner,key):
        with self.connection() as db:
            return db.execute('DELETE FROM structure_requests WHERE id=? AND owner=?',[key,owner]).rowcount>0

    def active(self):
        with self.connection() as db:
            db.execute('DELETE FROM structure_requests WHERE expires_at<=?',[self.clock()])
            return [dict(request_id=row['id'],owner=row['owner'],expires_at=row['expires_at'],
                         **json.loads(row['plan'])) for row in db.execute('SELECT * FROM structure_requests ORDER BY id')]

    def desired(self):
        return {tt:meta for row in self.active() for tt,meta in row['contracts'].items()}

    def publish(self,key,payload):
        with self.connection() as db:
            db.execute('''INSERT INTO published_quotes SELECT id,?,? FROM structure_requests
                WHERE id=? AND expires_at>? ON CONFLICT(request_id) DO UPDATE
                SET payload=excluded.payload,computed_at=excluded.computed_at''',
                [json.dumps(payload,allow_nan=False),self.clock(),key,self.clock()])

    def read(self,key):
        with self.connection() as db:
            row=db.execute('''SELECT r.*,q.payload,q.computed_at FROM structure_requests r
                LEFT JOIN published_quotes q ON r.id=q.request_id WHERE r.id=? AND r.expires_at>?''',
                [key,self.clock()]).fetchone()
            if row is None:
                return None
            return dict(request_id=key,expression=row['expression'],expires_at=row['expires_at'],
                        computed_at=row['computed_at'],quote=json.loads(row['payload']) if row['payload'] else None)

    def clear_quotes(self):
        with self.connection() as db:
            db.execute('DELETE FROM published_quotes')
