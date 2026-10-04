"""Album delivery: the whole folder a chosen copy came from.

The best copy of a track almost always sits inside a complete release on the
same peer. browse_folder lists that folder's audio files, fetch_folder
downloads them all, track_info_for names each file from its tags (or its file
name), and AlbumArt looks the album's Spotify cover up once for every file.
Pipeline.album composes them and keeps the job in SQLite.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
import mutagen

from music_downloader.formats import AUDIO_EXTENSIONS
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.album_repo import FileOutcome, FolderFile
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, ENQUEUE_FAILED, FILE_NOT_FOUND
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.slskd_client import DownloadStatus, SlskdClient
from music_downloader.tools.embed_artwork import fetch_spotify_artwork

logger = logging.getLogger(__name__)

# How long a peer gets to answer the directory listing.
BROWSE_TIMEOUT_SECS = 30

# FolderListing.reason values.
REASON_NO_ANSWER = "no_answer"  # the peer did not answer within BROWSE_TIMEOUT_SECS
REASON_UNREACHABLE = "unreachable"  # slskd or the peer refused (offline, browse denied, error)
REASON_NO_AUDIO = "no_audio"  # the peer answered, the folder holds no audio file

# slskd file attribute names, and the Soulseek attribute codes some versions send instead.
_ATTRIBUTES = {
    "BitRate": "bit_rate",
    "Length": "length",
    "SampleRate": "sample_rate",
    "BitDepth": "bit_depth",
    0: "bit_rate",
    1: "length",
    4: "sample_rate",
    5: "bit_depth",
}

# (index, total, slskd state, percent) for the file being waited on; index is 0-based.
FolderProgress = Callable[[int, int, str, float], Awaitable[None]]
# Called as each file finishes, before the next one is waited on; may change the outcome in place.
FileDone = Callable[[int, FileOutcome], Awaitable[None]]


def remote_dir(remote_filename: str) -> str:
    """The remote folder holding *remote_filename* (backslash paths); "" for a bare name."""
    return remote_filename.rsplit("\\", 1)[0] if "\\" in remote_filename else ""


@dataclass
class FolderListing:
    """The audio files of one folder on a peer, or why there are none."""

    username: str
    remote_dir: str
    files: list[FolderFile] = field(default_factory=list)
    answered: bool = True
    reason: str = ""  # REASON_*, empty when there are files
    detail: str = ""  # the error text behind REASON_UNREACHABLE

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def formats(self) -> list[str]:
        return sorted({f.extension for f in self.files})


def _attribute(file: dict, key: str, wanted: str) -> int | None:
    if file.get(key) is not None:
        return file[key]
    for attr in file.get("attributes") or []:
        if _ATTRIBUTES.get(attr.get("type")) == wanted:
            return attr.get("value")
    return None


def parse_directory(directories: list[dict] | dict, folder: str) -> list[FolderFile]:
    """The audio files of slskd's directory listing, sorted by name; other files are dropped."""
    if isinstance(directories, dict):  # one directory object instead of a list
        directories = [directories]
    files: list[FolderFile] = []
    for directory in directories:
        base = (directory.get("name") or folder).rstrip("\\")
        for f in directory.get("files") or []:
            name = f.get("filename") or ""
            if not name:
                continue
            full = name if "\\" in name else f"{base}\\{name}"
            extension = (f.get("extension") or "").lower().lstrip(".")
            if not extension and "." in name:
                extension = name.rsplit(".", 1)[-1].lower()
            if extension not in AUDIO_EXTENSIONS:
                continue
            files.append(
                FolderFile(
                    filename=full,
                    size=f.get("size") or 0,
                    extension=extension,
                    bit_rate=_attribute(f, "bitRate", "bit_rate"),
                    bit_depth=_attribute(f, "bitDepth", "bit_depth"),
                    sample_rate=_attribute(f, "sampleRate", "sample_rate"),
                    length=_attribute(f, "length", "length"),
                )
            )
    return sorted(files, key=lambda f: f.basename.casefold())


async def browse_folder(
    slskd: SlskdClient, username: str, folder: str, timeout_secs: float = BROWSE_TIMEOUT_SECS
) -> FolderListing:
    """The audio files in *folder* on *username*'s share.

    Never raises: a peer that is offline, refuses or does not answer within
    *timeout_secs* gives an empty listing with answered False and a reason.
    """
    try:
        directories = await asyncio.wait_for(
            asyncio.to_thread(slskd.browse_directory, username, folder), timeout=timeout_secs
        )
    except TimeoutError:
        logger.info("%s did not list %s within %s s", username, folder, timeout_secs)
        return FolderListing(username, folder, answered=False, reason=REASON_NO_ANSWER)
    except Exception as exc:
        logger.info("Could not list %s on %s: %s", folder, username, exc)
        return FolderListing(username, folder, answered=False, reason=REASON_UNREACHABLE, detail=str(exc))
    files = parse_directory(directories, folder)
    return FolderListing(username, folder, files, answered=True, reason="" if files else REASON_NO_AUDIO)


