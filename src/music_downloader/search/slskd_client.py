"""
slskd API client for searching and downloading files from Soulseek.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

import slskd_api

logger = logging.getLogger(__name__)


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
    def duration_display(self) -> str:
        """Human-readable duration."""
        if not self.length:
            return "??:??"
        mins, secs = divmod(self.length, 60)
        return f"{mins}:{secs:02d}"

    @property
    def size_mb(self) -> float:
        """File size in MB."""
        return self.size / (1024 * 1024)

    @property
    def quality_display(self) -> str:
        """Human-readable quality info."""
        parts = []
        if self.bit_depth and self.sample_rate:
            parts.append(f"{self.bit_depth}bit/{self.sample_rate / 1000:.1f}kHz")
        if self.bit_rate:
            parts.append(f"{self.bit_rate}kbps")
        return ", ".join(parts) if parts else "FLAC"

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

    def __init__(self, host: str, api_key: str):
        self.client = slskd_api.SlskdClient(host, api_key)
        logger.info(f"slskd client initialized for {host}")

    async def search(self, query: str, timeout_secs: int = 30) -> list[dict]:
        """
        Start a search on slskd and wait for results.

        All synchronous slskd API calls are run in a thread executor so they
        don't block the event loop.  On timeout the search is explicitly
        stopped and whatever partial results arrived are returned.

        Args:
            query: Search query text.
            timeout_secs: Maximum time to wait for results.

        Returns:
            List of raw search response dicts from slskd API.
        """
        search_id: str | None = None

        try:
            return await asyncio.wait_for(
                self._search_inner(query, timeout_secs),
                timeout=timeout_secs + 10,  # hard safety net
            )
        except TimeoutError:
            logger.warning(f"Hard timeout hit for search: {query}")
            # _search_inner handles its own cleanup, but if the hard
            # safety net fires we still try to grab partial results.
            if search_id:
                return await self._stop_and_collect(search_id)
            return []
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
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.client.searches.delete, id=s["id"])
        except Exception:
            logger.debug("Failed to clean stale searches", exc_info=True)

    async def _search_inner(self, query: str, timeout_secs: int) -> list[dict]:
        """Core search logic with polling, stop-on-timeout, and partial results."""
        await self._cleanup_stale_searches()

        search_state = await asyncio.to_thread(
            self.client.searches.search_text,
            searchText=query,
            searchTimeout=timeout_secs * 1000,  # align server-side timeout (ms)
        )
        search_id = search_state["id"]
        logger.info(f"Search started: id={search_id}, query='{query}'")

        timed_out = False
        try:
            start = time.time()
            last_count = 0
            stable_since: float | None = None

            while time.time() - start < timeout_secs:
                await asyncio.sleep(2)
                state = await asyncio.to_thread(self.client.searches.state, id=search_id)

                current_count = state.get("fileCount", 0)
                is_complete = state.get("isComplete", False)

                if current_count != last_count:
                    last_count = current_count
                    stable_since = time.time()
                    logger.debug(f"Search progress: {current_count} files found")
                elif stable_since and (time.time() - stable_since > 8):
                    logger.info(f"Search stabilized with {current_count} files")
                    break

                if is_complete:
                    logger.info(f"Search completed with {current_count} files")
                    break
            else:
                timed_out = True
                logger.info(
                    f"Search polling timeout ({timeout_secs}s) for '{query}', stopping and grabbing partial results"
                )

        except Exception:
            logger.exception(f"Error during search polling for: {query}")
            timed_out = True

        # Stop the search if it timed out (graceful cancel)
        if timed_out:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.client.searches.stop, id=search_id)

        # Fetch responses embedded in the state dict.  The separate
        # /searches/{id}/responses endpoint sometimes returns an empty
        # list even when files exist; using state(includeResponses=True)
        # is the reliable alternative.
        final_state = await asyncio.to_thread(self.client.searches.state, id=search_id, includeResponses=True)
        responses: list[dict] = final_state.get("responses", [])

        # Clean up
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.client.searches.delete, id=search_id)

        return responses

    async def _stop_and_collect(self, search_id: str) -> list[dict]:
        """Stop a search and return whatever partial results exist."""
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.client.searches.stop, id=search_id)
        try:
            final_state = await asyncio.to_thread(self.client.searches.state, id=search_id, includeResponses=True)
            responses: list[dict] = final_state.get("responses", [])
        except Exception:
            logger.exception(f"Failed to collect partial results for {search_id}")
            responses = []
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.client.searches.delete, id=search_id)
        return responses

    # Audio formats accepted in fallback mode (lossless + common lossy)
    AUDIO_EXTENSIONS = {"flac", "alac", "wav", "aiff", "mp3", "aac", "m4a", "ogg", "opus", "wma"}

    def parse_results(self, responses: list[dict], flac_only: bool = True) -> list[SearchResult]:
        """
        Parse raw slskd search responses into SearchResult objects.

        Args:
            responses: Raw responses from slskd search API.
            flac_only: If True, only include FLAC files. If False, include all audio formats.

        Returns:
            List of SearchResult objects.
        """
        results = []
        allowed = {"flac"} if flac_only else self.AUDIO_EXTENSIONS

        for response in responses:
            username = response.get("username", "")
            has_free_slot = response.get("hasFreeUploadSlot", False)
            upload_speed = response.get("uploadSpeed", 0)
            queue_length = response.get("queueLength", 0)

            for f in response.get("files", []):
                filename = f.get("filename", "")
                extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

                if extension not in allowed:
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

        label = "FLAC" if flac_only else "audio"
        logger.info(f"Parsed {len(results)} {label} results from {len(responses)} responses")
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

    def get_download_status(self, username: str, filename: str) -> DownloadStatus | None:
        """
        Get the download status for a specific file.

        Args:
            username: The Soulseek username of the source.
            filename: The remote filename.

        Returns:
            DownloadStatus or None if not found.
        """
        try:
            downloads = self.client.transfers.get_downloads(username=username)

            if not downloads:
                return None

            # Downloads response is a dict with 'directories' containing transfer info
            for directory in downloads.get("directories", []):
                for transfer in directory.get("files", []):
                    if transfer.get("filename") == filename:
                        return DownloadStatus(
                            username=username,
                            filename=filename,
                            state=transfer.get("state", "Unknown"),
                            percent_complete=transfer.get("percentComplete", 0),
                            bytes_transferred=transfer.get("bytesTransferred", 0),
                            size=transfer.get("size", 0),
                            average_speed=transfer.get("averageSpeed", 0),
                        )

            return None

        except Exception:
            logger.exception(f"Failed to get download status for {filename}")
            return None

    async def wait_for_download(self, username: str, filename: str, timeout_secs: int = 600) -> DownloadStatus | None:
        """
        Wait for a download to complete, polling periodically.

        Args:
            username: Source username.
            filename: Remote filename.
            timeout_secs: Maximum wait time.

        Returns:
            Final DownloadStatus, or None on timeout.
        """
        start = time.time()

        while time.time() - start < timeout_secs:
            await asyncio.sleep(3)
            status = self.get_download_status(username, filename)

            if status is None:
                logger.debug(f"No status yet for {filename}")
                continue

            if status.is_complete:
                logger.info(f"Download complete: {filename}")
                return status

            if status.is_failed:
                logger.warning(f"Download failed ({status.state}): {filename}")
                return status

            logger.debug(f"Download {status.percent_complete:.0f}%: {filename}")

        logger.warning(f"Download timed out after {timeout_secs}s: {filename}")
        return None

    def get_downloads_directory(self) -> list[dict]:
        """Get the contents of the slskd downloads directory."""
        try:
            return self.client.files.get_downloads_dir()
        except Exception:
            logger.exception("Failed to list downloads directory")
            return []
