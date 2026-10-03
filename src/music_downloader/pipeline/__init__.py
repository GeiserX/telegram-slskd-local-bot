"""Everything between "we have a query" and "we have a file ready to hand over".

Nothing under this package imports telegram. The bot (music_downloader.bot)
parses updates, edits messages and sends files; the Pipeline resolves tracks
on Spotify, searches Soulseek through slskd, fetches files, checks them and
saves them to the library. Callbacks are plain async callables and results
are plain dataclasses, so another front end can drive the same Pipeline.
"""

import asyncio
from collections.abc import Callable

from music_downloader.config import Config
from music_downloader.metadata.playlist import PlaylistResolver
from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.history_repo import HistoryRepository
from music_downloader.persistence.import_repo import ImportRepository
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.pipeline import fetch as _fetch
from music_downloader.pipeline import library as _library
from music_downloader.pipeline import resolve as _resolve
from music_downloader.pipeline import search as _search
from music_downloader.pipeline.fetch import FetchOutcome, ProgressCallback
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.lossless_analyzer import LosslessVerdict
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

__all__ = ["FetchOutcome", "Pipeline", "SlskdClient", "SpotifyResolver"]


class Pipeline:
    """Owns the resolver, the slskd client, the scorer, the file processor and the repos."""

    def __init__(self, config: Config):
        self.config = config
        self.spotify = SpotifyResolver(config.spotify_client_id, config.spotify_client_secret)
        self.slskd = SlskdClient(config.slskd_host, config.slskd_api_key)
        self.scorer = ResultScorer(
            duration_tolerance_secs=config.duration_tolerance_secs,
            exclude_keywords=config.exclude_keywords,
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
    ) -> list[SearchResult]:
        """Run one slskd search for *query* and rank the responses for *track* under *profile*."""
        kwargs = {"response_limit": response_limit} if response_limit else {}
        raw_responses = await self.slskd.search(query, timeout_secs=self.config.search_timeout_secs, **kwargs)
        return self.rank(raw_responses, track, profile, max_duration_diff)

    def rank(
        self, raw_responses, track: TrackInfo, profile: str, max_duration_diff: int | None = None
    ) -> list[SearchResult]:
        return _search.rank(self.slskd, self.scorer, raw_responses, track, profile, max_duration_diff)

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

    async def preview_clip(self, path: str, duration_secs: float = 60.0) -> str | None:
        return await _fetch.preview_clip(path, duration_secs)

    # ------------------------------------------------------------------ library

    def find_similar(self, query: str) -> list[str]:
        """Library files that look like *query* (the duplicate check before a search)."""
        return self.processor.find_similar(query)

    def target_filename(self, track: TrackInfo, extension: str, title: str | None = None) -> str:
        """The library file name for *track* in *extension*, also used when sending into a chat.

        *title* overrides the track title (the "(1min preview)" clip).
        """
        return self.processor.build_filename(track.artist, title or track.title, extension)

    async def save(self, source_path: str, track: TrackInfo, result: SearchResult) -> str | None:
        """Move a fetched file into the library, embed artwork, record the history row.

        Returns the library path, or None when processing failed (recorded as
        "process_failed"; the source is left in place).
        """
        target_path = await asyncio.to_thread(self.processor.process_file, source_path, track.artist, track.title)
        if target_path:
            await self.discard(source_path)
            await self.embed_artwork(target_path, track)
            await self.record_history(track, result, "success")
        else:
            await self.record_history(track, result, "process_failed")
        return target_path

    async def embed_artwork(self, path: str, track: TrackInfo) -> None:
        await _library.embed_artwork(self.spotify.sp, path, track)

    async def discard(self, path: str) -> None:
        """Delete a source file from the downloads dir once it has been saved or sent."""
        await asyncio.to_thread(self.processor.cleanup_download, path)

    remove_file = staticmethod(_library.remove_file)

    async def record_history(
        self, track: TrackInfo, result: SearchResult, status: str, filename: str | None = None
    ) -> None:
        await _library.record_history(self.history_repo, track, result, status, filename)

    async def orphan_sweep_loop(self, protected_paths: Callable[[], set[str]]) -> None:
        await _library.orphan_sweep_loop(self.processor, self.config.download_cleanup_hours, protected_paths)