def locate(processor: FileProcessor, username: str, remote_filename: str) -> str | None:
    """Where slskd put *remote_filename*: <download dir>[/<username>]/<remote folder name>/<file> first.

    Album files share names across releases ("01 - Intro.flac"), so the file
    inside a folder named like the remote one wins over a bare name match.
    """
    parts = remote_filename.split("\\")
    basename, leaf = parts[-1], parts[-2] if len(parts) > 1 else ""
    if leaf:
        root = os.path.realpath(processor.download_dir)
        for candidate in (
            os.path.join(processor.download_dir, leaf, basename),
            os.path.join(processor.download_dir, username, leaf, basename),
        ):
            real = os.path.realpath(candidate)
            if real.startswith(root + os.sep) and os.path.isfile(real):
                return candidate
    return processor.find_downloaded_file(username, remote_filename)


async def _call(cb, *args) -> None:
    if cb is not None:
        with contextlib.suppress(Exception):
            await cb(*args)


def _enqueue(slskd: SlskdClient, username: str, files: list[FolderFile]) -> set[int]:
    """Enqueue every file, in one request when slskd takes the list, else one by one.

    Returns the indexes that could not be enqueued. A one-by-one refusal for a
    file slskd already lists (the batch got it in before failing) counts as queued.
    """
    if slskd.enqueue_files(username, [(f.filename, f.size) for f in files]):
        return set()
    failed = set()
    for i, f in enumerate(files):
        if slskd.enqueue_download(f.as_result(username)):
            continue
        if slskd.get_download_status(username, f.filename) is None:
            failed.add(i)
    return failed


async def _wait_unless_cancelled(wait: Awaitable, cancel: asyncio.Event | None):
    """(result of *wait*, False), or (None, True) when *cancel* is set first (the wait is dropped)."""
    if cancel is None:
        return await wait, False
    waiting = asyncio.ensure_future(wait)
    stopping = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        waiting.cancel()
        raise
    finally:
        stopping.cancel()
    if waiting.done():
        return waiting.result(), False
    waiting.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiting
    return None, True


async def fetch_folder(
    slskd: SlskdClient,
    processor: FileProcessor,
    listing: FolderListing,
    per_file_timeout_secs: int,
    total_timeout_secs: int,
    progress_cb: FolderProgress | None = None,
    on_file: FileDone | None = None,
    clock: Callable[[], float] = time.monotonic,
    cancel: asyncio.Event | None = None,
) -> list[FileOutcome]:
    """Download every file of *listing*; one FileOutcome per file, in listing order.

    Each file is waited on for at most *per_file_timeout_secs*, and the whole
    album for at most *total_timeout_secs*: once that is spent the files left
    fail with state "Timeout" (slskd keeps their transfers). Never raises on a
    failed file. *on_file* runs as each file finishes (the pipeline saves it
    there), so what landed is known even if the album never completes.

    Setting *cancel* stops the waiting: the file being waited on and the ones
    after it get no outcome (the list comes back shorter) and slskd keeps
    their transfers. A file already handed to *on_file* is finished first.
    """
    files, username, total = listing.files, listing.username, len(listing.files)
    outcomes: list[FileOutcome] = []
    if not files:
        return outcomes
    try:
        not_queued = await asyncio.to_thread(_enqueue, slskd, username, files)
    except Exception:
        logger.exception("Enqueueing %s from %s failed", listing.remote_dir, username)
        not_queued = set(range(total))
    started = clock()

    for i, f in enumerate(files):
        if cancel is not None and cancel.is_set():
            break
        outcome = FileOutcome(filename=f.filename)
        try:
            remaining = total_timeout_secs - (clock() - started)
            if i in not_queued:
                outcome.error = ENQUEUE_FAILED
            elif remaining <= 0:
                outcome.error, outcome.state = DOWNLOAD_FAILED, "Timeout"
            else:
                await _call(progress_cb, i, total, "Queued", 0.0)

                async def on_status(status: DownloadStatus, i=i) -> None:
                    await _call(progress_cb, i, total, status.state, status.percent_complete)

                status, cancelled = await _wait_unless_cancelled(
                    slskd.wait_for_download(
                        username=username,
                        filename=f.filename,
                        timeout_secs=max(1, int(min(per_file_timeout_secs, remaining))),
                        progress_cb=on_status,
                    ),
                    cancel,
                )
                if cancelled:
                    logger.info("Album fetch of %s from %s cancelled at %s", listing.remote_dir, username, f.basename)
                    break
                if status is None or status.is_failed:
                    outcome.error, outcome.state = DOWNLOAD_FAILED, status.state if status else "Timeout"
                else:
                    outcome.transfer_id = status.transfer_id
                    outcome.path = await asyncio.to_thread(locate, processor, username, f.filename)
                    if outcome.path is None:
                        outcome.error = FILE_NOT_FOUND
                    await _call(progress_cb, i, total, status.state, 100.0)
        except Exception as exc:
            logger.exception("Album file %s from %s failed", f.basename, username)
            outcome.path, outcome.error, outcome.state = None, DOWNLOAD_FAILED, str(exc)
        if on_file is not None:
            try:
                await on_file(i, outcome)
            except Exception:
                logger.exception("Handling album file %s failed", f.basename)
        outcomes.append(outcome)
    return outcomes


