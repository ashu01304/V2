"""Shared SQLite response cache; expiry is measured from fetch, not last read."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time


class ResponseCache:
    def __init__(self, directory, *, clock=time.time):
        self.path = Path(directory) / "responses.sqlite3"
        self.clock = clock

    @staticmethod
    def key(token, base_url, method, path, params, body):
        request = json.dumps([token, base_url, method, path, params, body],
                             sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(request.encode()).hexdigest()

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS responses (
                cache_key TEXT PRIMARY KEY, created REAL NOT NULL,
                expires REAL NOT NULL, payload TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS responses_expiry ON responses(expires)")
        except BaseException:
            connection.close()
            raise
        return connection

    def get(self, key, ttl):
        now = self.clock()
        connection = self._connect()
        try:
            with connection:
                connection.execute("DELETE FROM responses WHERE expires <= ?", (now,))
                row = connection.execute(
                    "SELECT created, expires, payload FROM responses WHERE cache_key = ?",
                    (key,)).fetchone()
                if row is None or ttl <= 0 or now >= min(row[1], row[0] + ttl):
                    return None
                try:
                    payload = json.loads(row[2])
                except ValueError:
                    connection.execute("DELETE FROM responses WHERE cache_key = ?", (key,))
                    return None
                return {"payload": payload, "age_seconds": max(0, now - row[0]),
                        "expires_at": min(row[1], row[0] + ttl)}
        finally:
            connection.close()

    def put(self, key, payload, ttl):
        if ttl <= 0:
            return
        now = self.clock()
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        connection = self._connect()
        try:
            with connection:
                connection.execute("DELETE FROM responses WHERE expires <= ?", (now,))
                connection.execute("""INSERT OR REPLACE INTO responses
                    (cache_key, created, expires, payload) VALUES (?, ?, ?, ?)""",
                                   (key, now, now + ttl, encoded))
        finally:
            connection.close()
