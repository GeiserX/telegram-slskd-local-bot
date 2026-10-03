"""The library's audio files in SQLite, so the duplicate check never walks OUTPUT_DIR per message.

One row per audio file under OUTPUT_DIR, subfolders included. rebuild()
walks the folder (startup and hourly), add() records one saved file, and
find_similar() narrows the rows with a LIKE prefilter on the normalised stem
before running the fuzzy match on at most CANDIDATE_LIMIT of them. Every
method does blocking I/O: call them from a thread, never the event loop.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from difflib import SequenceMatcher

from music_downloader.formats import AUDIO_EXTENSIONS

from .database import Database

logger = logging.getLogger(__name__)

# Rows the fuzzy match looks at per query, best prefilter matches first.
CANDIDATE_LIMIT = 300
# Query words used in the prefilter (a pasted paragraph must not build a huge query).
_MAX_QUERY_WORDS = 12
DEFAULT_THRESHOLD = 0.6


def normalise(text: str) -> str:
    """Casefold and strip accents ("Beyoncé" -> "beyonce")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def similarity(query: str, stem: str) -> float:
    """How alike a query and a file stem are: the best of word overlap and sequence ratio.

    Both arguments are lowercased by the caller. Word overlap is the share of
    common words over the shorter of the two word sets.
    """
    query_words = set(re.findall(r"\w+", query))
    stem_words = set(re.findall(r"\w+", stem))
    word_ratio = (
        len(query_words & stem_words) / min(len(query_words), len(stem_words)) if query_words and stem_words else 0.0
    )
    return max(word_ratio, SequenceMatcher(None, query, stem).ratio())


class LibraryIndex:
    """The library_index table for one OUTPUT_DIR, on its own SQLite connection.

    Its own connection (WAL mode allows it next to the main one) keeps a long
    rebuild transaction from mixing with the bot's writes; a lock serialises
    the threads that share it.
    """

    def __init__(self, db: Database, root: str) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db.path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=5000")
        atexit.register(self.close)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()

    def _row(self, path: str) -> tuple[str, str, str, str, float] | None:
        """(rel_path, stem, norm_stem, extension, mtime) for an audio file under root, else None."""
        stem, ext = os.path.splitext(os.path.basename(path))
        ext = ext.lower().lstrip(".")
        if ext not in AUDIO_EXTENSIONS:
            return None
        rel_path = os.path.relpath(path, self.root)
        if rel_path == os.pardir or rel_path.startswith(os.pardir + os.sep):
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        return rel_path, stem, normalise(stem), ext, mtime

    def rebuild(self) -> int:
        """Walk root recursively and replace every row. Returns the number of files indexed.

        A missing or empty root never wipes a non-empty index: that is an
        unmounted share far more often than a library emptied on purpose, and
        wiping would silence the duplicate check until the next pass.
        """
        rows = []
        for dirpath, _, files in os.walk(self.root):
            for name in files:
                row = self._row(os.path.join(dirpath, name))
                if row is not None:
                    rows.append(row)
        with self._lock, self._conn:
            if not rows:
                kept = self._conn.execute("SELECT COUNT(*) FROM library_index").fetchone()[0]
                if kept:
                    logger.warning(
                        "Library index: no audio files under %s (missing or unmounted?); keeping the %d previous row(s)",
                        self.root,
                        kept,
                    )
                    return 0
            self._conn.execute("DELETE FROM library_index")
            self._conn.executemany(
                "INSERT OR REPLACE INTO library_index (rel_path, stem, norm_stem, extension, mtime) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        logger.info("Library index: %d audio file(s) under %s", len(rows), self.root)
        return len(rows)

    def add(self, path: str) -> bool:
        """Record one file (a fresh save). Returns False when it is not an audio file under root."""
        row = self._row(path)
        if row is None:
            return False
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO library_index (rel_path, stem, norm_stem, extension, mtime) VALUES (?, ?, ?, ?, ?)",
                row,
            )
        return True

    def find_similar(self, query: str, threshold: float = DEFAULT_THRESHOLD) -> list[str]:
        """Library files (paths relative to root) whose stem looks like *query*, best first.

        The prefilter keeps rows whose normalised stem contains at least one
        query word, the ones containing most words first, up to
        CANDIDATE_LIMIT; only those go through similarity().
        """
        words = list(dict.fromkeys(re.findall(r"[^\W_]+", normalise(query))))[:_MAX_QUERY_WORDS]
        if not words:
            return []
        likes = ["(norm_stem LIKE ?)"] * len(words)
        params = [f"%{w}%" for w in words]
        sql = (
            f"SELECT rel_path, stem FROM library_index WHERE {' OR '.join(likes)} "
            f"ORDER BY ({' + '.join(likes)}) DESC, rel_path LIMIT ?"
        )
        with self._lock:
            candidates = self._conn.execute(sql, [*params, *params, CANDIDATE_LIMIT]).fetchall()
        query_lower = query.lower()
        matches = []
        for rel_path, stem in candidates:
            ratio = similarity(query_lower, stem.lower())
            if ratio >= threshold:
                matches.append((rel_path, ratio))
        matches.sort(key=lambda m: m[1], reverse=True)
        return [m[0] for m in matches]
