"""Tests for chat delivery: the track is sent into the chat and nothing is saved (v0.13.0)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest, NetworkError

from music_downloader.bot.handlers import (
    TELEGRAM_FILE_LIMIT,
    MusicBot,
    PendingDownload,
    PendingSearch,
)
from music_downloader.bot.keyboards import build_delivery_mode_keyboard
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.import_repo import TrackStatus
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.pipeline import fetch as pipeline_fetch
from music_downloader.pipeline.fetch import opus_bitrates_that_fit
from music_downloader.search.slskd_client import DownloadStatus, SearchResult

CHAT = 67890
OTHER = 12345  # allowed, not in the chat-delivery list
GROUP = -100123


def _make_config(td=None, chat_users=None):
    td = td or tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {12345, CHAT}
    config.telegram_chat_delivery_users = set(chat_users or ())
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


def _make_bot(config=None):
    with (
        patch("music_downloader.pipeline.SpotifyResolver"),
        patch("music_downloader.pipeline.SlskdClient"),
    ):
        return MusicBot(config or _make_config())


def _chat_bot():
    """A bot whose chat CHAT is in chat delivery, with the processor mocked."""
    bot = _make_bot(_make_config(chat_users={CHAT}))
    bot.processor = MagicMock()
    bot.processor.build_filename = MagicMock(side_effect=lambda a, t, ext="flac": f"{a} - {t}.{ext}")
    bot.processor.cleanup_download = MagicMock(side_effect=lambda p: os.remove(p) or True)
    bot.pipeline.embed_artwork = AsyncMock()
    return bot


def _make_track(duration_ms=162_000):
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="X",
        duration_ms=duration_ms,
        spotify_url="u",
        year="1966",
    )


def _make_result(idx=0, size=30_000_000, ext="flac"):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\track{idx}.{ext}",
        size=size,
        bit_depth=16,
        sample_rate=44100,
        length=162,
    )


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


def _file(tmp_path, name="track.flac", size=1000):
    """A real file; sizes over the limit are sparse so they cost no disk."""
    path = tmp_path / name
    with open(path, "wb") as f:
        f.truncate(size)
    return str(path)


def _setup_download(bot, source_path):
    bot.slskd = MagicMock()
    bot.slskd.enqueue_download = MagicMock(return_value=True)
    bot.slskd.wait_for_download = AsyncMock(
        return_value=DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
    )
    bot.processor.find_downloaded_file = MagicMock(return_value=source_path)
    bot.pipeline.analyze = AsyncMock(return_value=None)


def _status_msg():
    msg = AsyncMock()
    msg.message_id = 1
    return msg


def _edits(msg):
    return [c.args[0] for c in msg.edit_text.call_args_list]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfigParsing:
    @pytest.fixture
    def env_vars(self):
        return {
            "TELEGRAM_BOT_TOKEN": "t",
            "SPOTIFY_CLIENT_ID": "i",
            "SPOTIFY_CLIENT_SECRET": "s",
            "SLSKD_HOST": "http://localhost:5030",
            "SLSKD_API_KEY": "k",
        }

    def _config(self, env_vars, value):
        from music_downloader.config import Config

        env = dict(env_vars)
        if value is not None:
            env["TELEGRAM_CHAT_DELIVERY_USERS"] = value
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("TELEGRAM_CHAT_DELIVERY_USERS", None)
            return Config()

    def test_unset_is_empty(self, env_vars):
        assert self._config(env_vars, None).telegram_chat_delivery_users == set()

    def test_empty_is_empty(self, env_vars):
        assert self._config(env_vars, "").telegram_chat_delivery_users == set()

    def test_spaces_and_trailing_comma(self, env_vars):
        assert self._config(env_vars, " 111 , 222 ,").telegram_chat_delivery_users == {111, 222}

    def test_bad_token_fails_loudly_like_allowed_users(self, env_vars):
        with pytest.raises(ValueError):
            self._config(env_vars, "111,abc")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestSettingsRepo:
    def test_delivery_roundtrip_and_default(self, tmp_path):
        repo = SettingsRepository(Database(str(tmp_path / "db.sqlite")))
        assert repo.get_delivery_mode(1) is None
        repo.set_delivery_mode(1, "chat", auto_mode=False)
        assert repo.get_delivery_mode(1) == "chat"
        repo.set_delivery_mode(1, "library", auto_mode=False)
        assert repo.get_delivery_mode(1) == "library"

    def test_delivery_and_auto_do_not_clobber_each_other(self, tmp_path):
        repo = SettingsRepository(Database(str(tmp_path / "db.sqlite")))
        repo.set_auto_mode(1, True)
        repo.set_delivery_mode(1, "chat", auto_mode=False)  # existing row: auto untouched
        assert repo.get_auto_mode(1) is True
        repo.set_auto_mode(1, False)
        assert repo.get_delivery_mode(1) == "chat"
        repo.set_auto_mode(2, True)  # new row from auto: delivery stays unset
        assert repo.get_delivery_mode(2) is None
        repo.set_delivery_mode(3, "chat", auto_mode=True)  # new row from delivery
        assert repo.get_auto_mode(3) is True

    def test_existing_database_without_column_is_upgraded(self, tmp_path):
        db_path = str(tmp_path / "old.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY, auto_mode INTEGER NOT NULL DEFAULT 0, "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO chat_settings (chat_id, auto_mode) VALUES (5, 1)")
        conn.commit()
        conn.close()

        db = Database(db_path)
        columns = {row[1] for row in db.connection.execute("PRAGMA table_info(chat_settings)")}
        assert "delivery_mode" in columns
        repo = SettingsRepository(db)
        assert repo.get_auto_mode(5) is True  # old data kept
        assert repo.get_delivery_mode(5) is None
        repo.set_delivery_mode(5, "chat", auto_mode=False)
        assert repo.get_delivery_mode(5) == "chat"
        db.close()
        Database(db_path).close()  # reopening an upgraded DB is a no-op


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_default_is_library(self):
        bot = _make_bot()
        assert bot._delivery_mode(CHAT) == "library"

    def test_env_list_makes_chat_the_default(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        assert bot._delivery_mode(CHAT) == "chat"
        assert bot._delivery_mode(12345) == "library"

    def test_env_list_locks_chat_even_when_library_is_stored(self, tmp_path):
        config = _make_config(str(tmp_path), chat_users={CHAT})
        bot = _make_bot(config)
        bot._set_delivery(CHAT, "library")
        assert bot._delivery_mode(CHAT) == "chat"
        assert _make_bot(config)._delivery_mode(CHAT) == "chat"  # survives restart too
        assert _make_bot(_make_config(str(tmp_path)))._delivery_mode(CHAT) == "library"  # off the list: stored wins

    def test_locked_user_in_a_group_stays_chat_despite_stored_library(self, tmp_path):
        config = _make_config(str(tmp_path), chat_users={CHAT})
        bot = _make_bot(config)
        bot._set_delivery(GROUP, "library")
        assert bot._delivery_mode(GROUP, CHAT) == "chat"
        assert bot._delivery_mode(GROUP, OTHER) == "library"

    def test_stored_chat_without_env_list(self, tmp_path):
        config = _make_config(str(tmp_path))
        _make_bot(config)._set_delivery(CHAT, "chat")
        assert _make_bot(config)._delivery_mode(CHAT) == "chat"

    def test_group_chat_uses_the_starting_users_id(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        assert bot._delivery_mode(GROUP) == "library"
        assert bot._delivery_mode(GROUP, CHAT) == "chat"
        assert bot._delivery_mode(GROUP, OTHER) == "library"

    def test_setting_delivery_keeps_effective_auto(self, tmp_path):
        config = _make_config(str(tmp_path))
        config.auto_mode = True
        _make_bot(config)._set_delivery(CHAT, "chat")
        assert _make_bot(config)._is_auto(CHAT) is True


# ---------------------------------------------------------------------------
# Command and toggle
# ---------------------------------------------------------------------------


class TestDeliverCommand:
    def test_keyboard_toggles(self):
        assert build_delivery_mode_keyboard("chat").inline_keyboard[0][0].callback_data == "deliver:library"
        assert build_delivery_mode_keyboard("library").inline_keyboard[0][0].callback_data == "deliver:chat"

    @pytest.mark.asyncio
    async def test_cmd_deliver_shows_mode_and_toggle(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        bot._set_delivery(CHAT, "chat")
        update = MagicMock()
        update.effective_user.id = CHAT
        update.effective_chat.id = CHAT
        update.message = AsyncMock()
        await bot.cmd_deliver(update, _make_context())
        kwargs = update.message.reply_text.call_args.kwargs
        assert "<b>Chat</b>" in update.message.reply_text.call_args.args[0]
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "deliver:library"

    @pytest.mark.asyncio
    async def test_cmd_deliver_for_a_locked_account_has_no_toggle(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        update = MagicMock()
        update.effective_user.id = CHAT
        update.effective_chat.id = CHAT
        update.message = AsyncMock()
        await bot.cmd_deliver(update, _make_context())
        args, kwargs = update.message.reply_text.call_args
        assert "fixed" in args[0] and "<b>Chat</b>" in args[0]
        assert "reply_markup" not in kwargs

    @pytest.mark.asyncio
    async def test_callback_cannot_unlock_a_listed_account(self, tmp_path):
        config = _make_config(str(tmp_path), chat_users={CHAT})
        bot = _make_bot(config)
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = CHAT
        update.callback_query.data = "deliver:library"
        update.effective_chat.id = CHAT
        await bot.handle_callback(update, _make_context())
        assert SettingsRepository(bot.db).get_delivery_mode(CHAT) is None
        assert _make_bot(config)._delivery_mode(CHAT) == "chat"
        assert "fixed" in update.callback_query.edit_message_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_callback_persists_mode(self, tmp_path):
        config = _make_config(str(tmp_path))
        bot = _make_bot(config)
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = 12345
        update.callback_query.data = "deliver:chat"
        update.effective_chat.id = CHAT
        await bot.handle_callback(update, _make_context())
        assert _make_bot(config)._delivery_mode(CHAT) == "chat"

    def test_command_registered(self):
        from music_downloader.bot.handlers import create_bot

        with patch("music_downloader.bot.handlers.Application") as mock_app_cls:
            builder = mock_app_cls.builder.return_value
            builder.token.return_value = builder
            builder.post_init.return_value = builder
            builder.post_shutdown.return_value = builder
            app = create_bot(_make_config())
        handlers = [c.args[0] for c in app.add_handler.call_args_list]
        commands = {cmd for h in handlers for cmd in getattr(h, "commands", ())}
        assert "deliver" in commands

    @pytest.mark.asyncio
    async def test_history_renders_delivered(self):
        bot = _make_bot()
        bot.history_repo.add(artist="a", title="t", filename="a - t.flac", source_user="u", status="delivered")
        update = MagicMock()
        update.effective_user.id = 12345
        update.message = AsyncMock()
        await bot.cmd_history(update, _make_context())
        assert "\U0001f4e8 <code>a - t.flac</code>" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_chat_mode_skips_library_duplicate_check(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        bot.processor = MagicMock()
        bot.processor.find_similar = MagicMock(return_value=["Nancy Sinatra - Bang Bang.flac"])
        update = MagicMock()
        update.effective_user.id = CHAT
        update.effective_chat.id = CHAT
        update.message = AsyncMock()
        update.message.text = "Nancy Sinatra Bang Bang"
        with patch.object(bot, "_do_search", new_callable=AsyncMock) as mock_search:
            await bot.handle_text(update, _make_context())
        bot.processor.find_similar.assert_not_called()
        mock_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def _ranked(self, bot, chat_id):
        big = TELEGRAM_FILE_LIMIT + 10_000_000
        results = [
            _make_result(0, size=big),  # top score, hi-res, over the limit
            _make_result(1, size=40_000_000),
            _make_result(2, size=big),
            _make_result(3, size=10_000_000),
        ]
        bot.slskd.parse_results = MagicMock(return_value=results)
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=list(results))
        ranked = bot.pipeline.rank([], _make_track(), bot._profile(chat_id))
        return [r.username for r in ranked]

    def test_chat_mode_puts_oversize_after_every_fit_stably(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        assert self._ranked(bot, CHAT) == ["user1", "user3", "user0", "user2"]

    def test_library_mode_order_untouched(self):
        bot = _make_bot()
        assert self._ranked(bot, CHAT) == ["user0", "user1", "user2", "user3"]

    def test_bitrate_choice(self):
        assert opus_bitrates_that_fit(162) == [192, 160]
        assert opus_bitrates_that_fit(2200) == [160, 128]
        assert opus_bitrates_that_fit(3000) == [128, 96]
        assert opus_bitrates_that_fit(3500) == [96]
        assert opus_bitrates_that_fit(5000) == []

    def test_unknown_duration_tries_every_bitrate(self):
        # Nothing can be estimated, so a long mix must still reach 128 and 96.
        assert opus_bitrates_that_fit(0) == [192, 160, 128, 96]

    def test_result_exactly_at_the_limit_counts_as_fitting(self):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        results = [_make_result(0, size=TELEGRAM_FILE_LIMIT + 1), _make_result(1, size=TELEGRAM_FILE_LIMIT)]
        bot.slskd.parse_results = MagicMock(return_value=results)
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=list(results))
        ranked = bot.pipeline.rank([], _make_track(), bot._profile(CHAT))
        assert [r.username for r in ranked] == ["user1", "user0"]


# ---------------------------------------------------------------------------
# _do_download in chat delivery
# ---------------------------------------------------------------------------


class TestChatDownload:
    @pytest.mark.asyncio
    async def test_small_file_sent_without_keyboard_and_nothing_saved(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path)
        _setup_download(bot, source)
        seen_during_cleanup = []
        bot.processor.cleanup_download = MagicMock(
            side_effect=lambda p: seen_during_cleanup.append(bool(bot.downloads)) or os.remove(p) or True
        )
        context = _make_context()
        status = _status_msg()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), status)

        context.bot.send_audio.assert_awaited_once()
        kwargs = context.bot.send_audio.call_args.kwargs
        assert "reply_markup" not in kwargs
        assert kwargs["filename"] == "Nancy Sinatra - Bang Bang.flac"
        assert kwargs["caption"].startswith("#1 Quality:")
        bot.processor.process_file.assert_not_called()
        bot.processor.cleanup_download.assert_called_once_with(source)
        assert seen_during_cleanup == [True]  # entry still protected the file during cleanup
        assert bot.downloads == {}
        bot.pipeline.embed_artwork.assert_awaited_once()
        assert "#1 Sent" in _edits(status)[-1]
        assert bot.history_repo.get_recent(1)[0].status == "delivered"

    @pytest.mark.asyncio
    async def test_bad_request_falls_back_to_document(self, tmp_path):
        bot = _chat_bot()
        _setup_download(bot, _file(tmp_path))
        context = _make_context()
        context.bot.send_audio = AsyncMock(side_effect=BadRequest("nope"))

        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        context.bot.send_document.assert_awaited_once()
        assert "reply_markup" not in context.bot.send_document.call_args.kwargs
        assert bot.history_repo.get_recent(1)[0].status == "delivered"

    @pytest.mark.asyncio
    async def test_oversize_converted_at_best_fitting_bitrate(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path, size=TELEGRAM_FILE_LIMIT + 11 * 1024 * 1024)
        _setup_download(bot, source)
        ogg = _file(tmp_path, "out.ogg", 5000)
        bot.pipeline.convert_to_opus = AsyncMock(return_value=ogg)
        bot.pipeline.preview_clip = AsyncMock()
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        bot.pipeline.convert_to_opus.assert_awaited_once_with(source, 192)
        bot.pipeline.preview_clip.assert_not_awaited()
        kwargs = context.bot.send_audio.call_args.kwargs
        assert "Converted to Opus 192 kbps, original 61 MB FLAC" in kwargs["caption"]
        assert kwargs["filename"].endswith(".ogg")
        assert "reply_markup" not in kwargs
        assert not os.path.exists(ogg)
        assert not os.path.exists(source)
        bot.pipeline.embed_artwork.assert_not_awaited()
        assert bot.history_repo.get_recent(1)[0].filename == "Nancy Sinatra - Bang Bang.ogg"

    @pytest.mark.asyncio
    async def test_file_exactly_at_the_limit_is_sent_as_is(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path, size=TELEGRAM_FILE_LIMIT)
        _setup_download(bot, source)
        bot.pipeline.convert_to_opus = AsyncMock()
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        bot.pipeline.convert_to_opus.assert_not_awaited()
        bot.pipeline.embed_artwork.assert_awaited_once()
        context.bot.send_audio.assert_awaited_once()
        assert context.bot.send_audio.call_args.kwargs["filename"] == "Nancy Sinatra - Bang Bang.flac"
        assert bot.history_repo.get_recent(1)[0].status == "delivered"

    @pytest.mark.asyncio
    async def test_oversize_retries_once_lower_when_output_still_too_big(self, tmp_path):
        bot = _chat_bot()
        _setup_download(bot, _file(tmp_path, size=TELEGRAM_FILE_LIMIT + 1))
        too_big = _file(tmp_path, "a.ogg", TELEGRAM_FILE_LIMIT + 1)
        fits = _file(tmp_path, "b.ogg", 5000)
        bot.pipeline.convert_to_opus = AsyncMock(side_effect=[too_big, fits])
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        assert [c.args[1] for c in bot.pipeline.convert_to_opus.await_args_list] == [192, 160]
        assert "Opus 160 kbps" in context.bot.send_audio.call_args.kwargs["caption"]
        assert not os.path.exists(too_big) and not os.path.exists(fits)

    @pytest.mark.asyncio
    async def test_nothing_fits_keeps_source_and_offers_retry(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path, size=TELEGRAM_FILE_LIMIT + 1)
        _setup_download(bot, source)
        bot.pipeline.convert_to_opus = AsyncMock()
        bot.pipeline.preview_clip = AsyncMock()
        bot.pending[CHAT] = PendingSearch(query="q", results=[_make_result(0), _make_result(1)])
        context = _make_context()
        status = _status_msg()

        await bot._do_download(context, CHAT, _make_track(duration_ms=5_000_000), _make_result(), status)

        bot.pipeline.convert_to_opus.assert_not_awaited()
        bot.pipeline.preview_clip.assert_not_awaited()
        context.bot.send_audio.assert_not_awaited()
        assert "under 50 MB" in _edits(status)[-1]
        markup = status.edit_text.call_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert any(c.startswith("retry:") for c in callbacks)
        assert any(c.startswith("next:") for c in callbacks)
        assert os.path.exists(source)
        bot.processor.cleanup_download.assert_not_called()
        (entry,) = bot.downloads.values()
        assert entry.source_path is None  # no longer shields the file from the sweep
        assert bot.history_repo.get_recent(1)[0].status == "too_large"

    @pytest.mark.asyncio
    async def test_send_failure_keeps_source_and_offers_retry(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path)
        _setup_download(bot, source)
        context = _make_context()
        context.bot.send_audio = AsyncMock(side_effect=NetworkError("connection reset"))
        status = _status_msg()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), status)

        assert "Could not send" in _edits(status)[-1]
        callbacks = [
            b.callback_data for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row
        ]
        assert any(c.startswith("retry:") for c in callbacks)
        assert os.path.exists(source)
        bot.processor.cleanup_download.assert_not_called()
        assert len(bot.downloads) == 1
        assert bot.history_repo.get_recent(1)[0].status == "send_failed"

    @pytest.mark.asyncio
    async def test_auto_plus_chat_delivers_without_saving(self, tmp_path):
        bot = _chat_bot()
        bot._set_auto(CHAT, True)
        _setup_download(bot, _file(tmp_path))
        context = _make_context()

        with patch.object(bot, "_auto_save", new_callable=AsyncMock) as mock_auto_save:
            await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        mock_auto_save.assert_not_awaited()
        bot.processor.process_file.assert_not_called()
        assert "reply_markup" not in context.bot.send_audio.call_args.kwargs

    @pytest.mark.asyncio
    async def test_library_mode_positive_control_still_previews_with_keyboard(self, tmp_path):
        bot = _make_bot()
        bot.processor = MagicMock()
        bot.processor.build_filename = MagicMock(return_value="x.flac")
        _setup_download(bot, _file(tmp_path))
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())

        assert context.bot.send_audio.call_args.kwargs["reply_markup"] is not None
        bot.processor.cleanup_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_approving_old_preview_in_chat_mode_delivers_instead_of_saving(self, tmp_path):
        bot = _chat_bot()
        source = _file(tmp_path)
        bot.downloads["d1"] = PendingDownload(
            track=_make_track(), result=_make_result(), chat_id=CHAT, source_path=source
        )
        update = MagicMock()
        update.callback_query = AsyncMock()
        context = _make_context()

        await bot._handle_approval(update, context, CHAT, "approve:d1")

        bot.processor.process_file.assert_not_called()
        context.bot.send_audio.assert_awaited_once()
        assert not os.path.exists(source)
        assert bot.downloads == {}
        # The preview under the user's finger ends with the outcome, not "Sending..."
        assert "Sent to this chat" in update.callback_query.edit_message_caption.call_args.kwargs["caption"]


# ---------------------------------------------------------------------------
# Group chats: the user who started an operation decides its delivery
# ---------------------------------------------------------------------------


def _group_bot():
    """GROUP has no stored mode; CHAT (a user) is in the chat-delivery list, OTHER is not."""
    bot = _chat_bot()
    bot.config.telegram_allowed_users = {CHAT, OTHER}
    return bot


async def _someone_else_speaks(bot, user_id):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = GROUP
    update.effective_message = AsyncMock()
    assert await bot._check_auth(update)


class TestGroupChats:
    @pytest.mark.asyncio
    async def test_another_member_speaking_mid_download_does_not_flip_delivery(self, tmp_path):
        bot = _group_bot()
        source = _file(tmp_path)
        _setup_download(bot, source)
        context = _make_context()
        await _someone_else_speaks(bot, OTHER)

        await bot._do_download(context, GROUP, _make_track(), _make_result(), _status_msg(), user_id=CHAT)

        bot.processor.process_file.assert_not_called()
        assert "reply_markup" not in context.bot.send_audio.call_args.kwargs
        assert not os.path.exists(source)

    @pytest.mark.asyncio
    async def test_library_users_download_stays_library_after_a_chat_user_speaks(self, tmp_path):
        bot = _group_bot()
        source = _file(tmp_path)
        _setup_download(bot, source)
        context = _make_context()
        await _someone_else_speaks(bot, CHAT)

        await bot._do_download(context, GROUP, _make_track(), _make_result(), _status_msg(), user_id=OTHER)

        assert context.bot.send_audio.call_args.kwargs["reply_markup"] is not None
        bot.processor.cleanup_download.assert_not_called()
        assert os.path.exists(source)

    @pytest.mark.asyncio
    async def test_picking_a_result_starts_the_download_as_the_picker(self):
        bot = _group_bot()
        bot.pending[GROUP] = PendingSearch(query="q", track=_make_track(), results=[_make_result()], search_id="s1")
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = CHAT
        context = _make_context()

        with patch.object(bot, "_do_download", new_callable=AsyncMock) as mock_dl:
            await bot._handle_download_selection(update, context, GROUP, "dl:s1:0")
            await asyncio.sleep(0)

        assert mock_dl.call_args.kwargs["user_id"] == CHAT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", ["retry", "next"])
    async def test_retry_and_next_keep_the_original_starter(self, prefix):
        bot = _group_bot()
        bot.pending[GROUP] = PendingSearch(
            query="q", track=_make_track(), results=[_make_result(0), _make_result(1)], search_id="s1"
        )
        bot.downloads["d1"] = PendingDownload(
            track=_make_track(), result=_make_result(), chat_id=GROUP, search_id="s1", user_id=CHAT
        )
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = OTHER  # someone else taps the button
        context = _make_context()
        handler = bot._handle_retry if prefix == "retry" else bot._handle_next_result

        with patch.object(bot, "_do_download", new_callable=AsyncMock) as mock_dl:
            await handler(update, context, GROUP, f"{prefix}:d1")
            await asyncio.sleep(0)

        assert mock_dl.call_args.kwargs["user_id"] == CHAT

    @pytest.mark.asyncio
    async def test_old_preview_approval_follows_the_downloads_starter(self, tmp_path):
        bot = _group_bot()
        source = _file(tmp_path)
        bot.downloads["d1"] = PendingDownload(
            track=_make_track(), result=_make_result(), chat_id=GROUP, source_path=source, user_id=CHAT
        )
        update = MagicMock()
        update.callback_query = AsyncMock()
        context = _make_context()
        await _someone_else_speaks(bot, OTHER)

        await bot._handle_approval(update, context, GROUP, "approve:d1")

        bot.processor.process_file.assert_not_called()
        context.bot.send_audio.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_import_follows_the_user_who_confirmed_it(self, tmp_path):
        bot = _group_bot()
        bot.import_repo = MagicMock()
        bot.import_repo.get_job_for_chat = MagicMock(return_value=MagicMock())
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = CHAT
        context = _make_context()
        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._handle_import_callback(update, context, GROUP, "ic:1")
        await _someone_else_speaks(bot, OTHER)

        source = _file(tmp_path)
        _setup_download(bot, source)
        bot.downloads["dl1"] = PendingDownload(track=_make_track(), result=_make_result(), chat_id=GROUP)
        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._do_import_download(
                context,
                GROUP,
                _make_track(),
                _make_result(),
                _status_msg(),
                generation=0,
                job_id=1,
                track_id=2,
                dl_id="dl1",
            )

        bot.processor.process_file.assert_not_called()
        assert "reply_markup" not in context.bot.send_audio.call_args.kwargs


# ---------------------------------------------------------------------------
# Imports in chat delivery
# ---------------------------------------------------------------------------


def _import_bot():
    bot = _chat_bot()
    bot.import_repo = MagicMock()
    return bot


async def _run_import_download(bot, source, context):
    _setup_download(bot, source)
    bot.downloads["dl1"] = PendingDownload(track=_make_track(), result=_make_result(), chat_id=CHAT)
    status = _status_msg()
    with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock) as mock_next:
        await bot._do_import_download(
            context, CHAT, _make_track(), _make_result(), status, generation=0, job_id=1, track_id=2, dl_id="dl1"
        )
    return status, mock_next


class TestChatImports:
    @pytest.mark.asyncio
    async def test_review_import_delivers_and_continues(self, tmp_path):
        bot = _import_bot()
        source = _file(tmp_path)
        context = _make_context()

        status, mock_next = await _run_import_download(bot, source, context)

        assert "reply_markup" not in context.bot.send_audio.call_args.kwargs
        bot.processor.process_file.assert_not_called()
        assert not os.path.exists(source)
        assert "dl1" not in bot.downloads
        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.completed)
        mock_next.assert_awaited_once()
        assert "Sent" in _edits(status)[-1]
        assert bot.history_repo.get_recent(1)[0].status == "delivered"

    @pytest.mark.asyncio
    async def test_auto_import_delivers_without_saving(self, tmp_path):
        bot = _import_bot()
        bot._import_auto[CHAT] = True
        source = _file(tmp_path)
        context = _make_context()

        with patch.object(bot, "_import_auto_save", new_callable=AsyncMock) as mock_save:
            _, mock_next = await _run_import_download(bot, source, context)

        mock_save.assert_not_awaited()
        bot.processor.process_file.assert_not_called()
        context.bot.send_audio.assert_awaited_once()
        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.completed)
        mock_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_import_send_failure_marks_failed_and_continues(self, tmp_path):
        bot = _import_bot()
        bot._import_auto[CHAT] = True
        source = _file(tmp_path)
        context = _make_context()
        context.bot.send_audio = AsyncMock(side_effect=NetworkError("down"))

        _, mock_next = await _run_import_download(bot, source, context)

        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.failed, "send_failed")
        mock_next.assert_awaited_once()
        assert os.path.exists(source)

    @pytest.mark.asyncio
    async def test_review_import_send_failure_pauses_with_skip_keyboard(self, tmp_path):
        bot = _import_bot()
        source = _file(tmp_path)
        context = _make_context()
        context.bot.send_audio = AsyncMock(side_effect=NetworkError("down"))

        status, mock_next = await _run_import_download(bot, source, context)

        mock_next.assert_not_awaited()
        callbacks = [
            b.callback_data for row in status.edit_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row
        ]
        assert "is:1:2" in callbacks
        bot.import_repo.update_track_status.assert_called_with(2, TrackStatus.awaiting_approval)
        assert os.path.exists(source)

    @pytest.mark.asyncio
    async def test_review_import_failure_heading_escapes_html(self, tmp_path):
        """A name like <NSYNC> must not break the HTML edit that carries the Skip keyboard."""
        bot = _import_bot()
        source = _file(tmp_path)
        context = _make_context()
        context.bot.send_audio = AsyncMock(side_effect=NetworkError("down"))
        track = TrackInfo(
            artist="*NSYNC <&>", title="Bye_Bye [Bye]", album="X", duration_ms=162_000, spotify_url="u", year="2000"
        )
        _setup_download(bot, source)
        bot.downloads["dl1"] = PendingDownload(track=track, result=_make_result(), chat_id=CHAT)
        status = _status_msg()

        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._do_import_download(
                context, CHAT, track, _make_result(), status, generation=0, job_id=1, track_id=2, dl_id="dl1"
            )

        text = _edits(status)[-1]
        assert "*NSYNC &lt;&amp;&gt; - Bye_Bye [Bye]" in text
        assert "<&>" not in text

    @pytest.mark.asyncio
    async def test_import_approve_in_chat_mode_delivers_instead_of_saving(self, tmp_path):
        bot = _import_bot()
        source = _file(tmp_path)
        bot.downloads["dl1"] = PendingDownload(
            track=_make_track(), result=_make_result(), chat_id=CHAT, source_path=source
        )
        update = MagicMock()
        update.callback_query = AsyncMock()
        context = _make_context()

        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock) as mock_next:
            await bot._handle_import_approve(update, context, CHAT, 1, 2, "dl1")

        bot.processor.process_file.assert_not_called()
        context.bot.send_audio.assert_awaited_once()
        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.completed)
        mock_next.assert_awaited_once()
        assert not os.path.exists(source)


# ---------------------------------------------------------------------------
# Converter bitrate
# ---------------------------------------------------------------------------


class TestConvertBitrate:
    def test_bitrate_reaches_ffmpeg(self, tmp_path):
        from music_downloader.processor.lossless_analyzer import convert_to_ogg

        def fake_run(cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"ogg")

        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            out = convert_to_ogg(str(tmp_path / "in.flac"), 96)
        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("-b:a") + 1] == "96k"
        os.unlink(out)

    def test_default_bitrate_unchanged(self, tmp_path):
        from music_downloader.processor.lossless_analyzer import convert_to_ogg

        def fake_run(cmd, **kwargs):
            with open(cmd[-1], "wb") as f:
                f.write(b"ogg")

        with patch("subprocess.run", side_effect=fake_run) as mock_run:
            out = convert_to_ogg(str(tmp_path / "in.flac"))
        cmd = mock_run.call_args.args[0]
        assert cmd[cmd.index("-b:a") + 1] == "128k"
        os.unlink(out)

    @pytest.mark.asyncio
    async def test_handler_wrapper_passes_bitrate(self):
        with patch("music_downloader.pipeline.fetch.convert_to_ogg", return_value="/tmp/x.ogg") as mock_conv:
            assert await pipeline_fetch.convert_to_opus("/fake.flac", 160) == "/tmp/x.ogg"
        mock_conv.assert_called_once_with("/fake.flac", 160)
