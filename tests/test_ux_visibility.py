"""Tests for the UX visibility pass: download progress, no-results escape
hatches, superseded-message marking, and command discoverability."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.bot.handlers import (
    MusicBot,
    PendingSearch,
    _register_commands,
)
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import DownloadStatus, SlskdClient


def _make_config():
    td = tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {12345}
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
    return config


def _make_bot():
    with (
        patch("music_downloader.bot.handlers.SpotifyResolver"),
        patch("music_downloader.bot.handlers.SlskdClient"),
    ):
        return MusicBot(_make_config())


def _make_track():
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="X",
        duration_ms=162_000,
        spotify_url="u",
        year="1966",
    )


def _make_update(user_id=12345, chat_id=67890, text="hello"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message = AsyncMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_message = update.message
    return update


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


# ---------------------------------------------------------------------------
# Download progress
# ---------------------------------------------------------------------------


class TestDownloadProgress:
    @pytest.mark.asyncio
    async def test_wait_for_download_reports_progress(self):
        """The in-flight status must reach the callback, not just a DEBUG log."""
        with patch("music_downloader.search.slskd_client.slskd_api.SlskdClient"):
            client = SlskdClient("http://x", "k")

        active = DownloadStatus(
            username="u", filename="f", state="InProgress", percent_complete=42.0, average_speed=2_000_000
        )
        complete = DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        client.get_download_status = MagicMock(side_effect=[active, complete])

        seen = []

        async def progress_cb(status):
            seen.append(status)

        with patch("music_downloader.search.slskd_client.asyncio.sleep", new_callable=AsyncMock):
            result = await client.wait_for_download(
                "u", "f", timeout_secs=60, progress_cb=progress_cb, progress_interval_secs=0
            )

        assert result is complete
        assert seen == [active]

    @pytest.mark.asyncio
    async def test_progress_reporter_dedupes_and_formats(self):
        msg = AsyncMock()
        reporter = MusicBot._make_progress_reporter(msg, "HEADER")

        queued = DownloadStatus(username="u", filename="f", state="Queued, Remotely")
        await reporter(queued)
        await reporter(queued)  # identical line → no second edit
        assert msg.edit_text.call_count == 1
        assert "Queued" in msg.edit_text.call_args[0][0]

        progressing = DownloadStatus(
            username="u", filename="f", state="InProgress", percent_complete=42.0, average_speed=2 * 1024 * 1024
        )
        await reporter(progressing)
        assert msg.edit_text.call_count == 2
        text = msg.edit_text.call_args[0][0]
        assert "42%" in text
        assert "2.0 MB/s" in text


# ---------------------------------------------------------------------------
# No-results escape hatch
# ---------------------------------------------------------------------------


class TestNoResultsEscapeHatch:
    @pytest.mark.asyncio
    async def test_spotify_path_no_results_offers_direct_search(self):
        bot = _make_bot()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[])
        track = _make_track()

        with patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock) as mock_edit:
            await bot._do_slskd_search(_make_context(), 67890, track, AsyncMock(), generation=0)

        assert 67890 in bot.pending, "query must be stored for the direct-search button"
        assert bot.pending[67890].track is None
        final_kwargs = mock_edit.call_args.kwargs
        assert final_kwargs.get("reply_markup") is not None, "no-results reply must carry the escape-hatch button"

    @pytest.mark.asyncio
    async def test_direct_path_no_results_offers_retry(self):
        bot = _make_bot()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[])

        with patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock) as mock_edit:
            await bot._do_direct_slskd_search(_make_context(), 67890, "some query", AsyncMock(), generation=0)

        assert 67890 in bot.pending
        assert mock_edit.call_args.kwargs.get("reply_markup") is not None


# ---------------------------------------------------------------------------
# Superseded messages
# ---------------------------------------------------------------------------


class TestSupersededMessages:
    @pytest.mark.asyncio
    async def test_new_text_marks_old_search_message_superseded(self):
        bot = _make_bot()
        bot.processor.find_similar = MagicMock(return_value=[])
        bot.pending[67890] = PendingSearch(query="old song", track=_make_track(), message_id=555)

        update = _make_update(chat_id=67890, text="new song")
        context = _make_context()
        with patch.object(bot, "_do_search", new_callable=AsyncMock):
            await bot.handle_text(update, context)

        context.bot.edit_message_text.assert_called_once()
        kwargs = context.bot.edit_message_text.call_args.kwargs
        assert kwargs["message_id"] == 555
        assert "Superseded" in kwargs["text"]


# ---------------------------------------------------------------------------
# Command discoverability
# ---------------------------------------------------------------------------


class TestCommandDiscoverability:
    @pytest.mark.asyncio
    async def test_command_menu_registered_with_import_and_cancel(self):
        app = MagicMock()
        app.bot = AsyncMock()
        await _register_commands(app)
        app.bot.set_my_commands.assert_awaited_once()
        names = {c.command for c in app.bot.set_my_commands.call_args[0][0]}
        assert {"import", "cancel", "auto", "status", "history", "help"} <= names

    @pytest.mark.asyncio
    async def test_help_lists_import_and_cancel(self):
        bot = _make_bot()
        update = _make_update()
        await bot.cmd_start(update, _make_context())
        text = update.message.reply_text.call_args[0][0]
        assert "/import" in text
        assert "/cancel" in text


# ---------------------------------------------------------------------------
# Import failure keyboards (no dead Save buttons)
# ---------------------------------------------------------------------------


class TestImportFailureKeyboards:
    @pytest.mark.asyncio
    async def test_enqueue_failure_offers_no_save_button(self):
        """With no file on disk, Save could only strip the keyboard and stall the job."""
        bot = _make_bot()
        bot.slskd.enqueue_download = MagicMock(return_value=False)
        bot.import_repo = MagicMock()

        with patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock) as mock_edit:
            await bot._do_import_download(
                _make_context(),
                67890,
                _make_track(),
                MagicMock(username="u", filename="f"),
                AsyncMock(),
                0,
                job_id=1,
                track_id=2,
                dl_id="abc",
            )

        markup = mock_edit.call_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert not any(c.startswith("ia:") for c in callbacks), "no Save button without a file"
        assert any(c.startswith("ir:") for c in callbacks)
        assert any(c.startswith("is:") for c in callbacks)

    @pytest.mark.asyncio
    async def test_not_ready_approve_keeps_buttons(self):
        """Tapping Save before the file lands must not strip the keyboard."""
        from music_downloader.bot.handlers import PendingDownload

        bot = _make_bot()
        bot.downloads["abc"] = PendingDownload(track=_make_track(), result=MagicMock(), chat_id=67890, source_path=None)
        query = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        await bot._handle_import_approve(update, _make_context(), 67890, job_id=1, track_id=2, dl_id="abc")

        assert "abc" in bot.downloads, "pending download must survive the early tap"
        assert query.edit_message_caption.call_args.kwargs.get("reply_markup") is not None
