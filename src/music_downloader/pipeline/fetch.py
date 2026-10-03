"""Get one Soulseek result onto disk: enqueue, wait, locate, check; plus the Opus conversion."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.lossless_analyzer import (
    CHECKABLE_EXTENSIONS,
    LosslessVerdict,
    analyze_lossless,
    convert_to_ogg,
    create_preview_clip,
)
from music_downloader.search.scorer import CHAT_SIZE_LIMIT_BYTES
from music_downloader.search.slskd_client import DownloadStatus, SearchResult, SlskdClient

logger = logging.getLogger(__name__)

# FetchOutcome.error values. The two that are also download-history statuses
# carry the same spelling, so a flow can record the outcome as it is.
ENQUEUE_FAILED = "enqueue_failed"
DOWNLOAD_FAILED = "failed"
FILE_NOT_FOUND = "file_not_found"

# Opus bitrates tried (highest first) when a chat-delivery track is over the limit.
_OPUS_BITRATES_KBPS = (192, 160, 128, 96)

ProgressCallback = Callable[[DownloadStatus], Awaitable[None]]
Analyzer = Callable[[str], Awaitable[LosslessVerdict | None]]


@dataclass
class FetchOutcome:
    """What fetch() produced: a file on disk, or why there is none."""

    path: str | None = None
    verdict: LosslessVerdict | None = None
    # None on success, else ENQUEUE_FAILED / DOWNLOAD_FAILED / FILE_NOT_FOUND.
    error: str | None = None
    # slskd transfer state behind DOWNLOAD_FAILED ("Timeout" when it never finished).
    state: str = ""
    # slskd's id for the finished transfer (see Pipeline.forget_transfer).
    transfer_id: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


async def fetch(
    slskd: SlskdClient,
    processor: FileProcessor,
    result: SearchResult,
    timeout_secs: int,
    progress_cb: ProgressCallback,
    analyze: Analyzer | None,
) -> FetchOutcome:
    """Enqueue *result*, wait for it, find it on disk and (optionally) run the lossless check.

    *analyze* runs only on formats the spectrum check understands
    (CHECKABLE_EXTENSIONS); pass None to skip the check entirely.
    Exceptions from slskd propagate: the caller decides how to report them.
    """
    success = await asyncio.to_thread(slskd.enqueue_download, result)
    if not success:
        return FetchOutcome(error=ENQUEUE_FAILED)

    status = await slskd.wait_for_download(
        username=result.username,
        filename=result.filename,
        timeout_secs=timeout_secs,
        progress_cb=progress_cb,
    )
    if status is None or status.is_failed:
        return FetchOutcome(error=DOWNLOAD_FAILED, state=status.state if status else "Timeout")

    path = processor.find_downloaded_file(result.username, result.filename)
    if not path:
        return FetchOutcome(error=FILE_NOT_FOUND)

    verdict = await analyze(path) if analyze is not None and result.extension in CHECKABLE_EXTENSIONS else None
    return FetchOutcome(path=path, verdict=verdict, transfer_id=status.transfer_id)


def opus_bitrates_that_fit(duration_secs: int) -> list[int]:
    """The highest Opus bitrate whose estimated size fits, then one lower fallback.

    Estimate = bitrate * duration * 5 % container overhead + 1 MiB headroom.
    With an unknown duration nothing can be estimated, so every bitrate is tried.
    """
    if duration_secs <= 0:
        return list(_OPUS_BITRATES_KBPS)
    fitting = [
        kbps
        for kbps in _OPUS_BITRATES_KBPS
        if kbps * 1000 / 8 * duration_secs * 1.05 + 1024 * 1024 <= CHAT_SIZE_LIMIT_BYTES
    ]
    return fitting[:2]


async def analyze(filepath: str) -> LosslessVerdict | None:
    """Run the spectral lossless check in a thread to avoid blocking."""
    try:
        verdict = await asyncio.to_thread(analyze_lossless, filepath)
        if verdict:
            logger.info("Lossless check for %s: %s (cutoff=%.1fkHz)", filepath, verdict.verdict, verdict.cutoff_khz)
        return verdict
    except Exception:
        logger.exception("Lossless check failed for %s", filepath)
        return None


async def convert_to_opus(filepath: str, bitrate_kbps: int = 128) -> str | None:
    """Convert a full audio file to OGG Opus in a thread."""
    try:
        return await asyncio.to_thread(convert_to_ogg, filepath, bitrate_kbps)
    except Exception:
        logger.exception("OGG conversion failed for %s", filepath)
        return None


async def preview_clip(filepath: str, duration_secs: float = 60.0) -> str | None:
    """Create a trimmed audio preview clip in a thread to avoid blocking."""
    try:
        return await asyncio.to_thread(create_preview_clip, filepath, duration_secs)
    except Exception:
        logger.exception("Preview clip creation failed for %s", filepath)
        return None
