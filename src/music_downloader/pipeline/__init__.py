"""Everything between "we have a query" and "we have a file ready to hand over".

Nothing under this package imports telegram. The bot (music_downloader.bot)
parses updates, edits messages and sends files; the Pipeline resolves tracks
on Spotify, searches Soulseek through slskd, fetches files, checks them and
saves them to the library. Callbacks are plain async callables and results
are plain dataclasses, so another front end can drive the same Pipeline.
"""

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable

from music_downloader.config import Config
from music_downloader.metadata.playlist import PlaylistResolver
from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo
from music_downloader.persistence.album_repo import (
    ALBUM_CANCELLED,
    ALBUM_DONE,
    ALBUM_INTERRUPTED,
    ALBUM_OWNER_BOT,
    ALBUM_RUNNING,
    AlbumJob,
    AlbumRepository,
    FileOutcome,
)
from music_downloader.persistence.database import Database
from music_downloader.persistence.history_repo import HistoryRepository
from music_downloader.persistence.import_repo import ImportRepository
from music_downloader.persistence.library_index import LibraryIndex
from music_downloader.persistence.pending_repo import PendingRepository
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.persistence.wishlist_repo import WANTED_ANY, WANTED_BETTER, Wish, WishlistRepository
from music_downloader.pipeline import album as _album
from music_downloader.pipeline import fetch as _fetch
from music_downloader.pipeline import library as _library
from music_downloader.pipeline import resolve as _resolve
from music_downloader.pipeline import search as _search
from music_downloader.pipeline import wishlist as _wishlist
from music_downloader.pipeline.album import FolderListing
from music_downloader.pipeline.fetch import FetchOutcome, ProgressCallback
from music_downloader.pipeline.search import RankedResults
from music_downloader.pipeline.wishlist import WishDelivery
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.lossless_analyzer import LosslessVerdict
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

__all__ = [
    "AlbumJob",
    "FetchOutcome",
    "FileOutcome",
    "FolderListing",
    "Pipeline",
    "RankedResults",
    "SlskdClient",
    "SpotifyResolver",
    "Wish",
    "WishDelivery",
]

logger = logging.getLogger(__name__)

# How often the library index is rebuilt from OUTPUT_DIR (the first pass runs at startup).
LIBRARY_RESCAN_SECS = 3600

# Pipeline.album deliver values: save into OUTPUT_DIR, leave the files in DOWNLOAD_DIR,
# or hand each file to the front end (Telegram chat delivery sends it, then discards it).
ALBUM_DELIVER_LIBRARY = "library"
ALBUM_DELIVER_PATH = "path"
ALBUM_DELIVER_CHAT = "chat"
_ALBUM_DELIVER = (ALBUM_DELIVER_LIBRARY, ALBUM_DELIVER_PATH, ALBUM_DELIVER_CHAT)

# The front end's per-file hook: (index, outcome, the file's TrackInfo or None when it never landed).
# Runs after the pipeline handled the file and before the outcome is written; may change the outcome.
AlbumFileHook = Callable[[int, FileOutcome, TrackInfo | None], Awaitable[None]]


