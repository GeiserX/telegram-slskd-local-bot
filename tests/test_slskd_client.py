"""Tests for slskd API client."""

from unittest.mock import MagicMock, patch

import pytest

from music_downloader.search.slskd_client import (
    ActiveDownload,
    DownloadStatus,
    SearchResult,
    SlskdClient,
)


class TestSearchResult:
    """Test SearchResult dataclass properties."""

    def test_basename_with_backslash(self):
        r = SearchResult(username="u", filename="\\Music\\Artist\\Song.flac", size=100)
        assert r.basename == "Song.flac"

    def test_basename_no_backslash(self):
        r = SearchResult(username="u", filename="Song.flac", size=100)
        assert r.basename == "Song.flac"

    def test_extension(self):
        r = SearchResult(username="u", filename="\\Music\\Song.flac", size=100)
        assert r.extension == "flac"

    def test_extension_mp3(self):
        r = SearchResult(username="u", filename="\\Music\\Song.MP3", size=100)
        assert r.extension == "mp3"

    def test_extension_no_dot(self):
        r = SearchResult(username="u", filename="noextension", size=100)
        assert r.extension == ""

    def test_duration_display_valid(self):
        r = SearchResult(username="u", filename="f.flac", size=100, length=185)
        assert r.duration_display == "3:05"

    def test_duration_display_none(self):
        r = SearchResult(username="u", filename="f.flac", size=100, length=None)
        assert r.duration_display == "??:??"

    def test_duration_display_zero(self):
        r = SearchResult(username="u", filename="f.flac", size=100, length=0)
        assert r.duration_display == "??:??"

    def test_size_mb(self):
        r = SearchResult(username="u", filename="f.flac", size=52_428_800)
        assert r.size_mb == 50.0

    def test_quality_display_full(self):
        r = SearchResult(
            username="u",
            filename="f.flac",
            size=100,
            bit_depth=24,
            sample_rate=96000,
            bit_rate=2000,
        )
        assert "24bit" in r.quality_display
        assert "96.0kHz" in r.quality_display
        assert "2000kbps" in r.quality_display

    def test_quality_display_bitrate_only(self):
        r = SearchResult(
            username="u",
            filename="f.flac",
            size=100,
            bit_rate=320,
        )
        assert r.quality_display == "320kbps"

    def test_quality_display_no_info(self):
        r = SearchResult(username="u", filename="f.flac", size=100)
        assert r.quality_display == "FLAC"

    def test_str(self):
        r = SearchResult(
            username="u",
            filename="\\Music\\Song.flac",
            size=30_000_000,
            length=180,
            bit_depth=16,
            sample_rate=44100,
        )
        s = str(r)
        assert "Song.flac" in s
        assert "3:00" in s

    def test_quality_display_bit_depth_sample_rate_only(self):
        r = SearchResult(
            username="u",
            filename="f.flac",
            size=100,
            bit_depth=16,
            sample_rate=44100,
        )
        assert "16bit/44.1kHz" in r.quality_display
        assert "kbps" not in r.quality_display


class TestDownloadStatus:
    """Test DownloadStatus dataclass properties."""

    def test_is_complete_completed(self):
        s = DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        assert s.is_complete is True

    def test_is_complete_succeeded(self):
        s = DownloadStatus(username="u", filename="f", state="Succeeded")
        assert s.is_complete is True

    def test_is_complete_in_progress(self):
        s = DownloadStatus(username="u", filename="f", state="InProgress")
        assert s.is_complete is False

    def test_is_failed_errored(self):
        s = DownloadStatus(username="u", filename="f", state="Errored")
        assert s.is_failed is True

    def test_is_failed_rejected(self):
        s = DownloadStatus(username="u", filename="f", state="Rejected")
        assert s.is_failed is True

    def test_is_failed_timedout(self):
        s = DownloadStatus(username="u", filename="f", state="TimedOut")
        assert s.is_failed is True

    def test_is_failed_cancelled(self):
        s = DownloadStatus(username="u", filename="f", state="Cancelled")
        assert s.is_failed is True

    def test_is_failed_normal(self):
        s = DownloadStatus(username="u", filename="f", state="InProgress")
        assert s.is_failed is False

    def test_is_active_in_progress(self):
        s = DownloadStatus(username="u", filename="f", state="InProgress")
        assert s.is_active is True

    def test_is_active_complete(self):
        s = DownloadStatus(username="u", filename="f", state="Completed")
        assert s.is_active is False

    def test_is_active_failed(self):
        s = DownloadStatus(username="u", filename="f", state="Errored")
        assert s.is_active is False


