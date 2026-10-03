"""The library side: artwork, the download-history row, deleting sources, the orphan sweep.

Saving itself (process_file, then these steps) is composed in Pipeline.save,
so one step can be replaced without the others noticing.
"""

import asyncio
import logging
import os
from collections.abc import Callable

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.history_repo import HistoryRepository
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.slskd_client import SearchResult
from music_downloader.tools.embed_artwork import embed_artwork_into_file, fetch_spotify_artwork

logger = logging.getLogger(__name__)


async def embed_artwork(sp, filepath: str, track: TrackInfo) -> None:
    """Fetch album artwork from Spotify and embed into the saved file."""
    try:
        art = await asyncio.to_thread(fetch_spotify_artwork, sp, track.artist, track.title)
        if art:
            ok = await asyncio.to_thread(embed_artwork_into_file, filepath, art)
            if ok:
                logger.info("Embedded Spotify artwork into %s (%d KB)", filepath, len(art) // 1024)
    except Exception:
        logger.debug("Artwork embedding failed for %s", filepath, exc_info=True)


def remove_file(path: str | None) -> None:
    """Delete a file from the downloads dir, never letting failure break the flow.

    The downloads volume may be mounted read-only; a failed delete must not
    swallow the reject/dismiss handling around it.
    """
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
            logger.info(f"Deleted file from downloads: {path}")
    except OSError as exc:
        logger.warning("Could not delete %s (downloads mount read-only?): %s", path, exc)


async def record_history(
    history_repo: HistoryRepository,
    track: TrackInfo,
    result: SearchResult,
    status: str,
    filename: str | None = None,
) -> None:
    """Add an entry to download history (persisted in SQLite)."""
    await asyncio.to_thread(
        history_repo.add,
        artist=track.artist,
        title=track.title,
        album=track.album,
        filename=filename or f"{track.artist} - {track.title}.{result.extension}",
        source_user=result.username,
        remote_path=result.filename,
        status=status,
        duration_secs=track.duration_secs,
        file_size=result.size,
    )


async def orphan_sweep_loop(
    processor: FileProcessor,
    max_age_hours: int,
    protected_paths: Callable[[], set[str]],
    prune: Callable[[], None] | None = None,
) -> None:
    """Hourly TTL sweep of abandoned files in the downloads dir.

    Runs once at startup (catching leftovers from before a restart) and
    then every hour. *protected_paths* names the in-flight downloads, which
    are protected explicitly on top of the mtime-based safety in
    FileProcessor.sweep_orphans. *prune*, when given, runs first on every
    pass (the caller drops its expired pending entries and their files).
    """
    while True:
        try:
            if prune is not None:
                prune()
            deleted, freed = await asyncio.to_thread(processor.sweep_orphans, max_age_hours, protected_paths())
            if deleted:
                logger.info(f"Orphan sweep: removed {deleted} abandoned file(s), freed {freed / (1024 * 1024):.0f} MB")
        except Exception:
            logger.exception("Orphan sweep failed")
        await asyncio.sleep(3600)