class Pipeline:
    """Owns the resolver, the slskd client, the scorer, the file processor and the repos."""

    def __init__(self, config: Config):
        self.config = config
        # Largest file a chat send may be (TELEGRAM_MAX_UPLOAD_MB): the chat
        # ranking, the Opus ladder and the send paths all read this one value.
        self.upload_limit_bytes = config.telegram_upload_limit_bytes
        self.spotify = SpotifyResolver(config.spotify_client_id, config.spotify_client_secret)
        self.slskd = SlskdClient(config.slskd_host, config.slskd_api_key)
        self.scorer = ResultScorer(
            duration_tolerance_secs=config.duration_tolerance_secs,
            exclude_keywords=config.exclude_keywords,
            chat_size_limit_bytes=self.upload_limit_bytes,
        )
        self.processor = FileProcessor(
            download_dir=config.download_dir,
            output_dir=config.output_dir,
            filename_template=config.filename_template,
        )
        self.db = Database(f"{config.data_dir}/importer.db")
        self.history_repo = HistoryRepository(self.db)
        self.import_repo = ImportRepository(self.db)
        self.settings_repo = SettingsRepository(self.db)
        self.pending_repo = PendingRepository(self.db)
        self.wishlist_repo = WishlistRepository(self.db)
        self.album_repo = AlbumRepository(self.db)
        # Written on every album job; album_recover takes over only this owner's jobs.
        self.album_owner = ALBUM_OWNER_BOT
        self.library_index = LibraryIndex(self.db, config.output_dir)
        self.playlist_resolver = PlaylistResolver(self.spotify)

    # ------------------------------------------------------------------ resolve

    def resolve(self, query: str) -> list[TrackInfo]:
        """Spotify candidates for a free-text query (empty when nothing matched)."""
        return _resolve.lookup(self.spotify, query)

    # ------------------------------------------------------------------- search

    async def search(
        self,
        query: str,
        track: TrackInfo,
        profile: str,
        max_duration_diff: int | None = None,
        response_limit: int | None = None,
    ) -> RankedResults:
        """Run one slskd search for *query* and rank the responses for *track* under *profile*.

        The returned list's ``hidden`` is how many copies the title guard dropped.
        """
        kwargs = {"response_limit": response_limit} if response_limit else {}
        raw_responses = await self.slskd.search(query, timeout_secs=self.config.search_timeout_secs, **kwargs)
        return self.rank(raw_responses, track, profile, max_duration_diff)

    def rank(
        self, raw_responses, track: TrackInfo, profile: str, max_duration_diff: int | None = None
    ) -> RankedResults:
        return _search.rank(
            self.slskd, self.scorer, raw_responses, track, profile, max_duration_diff, self.upload_limit_bytes
        )

    # -------------------------------------------------------------------- fetch

    async def fetch(self, result: SearchResult, progress_cb: ProgressCallback, analyze: bool = True) -> FetchOutcome:
        """Download *result* to the downloads dir; *analyze* runs the lossless check on checkable formats."""
        return await _fetch.fetch(
            self.slskd,
            self.processor,
            result,
            self.config.download_timeout_secs,
            progress_cb,
            self.analyze if analyze else None,
        )

    async def analyze(self, path: str) -> LosslessVerdict | None:
        return await _fetch.analyze(path)

    async def convert_to_opus(self, path: str, bitrate_kbps: int = 128) -> str | None:
        return await _fetch.convert_to_opus(path, bitrate_kbps)

    async def transcode(
        self, path: str, fmt: str, track: TrackInfo, album_art: _album.AlbumArt | None = None
    ) -> str | None:
        """*path* transcoded to send format *fmt* (fetch.SEND_FORMATS) with title, artist and cover.

        The cover is *album_art*'s when given (an album file), else the track's on Spotify.
        Returns a temporary file the caller deletes, or None when ffmpeg failed.
        """
        out_path = await _fetch.transcode(path, fmt, track.title, track.artist)
        if out_path:
            await self._embed(out_path, track, album_art)
        return out_path

    async def preview_clip(self, path: str, duration_secs: float = 60.0) -> str | None:
        return await _fetch.preview_clip(path, duration_secs)

    # ------------------------------------------------------------------ library

    async def find_similar(self, query: str) -> list[str]:
        """Library files that look like *query* (the duplicate check before a search), from the index."""
        return await asyncio.to_thread(self.library_index.find_similar, query)

    async def library_index_loop(self) -> None:
        """Build the library index now, then rebuild it every LIBRARY_RESCAN_SECS.

        Runs whatever DOWNLOAD_CLEANUP_HOURS says: the duplicate check needs
        the index even when the orphan sweep is off. Saves add their own row
        in between, so the rescan only catches files changed by hand.
        """
        while True:
            try:
                await asyncio.to_thread(self.library_index.rebuild)
            except Exception:
                logger.exception("Library index rebuild failed")
            await asyncio.sleep(LIBRARY_RESCAN_SECS)

    def opus_bitrates_that_fit(self, duration_secs: int) -> list[int]:
        return _fetch.opus_bitrates_that_fit(duration_secs, self.upload_limit_bytes)

    def target_filename(self, track: TrackInfo, extension: str, title: str | None = None) -> str:
        """The library file name for *track* in *extension*, also used when sending into a chat.

        *title* overrides the track title (the "(1min preview)" clip).
        """
        return self.processor.build_filename(track.artist, title or track.title, extension)

    async def save(
        self,
        source_path: str,
        track: TrackInfo,
        result: SearchResult,
        transfer_id: str = "",
        *,
        album_art: _album.AlbumArt | None = None,
        note: str = "",
    ) -> str | None:
        """Move a fetched file into the library, embed artwork, record the history row.

        Returns the library path, or None when processing failed (recorded as
        "process_failed"; the source is left in place). *transfer_id* is
        slskd's id for the download, removed from slskd once the source is gone.

        An album file passes *album_art* (the album's cover, looked up once for
        every file) and *note* "album" for its history row; its track number
        (``track.track_number``) reaches the name only through {track} in
        FILENAME_TEMPLATE, and the history row names the file as saved.
        """
        number = {"track": track.track_number} if track.track_number else {}
        target_path = await asyncio.to_thread(
            self.processor.process_file, source_path, track.artist, track.title, **number
        )
        saved_name = os.path.basename(target_path) if target_path and album_art is not None else None
        if target_path:
            try:
                await asyncio.to_thread(self.library_index.add, target_path)
            except Exception:
                logger.warning("Could not add %s to the library index", target_path, exc_info=True)
            await self.discard(source_path, result.username, transfer_id)
            await self._embed(target_path, track, album_art)
            await self.record_history(track, result, "success", filename=saved_name, note=note)
        else:
            await self.record_history(track, result, "process_failed", note=note)
        return target_path

    async def embed_artwork(self, path: str, track: TrackInfo, album_art: _album.AlbumArt | None = None) -> None:
        """Embed the track's Spotify cover, or *album_art*'s (looked up once per album) when given."""
        if album_art is None:
            await _library.embed_artwork(self.spotify.sp, path, track)
        else:
            await _library.embed_artwork_bytes(path, await album_art.get())

    async def _embed(self, path: str, track: TrackInfo, album_art: _album.AlbumArt | None) -> None:
        """embed_artwork with the album's cover when there is one (callers of a single track pass none)."""
        if album_art is None:
            await self.embed_artwork(path, track)
        else:
            await self.embed_artwork(path, track, album_art)

    async def discard(self, path: str, username: str = "", transfer_id: str = "") -> None:
        """Delete a source file from the downloads dir once it has been saved or sent.

        With a *transfer_id* the finished transfer is also removed from slskd.
        """
        await asyncio.to_thread(self.processor.cleanup_download, path)
        self.forget_transfer(username, transfer_id)

    remove_file = staticmethod(_library.remove_file)

    def forget_transfer(self, username: str, transfer_id: str) -> threading.Thread | None:
        """Remove a finished transfer from slskd's list once its file is gone.

        Best effort and fire-and-forget: runs in a daemon thread (no event loop
        needed, so it also works at startup), failures are logged at INFO.
        Returns the thread, or None when there is no transfer to remove.
        """
        if not username or not transfer_id:
            return None
        thread = threading.Thread(
            target=self.slskd.remove_transfer, args=(username, transfer_id), daemon=True, name="slskd-forget"
        )
        thread.start()
        return thread

    async def record_history(
        self, track: TrackInfo, result: SearchResult, status: str, filename: str | None = None, note: str = ""
    ) -> None:
        await _library.record_history(self.history_repo, track, result, status, filename, note)

    # -------------------------------------------------------------------- album

    async def album_listing(self, result: SearchResult) -> FolderListing:
        """The audio files of the peer folder *result* came from (empty, with a reason, when the peer is away)."""
        return await _album.browse_folder(self.slskd, result.username, _album.remote_dir(result.filename))

    async def album(
        self,
        result: SearchResult,
        track: TrackInfo,
        deliver: str = ALBUM_DELIVER_LIBRARY,
        progress_cb: _album.FolderProgress | None = None,
        listing: FolderListing | None = None,
        chat_id: int | None = None,
        *,
        on_file: AlbumFileHook | None = None,
        album_art: _album.AlbumArt | None = None,
        cancel: asyncio.Event | None = None,
    ) -> tuple[AlbumJob, FolderListing]:
        """Fetch every audio file of the folder *result* came from; *track* is the chosen copy's track.

        deliver "library" saves each file into OUTPUT_DIR as it lands (tags or
        file name for artist and title, the album's cover looked up once,
        history rows noted "album"), except a file the library already has
        under the name it would get: that download is deleted and the outcome
        marked skipped. "path" leaves the files in DOWNLOAD_DIR. "chat" does
        nothing with a file: *on_file* (the front end) sends it and discards it.
        Pass *listing* when album_listing already ran, and *album_art* to share
        the cover lookup with the front end. Setting *cancel* stops the
        waiting: the job ends "cancelled" with the files left unfinished. The
        job is kept in SQLite from the start; one failed file never stops the
        others. Returns the job (one outcome per file) and the listing it used.
        """
        if deliver not in _ALBUM_DELIVER:
            raise ValueError(f"deliver must be one of {', '.join(map(repr, _ALBUM_DELIVER))}, not {deliver!r}")
        if listing is None:
            listing = await self.album_listing(result)
        job = AlbumJob(
            username=listing.username,
            remote_dir=listing.remote_dir,
            files=list(listing.files),
            track=track,
            deliver=deliver,
            chat_id=chat_id,
            owner=self.album_owner,
        )
        if not listing.files:
            job.status = ALBUM_DONE
            return job, listing
        await asyncio.to_thread(self.album_repo.add, job)
        art = album_art or self.album_art(track)

        async def landed(index: int, outcome: FileOutcome) -> None:
            await self._album_file_landed(job, index, outcome, art, on_file)

        await _album.fetch_folder(
            self.slskd,
            self.processor,
            listing,
            self.config.download_timeout_secs,
            self.config.album_timeout_secs,
            progress_cb,
            landed,
            cancel=cancel,
        )
        job.status = ALBUM_CANCELLED if cancel is not None and cancel.is_set() and job.unfinished else ALBUM_DONE
        await asyncio.to_thread(self.album_repo.save, job)
        return job, listing

    async def album_recover(self) -> list[AlbumJob]:
        """At startup: mark this owner's album jobs left running as interrupted and process what already landed.

        Only jobs whose owner is *album_owner* are touched: a stdio MCP server
        sharing DATA_DIR may be running its own album right now. Transfers are
        not re-enqueued (slskd keeps them). A file with no outcome yet that is
        on disk in DOWNLOAD_DIR, inside a folder named like its remote one, is
        saved (or recorded, for "path") as if it had just finished; the rest
        stay without an outcome. A "chat" job's files are left to the orphan
        sweep: nobody is there to send them. Returns the interrupted jobs, so
        the front end can say what landed.
        """
        jobs = await asyncio.to_thread(self.album_repo.list_by_status, ALBUM_RUNNING, self.album_owner)
        for job in jobs:
            job.status = ALBUM_INTERRUPTED
            art = self.album_art(job.track)
            for index in job.unfinished if job.deliver != ALBUM_DELIVER_CHAT else ():
                remote = job.files[index].filename
                path = await asyncio.to_thread(_album.locate, self.processor, job.username, remote, True)
                if path:
                    await self._album_file_landed(job, index, FileOutcome(filename=remote, path=path), art)
            await asyncio.to_thread(self.album_repo.save, job)
            logger.info(
                "Album job %s (%s from %s) interrupted: %d landed, %d failed, %d unfinished",
                job.id,
                job.remote_dir,
                job.username,
                len(job.landed),
                len(job.failed),
                len(job.unfinished),
            )
        return jobs

    def album_art(self, track: TrackInfo) -> _album.AlbumArt:
        """The album cover of *track*'s album, looked up on first use and shared by every file."""
        return _album.AlbumArt(self.spotify.sp, track.artist, track.album, track.title)

    async def library_copy(self, path: str, track: TrackInfo) -> str | None:
        """The library file that saving *path* as *track* would duplicate: same name (accents and case aside)."""
        extension = os.path.splitext(path)[1].lstrip(".").lower() or "flac"
        name = self.processor.build_filename(track.artist, track.title, extension, track.track_number)
        rel_path = await asyncio.to_thread(self.library_index.find_stem, os.path.splitext(name)[0])
        return os.path.join(self.config.output_dir, rel_path) if rel_path else None

    async def _album_file_landed(
        self,
        job: AlbumJob,
        index: int,
        outcome: FileOutcome,
        art: _album.AlbumArt,
        on_file: AlbumFileHook | None = None,
    ) -> None:
        """Save (or record) one finished album file, run the front end's hook, then write its outcome."""
        file = job.files[index]
        result = file.as_result(job.username)
        info = None
        if outcome.ok and outcome.path:
            info = await asyncio.to_thread(_album.track_info_for, outcome.path, job.track.artist, job.track.album)
            if job.deliver == ALBUM_DELIVER_LIBRARY:
                existing = await self.library_copy(outcome.path, info)
                # A name this job saved itself is a repeated title on the album, not a
                # copy the library had: save() gives it a " (1)" name.
                ours = {os.path.normpath(o.path) for o in job.landed if o.path and not o.skipped}
                if existing and os.path.normpath(existing) not in ours:
                    await self.discard(outcome.path, job.username, outcome.transfer_id)
                    outcome.path, outcome.skipped = existing, True
                    logger.info("Album file %s skipped: the library has %s", file.basename, existing)
                else:
                    target = await self.save(
                        outcome.path, info, result, outcome.transfer_id, album_art=art, note="album"
                    )
                    if target:
                        outcome.path = target
                    else:
                        outcome.error = "process_failed"
            elif job.deliver == ALBUM_DELIVER_PATH:
                await self.record_history(info, result, "delivered", filename=outcome.path, note="album")
        if on_file is not None:
            try:
                await on_file(index, outcome, info)
            except Exception:
                logger.exception("The front end's handling of album file %s failed", file.basename)
        job.outcomes[index] = outcome
        try:
            await asyncio.to_thread(self.album_repo.save, job)
        except Exception:
            logger.warning("Could not write album job %s", job.id, exc_info=True)

    # ----------------------------------------------------------------- wishlist

    def wishlist_add(
        self,
        chat_id: int,
        user_id: int | None,
        track: TrackInfo,
        profile: str,
        wanted: str,
        baseline_tier: int | None = None,
    ) -> Wish:
        """Remember *track* to search again: *wanted* "any" copy, or "better" than *baseline_tier*."""
        if wanted not in (WANTED_ANY, WANTED_BETTER):
            raise ValueError(f"wanted must be {WANTED_ANY!r} or {WANTED_BETTER!r}, not {wanted!r}")
        if wanted == WANTED_BETTER and baseline_tier is None:
            raise ValueError("a 'better' wish needs the baseline tier")
        wish = Wish(
            chat_id=chat_id,
            user_id=user_id,
            track=track,
            profile=profile,
            wanted=wanted,
            baseline_tier=baseline_tier if wanted == WANTED_BETTER else None,
        )
        return self.wishlist_repo.add(wish)

    def wishlist_find(self, chat_id: int, track: TrackInfo) -> Wish | None:
        """The chat's wish for the same artist and title as *track*, if it has one."""
        key = (track.artist.casefold(), track.title.casefold())
        return next(
            (w for w in self.wishlist_list(chat_id) if (w.track.artist.casefold(), w.track.title.casefold()) == key),
            None,
        )

    def wishlist_list(self, chat_id: int) -> list[Wish]:
        """The chat's wishes, oldest first."""
        return self.wishlist_repo.list_for_chat(chat_id)

    def wishlist_remove(self, chat_id: int, wish_id: int) -> bool:
        """Drop a wish of *chat_id*; False when it was not there."""
        return self.wishlist_repo.remove(chat_id, wish_id)

    async def wishlist_check_due(
        self, deliver: WishDelivery, sleep=asyncio.sleep, skip: _wishlist.WishSkip | None = None
    ) -> int:
        """Search every due wish once (WISHLIST_CHECK_HOURS, WISHLIST_PAUSE_SECS between searches).

        Returns how many searches ran; see pipeline.wishlist.check_due (and *skip* there).
        """
        return await _wishlist.check_due(
            self.wishlist_repo,
            self.search,
            deliver,
            period_secs=self.config.wishlist_check_hours * 3600,
            pause_secs=self.config.wishlist_pause_secs,
            sleep=sleep,
            skip=skip,
        )

    async def wishlist_loop(self, deliver: WishDelivery, skip: _wishlist.WishSkip | None = None) -> None:
        """Check due wishes now, then every WISHLIST_TICK_SECS."""
        while True:
            try:
                await self.wishlist_check_due(deliver, skip=skip)
            except Exception:
                logger.exception("Wishlist check failed")
            await asyncio.sleep(_wishlist.WISHLIST_TICK_SECS)

    async def orphan_sweep_loop(
        self, protected_paths: Callable[[], set[str]], prune: Callable[[], None] | None = None
    ) -> None:
        await _library.orphan_sweep_loop(self.processor, self.config.download_cleanup_hours, protected_paths, prune)