# ------------------------------------------------------------- names and tags

# "1-03 Title", "2.01 - Title": disc and track number.
_DISC_TRACK = re.compile(r"^\d{1,2}[-.](\d{2})\s*[-._)]?\s+(.+)$")
# "03 - Title", "03. Title", "03_Title", "03) Title", "3 - Title": a number and a separator.
_NUMBER_SEP = re.compile(r"^(\d{1,3})\s*[-._)]\s*(.+)$")
# "03 Title": a two-digit number, then a space.
_NUMBER_SPACE = re.compile(r"^(\d{2})\s+(\D.*)$")


def parse_filename(name: str) -> tuple[str | None, str, int | None]:
    """(artist, title, track number) from a file name; artist and number None when absent.

    Understands "NN - Title", "NN. Title", "NN Title", "D-NN Title" (disc and
    track), "Artist - Title", "NN - Artist - Title" and "Artist - NN - Title".
    Anything else is the title. A bare two-digit start ("99 Luftballons") reads
    as a track number; tags, when present, win over all of this.
    """
    stem = os.path.splitext(os.path.basename(name.replace("\\", "/")))[0].strip()
    track: int | None = None
    rest = stem
    m = _DISC_TRACK.match(stem) or _NUMBER_SEP.match(stem) or _NUMBER_SPACE.match(stem)
    if m:
        track, rest = int(m.group(1)), m.group(2).strip()
    parts = [p.strip() for p in rest.split(" - ")]
    artist: str | None = None
    if len(parts) >= 3 and track is None and parts[1].isdigit():
        artist, track, parts = parts[0], int(parts[1]), parts[2:]
    elif len(parts) >= 2:
        artist, parts = parts[0], parts[1:]
    title = " - ".join(p for p in parts if p) or stem or name
    return (artist or None), title, (track or None)


def _first(tags, key: str) -> str:
    try:
        values = tags.get(key) if tags is not None else None
    except Exception:
        return ""
    if not values:
        return ""
    value = values[0] if isinstance(values, list) else values
    return str(value).strip()


def _number(text: str) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return (int(m.group(1)) or None) if m else None


def read_tags(path: str) -> tuple[dict[str, str], int]:
    """Artist, title, album, track number and date tags of *path*, and its length in ms (0 unknown)."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        logger.debug("Could not read tags of %s", path, exc_info=True)
        return {}, 0
    if audio is None:
        return {}, 0
    tags = audio.tags
    found = {key: _first(tags, key) for key in ("artist", "albumartist", "title", "album", "tracknumber", "date")}
    length = getattr(getattr(audio, "info", None), "length", 0) or 0
    return {k: v for k, v in found.items() if v}, int(length * 1000)


def track_info_for(path: str, fallback_artist: str, album_hint: str) -> TrackInfo:
    """Name one album file: its tags first, then its file name, then the chosen track's artist and album."""
    tags, duration_ms = read_tags(path)
    name_artist, name_title, name_track = parse_filename(path)
    return TrackInfo(
        artist=tags.get("artist") or tags.get("albumartist") or name_artist or fallback_artist,
        title=tags.get("title") or name_title,
        album=tags.get("album") or album_hint,
        duration_ms=duration_ms,
        spotify_url="",
        year=tags.get("date", "")[:4],
        track_number=_number(tags.get("tracknumber", "")) or name_track,
    )


# ---------------------------------------------------------------------- cover


def fetch_spotify_album_artwork(sp, artist: str, album: str, title: str = "") -> bytes | None:
    """The cover of *album* by *artist* on Spotify; the cover of *title*'s album when no album matches."""
    if album:
        try:
            found = sp.search(q=f"album:{album} artist:{artist}", type="album", limit=1)
            items = (found or {}).get("albums", {}).get("items", [])
            images = items[0].get("images", []) if items else []
            if images:
                resp = httpx.get(images[0]["url"], timeout=15, follow_redirects=True)
                resp.raise_for_status()
                return resp.content
        except Exception:
            logger.debug("Spotify album artwork lookup failed for %s - %s", artist, album, exc_info=True)
    return fetch_spotify_artwork(sp, artist, title) if title else None


class AlbumArt:
    """The album's Spotify cover: looked up on first use, then the same bytes for every file."""

    def __init__(self, sp, artist: str, album: str, title: str = ""):
        self._args = (sp, artist, album, title)
        self._lock = asyncio.Lock()
        self._looked_up = False
        self._data: bytes | None = None

    async def get(self) -> bytes | None:
        async with self._lock:
            if not self._looked_up:
                self._looked_up = True
                try:
                    self._data = await asyncio.to_thread(fetch_spotify_album_artwork, *self._args)
                except Exception:
                    logger.debug("Album artwork lookup failed", exc_info=True)
        return self._data
