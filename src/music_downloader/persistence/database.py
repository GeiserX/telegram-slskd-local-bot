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

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    auto_mode INTEGER NOT NULL DEFAULT 0,
    delivery_mode TEXT,
    send_format TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Buttons that must keep working across a restart: one row per download
-- waiting on a Save/Reject/Retry tap, one row per chat with a result list.
-- track/result are JSON; created_at is a Unix timestamp (pruned by age).
CREATE TABLE IF NOT EXISTS pending_downloads (
    dl_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    track TEXT NOT NULL,
    result TEXT NOT NULL,
    source_path TEXT,
    status_message_id INTEGER,
    approval_message_id INTEGER,
    result_index INTEGER NOT NULL DEFAULT 0,
    search_id TEXT NOT NULL DEFAULT '',
    transfer_id TEXT NOT NULL DEFAULT '',
    job_id INTEGER,
    track_id INTEGER,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_searches (
    chat_id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    track TEXT,
    results TEXT NOT NULL DEFAULT '[]',
    message_id INTEGER,
    page INTEGER NOT NULL DEFAULT 0,
    search_id TEXT NOT NULL DEFAULT '',
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

-- Result lists the wishlist checker sent, one row per notification. Kept
-- apart from pending_searches so a chat's own search and a wish's list never
-- replace each other. wish_id is NULL once the wish is gone.
CREATE TABLE IF NOT EXISTS wish_searches (
    chat_id INTEGER NOT NULL,
    search_id TEXT NOT NULL,
    wish_id INTEGER,
    query TEXT NOT NULL,
    track TEXT,
    results TEXT NOT NULL DEFAULT '[]',
    message_id INTEGER,
    page INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, search_id)
);

-- Audio files under OUTPUT_DIR (subfolders included), for the duplicate
-- check. Rebuilt by walking the folder at startup and hourly, and updated by
-- every save. rel_path is relative to OUTPUT_DIR; stem is the file name
-- without extension; norm_stem is the stem casefolded with accents removed
-- (the LIKE prefilter); mtime is a Unix timestamp.
CREATE TABLE IF NOT EXISTS library_index (
    rel_path TEXT PRIMARY KEY,
    stem TEXT NOT NULL,
    norm_stem TEXT NOT NULL,
    extension TEXT NOT NULL,
    mtime REAL NOT NULL
);

-- Tracks to search again later (/wishlist). wanted is 'any' (nothing was
-- found) or 'better' (only a copy above baseline_tier counts, see
-- search.scorer.quality_tier). track is JSON; profile is the ranking profile
-- of the search that made the wish; times are Unix timestamps, NULL = never.
CREATE TABLE IF NOT EXISTS wishlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER,
    track TEXT NOT NULL,
    profile TEXT NOT NULL,
    wanted TEXT NOT NULL,
    baseline_tier INTEGER,
    created_at REAL NOT NULL,
    last_checked_at REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    notified_at REAL
);

CREATE INDEX IF NOT EXISTS idx_wishlist_chat ON wishlist(chat_id);
CREATE INDEX IF NOT EXISTS idx_import_tracks_job_status ON import_tracks(job_id, status);
CREATE INDEX IF NOT EXISTS idx_import_jobs_status ON import_jobs(status);
CREATE INDEX IF NOT EXISTS idx_download_history_created ON download_history(created_at);
"""

# sqlite3.OperationalError (disk full, disk I/O error, readonly database,
# locked database) is a SUBCLASS of DatabaseError. Only genuine file
# corruption justifies replacing the database; everything else must
# propagate so a transient condition can't destroy history.
_CORRUPTION_MARKERS: tuple[str, ...] = ("malformed", "not a database", "file is encrypted")


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never
# touches an existing table, so a database created by an older release gets
# them here. Each must be nullable (or have a default) for ALTER TABLE ADD.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("chat_settings", "delivery_mode", "TEXT"),
    ("chat_settings", "send_format", "TEXT"),
)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.commit()


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


class Database:
    def __init__(self, db_path: str) -> None:
        self.path = db_path
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
            _add_missing_columns(conn)
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
