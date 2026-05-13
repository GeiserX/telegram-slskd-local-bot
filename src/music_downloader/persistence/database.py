from __future__ import annotations

import atexit
import contextlib
import logging
import os
import sqlite3
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


class Database:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        except sqlite3.DatabaseError:
            logger.warning("Database corrupt or unreadable at %s — recreating", db_path)
            if os.path.exists(db_path):
                os.remove(db_path)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        atexit.register(self.close)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()