class TestActiveDownload:
    """Test ActiveDownload dataclass."""

    def test_defaults(self):
        sr = SearchResult(username="u", filename="f.flac", size=100)
        ad = ActiveDownload(search_result=sr, track_filename="Artist - Title.flac")
        assert ad.status is None
        assert ad.local_path is None


class TestSlskdClientParseResults:
    """Test SlskdClient.parse_results."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient"):
            return SlskdClient("http://localhost:5030", "test-key")

    def test_parse_flac_only(self, client):
        responses = [
            {
                "username": "user1",
                "hasFreeUploadSlot": True,
                "uploadSpeed": 5_000_000,
                "queueLength": 0,
                "files": [
                    {
                        "filename": "\\Music\\Song.flac",
                        "size": 30_000_000,
                        "length": 180,
                        "bitDepth": 16,
                        "sampleRate": 44100,
                    },
                    {"filename": "\\Music\\Song.mp3", "size": 10_000_000, "length": 180, "bitRate": 320},
                ],
            }
        ]
        results = client.parse_results(responses, flac_only=True)
        assert len(results) == 1
        assert results[0].extension == "flac"

    def test_parse_all_audio(self, client):
        responses = [
            {
                "username": "user1",
                "hasFreeUploadSlot": False,
                "uploadSpeed": 1_000_000,
                "queueLength": 2,
                "files": [
                    {"filename": "\\Music\\Song.flac", "size": 30_000_000},
                    {"filename": "\\Music\\Song.mp3", "size": 10_000_000},
                    {"filename": "\\Music\\Song.ogg", "size": 8_000_000},
                    {"filename": "\\Music\\cover.jpg", "size": 500_000},
                ],
            }
        ]
        results = client.parse_results(responses, flac_only=False)
        assert len(results) == 3
        exts = {r.extension for r in results}
        assert "jpg" not in exts

    def test_parse_empty_responses(self, client):
        assert client.parse_results([], flac_only=True) == []

    def test_parse_preserves_user_info(self, client):
        responses = [
            {
                "username": "cooluser",
                "hasFreeUploadSlot": True,
                "uploadSpeed": 9_000_000,
                "queueLength": 3,
                "files": [
                    {"filename": "\\Song.flac", "size": 100},
                ],
            }
        ]
        results = client.parse_results(responses, flac_only=True)
        assert results[0].username == "cooluser"
        assert results[0].has_free_slot is True
        assert results[0].upload_speed == 9_000_000
        assert results[0].queue_length == 3

    def test_parse_missing_fields(self, client):
        responses = [
            {
                "username": "u",
                "files": [{"filename": "\\Song.flac", "size": 0}],
            }
        ]
        results = client.parse_results(responses, flac_only=True)
        assert len(results) == 1
        assert results[0].has_free_slot is False
        assert results[0].upload_speed == 0


class TestSlskdClientEnqueue:
    """Test SlskdClient.enqueue_download."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    def test_enqueue_success(self, client):
        result = SearchResult(
            username="user1",
            filename="\\Music\\Song.flac",
            size=30_000_000,
        )
        client.client.transfers.enqueue = MagicMock()
        assert client.enqueue_download(result) is True
        client.client.transfers.enqueue.assert_called_once()

    def test_enqueue_failure(self, client):
        result = SearchResult(
            username="user1",
            filename="\\Music\\Song.flac",
            size=30_000_000,
        )
        client.client.transfers.enqueue = MagicMock(side_effect=Exception("Connection error"))
        assert client.enqueue_download(result) is False


