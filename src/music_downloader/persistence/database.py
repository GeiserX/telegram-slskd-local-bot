from __future__ import annotations

import atexit
import contextlib
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT DEFAULT '',
    filename TEXT NOT NULL,
    source_user TEXT NOT NULL,
    remote_path TEXT DEFAULT '',
    status TEXT NOT NULL,
    duration_secs INTEGER DEFAULT 0,
    file_size INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    spotify_url TEXT NOT NULL,
    name TEXT NOT NULL,
    total_tracks INTEGER NOT NULL,
    completed_tracks INTEGER NOT NULL DEFAULT 0,
    failed_tracks INTEGER NOT NULL DEFAULT 0,
    skipped_tracks INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS import_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    album TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    spotify_url TEXT DEFAULT '',
    year TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(job_id, position)
);

CREATE INDEX IF NOT EXISTS idx_import_tracks_job_status ON import_tracks(job_id, status);
CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status);
CREATE INDEX IF NOT EXISTS idx_download_history_created ON download_history(created_at);
"""

# sqlite3.OperationalError (disk full, disk I/O error, readonly database,
# locked database) is a SUBCLASS of DatabaseError. Only genuine file
# corruption justifies replacing the database; everything else must
# propagate so a transient condition can't destroy history.
_CORRUPTION_MARKERS: tuple[str, ...] = ("malformed", "not a database", "file is encrypted")


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._connect(db_path)
        except sqlite3.DatabaseError as exc:
            if not _is_corruption(exc):
                raise
            logger.warning(f"Database corrupt at {db_path} ({exc}) — moving it aside and recreating")
            self._move_corrupt_aside(db_path)
            self._conn = self._connect(db_path)
        atexit.register(self.close)

    @staticmethod
    def _connect(db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except Exception:
            with contextlib.suppress(Exception):
                conn.close()
            raise
        return conn

    @staticmethod
    def _move_corrupt_aside(db_path: str) -> None:
        """Preserve a corrupt database (and WAL/SHM siblings) instead of deleting it."""
        # Second-resolution timestamps can collide across rapid recoveries;
        # the random suffix keeps an earlier backup from being overwritten.
        stamp = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
        for suffix in ("", "-wal", "-shm"):
            src = f"{db_path}{suffix}"
            if os.path.exists(src):
                with contextlib.suppress(OSError):
                    os.replace(src, f"{db_path}.corrupt-{stamp}{suffix}")

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
