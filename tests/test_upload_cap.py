"""Telegram's upload cap: 50,000,000 bytes by default, TELEGRAM_MAX_UPLOAD_MB to raise it."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import FileSizeLimit

from music_downloader.config import BYTES_PER_MB, DEFAULT_UPLOAD_LIMIT_BYTES, Config
from music_downloader.search.scorer import PROFILE_CHAT
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import (
    CHAT,
    _chat_bot,
    _file,
    _make_bot,
    _make_config,
    _make_context,
    _make_result,
    _make_track,
    _setup_download,
    _status_msg,
)

ENV = {
    "TELEGRAM_BOT_TOKEN": "t",
    "SPOTIFY_CLIENT_ID": "i",
    "SPOTIFY_CLIENT_SECRET": "s",
    "SLSKD_HOST": "http://localhost:5030",
    "SLSKD_API_KEY": "k",
}


def _config(**extra):
    with patch.dict(os.environ, {**ENV, **extra}, clear=True):
        return Config()


class TestConstant:
    def test_default_is_telegrams_own_limit(self):
        assert DEFAULT_UPLOAD_LIMIT_BYTES == FileSizeLimit.FILESIZE_UPLOAD == 50_000_000
        assert BYTES_PER_MB == 1_000_000

    def test_default_config(self):
        config = _config()
        assert config.telegram_max_upload_mb == 50
        assert config.telegram_upload_limit_bytes == 50_000_000

    def test_env_raises_the_cap(self):
        assert _config(TELEGRAM_MAX_UPLOAD_MB="2000").telegram_upload_limit_bytes == 2_000_000_000

    def test_env_floor_is_one_mb(self):
        assert _config(TELEGRAM_MAX_UPLOAD_MB="0").telegram_upload_limit_bytes == 1_000_000


def _big_config(tmp_path=None, chat_users=(CHAT,)):
    config = _make_config(str(tmp_path) if tmp_path else None, chat_users=chat_users)
    config.telegram_upload_limit_bytes = 2_000_000_000
    return config


class TestOneValueEverywhere:
    def test_pipeline_hands_the_cap_to_scorer_ranking_and_opus_ladder(self):
        bot = _make_bot(_big_config())
        assert bot.pipeline.upload_limit_bytes == 2_000_000_000
        assert bot.scorer.chat_size_limit == 2_000_000_000
        # 3000 s at 192 kbps is ~76 MB: over 50 MB, well under 2000 MB.
        assert bot.pipeline.opus_bitrates_that_fit(3000) == [192, 160]
        assert _make_bot().pipeline.opus_bitrates_that_fit(3000) == [96]

    def test_chat_ranking_partition_follows_the_cap(self):
        # With a 50 MB cap the 60 MB FLAC sorts after the MP3; with 2000 MB it fits and leads.
        flac = SearchResult(
            username="flac",
            filename="\\x\\Bang Bang.flac",
            size=60_000_000,
            bit_depth=16,
            sample_rate=44100,
            length=162,
        )
        mp3 = SearchResult(username="mp3", filename="\\x\\Bang Bang.mp3", size=4_000_000, bit_rate=96, length=162)
        for config, expected in ((_make_config(chat_users={CHAT}), ["mp3", "flac"]), (_big_config(), ["flac", "mp3"])):
            bot = _make_bot(config)
            bot.slskd.parse_results = MagicMock(return_value=[flac, mp3])
            ranked = bot.pipeline.rank([], _make_track(), PROFILE_CHAT)
            assert [r.username for r in ranked] == expected

    def test_bytes_just_over_fifty_million_are_over_the_default_cap(self):
        # 50 MiB (52,428,800 bytes) used to count as fitting and lead; Telegram
        # refuses it, so it now sorts after a copy that really fits.
        mib = SearchResult(
            username="mib", filename="\\x\\Bang Bang.flac", size=52_428_800, bit_depth=16, sample_rate=44100, length=162
        )
        mp3 = SearchResult(username="mp3", filename="\\x\\Bang Bang.mp3", size=4_000_000, bit_rate=96, length=162)
        bot = _make_bot(_make_config(chat_users={CHAT}))
        bot.slskd.parse_results = MagicMock(return_value=[mib, mp3])
        assert [r.username for r in bot.pipeline.rank([], _make_track(), PROFILE_CHAT)] == ["mp3", "mib"]

    @pytest.mark.asyncio
    async def test_send_path_uses_the_configured_cap(self, tmp_path):
        bot = _chat_bot()
        bot.pipeline.upload_limit_bytes = 2_000_000_000
        source = _file(tmp_path, size=60_000_000)
        _setup_download(bot, source)
        bot.pipeline.convert_to_opus = AsyncMock()
        context = _make_context()
        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())
        bot.pipeline.convert_to_opus.assert_not_awaited()
        assert context.bot.send_audio.call_args.kwargs["filename"].endswith(".flac")

    @pytest.mark.asyncio
    async def test_messages_name_the_configured_cap(self):
        bot = _make_bot(_big_config())
        update = MagicMock()
        update.effective_user.id = CHAT
        update.effective_chat.id = CHAT
        update.message.reply_text = AsyncMock()
        await bot.cmd_deliver(update, _make_context())
        assert "Over 2000 MB" in update.message.reply_text.call_args.args[0]
