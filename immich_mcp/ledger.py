"""Persistent upload ledger (SQLite): idempotency across retries and restarts.

upload_id is deterministic: "up_" + first 32 hex chars of the content's SHA-256. The same bytes always
get the same upload_id, so a retried call never uploads twice, and album assignment is tracked
separately per (upload_id, album_id) so a failure there doesn't undo or repeat the upload.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  upload_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, sha1 TEXT NOT NULL, filename TEXT, mime TEXT,
  size INTEGER, source TEXT, status TEXT NOT NULL, asset_id TEXT, error TEXT,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS uploads_sha256 ON uploads(sha256);
CREATE TABLE IF NOT EXISTS album_adds (
  upload_id TEXT NOT NULL, album_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, updated REAL NOT NULL,
  PRIMARY KEY (upload_id, album_id));
CREATE TABLE IF NOT EXISTS share_links (
  link_id TEXT PRIMARY KEY, url TEXT NOT NULL, asset_ids TEXT NOT NULL, expires_at TEXT,
  created_by TEXT, created REAL NOT NULL, revoked REAL);
"""


def upload_id_for(sha256_hex: str) -> str:
    return "up_" + sha256_hex[:32]


class Ledger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def get(self, upload_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["albums"] = [dict(x) for x in self._db.execute(
                "SELECT album_id, status, error FROM album_adds WHERE upload_id=?", (upload_id,))]
            return d

    def begin(self, upload_id, sha256, sha1, filename, mime, size, source):
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO uploads(upload_id,sha256,sha1,filename,mime,size,source,status,created,updated) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(upload_id) DO UPDATE SET "
                "status=CASE WHEN status IN ('created','duplicate') THEN status ELSE 'pending' END, updated=excluded.updated",
                (upload_id, sha256, sha1, filename, mime, size, source, now, now))

    def finish(self, upload_id, status, asset_id=None, error=None):
        with self._lock:
            self._db.execute("UPDATE uploads SET status=?, asset_id=COALESCE(?,asset_id), error=?, updated=? "
                             "WHERE upload_id=?", (status, asset_id, error, time.time(), upload_id))

    def album(self, upload_id, album_id, status, error=None):
        with self._lock:
            self._db.execute("INSERT INTO album_adds(upload_id,album_id,status,error,updated) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(upload_id,album_id) DO UPDATE SET status=excluded.status, "
                             "error=excluded.error, updated=excluded.updated",
                             (upload_id, album_id, status, error, time.time()))

    def add_share_link(self, link_id, url, asset_ids, expires_at, created_by):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO share_links(link_id,url,asset_ids,expires_at,created_by,created) "
                             "VALUES(?,?,?,?,?,?)", (link_id, url, ",".join(asset_ids), expires_at, created_by, time.time()))

    def share_link(self, link_id) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM share_links WHERE link_id=?", (link_id,)).fetchone()
            return dict(r) if r else None

    def revoke_share_link(self, link_id):
        with self._lock:
            self._db.execute("UPDATE share_links SET revoked=? WHERE link_id=?", (time.time(), link_id))