class TestSlskdClientGetDownloadStatus:
    """Test SlskdClient.get_download_status."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    def test_status_found(self, client):
        client.client.transfers.get_downloads = MagicMock(
            return_value={
                "directories": [
                    {
                        "files": [
                            {
                                "filename": "\\Music\\Song.flac",
                                "state": "Completed, Succeeded",
                                "percentComplete": 100,
                                "bytesTransferred": 30_000_000,
                                "size": 30_000_000,
                                "averageSpeed": 5_000_000,
                            }
                        ]
                    }
                ]
            }
        )
        status = client.get_download_status("user1", "\\Music\\Song.flac")
        assert status is not None
        assert status.is_complete is True
        assert status.percent_complete == 100

    def test_status_not_found(self, client):
        client.client.transfers.get_downloads = MagicMock(return_value={"directories": [{"files": []}]})
        status = client.get_download_status("user1", "\\Music\\Song.flac")
        assert status is None

    def test_status_no_downloads(self, client):
        client.client.transfers.get_downloads = MagicMock(return_value=None)
        status = client.get_download_status("user1", "\\Music\\Song.flac")
        assert status is None

    def test_status_exception(self, client):
        client.client.transfers.get_downloads = MagicMock(side_effect=Exception("err"))
        status = client.get_download_status("user1", "\\Music\\Song.flac")
        assert status is None


class TestSlskdClientSearch:
    """Test SlskdClient.search async method."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    @pytest.mark.asyncio
    async def test_search_exception_returns_empty(self, client):
        """search() should return empty list on exception."""
        client.client.searches.get_all = MagicMock(side_effect=Exception("fail"))
        client.client.searches.search_text = MagicMock(side_effect=Exception("fail"))
        results = await client.search("test query", timeout_secs=2)
        assert results == []


class TestSlskdClientGetDownloadsDirectory:
    """Test SlskdClient.get_downloads_directory."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    def test_returns_list(self, client):
        client.client.files.get_downloads_dir = MagicMock(return_value=[{"name": "file.flac"}])
        result = client.get_downloads_directory()
        assert result == [{"name": "file.flac"}]

    def test_exception_returns_empty(self, client):
        client.client.files.get_downloads_dir = MagicMock(side_effect=Exception("err"))
        result = client.get_downloads_directory()
        assert result == []


class TestSlskdClientWaitForDownload:
    """Test SlskdClient.wait_for_download async method."""

    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    @pytest.mark.asyncio
    async def test_wait_completes(self, client):
        """wait_for_download returns completed status."""
        completed = DownloadStatus(
            username="u",
            filename="f.flac",
            state="Completed, Succeeded",
            percent_complete=100,
            bytes_transferred=100,
            size=100,
        )
        call_count = 0

        def mock_get_status(username, filename):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return completed
            return DownloadStatus(username="u", filename="f.flac", state="InProgress", percent_complete=50)

        client.get_download_status = mock_get_status
        result = await client.wait_for_download("u", "f.flac", timeout_secs=10)
        assert result is not None
        assert result.is_complete

    @pytest.mark.asyncio
    async def test_wait_fails(self, client):
        """wait_for_download returns failed status."""
        failed = DownloadStatus(username="u", filename="f.flac", state="Errored")
        client.get_download_status = MagicMock(return_value=failed)
        result = await client.wait_for_download("u", "f.flac", timeout_secs=10)
        assert result is not None
        assert result.is_failed

    @pytest.mark.asyncio
    async def test_wait_timeout(self, client):
        """wait_for_download returns None on timeout."""
        in_progress = DownloadStatus(username="u", filename="f.flac", state="InProgress")
        client.get_download_status = MagicMock(return_value=in_progress)
        result = await client.wait_for_download("u", "f.flac", timeout_secs=1)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_no_status_yet(self, client):
        """wait_for_download handles None status during polling."""
        call_count = 0
        completed = DownloadStatus(username="u", filename="f.flac", state="Completed")

        def mock_status(username, filename):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return None
            return completed

        client.get_download_status = mock_status
        result = await client.wait_for_download("u", "f.flac", timeout_secs=15)
        assert result is not None
        assert result.is_complete
