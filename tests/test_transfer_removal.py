"""Once the bot deletes a downloaded file, the finished transfer is removed from slskd too."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

from music_downloader.bot.handlers import MusicBot, PendingDownload
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.pipeline import fetch as pipeline_fetch
from music_downloader.search.slskd_client import DownloadStatus, SearchResult, SlskdClient

CHAT = 67890
USER = 12345


def _make_config():
    td = tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_upload_limit_bytes = 50_000_000
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {USER}
    config.telegram_chat_delivery_users = set()
    config.auto_mode = False
    config.max_results = 5
    config.duration_tolerance_secs = 5
    config.exclude_keywords = ["live", "remix"]
    config.download_dir = os.path.join(td, "downloads")
    config.output_dir = os.path.join(td, "music")
    config.data_dir = os.path.join(td, "data")
    config.filename_template = "{artist} - {title}"
    config.search_timeout_secs = 30
    config.download_timeout_secs = 600
    config.download_cleanup_hours = 24
    os.makedirs(config.download_dir, exist_ok=True)
    return config


def _make_bot(config=None):
    config = config or _make_config()
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        bot = MusicBot(config)
    bot.pipeline.forget_transfer = MagicMock()
    bot.pipeline.embed_artwork = AsyncMock()
    return bot


def _track():
    return TrackInfo(
        artist="Nancy Sinatra", title="Bang Bang", album="X", duration_ms=162_000, spotify_url="u", year="1966"
    )


def _result():
    return SearchResult(username="peer", filename="\\Music\\track.flac", size=30_000_000, length=162)


def _source(bot, name="track.flac"):
    path = os.path.join(bot.config.download_dir, name)
    with open(path, "wb") as f:
        f.write(b"fLaC" + b"\0" * 64)
    return path


def _entry(bot, **kw):
    return PendingDownload(
        track=_track(), result=_result(), chat_id=CHAT, source_path=_source(bot), user_id=USER, transfer_id="t-42", **kw
    )


def _tap(data):
    update = MagicMock()
    update.effective_chat.id = CHAT
    query = update.callback_query
    query.data = data
    query.from_user.id = USER
    query.answer = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update


class TestSlskdClient:
    def _client(self):
        with patch("slskd_api.SlskdClient"):
            return SlskdClient("http://localhost:5030", "k")

    def test_remove_transfer_deletes_with_remove_true(self):
        client = self._client()
        client.client.transfers.cancel_download = MagicMock(return_value=True)
        assert client.remove_transfer("peer", "t-42") is True
        client.client.transfers.cancel_download.assert_called_once_with("peer", "t-42", remove=True)

    def test_remove_transfer_failure_is_logged_at_info_and_returns_false(self, caplog):
        client = self._client()
        client.client.transfers.cancel_download = MagicMock(side_effect=ConnectionError("down"))
        with caplog.at_level(logging.INFO):
            assert client.remove_transfer("peer", "t-42") is False
        record = next(r for r in caplog.records if "t-42" in r.getMessage())
        assert record.levelno == logging.INFO

    def test_download_status_carries_the_transfer_id(self):
        client = self._client()
        client.client.transfers.get_downloads = MagicMock(
            return_value={"directories": [{"files": [{"filename": "\\a.flac", "state": "Completed", "id": "t-42"}]}]}
        )
        assert client.get_download_status("peer", "\\a.flac").transfer_id == "t-42"


class TestPipeline:
    async def test_fetch_returns_the_transfer_id(self):
        slskd = MagicMock()
        slskd.enqueue_download = MagicMock(return_value=True)
        slskd.wait_for_download = AsyncMock(
            return_value=DownloadStatus(username="peer", filename="f", state="Completed", transfer_id="t-42")
        )
        processor = MagicMock()
        processor.find_downloaded_file = MagicMock(return_value="/downloads/track.mp3")
        outcome = await pipeline_fetch.fetch(slskd, processor, _result(), 60, AsyncMock(), None)
        assert outcome.transfer_id == "t-42"

    def test_forget_transfer_runs_remove_in_a_thread(self):
        with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
            bot = MusicBot(_make_config())
        bot.slskd.remove_transfer = MagicMock(return_value=True)
        thread = bot.pipeline.forget_transfer("peer", "t-42")
        thread.join(5)
        bot.slskd.remove_transfer.assert_called_once_with("peer", "t-42")
        assert bot.pipeline.forget_transfer("peer", "") is None


class TestFourPaths:
    async def test_library_save_on_approve(self):
        bot = _make_bot()
        bot.downloads["d1"] = _entry(bot)
        bot.processor.process_file = MagicMock(return_value="/music/x.flac")
        await bot.handle_callback(_tap("approve:d1"), MagicMock())
        bot.pipeline.forget_transfer.assert_called_once_with("peer", "t-42")

    async def test_chat_delivery(self):
        bot = _make_bot()
        pending = _entry(bot)
        bot.downloads["d1"] = pending
        bot._send_audio_file = AsyncMock(return_value=None)
        status = MagicMock(edit_text=AsyncMock())
        sent = await bot._deliver_download(MagicMock(), CHAT, "d1", pending, status, "q", "#1")
        assert sent
        bot.pipeline.forget_transfer.assert_called_once_with("peer", "t-42")

    async def test_reject(self):
        bot = _make_bot()
        bot.downloads["d1"] = _entry(bot)
        await bot.handle_callback(_tap("reject:d1"), MagicMock())
        bot.pipeline.forget_transfer.assert_called_once_with("peer", "t-42")

    def test_orphan_sweep_prune(self):
        bot = _make_bot()
        bot.downloads["d1"] = _entry(bot, created_at=time.time() - 25 * 3600)
        bot._prune_pending()
        bot.pipeline.forget_transfer.assert_called_once_with("peer", "t-42")

    async def test_import_auto_save(self):
        bot = _make_bot()
        bot.downloads["d1"] = _entry(bot, job_id=1, track_id=2)
        bot.processor.process_file = MagicMock(return_value="/music/x.flac")
        bot.import_repo = MagicMock()
        bot._process_next_import_track = AsyncMock()
        status = MagicMock(edit_text=AsyncMock())
        await bot._import_auto_save(
            MagicMock(), CHAT, 1, 2, "d1", _track(), _result(), bot.downloads["d1"].source_path, status, 0
        )
        await asyncio.sleep(0)
        bot.pipeline.forget_transfer.assert_called_once_with("peer", "t-42")
