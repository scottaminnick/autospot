"""Bounded shared positive cache. Failures and unresolved values are not cached."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


def cache_key(*values):
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class Cache:
    def __init__(self, path, max_bytes=32 * 1024 * 1024):
        self.path, self.max_bytes = str(path), max_bytes
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            pages = max(32, (max_bytes * 2 + 4095) // 4096)
            db.execute(f"PRAGMA max_page_count={pages}")
            db.execute("""CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY, value BLOB NOT NULL, size INTEGER NOT NULL,
                expires REAL NOT NULL, accessed REAL NOT NULL)""")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        try:
            db.execute(
                f"PRAGMA max_page_count={max(32, (self.max_bytes * 2 + 4095) // 4096)}"
            )
            yield db
        finally:
            db.close()

    def get(self, key):
        try:
            with self.connection() as db:
                row = db.execute(
                    "SELECT value, expires FROM cache WHERE key=?", (key,)
                ).fetchone()
                if not row or row[1] <= time.time():
                    return None
                value = json.loads(row[0])
                db.execute(
                    "UPDATE cache SET accessed=? WHERE key=?", (time.time(), key)
                )
                return value
        except (sqlite3.Error, ValueError, TypeError, OSError):
            return None

    def put(self, key, value, ttl):
        payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        size = len(payload)
        if size > min(self.max_bytes, 2 * 1024 * 1024):
            return
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "DELETE FROM cache WHERE expires<=? OR key=?", (time.time(), key)
                )
                total = db.execute(
                    "SELECT COALESCE(SUM(size),0) FROM cache"
                ).fetchone()[0]
                while total + size > self.max_bytes:
                    oldest = db.execute(
                        "SELECT key,size FROM cache ORDER BY accessed LIMIT 1"
                    ).fetchone()
                    if not oldest:
                        break
                    db.execute("DELETE FROM cache WHERE key=?", (oldest[0],))
                    total -= oldest[1]
                db.execute(
                    "INSERT INTO cache VALUES (?,?,?,?,?)",
                    (key, payload, size, time.time() + ttl, time.time()),
                )
                db.commit()
        except (sqlite3.Error, OSError):
            # A full/unavailable cache never substitutes or discards model data.
            return

    def delete(self, key):
        try:
            with self.connection() as db:
                db.execute("DELETE FROM cache WHERE key=?", (key,))
        except sqlite3.Error:
            pass
