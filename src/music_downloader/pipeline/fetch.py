"""Get one Soulseek result onto disk: enqueue, wait, locate, check; plus the Opus conversion."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from music_downloader.config import BYTES_PER_MB, DEFAULT_UPLOAD_LIMIT_BYTES
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.lossless_analyzer import (
    CHECKABLE_EXTENSIONS,
    LosslessVerdict,
    analyze_lossless,
    convert_to_ogg,
    create_preview_clip,
    transcode_file,
)
from music_downloader.search.slskd_client import DownloadStatus, SearchResult, SlskdClient

logger = logging.getLogger(__name__)

# FetchOutcome.error values. The two that are also download-history statuses
# carry the same spelling, so a flow can record the outcome as it is.
ENQUEUE_FAILED = "enqueue_failed"
DOWNLOAD_FAILED = "failed"
FILE_NOT_FOUND = "file_not_found"

# slskd reports a transfer "Completed" a moment before the file is moved from its incomplete
# folder into the downloads folder, so the first look on disk can miss it (seen live on
# 2026-10-04). Keep looking this long, this often, before giving up.
LOCATE_RETRY_SECS = 10.0
LOCATE_RETRY_STEP_SECS = 0.5


async def locate_landed(find: Callable[[], str | None], *, sleep=asyncio.sleep) -> str | None:
    """Run *find* (a sync disk lookup) in a thread, retrying for LOCATE_RETRY_SECS until it answers."""
    waited = 0.0
    while True:
        path = await asyncio.to_thread(find)
        if path is not None or waited >= LOCATE_RETRY_SECS:
            return path
        await sleep(LOCATE_RETRY_STEP_SECS)
        waited += LOCATE_RETRY_STEP_SECS


# Opus bitrates tried (highest first) when a chat-delivery track is over the limit.
_OPUS_BITRATES_KBPS = (192, 160, 128, 96)

# Per-chat send formats (/format), used by chat delivery only. "original" sends
# the file as downloaded; the others transcode it unless it is already in that format.
FORMAT_ORIGINAL = "original"
FORMAT_MP3 = "mp3"
FORMAT_OPUS = "opus"


@dataclass(frozen=True)
class SendFormat:
    label: str
    extension: str  # of the transcoded file
    codec_args: tuple[str, ...]
    same_extensions: frozenset[str]  # source extensions already in this format


SEND_FORMATS = {
    FORMAT_MP3: SendFormat(
        "MP3 320 kbps", "mp3", ("-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"), frozenset({"mp3"})
    ),
    FORMAT_OPUS: SendFormat("Opus 192 kbps", "ogg", ("-c:a", "libopus", "-b:a", "192k"), frozenset({"opus"})),
}
FORMAT_LABELS = {FORMAT_ORIGINAL: "Original", **{key: fmt.label for key, fmt in SEND_FORMATS.items()}}

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

    path = await locate_landed(lambda: processor.find_downloaded_file(result.username, result.filename))
    if not path:
        return FetchOutcome(error=FILE_NOT_FOUND)

    verdict = await analyze(path) if analyze is not None and result.extension in CHECKABLE_EXTENSIONS else None
    return FetchOutcome(path=path, verdict=verdict, transfer_id=status.transfer_id)


def opus_bitrates_that_fit(duration_secs: int, limit_bytes: int = DEFAULT_UPLOAD_LIMIT_BYTES) -> list[int]:
    """The highest Opus bitrate whose estimated size fits *limit_bytes*, then one lower fallback.

    Estimate = bitrate * duration * 5 % container overhead + 1 MB headroom.
    With an unknown duration nothing can be estimated, so every bitrate is tried.
    """
    if duration_secs <= 0:
        return list(_OPUS_BITRATES_KBPS)
    fitting = [
        kbps for kbps in _OPUS_BITRATES_KBPS if kbps * 1000 / 8 * duration_secs * 1.05 + BYTES_PER_MB <= limit_bytes
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


def already_in_format(path: str, extension: str, fmt: str) -> bool:
    """Whether a file with *extension* is already in send format *fmt* (no transcode needed).

    An .ogg may hold Opus or Vorbis, so it is opened to tell.
    """
    send_format = SEND_FORMATS.get(fmt)
    if send_format is None:
        return True
    extension = extension.lower()
    if extension in send_format.same_extensions:
        return True
    if fmt == FORMAT_OPUS and extension == "ogg":
        try:
            import mutagen.oggopus

            mutagen.oggopus.OggOpus(path)
            return True
        except Exception:
            return False
    return False


async def transcode(path: str, fmt: str, title: str, artist: str) -> str | None:
    """Transcode *path* into send format *fmt* in a thread; the temp file path or None."""
    send_format = SEND_FORMATS[fmt]
    try:
        return await asyncio.to_thread(
            transcode_file, path, send_format.codec_args, send_format.extension, title, artist
        )
    except Exception:
        logger.exception("Transcoding %s to %s failed", path, fmt)
        return None


async def preview_clip(filepath: str, duration_secs: float = 60.0) -> str | None:
    """Create a trimmed audio preview clip in a thread to avoid blocking."""
    try:
        return await asyncio.to_thread(create_preview_clip, filepath, duration_secs)
    except Exception:
        logger.exception("Preview clip creation failed for %s", filepath)
        return None
