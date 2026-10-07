"""
slskd API client for searching and downloading files from Soulseek.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import requests.exceptions
import slskd_api

from music_downloader.config import BYTES_PER_MB
from music_downloader.formats import AUDIO_EXTENSIONS, is_lossless

logger = logging.getLogger(__name__)


class SlskdUnavailableError(Exception):
    """Raised when the slskd API is unreachable (network/connection errors)."""


@dataclass
class SearchResult:
    """A single file result from a slskd search."""

    username: str
    filename: str  # Full remote path (e.g., "\\Music\\Artist\\Song.flac")
    size: int  # Bytes
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    length: int | None = None  # Duration in seconds
    has_free_slot: bool = False
    upload_speed: int = 0
    queue_length: int = 0
    score: float = 0.0  # Assigned by scorer

    @property
    def basename(self) -> str:
        """Extract filename from the full remote path."""
        # slskd paths use backslashes
        return self.filename.rsplit("\\", 1)[-1] if "\\" in self.filename else self.filename

    @property
    def extension(self) -> str:
        """File extension in lowercase."""
        return self.basename.rsplit(".", 1)[-1].lower() if "." in self.basename else ""

    @property
    def is_lossless(self) -> bool:
        """True for a lossless format (see music_downloader.formats)."""
        return is_lossless(self.extension)

    @property
    def duration_display(self) -> str:
        """Human-readable duration."""
        if not self.length:
            return "??:??"
        mins, secs = divmod(self.length, 60)
        return f"{mins}:{secs:02d}"

    @property
    def size_mb(self) -> float:
        """File size in MB of 1,000,000 bytes, the unit Telegram's upload cap uses."""
        return self.size / BYTES_PER_MB

    @property
    def quality_display(self) -> str:
        """Human-readable quality info."""
        parts = []
        if self.bit_depth and self.sample_rate:
            parts.append(f"{self.bit_depth}bit/{self.sample_rate / 1000:.1f}kHz")
        if self.bit_rate:
            parts.append(f"{self.bit_rate}kbps")
        return ", ".join(parts) if parts else (self.extension.upper() or "?")

    def __str__(self) -> str:
        return f"{self.basename} ({self.duration_display}, {self.quality_display}, {self.size_mb:.1f}MB)"


@dataclass
class DownloadStatus:
    """Status of a file download."""

    username: str
    filename: str
    state: str  # e.g., "Completed", "InProgress", "Queued", etc.
    percent_complete: float = 0.0
    bytes_transferred: int = 0
    size: int = 0
    average_speed: float = 0.0
    transfer_id: str = ""  # slskd's id for this transfer (removes it from slskd later)

    @property
    def is_complete(self) -> bool:
        # slskd returns comma-separated states like "Completed, Succeeded"
        state_lower = self.state.lower()
        return "completed" in state_lower or "succeeded" in state_lower

    @property
    def is_failed(self) -> bool:
        state_lower = self.state.lower()
        return any(kw in state_lower for kw in ("errored", "rejected", "timedout", "cancelled"))

    @property
    def is_active(self) -> bool:
        return not self.is_complete and not self.is_failed


@dataclass
class ActiveDownload:
    """Tracks a download request with its context."""

    search_result: SearchResult
    track_filename: str  # Desired output filename (e.g., "Artist - Title.flac")
    status: DownloadStatus | None = None
    local_path: str | None = None  # Path to the downloaded file on disk


