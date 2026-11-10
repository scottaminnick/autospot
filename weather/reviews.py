"""Bounded, session-owned temporary review workspaces on the existing host."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 64
LIFETIME = 24 * 3600


class ReviewMissing(Exception):
    pass


class ReviewConflict(Exception):
    pass


class ReviewFull(Exception):
    pass


class ReviewStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connection() as db:
            db.execute("PRAGMA max_page_count=16384")
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, owner TEXT NOT NULL, revision INTEGER NOT NULL, expires REAL NOT NULL, payload BLOB NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS reviews_expiry ON reviews(expires)")
            db.execute("DELETE FROM reviews WHERE expires<=?", (time.time(),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def encode(self, payload):
        raw = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise ReviewFull()
        return raw

    def ready(self):
        with self.connection() as db:
            db.execute("SELECT 1 FROM reviews LIMIT 1").fetchone()

    def create(self, owner, payload):
        raw = self.encode(payload)
        identifier = secrets.token_urlsafe(24)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM reviews WHERE expires<=?", (time.time(),))
                count, size = db.execute(
                    "SELECT count(*),coalesce(sum(length(payload)),0) FROM reviews"
                ).fetchone()
                if count >= MAX_RECORDS or size + len(raw) > MAX_TOTAL_BYTES:
                    raise ReviewFull()
                db.execute(
                    "INSERT INTO reviews VALUES (?,?,1,?,?)",
                    (identifier, owner, time.time() + LIFETIME, raw),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        return identifier

    def get(self, identifier, owner):
        with self.connection() as db:
            db.execute("DELETE FROM reviews WHERE expires<=?", (time.time(),))
            row = db.execute(
                "SELECT revision,payload FROM reviews WHERE id=? AND owner=? AND expires>?",
                (identifier, owner, time.time()),
            ).fetchone()
        if not row:
            raise ReviewMissing()
        return row[0], json.loads(row[1])

    def save(self, identifier, owner, revision, payload):
        raw = self.encode(payload)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM reviews WHERE expires<=?", (time.time(),))
                row = db.execute(
                    "SELECT revision,length(payload) FROM reviews WHERE id=? AND owner=?",
                    (identifier, owner),
                ).fetchone()
                if not row:
                    raise ReviewMissing()
                if row[0] != revision:
                    raise ReviewConflict()
                size = db.execute(
                    "SELECT coalesce(sum(length(payload)),0) FROM reviews"
                ).fetchone()[0]
                if size - row[1] + len(raw) > MAX_TOTAL_BYTES:
                    raise ReviewFull()
                db.execute(
                    "UPDATE reviews SET revision=revision+1,payload=? WHERE id=? AND owner=? AND revision=?",
                    (raw, identifier, owner, revision),
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
