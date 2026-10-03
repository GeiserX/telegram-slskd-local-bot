"""Searches and downloads waiting on a button tap, kept in SQLite so a restart keeps them.

The bot holds them in dicts (see WriteThroughDict) and every change lands
here; at startup the dicts are loaded back from these rows.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

from .database import Database

logger = logging.getLogger(__name__)


@dataclass
class PendingSearch:
    """Holds state for an active search session."""

    query: str
    track: TrackInfo | None = None
    results: list[SearchResult] = field(default_factory=list)
    message_id: int | None = None
    page: int = 0
    # Unique id binding result keyboards to this search; stale buttons from an
    # earlier search must never resolve against a newer result list.
    search_id: str = ""
    # Ranking profile the results were ranked under ("library" or "chat").
    profile: str = ""
    # Copies the title guard dropped as unrelated (shown in the results header).
    hidden: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class PendingDownload:
    """Tracks a single file download waiting for approval."""

    track: TrackInfo
    result: SearchResult
    chat_id: int
    source_path: str | None = None  # Path in /downloads
    status_message_id: int | None = None
    approval_message_id: int | None = None  # Message with approve/reject buttons
    result_index: int = 0  # Position in ranked results list
    search_id: str = ""  # Search this download came from (see PendingSearch)
    # Who started it: TELEGRAM_CHAT_DELIVERY_USERS is keyed by user id, which
    # differs from the chat id outside private chats.
    user_id: int | None = None
    # slskd's id for the finished transfer, so it can be removed from slskd
    # once the file is gone.
    transfer_id: str = ""
    # Set for a download inside an /import: its job, its track, and the chat
    # generation it started under (a /cancel bumps it).
    job_id: int | None = None
    track_id: int | None = None
    generation: int = 0
    created_at: float = field(default_factory=time.time)


def _from_fields(cls, data: dict):
    """Build *cls* from a dict, ignoring keys the dataclass no longer has."""
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class PendingRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    # ---------------------------------------------------------------- downloads

    def save_download(self, dl_id: str, dl: PendingDownload) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO pending_downloads
            (dl_id, chat_id, user_id, track, result, source_path, status_message_id, approval_message_id,
             result_index, search_id, transfer_id, job_id, track_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dl_id,
                dl.chat_id,
                dl.user_id,
                json.dumps(dataclasses.asdict(dl.track)),
                json.dumps(dataclasses.asdict(dl.result)),
                dl.source_path,
                dl.status_message_id,
                dl.approval_message_id,
                dl.result_index,
                dl.search_id,
                dl.transfer_id,
                dl.job_id,
                dl.track_id,
                dl.created_at,
            ),
        )
        self._conn.commit()

    def delete_download(self, dl_id: str) -> None:
        self._conn.execute("DELETE FROM pending_downloads WHERE dl_id = ?", (dl_id,))
        self._conn.commit()

    def load_downloads(self) -> dict[str, PendingDownload]:
        loaded: dict[str, PendingDownload] = {}
        for row in self._conn.execute("SELECT * FROM pending_downloads"):
            data = dict(row)
            dl_id = data.pop("dl_id")
            try:
                data["track"] = _from_fields(TrackInfo, json.loads(data["track"]))
                data["result"] = _from_fields(SearchResult, json.loads(data["result"]))
                loaded[dl_id] = _from_fields(PendingDownload, data)
            except (TypeError, ValueError):
                logger.warning("Dropping unreadable pending download %s", dl_id)
                self.delete_download(dl_id)
        return loaded

    # ----------------------------------------------------------------- searches

    def save_search(self, chat_id: int, search: PendingSearch) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO pending_searches
            (chat_id, query, track, results, message_id, page, search_id, profile, hidden, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chat_id,
                search.query,
                json.dumps(dataclasses.asdict(search.track)) if search.track is not None else None,
                json.dumps([dataclasses.asdict(r) for r in search.results]),
                search.message_id,
                search.page,
                search.search_id,
                search.profile,
                search.hidden,
                search.created_at,
            ),
        )
        self._conn.commit()

    def delete_search(self, chat_id: int) -> None:
        self._conn.execute("DELETE FROM pending_searches WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    def load_searches(self) -> dict[int, PendingSearch]:
        loaded: dict[int, PendingSearch] = {}
        for row in self._conn.execute("SELECT * FROM pending_searches"):
            data = dict(row)
            chat_id = data.pop("chat_id")
            try:
                track = data.pop("track")
                data["track"] = _from_fields(TrackInfo, json.loads(track)) if track else None
                data["results"] = [_from_fields(SearchResult, r) for r in json.loads(data["results"])]
                loaded[chat_id] = _from_fields(PendingSearch, data)
            except (TypeError, ValueError):
                logger.warning("Dropping unreadable pending search for chat %s", chat_id)
                self.delete_search(chat_id)
        return loaded


class WriteThroughDict(dict):
    """A dict whose every set and delete is also written to the repository.

    Mutating a stored object in place (``d[k].source_path = p``) is not seen;
    call ``save(k)`` afterwards. A failed write is logged and never raised: the
    flow in progress must not break because the database could not be written.
    """

    def __init__(self, initial: dict, save: Callable, delete: Callable) -> None:
        super().__init__(initial)
        self._save = save
        self._delete = delete

    def _write(self, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.warning("Could not persist pending state for %s", args[0], exc_info=True)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self._write(self._save, key, value)

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self._write(self._delete, key)

    _MISSING = object()

    def pop(self, key, default=_MISSING):
        if key in self:
            value = super().pop(key)
            self._write(self._delete, key)
            return value
        if default is WriteThroughDict._MISSING:
            raise KeyError(key)
        return default

    def save(self, key) -> None:
        """Write the current value of *key* again after mutating it in place."""
        if key in self:
            self._write(self._save, key, self[key])
