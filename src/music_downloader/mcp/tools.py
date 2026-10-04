"""The MCP tools as plain async methods over a Pipeline, each returning a JSON-able dict.

Tracks and copies are handed out as short ids (t1a2b3c, c4d5e6f) kept in
memory for ID_TTL_SECS, so a client can chain resolve_track -> search_copies
-> download without passing whole records around.
"""

import asyncio
import dataclasses
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from mcp.server.mcpserver.exceptions import ToolError

from music_downloader import __version__
from music_downloader.config import BYTES_PER_MB
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.wishlist_repo import WANTED_ANY, WANTED_BETTER, Wish
from music_downloader.pipeline.album import FolderListing
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, FILE_NOT_FOUND
from music_downloader.pipeline.search import RankedResults, clean_search_title
from music_downloader.processor.lossless_analyzer import LosslessVerdict
from music_downloader.search.scorer import TIER_LABELS, TIER_LOSSLESS_24, quality_tier
from music_downloader.search.slskd_client import DownloadStatus, SearchResult

logger = logging.getLogger(__name__)

# How long a track or copy id stays valid after it was handed out.
ID_TTL_SECS = 3600
PROFILES = ("library", "chat")
DELIVER_LIBRARY = "library"
DELIVER_PATH = "path"

# progress(done, total, message): an MCP progress notification, or nothing.
Progress = Callable[[float, float | None, str | None], Awaitable[None]]


@dataclass
class _TrackEntry:
    track: TrackInfo
    # The last search_copies for this track: its profile and its ranked copies.
    profile: str | None = None
    ranked: list[SearchResult] = field(default_factory=list)


@dataclass
class _CopyEntry:
    track: TrackInfo
    result: SearchResult


class IdMap:
    """Short random ids for in-memory values, each expiring *ttl_secs* after it was made."""

    def __init__(self, prefix: str, ttl_secs: float = ID_TTL_SECS, clock: Callable[[], float] = time.monotonic):
        self._prefix = prefix
        self._ttl = ttl_secs
        self._clock = clock
        self._items: dict[str, tuple[float, object]] = {}

    def put(self, value) -> str:
        now = self._clock()
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        while True:
            key = f"{self._prefix}{secrets.token_hex(3)}"
            if key not in self._items:
                break
        self._items[key] = (now + self._ttl, value)
        return key

    def get(self, key: str):
        item = self._items.get(key)
        if item is None or item[0] <= self._clock():
            self._items.pop(key, None)
            return None
        return item[1]

    def __len__(self) -> int:
        now = self._clock()
        return sum(1 for expires, _ in self._items.values() if expires > now)


def _track_dict(track: TrackInfo) -> dict:
    return {
        "artist": track.artist,
        "title": track.title,
        "album": track.album,
        "year": track.year,
        "duration_secs": track.duration_secs,
        "spotify_url": track.spotify_url,
    }


def _verdict_dict(verdict: LosslessVerdict | None) -> dict | None:
    return None if verdict is None else dataclasses.asdict(verdict)


def _listing_dict(listing: FolderListing) -> dict:
    return {
        "answered": listing.answered,
        "reason": listing.reason or None,
        "source": listing.username,
        "folder": listing.remote_dir,
        "count": len(listing.files),
        "total_size_bytes": listing.total_size,
        "total_size_mb": round(listing.total_size / BYTES_PER_MB, 1),
        "formats": listing.formats,
        "files": [
            {
                "filename": f.basename,
                "format": f.extension,
                "size_bytes": f.size,
                "size_mb": round(f.size / BYTES_PER_MB, 1),
                "quality": f.as_result(listing.username).quality_display,
                "duration_secs": f.length,
            }
            for f in listing.files
        ],
    }


def _wish_dict(wish: Wish) -> dict:
    return {
        "id": wish.id,
        "chat_id": wish.chat_id,
        "track": _track_dict(wish.track),
        "profile": wish.profile,
        "wanted": wish.wanted,
        "baseline": None if wish.baseline_tier is None else TIER_LABELS.get(wish.baseline_tier),
        "checks": wish.checks,
        "created_at": wish.created_at,
        "last_checked_at": wish.last_checked_at,
    }