class SlskdClient:
    """Wrapper around slskd-api for search and download operations."""

    # Per-request HTTP timeout. Without it slskd-api defaults to no timeout at
    # all, so a hung (not down) slskd blocks the caller forever.
    HTTP_TIMEOUT_SECS: int = 15

    def __init__(self, host: str, api_key: str):
        self.client = slskd_api.SlskdClient(host, api_key, timeout=self.HTTP_TIMEOUT_SECS)
        logger.info(f"slskd client initialized for {host}")

    async def search(self, query: str, timeout_secs: int = 30, response_limit: int = 500) -> list[dict]:
        """
        Start a search on slskd and wait for results.

        All synchronous slskd API calls are run in a thread executor so they
        don't block the event loop.  On timeout the search is explicitly
        stopped and whatever partial results arrived are returned.

        Args:
            query: Search query text.
            timeout_secs: Maximum time to wait for results.
            response_limit: Max peer responses to collect (lower = faster + avoids payload bugs).

        Returns:
            List of raw search response dicts from slskd API.
        """
        search_id: str | None = None

        try:
            return await asyncio.wait_for(
                self._search_inner(query, timeout_secs, response_limit),
                timeout=timeout_secs + 10,  # hard safety net
            )
        except TimeoutError:
            logger.warning(f"Hard timeout hit for search: {query}")
            # _search_inner handles its own cleanup, but if the hard
            # safety net fires we still try to grab partial results.
            if search_id:
                return await self._stop_and_collect(search_id)
            return []
        except requests.exceptions.RequestException as exc:
            logger.exception(f"slskd search failed for: {query}")
            raise SlskdUnavailableError(f"slskd API unreachable: {exc}") from exc
        except Exception:
            logger.exception(f"slskd search failed for: {query}")
            return []

    async def _cleanup_stale_searches(self):
        """Delete old searches to prevent API response caching issues.

        slskd keeps completed searches in memory.  When too many accumulate
        the ``includeResponses`` parameter silently returns empty arrays
        even though ``responseCount`` is > 0.  Clearing them before each
        new search avoids this bug.
        """
        try:
            existing = await asyncio.to_thread(self.client.searches.get_all)
            if existing:
                logger.debug("Cleaning %d stale searches", len(existing))
                for s in existing:
                    with contextlib.suppress(requests.exceptions.RequestException, KeyError):
                        await asyncio.to_thread(self.client.searches.delete, id=s["id"])
        except Exception:
            logger.warning("Failed to clean stale searches", exc_info=True)

    async def _search_inner(self, query: str, timeout_secs: int, response_limit: int) -> list[dict]:
        """Core search logic with polling, stop-on-timeout, and partial results."""
        await self._cleanup_stale_searches()

        search_state = await asyncio.to_thread(
            self.client.searches.search_text,
            searchText=query,
            searchTimeout=timeout_secs * 1000,
            responseLimit=response_limit,
        )
        search_id = search_state["id"]
        logger.info(f"Search started: id={search_id}, query='{query}'")

        min_wait = 5
        try:
            start = time.time()
            last_count = 0
            stable_since: float | None = None

            while time.time() - start < timeout_secs:
                await asyncio.sleep(2)
                state = await asyncio.to_thread(self.client.searches.state, id=search_id)

                current_count = state.get("fileCount", 0)
                resp_count = state.get("responseCount", 0)
                is_complete = state.get("isComplete", False)
                elapsed = time.time() - start

                if current_count != last_count:
                    last_count = current_count
                    stable_since = time.time()
                    logger.debug(f"Search progress: {current_count} files from {resp_count} peers")
                elif stable_since and (time.time() - stable_since > 8):
                    logger.info(f"Search stabilized with {current_count} files from {resp_count} peers")
                    break

                if is_complete and elapsed >= min_wait:
                    logger.info(f"Search completed with {current_count} files from {resp_count} peers")
                    break
            else:
                logger.info(
                    f"Search polling timeout ({timeout_secs}s) for '{query}', stopping and grabbing partial results"
                )

        except Exception:
            logger.exception(f"Error during search polling for: {query}")

        # Always stop the search before retrieving responses — slskd returns
        # empty responses array while a search is still in-progress (when
        # responseLimit hasn't been reached but the search hasn't timed out).
        with contextlib.suppress(requests.exceptions.RequestException):
            await asyncio.to_thread(self.client.searches.stop, id=search_id)

        final_state = await asyncio.to_thread(
            self.client.searches.state,
            id=search_id,
            includeResponses=True,
        )
        responses: list[dict] = final_state.get("responses", [])

        if not responses:
            resp_count = final_state.get("responseCount", 0)
            file_count = final_state.get("fileCount", 0)
            if resp_count > 0 or file_count > 0:
                logger.info(
                    "state(includeResponses) empty despite %d peers / %d files — "
                    "falling back to search_responses endpoint",
                    resp_count,
                    file_count,
                )
                with contextlib.suppress(requests.exceptions.RequestException):
                    responses = await asyncio.to_thread(
                        self.client.searches.search_responses,
                        id=search_id,
                    )
                logger.info("search_responses returned %d responses", len(responses))

        # Clean up
        with contextlib.suppress(requests.exceptions.RequestException):
            await asyncio.to_thread(self.client.searches.delete, id=search_id)

        return responses

    async def _stop_and_collect(self, search_id: str) -> list[dict]:
        """Stop a search and return whatever partial results exist."""
        with contextlib.suppress(requests.exceptions.RequestException):
            await asyncio.to_thread(self.client.searches.stop, id=search_id)
        try:
            final_state = await asyncio.to_thread(
                self.client.searches.state,
                id=search_id,
                includeResponses=True,
            )
            responses: list[dict] = final_state.get("responses", [])
            if not responses and final_state.get("responseCount", 0) > 0:
                with contextlib.suppress(requests.exceptions.RequestException):
                    responses = await asyncio.to_thread(
                        self.client.searches.search_responses,
                        id=search_id,
                    )
        except Exception:
            logger.exception(f"Failed to collect partial results for {search_id}")
            responses = []
        with contextlib.suppress(requests.exceptions.RequestException):
            await asyncio.to_thread(self.client.searches.delete, id=search_id)
        return responses

    def parse_results(self, responses: list[dict]) -> list[SearchResult]:
        """
        Parse raw slskd search responses into SearchResult objects.

        Keeps every file whose extension is in music_downloader.formats.AUDIO_EXTENSIONS,
        lossless and lossy alike; SearchResult.is_lossless tells them apart.

        Args:
            responses: Raw responses from slskd search API.

        Returns:
            List of SearchResult objects.
        """
        results = []

        for response in responses:
            username = response.get("username", "")
            has_free_slot = response.get("hasFreeUploadSlot", False)
            upload_speed = response.get("uploadSpeed", 0)
            queue_length = response.get("queueLength", 0)

            for f in response.get("files", []):
                filename = f.get("filename", "")
                extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

                if extension not in AUDIO_EXTENSIONS:
                    continue

                results.append(
                    SearchResult(
                        username=username,
                        filename=filename,
                        size=f.get("size", 0),
                        bit_rate=f.get("bitRate"),
                        bit_depth=f.get("bitDepth"),
                        sample_rate=f.get("sampleRate"),
                        length=f.get("length"),
                        has_free_slot=has_free_slot,
                        upload_speed=upload_speed,
                        queue_length=queue_length,
                    )
                )

        lossless = sum(1 for r in results if r.is_lossless)
        logger.info(
            f"Parsed {len(results)} audio results ({lossless} lossless, {len(results) - lossless} lossy) "
            f"from {len(responses)} responses"
        )
        return results

    def enqueue_download(self, result: SearchResult) -> bool:
        """
        Enqueue a file for download via slskd.

        Args:
            result: The SearchResult to download.

        Returns:
            True if enqueue succeeded.
        """
        try:
            files = [{"filename": result.filename, "size": result.size}]
            self.client.transfers.enqueue(username=result.username, files=files)
            logger.info(f"Enqueued download: {result.basename} from {result.username}")
            return True
        except Exception:
            logger.exception(f"Failed to enqueue download: {result.basename}")
            return False

    def enqueue_files(self, username: str, files: list[tuple[str, int]]) -> bool:
        """Enqueue several files of one peer in a single request: *files* is (remote path, size) pairs.

        True when slskd accepted the list; False (logged) when it refused or failed.
        """
        try:
            # slskd_api raises on any non-2xx answer, so a refusal arrives here as requests.HTTPError.
            self.client.transfers.enqueue(
                username=username, files=[{"filename": name, "size": size} for name, size in files]
            )
        except Exception:
            logger.warning("Failed to enqueue %d files from %s in one request", len(files), username, exc_info=True)
            return False
        logger.info("Enqueued %d files from %s", len(files), username)
        return True

    def browse_directory(self, username: str, directory: str) -> list[dict]:
        """The peer's listing of one remote *directory* (slskd's users/directory), as a list of directories.

        Raises when slskd or the peer fails (offline, refused, timed out): the caller words the reason.
        """
        raw = self.client.users.directory(username=username, directory=directory)
        if isinstance(raw, dict):
            return [raw]
        return list(raw or [])

    def _transfers(self, username: str, filename: str) -> list[dict]:
        """slskd's download records of *filename* from *username* (raises when slskd fails)."""
        downloads = self.client.transfers.get_downloads(username=username)
        if not downloads:
            return []
        # Downloads response is a dict with 'directories' containing transfer info
        return [
            transfer
            for directory in downloads.get("directories", [])
            for transfer in directory.get("files", [])
            if transfer.get("filename") == filename
        ]

    def get_download_status(self, username: str, filename: str) -> DownloadStatus | None:
        """
        Get the download status for a specific file.

        Args:
            username: The Soulseek username of the source.
            filename: The remote filename.

        Returns:
            DownloadStatus or None if not found. When slskd lists the file more
            than once (a finished or given-up transfer next to a new one), the
            one still running wins, else the newest.
        """
        try:
            matches = self._transfers(username, filename)
            if not matches:
                return None
            statuses = [
                DownloadStatus(
                    username=username,
                    filename=filename,
                    state=transfer.get("state", "Unknown"),
                    percent_complete=transfer.get("percentComplete", 0),
                    bytes_transferred=transfer.get("bytesTransferred", 0),
                    size=transfer.get("size", 0),
                    average_speed=transfer.get("averageSpeed", 0),
                    transfer_id=transfer.get("id", ""),
                )
                for transfer in matches
            ]
            newest = max(
                range(len(matches)),
                key=lambda i: (statuses[i].is_active, matches[i].get("requestedAt") or "", i),
            )
            return statuses[newest]

        except Exception:
            logger.exception(f"Failed to get download status for {filename}")
            return None

    async def wait_for_download(
        self,
        username: str,
        filename: str,
        timeout_secs: int = 600,
        progress_cb: "Callable[[DownloadStatus], Awaitable[None]] | None" = None,
        progress_interval_secs: float = 10.0,
    ) -> DownloadStatus | None:
        """
        Wait for a download to complete, polling periodically.

        Args:
            username: Source username.
            filename: Remote filename.
            timeout_secs: Maximum wait time.
            progress_cb: Optional async callable receiving the in-flight
                DownloadStatus at most every ``progress_interval_secs`` —
                the data was previously fetched and dropped at DEBUG level,
                leaving the user staring at a static "Downloading…".
            progress_interval_secs: Minimum seconds between progress callbacks.

        Returns:
            Final DownloadStatus, or None on timeout.
        """
        start = time.time()
        last_progress = 0.0

        while time.time() - start < timeout_secs:
            await asyncio.sleep(3)
            status = await asyncio.to_thread(self.get_download_status, username, filename)

            if status is None:
                logger.debug(f"No status yet for {filename}")
                continue

            if status.is_complete:
                logger.info(f"Download complete: {filename}")
                return status

            if status.is_failed:
                logger.warning(f"Download failed ({status.state}): {filename}")
                return status

            if progress_cb is not None and time.time() - last_progress >= progress_interval_secs:
                last_progress = time.time()
                with contextlib.suppress(Exception):
                    await progress_cb(status)

            logger.debug(f"Download {status.percent_complete:.0f}%: {filename}")

        logger.warning(f"Download timed out after {timeout_secs}s: {filename}")
        return None

    def remove_transfer(self, username: str, transfer_id: str) -> bool:
        """Remove a finished download from slskd's transfer list (the file itself is not touched).

        Best effort: returns False instead of raising, logging at INFO.
        """
        try:
            ok = self.client.transfers.cancel_download(username, transfer_id, remove=True)
        except Exception as exc:
            logger.info("Could not remove transfer %s from %s in slskd: %s", transfer_id, username, exc)
            return False
        if not ok:
            logger.info("slskd refused to remove transfer %s from %s", transfer_id, username)
        return bool(ok)

    def cancel_downloads(self, username: str, filename: str) -> int:
        """Stop every slskd transfer of *filename* from *username* and drop its record (remove=true).

        For a transfer the bot gave up on (timeout, cancel, error). Without
        this slskd keeps downloading and the file lands later with nobody to
        pick it up. Best effort: returns how many transfers slskd cancelled,
        logging failures at INFO instead of raising.
        """
        try:
            transfers = self._transfers(username, filename)
        except Exception as exc:
            logger.info("Could not list the transfers of %s from %s to cancel them: %s", filename, username, exc)
            return 0
        cancelled = 0
        for transfer in transfers:
            transfer_id = transfer.get("id")
            if not transfer_id:
                continue
            try:
                ok = self.client.transfers.cancel_download(username, transfer_id, remove=True)
            except Exception as exc:
                logger.info("Could not cancel transfer %s of %s from %s: %s", transfer_id, filename, username, exc)
                continue
            if ok:
                cancelled += 1
                logger.info(
                    "Cancelled transfer %s of %s from %s (%s)", transfer_id, filename, username, transfer.get("state")
                )
        return cancelled

    def is_up(self) -> bool:
        """Whether slskd answers application/state (the health probe)."""
        try:
            self.client.application.state()
        except Exception as exc:
            logger.debug("slskd health probe failed: %s", exc)
            return False
        return True

    def get_downloads_directory(self) -> list[dict]:
        """Get the contents of the slskd downloads directory."""
        try:
            return self.client.files.get_downloads_dir()
        except Exception:
            logger.exception("Failed to list downloads directory")
            return []
