"""Comprehensive tests for bot handlers - MusicBot class and helper functions."""

from __future__ import annotations

import asyncio
import os

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.bot.handlers import (
    MusicBot,
    PendingDownload,
    PendingSearch,
    _clean_search_title,
    _escape_md,
    _extract_latin_keywords,
    _has_non_latin_script,
    _safe_edit,
)
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

_tmp_dir = None


def _get_tmp_dir():
    global _tmp_dir
    if _tmp_dir is None:
        _tmp_dir = tempfile.mkdtemp()
    return _tmp_dir


def _make_config():
    """Create a mock Config object."""
    td = _get_tmp_dir()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = set()
    config.auto_mode = False
    config.max_results = 5
    config.duration_tolerance_secs = 5
    config.exclude_keywords = ["live", "remix"]
    config.download_dir = os.path.join(td, "downloads")
    config.output_dir = os.path.join(td, "music")
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


def _make_update(user_id=12345, chat_id=67890, text="Nancy Sinatra Bang Bang"):
    """Create a mock Telegram Update object."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message = AsyncMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_callback_update(user_id=12345, chat_id=67890, data="dl:0"):
    """Create a mock callback query Update."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.callback_query = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.edit_message_caption = AsyncMock()
    return update


def _make_context(chat_id=67890):
    """Create a mock context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_audio = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.bot.edit_message_reply_markup = AsyncMock()
    context.bot.edit_message_caption = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestEscapeMd:
    def test_escapes_special_chars(self):
        assert _escape_md("hello_world") == "hello\\_world"
        assert _escape_md("*bold*") == "\\*bold\\*"
        assert _escape_md("[link](url)") == "\\[link\\]\\(url\\)"

    def test_plain_text_unchanged(self):
        assert _escape_md("hello world") == "hello world"

    def test_empty_string(self):
        assert _escape_md("") == ""


class TestSafeEdit:
    @pytest.mark.asyncio
    async def test_success(self):
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        result = await _safe_edit(msg, "new text")
        assert result is True
        msg.edit_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_bad_request(self):
        from telegram.error import BadRequest

        msg = AsyncMock()
        msg.edit_text = AsyncMock(side_effect=BadRequest("Message not modified"))
        result = await _safe_edit(msg, "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_timed_out(self):
        from telegram.error import TimedOut

        msg = AsyncMock()
        msg.edit_text = AsyncMock(side_effect=TimedOut())
        result = await _safe_edit(msg, "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_network_error(self):
        from telegram.error import NetworkError

        msg = AsyncMock()
        msg.edit_text = AsyncMock(side_effect=NetworkError("connection failed"))
        result = await _safe_edit(msg, "text")
        assert result is False


class TestHasNonLatinScript:
    def test_latin_only(self):
        assert _has_non_latin_script("Hello World") is False

    def test_cjk(self):
        assert _has_non_latin_script("紅") is True

    def test_cyrillic(self):
        assert _has_non_latin_script("Привет") is True

    def test_mixed(self):
        assert _has_non_latin_script("紅 - KURENAI") is True

    def test_empty(self):
        assert _has_non_latin_script("") is False

    def test_numbers_only(self):
        assert _has_non_latin_script("12345") is False


class TestExtractLatinKeywords:
    def test_mixed_script(self):
        result = _extract_latin_keywords("紅 - KURENAI - Single Long Version")
        assert "KURENAI" in result
        # Noise words should be filtered
        assert "Single" not in result
        assert "Long" not in result
        assert "Version" not in result

    def test_all_noise(self):
        result = _extract_latin_keywords("The Single Version Mix")
        assert result == []

    def test_pure_latin(self):
        result = _extract_latin_keywords("Purple Rain")
        assert "Purple" in result
        assert "Rain" in result

    def test_short_words_filtered(self):
        result = _extract_latin_keywords("I Am A Star")
        # Single-char words filtered by {2,} regex
        assert "Star" in result


class TestCleanSearchTitleExtended:
    def test_deluxe_edition(self):
        assert _clean_search_title("Song - Deluxe Edition") == "Song"

    def test_anniversary_edition(self):
        assert _clean_search_title("Song - Anniversary Edition") == "Song"

    def test_super_deluxe(self):
        assert _clean_search_title("Song - Super Deluxe") == "Song"

    def test_paren_mono(self):
        assert _clean_search_title("Song (Mono)") == "Song"

    def test_paren_stereo(self):
        assert _clean_search_title("Song (Stereo)") == "Song"

    def test_year_mix(self):
        assert _clean_search_title("Song (2009 Mix)") == "Song"


# ---------------------------------------------------------------------------
# MusicBot tests
# ---------------------------------------------------------------------------


class TestMusicBotInit:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_init(self, mock_slskd, mock_spotify):
        config = _make_config()
        bot = MusicBot(config)
        assert bot.auto_mode is False
        assert bot.pending == {}
        assert bot.downloads == {}
        assert bot.history == []


class TestMusicBotAuthorization:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_is_authorized_no_restrictions(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = set()
        bot = MusicBot(config)
        assert bot._is_authorized(99999) is True

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_is_authorized_allowed(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = {12345, 67890}
        bot = MusicBot(config)
        assert bot._is_authorized(12345) is True
        assert bot._is_authorized(99999) is False

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_check_auth_denied(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = {11111}
        bot = MusicBot(config)
        update = _make_update(user_id=99999)
        result = await bot._check_auth(update)
        assert result is False
        update.message.reply_text.assert_called_once_with("You are not authorized to use this bot.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_check_auth_allowed(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = set()
        bot = MusicBot(config)
        update = _make_update()
        result = await bot._check_auth(update)
        assert result is True


class TestMusicBotCancellation:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_cancel_chat_operations_empty(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        had_work = bot._cancel_chat_operations(12345)
        assert had_work is False

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_cancel_chat_operations_with_pending(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[12345] = PendingSearch(query="test")
        had_work = bot._cancel_chat_operations(12345)
        assert had_work is True
        assert 12345 not in bot.pending

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_cancel_removes_downloads_for_chat(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.downloads["1"] = PendingDownload(
            track=_make_track(),
            result=_make_search_result(),
            chat_id=12345,
        )
        bot.downloads["2"] = PendingDownload(
            track=_make_track(),
            result=_make_search_result(),
            chat_id=99999,
        )
        bot._cancel_chat_operations(12345)
        assert "1" not in bot.downloads
        assert "2" in bot.downloads

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_is_stale(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._chat_generation[123] = 5
        assert bot._is_stale(123, 5) is False
        assert bot._is_stale(123, 4) is True
        assert bot._is_stale(123, 6) is True

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_track_task(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        loop = asyncio.new_event_loop()
        task = loop.create_future()
        task.set_result(None)
        bot._track_task(123, task)
        assert task in bot._active_tasks.get(123, set())
        loop.close()


class TestMusicBotCommands:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_start(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update()
        context = _make_context()
        await bot.cmd_start(update, context)
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args
        assert "Send me a song name" in call_args[0][0]

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_start_unauthorized(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = {11111}
        bot = MusicBot(config)
        update = _make_update(user_id=99999)
        context = _make_context()
        await bot.cmd_start(update, context)
        update.message.reply_text.assert_called_once_with("You are not authorized to use this bot.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_help(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update()
        context = _make_context()
        await bot.cmd_help(update, context)
        update.message.reply_text.assert_called()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_auto(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update()
        context = _make_context()
        await bot.cmd_auto(update, context)
        call_args = update.message.reply_text.call_args
        assert "OFF" in call_args[0][0]

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_auto_on(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.auto_mode = True
        bot = MusicBot(config)
        update = _make_update()
        context = _make_context()
        await bot.cmd_auto(update, context)
        call_args = update.message.reply_text.call_args
        assert "ON" in call_args[0][0]

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_status_empty(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update()
        context = _make_context()
        await bot.cmd_status(update, context)
        update.message.reply_text.assert_called_once_with("No active searches or downloads.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_status_with_pending(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test", track=_make_track())
        update = _make_update()
        context = _make_context()
        await bot.cmd_status(update, context)
        call_args = update.message.reply_text.call_args
        assert "Nancy Sinatra" in call_args[0][0]

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_status_with_downloads(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.downloads["1"] = PendingDownload(
            track=_make_track(),
            result=_make_search_result(),
            chat_id=67890,
        )
        update = _make_update()
        context = _make_context()
        await bot.cmd_status(update, context)
        call_args = update.message.reply_text.call_args
        assert "Active downloads" in call_args[0][0]

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_history_empty(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update()
        context = _make_context()
        await bot.cmd_history(update, context)
        update.message.reply_text.assert_called_once_with("No downloads yet.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_cmd_history_with_entries(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.history = [
            {"filename": "Artist - Song.flac", "status": "success"},
            {"filename": "Artist - Song2.flac", "status": "rejected"},
            {"filename": "Artist - Song3.flac", "status": "failed"},
        ]
        update = _make_update()
        context = _make_context()
        await bot.cmd_history(update, context)
        call_args = update.message.reply_text.call_args
        text = call_args[0][0]
        assert "Recent downloads" in text


class TestMusicBotCallbackHandler:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_auto_toggle_on(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_callback_update(data="auto:on")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert bot.auto_mode is True

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_auto_toggle_off(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.auto_mode = True
        bot = MusicBot(config)
        update = _make_callback_update(data="auto:off")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert bot.auto_mode is False

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_duplicate_cancel(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test")
        update = _make_callback_update(data="dup:cancel")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert 67890 not in bot.pending

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_duplicate_continue(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test song")
        update = _make_callback_update(data="dup:continue")
        context = _make_context()
        # Mock _do_search to prevent actual execution
        bot._do_search = AsyncMock()
        await bot.handle_callback(update, context)
        bot._do_search.assert_called_once()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_cancel(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._spotify_candidates[67890] = [_make_track()]
        update = _make_callback_update(data="sp:cancel")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert 67890 not in bot._spotify_candidates

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_select(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._spotify_candidates[67890] = [_make_track(), _make_track()]
        update = _make_callback_update(data="sp:0")
        context = _make_context()
        bot._do_slskd_search = AsyncMock()
        await bot.handle_callback(update, context)
        bot._do_slskd_search.assert_called_once()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_select_invalid_index(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._spotify_candidates[67890] = [_make_track()]
        update = _make_callback_update(data="sp:99")
        context = _make_context()
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_page(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._spotify_candidates[67890] = [_make_track() for _ in range(12)]
        bot._spotify_page[67890] = 0
        update = _make_callback_update(data="sp_page:1")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert bot._spotify_page[67890] == 1

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_page_expired(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_callback_update(data="sp_page:0")
        context = _make_context()
        await bot.handle_callback(update, context)
        update.callback_query.edit_message_text.assert_called_with("Search expired. Send a new query.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_spotify_page_invalid(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot._spotify_candidates[67890] = [_make_track()]
        update = _make_callback_update(data="sp_page:abc")
        context = _make_context()
        # Should not raise
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_cancel(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test", track=_make_track(), results=[_make_search_result()])
        update = _make_callback_update(data="dl:cancel")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert 67890 not in bot.pending

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_select(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        bot.pending[67890] = PendingSearch(query="test", track=track, results=[result])
        update = _make_callback_update(data="dl:0")
        context = _make_context()
        # Mock the download to prevent actual execution
        bot._do_download = AsyncMock()
        context.application.create_task = MagicMock(return_value=MagicMock())
        await bot.handle_callback(update, context)
        context.application.create_task.assert_called_once()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_auto_pick(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        bot.pending[67890] = PendingSearch(query="test", track=track, results=[result])
        update = _make_callback_update(data="dl:auto")
        context = _make_context()
        bot._do_download = AsyncMock()
        context.application.create_task = MagicMock(return_value=MagicMock())
        await bot.handle_callback(update, context)
        context.application.create_task.assert_called_once()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_select_expired(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_callback_update(data="dl:0")
        context = _make_context()
        await bot.handle_callback(update, context)
        update.callback_query.edit_message_text.assert_called_with("Search expired. Send a new query.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_select_invalid_index(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test", track=_make_track(), results=[_make_search_result()])
        update = _make_callback_update(data="dl:99")
        context = _make_context()
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_results_page(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        results = [_make_search_result(i) for i in range(15)]
        bot.pending[67890] = PendingSearch(query="test", track=track, results=results)
        update = _make_callback_update(data="dl_page:1")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert bot.pending[67890].page == 1

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_results_page_expired(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_callback_update(data="dl_page:0")
        context = _make_context()
        await bot.handle_callback(update, context)
        update.callback_query.edit_message_text.assert_called_with("Search expired. Send a new query.")

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_approve_download(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value="/music/Artist - Song.flac")
        bot._embed_spotify_artwork = AsyncMock()
        bot._dismiss_other_downloads = AsyncMock()
        track = _make_track()
        result = _make_search_result()
        bot.downloads["1"] = PendingDownload(
            track=track,
            result=result,
            chat_id=67890,
            source_path="/downloads/song.flac",
        )
        update = _make_callback_update(data="approve:1")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert "1" not in bot.downloads
        assert len(bot.history) == 1

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_approve_process_fails(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value=None)
        track = _make_track()
        result = _make_search_result()
        bot.downloads["1"] = PendingDownload(
            track=track,
            result=result,
            chat_id=67890,
            source_path="/downloads/song.flac",
        )
        update = _make_callback_update(data="approve:1")
        context = _make_context()
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_approve_no_source_path(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        bot.downloads["1"] = PendingDownload(
            track=track,
            result=result,
            chat_id=67890,
            source_path=None,
        )
        update = _make_callback_update(data="approve:1")
        context = _make_context()
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_reject_download(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        bot.downloads["1"] = PendingDownload(
            track=track,
            result=result,
            chat_id=67890,
        )
        update = _make_callback_update(data="reject:1")
        context = _make_context()
        await bot.handle_callback(update, context)
        assert "1" not in bot.downloads
        assert len(bot.history) == 1
        assert bot.history[0]["status"] == "rejected"

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_approve_expired(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_callback_update(data="approve:999")
        context = _make_context()
        await bot.handle_callback(update, context)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_unauthorized_callback(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = {11111}
        bot = MusicBot(config)
        update = _make_callback_update(user_id=99999, data="auto:on")
        context = _make_context()
        await bot.handle_callback(update, context)
        # Should not change auto_mode
        assert bot.auto_mode is False


class TestMusicBotHelpers:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_format_results(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        results = [_make_search_result(i) for i in range(3)]
        text = bot._format_results(track, results)
        assert "Nancy Sinatra" in text
        assert "Bang Bang" in text
        assert "#1" in text
        assert "#3" in text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_format_results_fallback(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        results = [_make_search_result()]
        text = bot._format_results(track, results, is_fallback=True)
        assert "No FLAC found" in text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_format_results_pagination(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        results = [_make_search_result(i) for i in range(15)]
        text = bot._format_results(track, results, page=0, page_size=5)
        assert "Page 1/" in text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_format_spotify_results(self, mock_slskd, mock_spotify):
        tracks = [_make_track() for _ in range(3)]
        text = MusicBot._format_spotify_results(tracks)
        assert "Multiple matches" in text
        assert "Nancy Sinatra" in text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_format_spotify_results_pagination(self, mock_slskd, mock_spotify):
        tracks = [_make_track() for _ in range(12)]
        text = MusicBot._format_spotify_results(tracks, page=0, page_size=5)
        assert "page 1/" in text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_add_history(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        bot._add_history(track, result, "success")
        assert len(bot.history) == 1
        assert bot.history[0]["status"] == "success"

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_add_history_caps_at_50(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        track = _make_track()
        result = _make_search_result()
        for _ in range(55):
            bot._add_history(track, result, "success")
        assert len(bot.history) == 50

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    def test_next_dl_id(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        id1 = bot._next_dl_id()
        id2 = bot._next_dl_id()
        assert id1 != id2
        assert id1 == "1"
        assert id2 == "2"


class TestMusicBotHandleText:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        update = _make_update(text="   ")
        context = _make_context()
        await bot.handle_text(update, context)
        # Should not proceed to search

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_similar_files_found(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.processor = MagicMock()
        bot.processor.find_similar = MagicMock(return_value=["Artist - Song.flac"])
        update = _make_update(text="Artist Song")
        context = _make_context()
        await bot.handle_text(update, context)
        # Should show duplicate warning
        update.message.reply_text.assert_called_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "Similar files" in call_text

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_unauthorized_text(self, mock_slskd, mock_spotify):
        config = _make_config()
        config.telegram_allowed_users = {11111}
        bot = MusicBot(config)
        update = _make_update(user_id=99999, text="test")
        context = _make_context()
        await bot.handle_text(update, context)
        update.message.reply_text.assert_called_once_with("You are not authorized to use this bot.")


class TestMusicBotDoSearch:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_no_spotify_results(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(return_value=[])
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 0
        await bot._do_search(update, context, "nonexistent song", 0)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_single_spotify_result(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(return_value=[_make_track()])
        bot._do_slskd_search = AsyncMock()
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 0
        await bot._do_search(update, context, "Nancy Sinatra Bang Bang", 0)
        bot._do_slskd_search.assert_called_once()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_multiple_spotify_results(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        tracks = [_make_track(), _make_track()]
        tracks[1].album = "Different Album"
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(return_value=tracks)
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 0
        await bot._do_search(update, context, "Nancy Sinatra Bang Bang", 0)
        assert 67890 in bot._spotify_candidates

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_stale_search_aborted(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(return_value=[_make_track()])
        bot._do_slskd_search = AsyncMock()
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 5  # Set generation ahead
        await bot._do_search(update, context, "test", 0)  # generation 0 is stale
        bot._do_slskd_search.assert_not_called()

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_exception_handled(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(side_effect=Exception("API error"))
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 0
        await bot._do_search(update, context, "test", 0)
        # Should not raise

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_artist_filter(self, mock_slskd, mock_spotify):
        """When query has 'Artist - Title', filter by artist."""
        bot = MusicBot(_make_config())
        t1 = _make_track()
        t2 = TrackInfo(
            artist="Other Artist", title="Bang Bang", album="X", duration_ms=162000, spotify_url="", year="2024"
        )
        bot.spotify = MagicMock()
        bot.spotify.search_multiple = MagicMock(return_value=[t1, t2])
        bot._do_slskd_search = AsyncMock()
        update = _make_update()
        context = _make_context()
        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        context.bot.send_message = AsyncMock(return_value=msg)
        bot._chat_generation[67890] = 0
        await bot._do_search(update, context, "Nancy Sinatra - Bang Bang", 0)
        # Should filter to only Nancy Sinatra -> single result -> auto slskd search
        bot._do_slskd_search.assert_called_once()


class TestMusicBotDismissOtherDownloads:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_dismiss(self, mock_slskd, mock_spotify):
        bot = MusicBot(_make_config())
        bot.pending[67890] = PendingSearch(query="test", track=_make_track(), message_id=100)
        bot.downloads["2"] = PendingDownload(
            track=_make_track(),
            result=_make_search_result(),
            chat_id=67890,
            approval_message_id=200,
        )
        context = _make_context()
        await bot._dismiss_other_downloads(context, 67890)
        assert 67890 not in bot.pending
        assert "2" not in bot.downloads


class TestMusicBotEditApprovalMessage:
    @pytest.mark.asyncio
    async def test_edit_caption(self):
        query = AsyncMock()
        query.edit_message_caption = AsyncMock()
        await MusicBot._edit_approval_message(query, "test")
        query.edit_message_caption.assert_called_once()

    @pytest.mark.asyncio
    async def test_fallback_to_text(self):
        query = AsyncMock()
        query.edit_message_caption = AsyncMock(side_effect=Exception("no caption"))
        query.edit_message_text = AsyncMock()
        await MusicBot._edit_approval_message(query, "test")
        query.edit_message_text.assert_called_once()
