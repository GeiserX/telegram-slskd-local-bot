"""Regression tests for the reliability hardening pass.

Covers: database self-delete on transient errors, stale import-job recovery,
/cancel DB fallback, /status crash and cross-chat leak, guarded downloads
cleanup, stale results keyboards, flood-control retry, edited-message
filtering, and the slskd HTTP timeout.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import Chat, Message, MessageEntity, Update
from telegram.error import RetryAfter
from telegram.ext import CommandHandler, filters

from music_downloader.bot.handlers import (
    MusicBot,
    PendingDownload,
    PendingSearch,
    _safe_edit,
)
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.import_repo import ImportRepository, JobStatus
from music_downloader.search.slskd_client import SearchResult, SlskdClient

# ---------------------------------------------------------------------------
# Shared helpers (mirroring test_handlers_comprehensive style)
# ---------------------------------------------------------------------------


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
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="How Does That Grab You?",
        duration_ms=162_000,
        spotify_url="https://open.spotify.com/track/xxx",
        year="1966",
    )


def _make_search_result(idx=0):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {idx}.flac",
        size=30_000_000,
        bit_rate=900,
        bit_depth=16,
        sample_rate=44100,
        length=162,
        has_free_slot=True,
        upload_speed=1_000_000,
        queue_length=0,
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


def _make_callback_update(user_id=12345, chat_id=67890, data="dl:0"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.callback_query = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    return update


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


# ---------------------------------------------------------------------------
# Database: never destroy data over a transient error
# ---------------------------------------------------------------------------


class TestDatabaseSelfDeleteGuard:
    def test_transient_operational_error_propagates_and_preserves_file(self, tmp_path):
        """Disk-full/readonly must NOT be treated as corruption: no delete, error raised."""
        db_path = str(tmp_path / "importer.db")
        Database(db_path).close()  # create a real, healthy DB with content
        size_before = os.path.getsize(db_path)

        with (
            patch.object(
                Database,
                "_connect",
                side_effect=sqlite3.OperationalError("database or disk is full"),
            ),
            pytest.raises(sqlite3.OperationalError),
        ):
            Database(db_path)

        # The healthy file is untouched — not deleted, not renamed aside.
        assert os.path.exists(db_path)
        assert os.path.getsize(db_path) == size_before
        assert not list(tmp_path.glob("*.corrupt-*"))

    def test_corruption_moves_file_aside_instead_of_deleting(self, tmp_path):
        """Positive control: real corruption still recreates, but preserves the old file."""
        db_path = str(tmp_path / "importer.db")
        with open(db_path, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 200)

        db = Database(db_path)
        cursor = db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        assert {row[0] for row in cursor.fetchall()} >= {"download_history", "import_jobs"}
        db.close()

        corrupt_copies = list(tmp_path.glob("importer.db.corrupt-*"))
        assert len(corrupt_copies) == 1, "corrupt DB should be moved aside, not deleted"


# ---------------------------------------------------------------------------
# Import job lifecycle: no permanent bricks
# ---------------------------------------------------------------------------


class TestImportJobRecovery:
    def _repo(self, tmp_path):
        db = Database(str(tmp_path / "importer.db"))
        return ImportRepository(db)

    def test_cancel_stale_jobs_clears_pending_and_active(self, tmp_path):
        repo = self._repo(tmp_path)
        repo.create_job(chat_id=1, spotify_url="u", name="a", total_tracks=3)  # stays 'pending'
        job2 = repo.create_job(chat_id=2, spotify_url="u", name="b", total_tracks=3)
        repo.update_job_status(job2, JobStatus.active)

        assert repo.cancel_stale_jobs() == 2
        assert repo.get_active_job(1) is None
        assert repo.get_active_job(2) is None

    @pytest.mark.asyncio
    async def test_cmd_cancel_falls_back_to_db_job(self, tmp_path):
        """An abandoned confirm screen leaves a pending row with no in-memory state;
        /cancel must still clear it instead of reporting 'Nothing to cancel.'"""
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
        ):
            bot = MusicBot(_make_config())
        job_id = bot.import_repo.create_job(chat_id=67890, spotify_url="u", name="stuck", total_tracks=5)
        assert bot.import_repo.get_active_job(67890) is not None
        assert 67890 not in bot._active_import  # the brick precondition

        update = _make_update(chat_id=67890, text="/cancel")
        await bot.cmd_cancel(update, _make_context())

        assert bot.import_repo.get_active_job(67890) is None, f"job {job_id} must be cancelled"
        assert "Import cancelled" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# /status: no crash mid-search, no cross-chat leak
# ---------------------------------------------------------------------------


class TestStatusCommand:
    def _bot(self):
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
        ):
            return MusicBot(_make_config())

    @pytest.mark.asyncio
    async def test_status_with_unresolved_search_does_not_crash(self):
        """PendingSearch(track=None) is a routine state (Spotify disambiguation)."""
        bot = self._bot()
        bot.pending[67890] = PendingSearch(query="some song", track=None)

        update = _make_update(chat_id=67890)
        await bot.cmd_status(update, _make_context())

        update.message.reply_text.assert_called_once()
        assert "some song" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_status_only_shows_own_chat(self):
        bot = self._bot()
        bot.pending[111] = PendingSearch(query="other users song", track=_make_track())
        bot.downloads["aa"] = PendingDownload(track=_make_track(), result=_make_search_result(), chat_id=111)

        update = _make_update(chat_id=67890)
        await bot.cmd_status(update, _make_context())

        text = update.message.reply_text.call_args[0][0]
        assert text == "No active searches or downloads."


# ---------------------------------------------------------------------------
# Downloads cleanup: a read-only mount must not break reject/dismiss
# ---------------------------------------------------------------------------


class TestGuardedDownloadCleanup:
    @pytest.mark.asyncio
    async def test_reject_survives_undeletable_file(self, tmp_path):
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
        ):
            bot = MusicBot(_make_config())

        source = tmp_path / "track.flac"
        source.write_bytes(b"flac")
        dl_id = "deadbeef"
        bot.downloads[dl_id] = PendingDownload(
            track=_make_track(),
            result=_make_search_result(),
            chat_id=67890,
            source_path=str(source),
        )

        update = _make_callback_update(chat_id=67890, data=f"reject:{dl_id}")
        with patch("music_downloader.bot.handlers.os.remove", side_effect=PermissionError("read-only")):
            await bot.handle_callback(update, _make_context())

        # The flow completed: entry removed, message edited, history recorded.
        assert dl_id not in bot.downloads
        update.callback_query.edit_message_caption.assert_called()
        records = bot.history_repo.get_recent(5)
        assert records and records[0].status == "rejected"


# ---------------------------------------------------------------------------
# Stale results keyboards
# ---------------------------------------------------------------------------


class TestStaleResultsKeyboard:
    def _bot_with_search(self, search_id="newsrch1"):
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
        ):
            bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(
            query="q",
            track=_make_track(),
            results=[_make_search_result(i) for i in range(3)],
            search_id=search_id,
        )
        return bot

    @pytest.mark.asyncio
    async def test_button_from_older_search_is_refused(self):
        bot = self._bot_with_search()
        update = _make_callback_update(chat_id=67890, data="dl:oldsrch9:1")
        context = _make_context()

        await bot.handle_callback(update, context)

        context.application.create_task.assert_not_called()
        assert "out of date" in update.callback_query.edit_message_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_legacy_button_without_search_id_is_refused(self):
        bot = self._bot_with_search()
        update = _make_callback_update(chat_id=67890, data="dl:1")
        context = _make_context()

        await bot.handle_callback(update, context)

        context.application.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_search_id_downloads(self):
        """Positive control: the guard must not refuse the current keyboard."""
        bot = self._bot_with_search(search_id="cursrch1")
        update = _make_callback_update(chat_id=67890, data="dl:cursrch1:1")
        context = _make_context()

        with patch.object(bot, "_do_download", new_callable=AsyncMock) as mock_dl:
            await bot.handle_callback(update, context)
            await asyncio.sleep(0)  # let the created task run
            mock_dl.assert_called_once()
            assert mock_dl.call_args[0][6] == "cursrch1"


# ---------------------------------------------------------------------------
# Telegram flood control
# ---------------------------------------------------------------------------


class TestFloodControl:
    @pytest.mark.asyncio
    async def test_safe_edit_waits_out_retry_after(self):
        msg = AsyncMock()
        msg.edit_text = AsyncMock(side_effect=[RetryAfter(0), None])
        with patch("music_downloader.bot.handlers.asyncio.sleep", new_callable=AsyncMock):
            assert await _safe_edit(msg, "text") is True
        assert msg.edit_text.call_count == 2

    @pytest.mark.asyncio
    async def test_safe_edit_gives_up_after_second_flood(self):
        msg = AsyncMock()
        msg.edit_text = AsyncMock(side_effect=[RetryAfter(0), RetryAfter(0)])
        with patch("music_downloader.bot.handlers.asyncio.sleep", new_callable=AsyncMock):
            assert await _safe_edit(msg, "text") is False


# ---------------------------------------------------------------------------
# Edited messages must not reach the handlers
# ---------------------------------------------------------------------------


class TestEditedMessageFiltering:
    def _message(self, text, entities=()):
        message = Message(
            message_id=1,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=1, type="private"),
            text=text,
            entities=list(entities),
        )
        bot = MagicMock()
        bot.username = "testbot"
        message.set_bot(bot)  # CommandHandler.check_update needs it to strip /cmd@botname
        return message

    def test_text_filter_rejects_edited_message(self):
        flt = filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE
        edited = Update(update_id=1, edited_message=self._message("fixed typo"))
        fresh = Update(update_id=2, message=self._message("new song"))
        assert not flt.check_update(edited)
        assert flt.check_update(fresh)  # positive control

    def test_command_handler_rejects_edited_command(self):
        handler = CommandHandler("status", AsyncMock(), filters=filters.UpdateType.MESSAGE)
        cmd_entity = MessageEntity(type="bot_command", offset=0, length=7)
        edited = Update(update_id=1, edited_message=self._message("/status", [cmd_entity]))
        fresh = Update(update_id=2, message=self._message("/status", [cmd_entity]))
        assert not handler.check_update(edited)
        assert handler.check_update(fresh)  # positive control


# ---------------------------------------------------------------------------
# slskd client hardening
# ---------------------------------------------------------------------------


class TestSlskdClientTimeout:
    def test_client_constructed_with_http_timeout(self):
        """Without a timeout, a hung slskd blocks the event loop forever."""
        with patch("music_downloader.search.slskd_client.slskd_api.SlskdClient") as mock_cls:
            SlskdClient("http://localhost:5030", "key")
        mock_cls.assert_called_once_with("http://localhost:5030", "key", timeout=SlskdClient.HTTP_TIMEOUT_SECS)
