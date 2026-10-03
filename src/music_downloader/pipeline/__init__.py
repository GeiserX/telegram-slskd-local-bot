"""Everything between "we have a query" and "we have a file ready to hand over".

Nothing under this package imports telegram. The bot (music_downloader.bot)
parses updates, edits messages and sends files; the Pipeline resolves tracks
on Spotify, searches Soulseek through slskd, fetches files, checks them and
saves them to the library. Callbacks are plain async callables and results
are plain dataclasses, so another front end can drive the same Pipeline.
"""

import asyncio
import logging
import threading
from collections.abc import Callable

from music_downloader.config import Config
from music_downloader.metadata.playlist import PlaylistResolver
from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.history_repo import HistoryRepository
from music_downloader.persistence.import_repo import ImportRepository
from music_downloader.persistence.library_index import LibraryIndex
from music_downloader.persistence.pending_repo import PendingRepository
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.persistence.wishlist_repo import WANTED_ANY, WANTED_BETTER, Wish, WishlistRepository
from music_downloader.pipeline import fetch as _fetch
from music_downloader.pipeline import library as _library
from music_downloader.pipeline import resolve as _resolve
from music_downloader.pipeline import search as _search
from music_downloader.pipeline import wishlist as _wishlist
from music_downloader.pipeline.fetch import FetchOutcome, ProgressCallback
from music_downloader.pipeline.search import RankedResults
from music_downloader.pipeline.wishlist import WishDelivery
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.lossless_analyzer import LosslessVerdict
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

__all__ = ["FetchOutcome", "Pipeline", "RankedResults", "SlskdClient", "SpotifyResolver", "Wish", "WishDelivery"]

logger = logging.getLogger(__name__)

# How often the library index is rebuilt from OUTPUT_DIR (the first pass runs at startup).
LIBRARY_RESCAN_SECS = 3600


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

    async def transcode(self, path: str, fmt: str, track: TrackInfo) -> str | None:
        """*path* transcoded to send format *fmt* (fetch.SEND_FORMATS) with title, artist and cover.

        Returns a temporary file the caller deletes, or None when ffmpeg failed.
        """
        out_path = await _fetch.transcode(path, fmt, track.title, track.artist)
        if out_path:
            await self.embed_artwork(out_path, track)
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

    async def save(self, source_path: str, track: TrackInfo, result: SearchResult, transfer_id: str = "") -> str | None:
        """Move a fetched file into the library, embed artwork, record the history row.

        Returns the library path, or None when processing failed (recorded as
        "process_failed"; the source is left in place). *transfer_id* is
        slskd's id for the download, removed from slskd once the source is gone.
        """
        target_path = await asyncio.to_thread(self.processor.process_file, source_path, track.artist, track.title)
        if target_path:
            try:
                await asyncio.to_thread(self.library_index.add, target_path)
            except Exception:
                logger.warning("Could not add %s to the library index", target_path, exc_info=True)
            await self.discard(source_path, result.username, transfer_id)
            await self.embed_artwork(target_path, track)
            await self.record_history(track, result, "success")
        else:
            await self.record_history(track, result, "process_failed")
        return target_path

    async def embed_artwork(self, path: str, track: TrackInfo) -> None:
        await _library.embed_artwork(self.spotify.sp, path, track)

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
        self, track: TrackInfo, result: SearchResult, status: str, filename: str | None = None
    ) -> None:
        await _library.record_history(self.history_repo, track, result, status, filename)

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

    def wishlist_list(self, chat_id: int) -> list[Wish]:
        """The chat's wishes, oldest first."""
        return self.wishlist_repo.list_for_chat(chat_id)

    def wishlist_remove(self, chat_id: int, wish_id: int) -> bool:
        """Drop a wish of *chat_id*; False when it was not there."""
        return self.wishlist_repo.remove(chat_id, wish_id)

    async def wishlist_check_due(self, deliver: WishDelivery, sleep=asyncio.sleep) -> int:
        """Search every due wish once (WISHLIST_CHECK_HOURS, WISHLIST_PAUSE_SECS between searches).

        Returns how many searches ran; see pipeline.wishlist.check_due.
        """
        return await _wishlist.check_due(
            self.wishlist_repo,
            self.search,
            deliver,
            period_secs=self.config.wishlist_check_hours * 3600,
            pause_secs=self.config.wishlist_pause_secs,
            sleep=sleep,
        )

    async def wishlist_loop(self, deliver: WishDelivery) -> None:
        """Check due wishes now, then every WISHLIST_TICK_SECS."""
        while True:
            try:
                await self.wishlist_check_due(deliver)
            except Exception:
                logger.exception("Wishlist check failed")
            await asyncio.sleep(_wishlist.WISHLIST_TICK_SECS)

    async def orphan_sweep_loop(
        self, protected_paths: Callable[[], set[str]], prune: Callable[[], None] | None = None
    ) -> None:
        await _library.orphan_sweep_loop(self.processor, self.config.download_cleanup_hours, protected_paths, prune)