class McpTools:
    """What each MCP tool does, over one Pipeline (shared with the bot when served over HTTP)."""

    def __init__(self, pipeline, ttl_secs: float = ID_TTL_SECS, clock: Callable[[], float] = time.monotonic):
        self.pipeline = pipeline
        self.tracks = IdMap("t", ttl_secs, clock)
        self.copies = IdMap("c", ttl_secs, clock)
        self.downloads_in_progress = 0

    def _track(self, track_id: str) -> _TrackEntry:
        entry = self.tracks.get(track_id)
        if entry is None:
            raise ToolError(f"Unknown or expired track id {track_id!r}: run resolve_track again.")
        return entry

    def _copy_dict(self, copy_id: str, result: SearchResult) -> dict:
        tier = quality_tier(result)
        return {
            "id": copy_id,
            "filename": result.basename,
            "format": result.extension,
            "quality": result.quality_display,
            "tier": tier,
            "tier_label": TIER_LABELS.get(tier),
            "lossless": result.is_lossless,
            "size_bytes": result.size,
            "size_mb": round(result.size / BYTES_PER_MB, 1),
            "fits_cap": result.size <= self.pipeline.upload_limit_bytes,
            "duration_secs": result.length,
            "source": result.username,
            "free_slot": result.has_free_slot,
            "queue_length": result.queue_length,
            "score": round(result.score, 1),
        }

    # ------------------------------------------------------------------ tools

    async def resolve_track(self, query: str) -> dict:
        """Spotify candidates for *query*, each with a track id."""
        query = query.strip()
        if not query:
            raise ToolError("The query is empty.")
        tracks = await asyncio.to_thread(self.pipeline.resolve, query)
        return {"candidates": [{"id": self.tracks.put(_TrackEntry(t)), **_track_dict(t)} for t in tracks]}

    async def search_copies(self, track_id: str, profile: str = "library", limit: int = 10) -> dict:
        """Soulseek copies of a resolved track, best first under *profile*, each with a copy id.

        Searches "artist title" and, when that finds nothing, the title alone.
        """
        if profile not in PROFILES:
            raise ToolError(f"profile must be one of {', '.join(PROFILES)}, not {profile!r}.")
        entry = self._track(track_id)
        track = entry.track
        clean_title = clean_search_title(track.title)
        ranked: RankedResults = await self.pipeline.search(f"{track.artist} {clean_title}", track, profile)
        if not ranked:
            ranked = await self.pipeline.search(clean_title, track, profile)
        entry.profile, entry.ranked = profile, list(ranked)
        shown = list(ranked)[: max(1, limit)]
        return {
            "track": _track_dict(track),
            "profile": profile,
            "total": len(ranked),
            "hidden_by_title_guard": getattr(ranked, "hidden", 0),
            "copies": [self._copy_dict(self.copies.put(_CopyEntry(track, r)), r) for r in shown],
        }

    async def download(self, copy_id: str, deliver: str = DELIVER_LIBRARY, progress: Progress | None = None) -> dict:
        """Fetch a copy and save it to the library (deliver="library") or leave it in DOWNLOAD_DIR ("path")."""
        if deliver not in (DELIVER_LIBRARY, DELIVER_PATH):
            raise ToolError(f"deliver must be {DELIVER_LIBRARY!r} or {DELIVER_PATH!r}, not {deliver!r}.")
        entry = self.copies.get(copy_id)
        if entry is None:
            raise ToolError(f"Unknown or expired copy id {copy_id!r}: run search_copies again.")
        track, result = entry.track, entry.result

        async def on_status(status: DownloadStatus) -> None:
            if progress is not None:
                await progress(status.percent_complete, 100.0, f"{status.state} {status.percent_complete:.0f}%")

        self.downloads_in_progress += 1
        try:
            outcome = await self.pipeline.fetch(result, on_status)
        finally:
            self.downloads_in_progress -= 1
        if not outcome.ok:
            if outcome.error in (DOWNLOAD_FAILED, FILE_NOT_FOUND):
                await self.pipeline.record_history(track, result, outcome.error)
            return {"ok": False, "error": outcome.error, "state": outcome.state or None}

        verdict = _verdict_dict(outcome.verdict)
        if deliver == DELIVER_PATH:
            await self.pipeline.record_history(track, result, "delivered", filename=outcome.path)
            return {"ok": True, "deliver": DELIVER_PATH, "path": outcome.path, "lossless_check": verdict}
        target = await self.pipeline.save(outcome.path, track, result, outcome.transfer_id)
        if target is None:
            return {"ok": False, "error": "process_failed", "path": outcome.path, "lossless_check": verdict}
        return {"ok": True, "deliver": DELIVER_LIBRARY, "path": target, "lossless_check": verdict}

    def _copy(self, copy_id: str) -> _CopyEntry:
        entry = self.copies.get(copy_id)
        if entry is None:
            raise ToolError(f"Unknown or expired copy id {copy_id!r}: run search_copies again.")
        return entry

    async def album_listing(self, copy_id: str) -> dict:
        """The audio files of the peer folder a copy came from: names, formats, sizes, total."""
        entry = self._copy(copy_id)
        listing = await self.pipeline.album_listing(entry.result)
        return _listing_dict(listing)

    async def album_download(
        self, copy_id: str, deliver: str = DELIVER_LIBRARY, progress: Progress | None = None
    ) -> dict:
        """Fetch every audio file of the folder a copy came from; one outcome per file.

        deliver="library" saves each file into the library as it lands (a file
        the library already has is skipped: skipped true, path the library's
        file); "path" leaves them in DOWNLOAD_DIR. A failed file never stops the others.
        """
        if deliver not in (DELIVER_LIBRARY, DELIVER_PATH):
            raise ToolError(f"deliver must be {DELIVER_LIBRARY!r} or {DELIVER_PATH!r}, not {deliver!r}.")
        entry = self._copy(copy_id)

        async def on_progress(index: int, total: int, state: str, percent: float) -> None:
            if progress is not None:
                await progress(index + percent / 100.0, float(total), f"{index + 1}/{total} {state} {percent:.0f}%")

        self.downloads_in_progress += 1
        try:
            job, listing = await self.pipeline.album(entry.result, entry.track, deliver, on_progress)
        finally:
            self.downloads_in_progress -= 1
        if not listing.files:
            return {"ok": False, "error": listing.reason, "detail": listing.detail or None, **_listing_dict(listing)}
        outcomes = [o for o in job.outcomes if o is not None]
        return {
            "ok": bool(outcomes) and all(o.ok for o in outcomes) and len(outcomes) == len(job.files),
            "job_id": job.id,
            "deliver": deliver,
            "source": job.username,
            "folder": job.remote_dir,
            "total": len(job.files),
            "landed": len(job.landed),
            "failed": len(job.failed),
            "files": [
                {
                    "filename": f.basename,
                    "ok": o is not None and o.ok,
                    "path": o.path if o is not None else None,
                    "skipped": o is not None and o.skipped,
                    "error": (o.error if o is not None else "unfinished"),
                    "state": (o.state or None) if o is not None else None,
                }
                for f, o in zip(job.files, job.outcomes, strict=True)
            ],
        }

    async def history(self, limit: int = 20) -> dict:
        records = await asyncio.to_thread(self.pipeline.history_repo.get_recent, min(max(1, limit), 200))
        return {"downloads": [dataclasses.asdict(r) for r in records]}

    async def library_has(self, artist: str, title: str) -> dict:
        """Library files that look like "artist - title" (the bot's duplicate check)."""
        matches = await self.pipeline.find_similar(f"{artist} - {title}")
        return {"found": bool(matches), "matches": matches[:10]}

    async def wishlist_add(self, track_id: str, wanted: str = WANTED_ANY) -> dict:
        """Search a resolved track again every WISHLIST_CHECK_HOURS.

        "any": the first copy that turns up. "better": a copy above the best
        one search_copies found. Hits go to the owner's Telegram chat. A track
        the owner already waits for comes back as that wish, already_waiting true.
        """
        entry = self._track(track_id)
        if wanted not in (WANTED_ANY, WANTED_BETTER):
            raise ToolError(f"wanted must be {WANTED_ANY!r} or {WANTED_BETTER!r}, not {wanted!r}.")
        owner = self.pipeline.config.telegram_owner_id
        if owner is None:
            raise ToolError("TELEGRAM_ALLOWED_USERS is empty: there is no owner chat to notify.")
        existing = self.pipeline.wishlist_find(owner, entry.track)
        if existing is not None:
            return {"wish": _wish_dict(existing), "already_waiting": True}
        baseline = None
        if wanted == WANTED_BETTER:
            if not entry.ranked:
                raise ToolError("A 'better' wish needs a baseline: run search_copies for this track first.")
            baseline = max(quality_tier(r) for r in entry.ranked)
            if baseline >= TIER_LOSSLESS_24:
                raise ToolError("A lossless 24-bit copy is already there: nothing ranks higher.")
        wish = self.pipeline.wishlist_add(owner, owner, entry.track, entry.profile or "library", wanted, baseline)
        return {"wish": _wish_dict(wish), "already_waiting": False}

    async def wishlist_list(self) -> dict:
        """Every wish, from every chat (the MCP caller is the owner)."""
        return {"wishes": [_wish_dict(w) for w in self.pipeline.wishlist_repo.list_all()]}

    async def wishlist_remove(self, wish_id: int) -> dict:
        wish = self.pipeline.wishlist_repo.get(wish_id)
        removed = wish is not None and self.pipeline.wishlist_remove(wish.chat_id, wish_id)
        return {"removed": removed}

    async def status(self) -> dict:
        slskd_up = await asyncio.to_thread(self.pipeline.slskd.is_up)
        pending = await asyncio.to_thread(self.pipeline.pending_repo.load_downloads)
        return {
            "version": __version__,
            "slskd_reachable": bool(slskd_up),
            "pending_downloads": len(pending),
            "mcp_downloads_in_progress": self.downloads_in_progress,
            "wishes": len(self.pipeline.wishlist_repo.list_all()),
            "upload_cap_mb": self.pipeline.upload_limit_bytes // BYTES_PER_MB,
        }
