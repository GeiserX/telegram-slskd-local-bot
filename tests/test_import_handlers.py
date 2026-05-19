"""Tests for import flow, direct search, cancel command, and _safe_query_edit."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest, NetworkError, TimedOut

from music_downloader.bot.handlers import (
    MusicBot,
    PendingDownload,
    PendingSearch,
    _safe_query_edit,
)
from music_downloader.metadata.playlist import PlaylistInfo
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.import_repo import (
    ImportJob,
    ImportTrack,
    JobStatus,
    TrackStatus,
)
from music_downloader.search.slskd_client import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


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
    return config


def _make_track():
    return TrackInfo(
        artist="Artist",
        title="Title",
        album="Album",
        duration_ms=162000,
        spotify_url="https://open.spotify.com/track/xxx",
        year="2024",
    )


def _make_result(idx=0):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\track{idx}.flac",
        size=30_000_000,
        bit_rate=900,
        bit_depth=16,
        sample_rate=44100,
        length=162,
        has_free_slot=True,
        upload_speed=1_000_000,
        queue_length=0,
    )


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    context.bot.send_audio = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(return_value=MagicMock())
    return context


def _make_update(chat_id=67890, user_id=12345, text="/import https://open.spotify.com/playlist/abc123"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.edit_message_caption = AsyncMock()
    return update


@patch("music_downloader.bot.handlers.SpotifyResolver")
@patch("music_downloader.bot.handlers.SlskdClient")
def _setup_bot(mock_slskd_cls, mock_spotify_cls):
    config = _make_config()
    mock_slskd_cls.return_value = MagicMock()
    mock_spotify_cls.return_value = MagicMock()
    bot = MusicBot(config)
    bot.slskd = mock_slskd_cls.return_value
    bot.slskd.search = AsyncMock(return_value=[])
    bot.slskd.enqueue_download = MagicMock(return_value=True)
    bot.slskd.wait_for_download = AsyncMock()
    bot.import_repo = MagicMock()
    bot.playlist_resolver = MagicMock()
    return bot


def _make_import_job(job_id=1, chat_id=67890, status="active"):
    return ImportJob(
        id=job_id,
        chat_id=chat_id,
        spotify_url="https://open.spotify.com/playlist/abc123",
        name="Test Playlist",
        total_tracks=10,
        completed_tracks=3,
        failed_tracks=1,
        skipped_tracks=0,
        status=status,
        created_at="2024-01-01",
        updated_at="2024-01-01",
    )


def _make_import_track(track_id=1, job_id=1, position=1):
    return ImportTrack(
        id=track_id,
        job_id=job_id,
        position=position,
        artist="Artist",
        title="Title",
        album="Album",
        duration_ms=162000,
        spotify_url="https://open.spotify.com/track/xxx",
        year="2024",
        status="pending",
        error_message="",
        created_at="2024-01-01",
        updated_at="2024-01-01",
    )


# ---------------------------------------------------------------------------
# _safe_query_edit
# ---------------------------------------------------------------------------


class TestSafeQueryEdit:
    async def test_safe_query_edit_success(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        result = await _safe_query_edit(query, "hello")
        assert result is True
        query.edit_message_text.assert_awaited_once_with("hello")

    async def test_safe_query_edit_bad_request(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=BadRequest("message not modified"))
        result = await _safe_query_edit(query, "hello")
        assert result is False

    async def test_safe_query_edit_timed_out(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=TimedOut())
        result = await _safe_query_edit(query, "hello")
        assert result is False

    async def test_safe_query_edit_network_error(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock(side_effect=NetworkError("connection reset"))
        result = await _safe_query_edit(query, "hello")
        assert result is False


# ---------------------------------------------------------------------------
# cmd_import
# ---------------------------------------------------------------------------


class TestCmdImport:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_cmd_import_no_args(self, mock_edit, mock_thread):
        bot = _setup_bot()
        update = _make_update(text="/import")
        context = _make_context()
        await bot.cmd_import(update, context)
        update.message.reply_text.assert_awaited_once()
        assert "Usage" in update.message.reply_text.call_args[0][0]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_cmd_import_invalid_url(self, mock_edit, mock_thread):
        bot = _setup_bot()
        update = _make_update(text="/import https://example.com/not-spotify")
        context = _make_context()
        await bot.cmd_import(update, context)
        update.message.reply_text.assert_awaited_once()
        assert "valid Spotify" in update.message.reply_text.call_args[0][0]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    @patch("music_downloader.bot.handlers.PlaylistResolver.is_spotify_url", return_value=True)
    async def test_cmd_import_active_job_exists(self, mock_is_url, mock_edit, mock_thread):
        bot = _setup_bot()
        bot.import_repo.get_active_job = MagicMock(return_value=_make_import_job())
        update = _make_update(text="/import https://open.spotify.com/playlist/abc123")
        context = _make_context()
        await bot.cmd_import(update, context)
        update.message.reply_text.assert_awaited_once()
        assert "already have an active import" in update.message.reply_text.call_args[0][0]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    @patch("music_downloader.bot.handlers.PlaylistResolver.is_spotify_url", return_value=True)
    async def test_cmd_import_resolve_fails(self, mock_is_url, mock_edit, mock_thread):
        bot = _setup_bot()
        bot.import_repo.get_active_job = MagicMock(return_value=None)
        bot.playlist_resolver.resolve = MagicMock(return_value=None)
        update = _make_update(text="/import https://open.spotify.com/playlist/abc123")
        context = _make_context()
        await bot.cmd_import(update, context)
        mock_edit.assert_awaited()
        assert "Failed to resolve" in mock_edit.call_args[0][1]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    @patch("music_downloader.bot.handlers.PlaylistResolver.is_spotify_url", return_value=True)
    @patch("music_downloader.bot.handlers.build_import_confirm_keyboard", return_value=None)
    async def test_cmd_import_success(self, mock_kb, mock_is_url, mock_edit, mock_thread):
        bot = _setup_bot()
        bot.import_repo.get_active_job = MagicMock(return_value=None)
        bot.import_repo.create_job = MagicMock(return_value=42)
        bot.import_repo.add_tracks = MagicMock()
        playlist_info = PlaylistInfo(
            name="My Playlist",
            owner="TestUser",
            total_tracks=3,
            spotify_url="https://open.spotify.com/playlist/abc123",
            tracks=[_make_track(), _make_track(), _make_track()],
            is_album=False,
        )
        bot.playlist_resolver.resolve = MagicMock(return_value=playlist_info)
        update = _make_update(text="/import https://open.spotify.com/playlist/abc123")
        context = _make_context()
        await bot.cmd_import(update, context)
        bot.import_repo.create_job.assert_called_once()
        bot.import_repo.add_tracks.assert_called_once()
        call_args = bot.import_repo.add_tracks.call_args[0]
        assert call_args[0] == 42
        assert len(call_args[1]) == 3
        mock_edit.assert_awaited()
        assert "My Playlist" in mock_edit.call_args[0][1]


# ---------------------------------------------------------------------------
# cmd_cancel
# ---------------------------------------------------------------------------


class TestCmdCancel:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    async def test_cmd_cancel_active_import(self, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot._active_import[chat_id] = 42
        bot.import_repo.update_job_status = MagicMock()
        update = _make_update(chat_id=chat_id, text="/cancel")
        context = _make_context()
        await bot.cmd_cancel(update, context)
        bot.import_repo.update_job_status.assert_called_once_with(42, JobStatus.cancelled)
        update.message.reply_text.assert_awaited_once()
        assert "Import cancelled" in update.message.reply_text.call_args[0][0]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    async def test_cmd_cancel_no_work(self, mock_thread):
        bot = _setup_bot()
        update = _make_update(chat_id=67890, text="/cancel")
        context = _make_context()
        await bot.cmd_cancel(update, context)
        update.message.reply_text.assert_awaited_once()
        assert "Nothing to cancel" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# _handle_import_callback
# ---------------------------------------------------------------------------


class TestHandleImportCallback:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_confirm_start(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        job = _make_import_job(job_id=1, chat_id=chat_id)
        bot.import_repo.get_job_for_chat = MagicMock(return_value=job)
        bot.import_repo.update_job_status = MagicMock()
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_callback(update, context, chat_id, "ic:1")
        bot.import_repo.update_job_status.assert_called_once_with(1, JobStatus.active)
        assert bot._active_import[chat_id] == 1
        context.application.create_task.assert_called_once()

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_cancel(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        job = _make_import_job(job_id=1, chat_id=chat_id)
        bot.import_repo.get_job_for_chat = MagicMock(return_value=job)
        bot.import_repo.update_job_status = MagicMock()
        bot._active_import[chat_id] = 1
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_callback(update, context, chat_id, "ix:1")
        bot.import_repo.update_job_status.assert_called_once_with(1, JobStatus.cancelled)
        assert chat_id not in bot._active_import
        mock_qedit.assert_awaited()
        assert "cancelled" in mock_qedit.call_args[0][1]

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_reject_track(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        job = _make_import_job(job_id=1, chat_id=chat_id)
        bot.import_repo.get_job_for_chat = MagicMock(return_value=job)
        bot.import_repo.complete_track = MagicMock()
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(5, 1, 0, 10))
        bot.import_repo.update_job_status = MagicMock()
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_callback(update, context, chat_id, "ir:1:5")
        bot.import_repo.complete_track.assert_called_once_with(1, 5, TrackStatus.failed, "Rejected by user")

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_skip_track(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        job = _make_import_job(job_id=1, chat_id=chat_id)
        bot.import_repo.get_job_for_chat = MagicMock(return_value=job)
        bot.import_repo.complete_track = MagicMock()
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(5, 1, 1, 10))
        bot.import_repo.update_job_status = MagicMock()
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_callback(update, context, chat_id, "is:1:5")
        bot.import_repo.complete_track.assert_called_once_with(1, 5, TrackStatus.skipped)

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_callback_wrong_chat(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.import_repo.get_job_for_chat = MagicMock(return_value=None)
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_callback(update, context, chat_id, "ic:1")
        mock_qedit.assert_awaited_once()
        assert "not found" in mock_qedit.call_args[0][1]


# ---------------------------------------------------------------------------
# _handle_import_approve
# ---------------------------------------------------------------------------


class TestHandleImportApprove:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_approve_expired_download(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        update = _make_update(chat_id=chat_id)
        # No download in bot.downloads
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(10, 0, 0, 10))
        bot.import_repo.update_job_status = MagicMock()
        await bot._handle_import_approve(update, _make_context(), chat_id, 1, 5, "nonexistent")

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_query_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_approve_source_not_ready(self, mock_qedit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        track = _make_track()
        result = _make_result()
        dl_id = "dl_1"
        bot.downloads[dl_id] = PendingDownload(track=track, result=result, chat_id=chat_id, source_path=None)
        update = _make_update(chat_id=chat_id)
        await bot._handle_import_approve(update, _make_context(), chat_id, 1, 5, dl_id)
        # Download should be put back
        assert dl_id in bot.downloads

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    async def test_import_approve_success(self, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        track = _make_track()
        result = _make_result()
        dl_id = "dl_2"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as source:
            source.write(b"fake flac data")
        bot.downloads[dl_id] = PendingDownload(track=track, result=result, chat_id=chat_id, source_path=source.name)
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value="/tmp/output.flac")
        bot.import_repo.complete_track = MagicMock()
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(10, 0, 0, 10))
        bot.import_repo.update_job_status = MagicMock()
        bot._embed_spotify_artwork = AsyncMock()
        bot._add_history = AsyncMock()
        bot._edit_approval_message = AsyncMock()
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_import_approve(update, context, chat_id, 1, 5, dl_id)
        bot.processor.process_file.assert_called_once_with(source.name, "Artist", "Title")
        bot.import_repo.complete_track.assert_called_once_with(1, 5, TrackStatus.completed)
        os.unlink(source.name)


# ---------------------------------------------------------------------------
# _process_next_import_track
# ---------------------------------------------------------------------------


class TestProcessNextImportTrack:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    async def test_process_next_no_more_tracks(self, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot._active_import[chat_id] = 1
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(7, 2, 1, 10))
        bot.import_repo.update_job_status = MagicMock()
        context = _make_context()
        await bot._process_next_import_track(context, chat_id, 1, generation=0)
        bot.import_repo.update_job_status.assert_called_once_with(1, JobStatus.completed)
        assert chat_id not in bot._active_import
        context.bot.send_message.assert_awaited_once()
        msg_text = context.bot.send_message.call_args[1]["text"]
        assert "Import complete" in msg_text
        assert "7" in msg_text  # completed
        assert "2" in msg_text  # failed

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_process_next_has_track(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        next_track = _make_import_track(track_id=3, job_id=1, position=4)
        bot.import_repo.get_next_pending_track = MagicMock(return_value=next_track)
        bot.import_repo.get_job_progress = MagicMock(return_value=(3, 0, 0, 10))
        bot.import_repo.update_track_status = MagicMock()
        bot.slskd.search = AsyncMock(return_value=[])
        context = _make_context()
        await bot._process_next_import_track(context, chat_id, 1, generation=0)
        bot.import_repo.update_track_status.assert_called()
        context.bot.send_message.assert_awaited()


# ---------------------------------------------------------------------------
# _do_import_slskd_search
# ---------------------------------------------------------------------------


class TestDoImportSlskdSearch:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_search_no_results(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.search = AsyncMock(return_value=[])
        bot.import_repo.update_track_status = MagicMock()
        track = _make_track()
        searching_msg = MagicMock(message_id=100)
        await bot._do_import_slskd_search(
            _make_context(), chat_id, track, searching_msg, generation=0, job_id=1, track_id=5
        )
        mock_edit.assert_awaited()
        assert "No results" in mock_edit.call_args[0][1]
        bot.import_repo.update_track_status.assert_called_with(5, TrackStatus.awaiting_approval)

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_search_success(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        results = [_make_result(0)]
        # Return results from first search call
        bot.slskd.search = AsyncMock(
            return_value=[
                {"username": "user0", "files": [{"filename": "\\Music\\track0.flac", "size": 30000000, "length": 162}]}
            ]
        )
        bot._rank_responses = MagicMock(return_value=(results, False))
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock()
        track = _make_track()
        searching_msg = MagicMock(message_id=100)
        context = _make_context()
        await bot._do_import_slskd_search(context, chat_id, track, searching_msg, generation=0, job_id=1, track_id=5)
        # Should store in _import_pending and start download task
        assert chat_id in bot._import_pending
        context.application.create_task.assert_called_once()

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_search_exception(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.search = AsyncMock(side_effect=RuntimeError("network failure"))
        bot.import_repo.complete_track = MagicMock()
        bot.import_repo.get_next_pending_track = MagicMock(return_value=None)
        bot.import_repo.get_job_progress = MagicMock(return_value=(5, 1, 0, 10))
        bot.import_repo.update_job_status = MagicMock()
        track = _make_track()
        searching_msg = MagicMock(message_id=100)
        context = _make_context()
        await bot._do_import_slskd_search(context, chat_id, track, searching_msg, generation=0, job_id=1, track_id=5)
        bot.import_repo.complete_track.assert_called_once_with(1, 5, TrackStatus.failed, "Search error")


# ---------------------------------------------------------------------------
# _do_import_download
# ---------------------------------------------------------------------------


class TestDoImportDownload:
    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_download_enqueue_fails(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.enqueue_download = MagicMock(return_value=False)
        bot.import_repo.update_track_status = MagicMock()
        result = _make_result()
        status_msg = MagicMock(message_id=100)
        dl_id = "dl_1"
        bot.downloads[dl_id] = PendingDownload(track=_make_track(), result=result, chat_id=chat_id)
        context = _make_context()
        await bot._do_import_download(
            context,
            chat_id,
            _make_track(),
            result,
            status_msg,
            generation=0,
            job_id=1,
            track_id=5,
            dl_id=dl_id,
        )
        mock_edit.assert_awaited()
        assert "Failed to enqueue" in mock_edit.call_args[0][1]
        bot.import_repo.update_track_status.assert_called_with(5, TrackStatus.awaiting_approval)

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_download_timeout(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock(return_value=None)
        bot.import_repo.update_track_status = MagicMock()
        result = _make_result()
        status_msg = MagicMock(message_id=100)
        dl_id = "dl_2"
        bot.downloads[dl_id] = PendingDownload(track=_make_track(), result=result, chat_id=chat_id)
        context = _make_context()
        await bot._do_import_download(
            context,
            chat_id,
            _make_track(),
            result,
            status_msg,
            generation=0,
            job_id=1,
            track_id=5,
            dl_id=dl_id,
        )
        mock_edit.assert_awaited()
        assert "Download failed" in mock_edit.call_args[0][1]
        bot.import_repo.update_track_status.assert_called_with(5, TrackStatus.awaiting_approval)

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_download_success_large_file(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        status = MagicMock()
        status.is_failed = False
        bot.slskd.wait_for_download = AsyncMock(return_value=status)
        # Create a file larger than TELEGRAM_FILE_LIMIT
        with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as source:
            source.write(b"x" * 100)
        bot.processor = MagicMock()
        bot.processor.find_downloaded_file = MagicMock(return_value=source.name)
        bot.import_repo.update_track_status = MagicMock()
        result = _make_result()
        status_msg = MagicMock(message_id=100)
        dl_id = "dl_3"
        bot.downloads[dl_id] = PendingDownload(track=_make_track(), result=result, chat_id=chat_id)
        context = _make_context()
        # Patch TELEGRAM_FILE_LIMIT to be smaller than our file
        with patch("music_downloader.bot.handlers.TELEGRAM_FILE_LIMIT", 50):
            await bot._do_import_download(
                context,
                chat_id,
                _make_track(),
                result,
                status_msg,
                generation=0,
                job_id=1,
                track_id=5,
                dl_id=dl_id,
            )
        mock_edit.assert_awaited()
        assert "too large" in mock_edit.call_args[0][1]
        os.unlink(source.name)

    @patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=_fake_to_thread)
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_import_download_success_sends_audio(self, mock_edit, mock_thread):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        status = MagicMock()
        status.is_failed = False
        bot.slskd.wait_for_download = AsyncMock(return_value=status)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as source:
            source.write(b"fake audio")
        bot.processor = MagicMock()
        bot.processor.find_downloaded_file = MagicMock(return_value=source.name)
        bot.processor.build_filename = MagicMock(return_value="Artist - Title.flac")
        bot.import_repo.update_track_status = MagicMock()
        result = _make_result()
        status_msg = MagicMock(message_id=100)
        dl_id = "dl_4"
        bot.downloads[dl_id] = PendingDownload(track=_make_track(), result=result, chat_id=chat_id)
        context = _make_context()
        await bot._do_import_download(
            context,
            chat_id,
            _make_track(),
            result,
            status_msg,
            generation=0,
            job_id=1,
            track_id=5,
            dl_id=dl_id,
        )
        context.bot.send_audio.assert_awaited_once()
        bot.import_repo.update_track_status.assert_called_with(5, TrackStatus.awaiting_approval)
        os.unlink(source.name)


# ---------------------------------------------------------------------------
# _handle_direct_search
# ---------------------------------------------------------------------------


class TestHandleDirectSearch:
    async def test_direct_search_no_pending(self):
        bot = _setup_bot()
        chat_id = 67890
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_direct_search(update, context, chat_id, "direct_search")
        update.callback_query.edit_message_text.assert_awaited_once()
        assert "expired" in update.callback_query.edit_message_text.call_args[0][0]

    async def test_direct_search_success(self):
        bot = _setup_bot()
        chat_id = 67890
        bot.pending[chat_id] = PendingSearch(query="test query", track=_make_track())
        bot._do_direct_slskd_search = AsyncMock()
        update = _make_update(chat_id=chat_id)
        context = _make_context()
        await bot._handle_direct_search(update, context, chat_id, "direct_search")
        bot._do_direct_slskd_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# _do_direct_slskd_search
# ---------------------------------------------------------------------------


class TestDoDirectSlskdSearch:
    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_direct_search_no_results(self, mock_edit):
        bot = _setup_bot()
        chat_id = 67890
        bot.slskd.search = AsyncMock(return_value=[])
        bot._rank_responses = MagicMock(return_value=([], False))
        searching_msg = MagicMock(message_id=100)
        await bot._do_direct_slskd_search(_make_context(), chat_id, "test query", searching_msg, generation=0)
        mock_edit.assert_awaited()
        assert "No results" in mock_edit.call_args[0][1]

    @patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock, return_value=True)
    async def test_direct_search_finds_results(self, mock_edit):
        bot = _setup_bot()
        chat_id = 67890
        results = [_make_result(0), _make_result(1)]
        bot.slskd.search = AsyncMock(return_value=[{"username": "u", "files": []}])
        bot._rank_responses = MagicMock(return_value=(results, False))
        bot._format_results = MagicMock(return_value="Results text")
        searching_msg = MagicMock(message_id=100)
        await bot._do_direct_slskd_search(_make_context(), chat_id, "test query", searching_msg, generation=0)
        assert chat_id in bot.pending
        assert bot.pending[chat_id].results == results
        mock_edit.assert_awaited()
