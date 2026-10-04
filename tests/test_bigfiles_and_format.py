"""A local Bot API server (TELEGRAM_API_BASE_URL, 2000 MB cap) and the per-chat send format (/format)."""

import asyncio
import logging
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.config import Config
from music_downloader.persistence.database import Database
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.pipeline import fetch as pipeline_fetch
from music_downloader.search.scorer import PROFILE_CHAT, ResultScorer
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import (
    CHAT,
    OTHER,
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
LOCAL = "http://telegram-bot-api:8081"


def _config(**extra):
    with patch.dict(os.environ, {**ENV, **extra}, clear=True):
        return Config()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestApiServerConfig:
    def test_cloud_by_default(self):
        config = _config()
        assert config.telegram_api_base_url == ""
        assert config.telegram_max_upload_mb == 50
        assert config.telegram_upload_timeout_secs is None

    def test_local_server_raises_cap_and_timeouts(self):
        config = _config(TELEGRAM_API_BASE_URL=f"{LOCAL}/")
        assert config.telegram_api_base_url == LOCAL
        assert config.telegram_max_upload_mb == 2000
        assert config.telegram_upload_limit_bytes == 2_000_000_000
        assert config.telegram_upload_timeout_secs == 600

    def test_env_cap_still_wins_with_a_local_server(self):
        assert _config(TELEGRAM_API_BASE_URL=LOCAL, TELEGRAM_MAX_UPLOAD_MB="100").telegram_max_upload_mb == 100

    def test_empty_cap_means_the_default(self):
        # docker-compose passes TELEGRAM_MAX_UPLOAD_MB through as an empty string when unset.
        assert _config(TELEGRAM_MAX_UPLOAD_MB="").telegram_max_upload_mb == 50
        assert _config(TELEGRAM_API_BASE_URL=LOCAL, TELEGRAM_MAX_UPLOAD_MB="").telegram_max_upload_mb == 2000

    def test_timeout_env(self):
        assert (
            _config(TELEGRAM_API_BASE_URL=LOCAL, TELEGRAM_UPLOAD_TIMEOUT_SECS="900").telegram_upload_timeout_secs == 900
        )
        assert _config(TELEGRAM_UPLOAD_TIMEOUT_SECS="120").telegram_upload_timeout_secs == 120


class TestBuilder:
    def _build(self, tmp_path, **env):
        from music_downloader.bot.handlers import create_bot

        config = _config(DATA_DIR=str(tmp_path), OUTPUT_DIR=str(tmp_path / "music"), **env)
        with (
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as app_cls,
        ):
            builder = app_cls.builder.return_value
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            create_bot(config)
        return builder

    def test_local_server_urls_and_timeouts(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger="music_downloader.bot.handlers"):
            builder = self._build(tmp_path, TELEGRAM_API_BASE_URL=LOCAL)
        builder.base_url.assert_called_once_with(f"{LOCAL}/bot")
        builder.base_file_url.assert_called_once_with(f"{LOCAL}/file/bot")
        builder.read_timeout.assert_called_once_with(600)
        builder.write_timeout.assert_called_once_with(600)
        builder.media_write_timeout.assert_called_once_with(600)
        assert f"local Bot API server at {LOCAL}; upload cap 2000 MB" in caplog.text

    def test_cloud_keeps_ptb_defaults(self, tmp_path, caplog):
        with caplog.at_level(logging.INFO, logger="music_downloader.bot.handlers"):
            builder = self._build(tmp_path)
        builder.base_url.assert_not_called()
        builder.base_file_url.assert_not_called()
        builder.read_timeout.assert_not_called()
        builder.write_timeout.assert_not_called()
        builder.media_write_timeout.assert_not_called()
        assert "Telegram's cloud Bot API; upload cap 50 MB" in caplog.text


# ---------------------------------------------------------------------------
# Chat ranking with a 2000 MB cap
# ---------------------------------------------------------------------------


def _flac(name, size):
    return SearchResult(
        username=name, filename=f"\\x\\Bang Bang {name}.flac", size=size, bit_depth=16, sample_rate=44100, length=162
    )


class TestRankingAboveFiftyMb:
    def test_penalty_keeps_growing_above_fifty_mb_up_to_the_cap(self):
        scorer = ResultScorer(chat_size_limit_bytes=2_000_000_000)
        at_50 = scorer._chat_quality_points(_flac("a", 50_000_000))
        assert at_50 == 20.0  # top tier 25 minus the full 5-point size cost
        at_60 = scorer._chat_quality_points(_flac("b", 60_000_000))
        at_900 = scorer._chat_quality_points(_flac("c", 908_000_000))
        at_cap = scorer._chat_quality_points(_flac("d", 2_000_000_000))
        assert at_50 > at_60 > at_900 > at_cap
        assert at_cap == pytest.approx(10.0)  # 5 + 10 points at the cap

    def test_big_cap_57mb_mp3_beats_908mb_hires_flac(self):
        # The live list that showed the flaw: with a 2000 MB cap, eight 908 MB 24/192 FLACs
        # led a 57 MB MP3 320 of the same 23-minute track.
        scorer = ResultScorer(chat_size_limit_bytes=2_000_000_000)
        mp3 = SearchResult(username="m", filename="\\x\\Echoes.mp3", size=57_000_000, bit_rate=320, length=1413)
        flac = SearchResult(
            username="f", filename="\\x\\Echoes.flac", size=908_000_000, bit_depth=24, sample_rate=192000, length=1413
        )
        ranked = scorer.score_results([flac, mp3], _make_track(1_413_000), profile=PROFILE_CHAT)
        assert [r.extension for r in ranked] == ["mp3", "flac"]
        # The curve between 5 MB and 50 MB is the same as with the 50 MB cloud cap.
        cloud = ResultScorer(chat_size_limit_bytes=50_000_000)
        mid = _flac("d", 27_500_000)
        assert scorer._chat_quality_points(mid) == cloud._chat_quality_points(mid) == 22.5

    def test_small_cap_keeps_its_own_curve(self):
        scorer = ResultScorer(chat_size_limit_bytes=20_000_000)
        assert scorer._chat_quality_points(_flac("a", 20_000_000)) == 20.0
        assert scorer._chat_quality_points(_flac("b", 3_000_000)) == 25.0

    def test_only_copies_over_the_cap_sort_last(self):
        config = _make_config(chat_users={CHAT})
        config.telegram_upload_limit_bytes = 2_000_000_000
        bot = _make_bot(config)
        big = _flac("big", 300_000_000)  # fits under 2000 MB: ranked by score, not partitioned
        oversize = _flac("over", 2_100_000_000)
        mp3 = SearchResult(username="mp3", filename="\\x\\Bang Bang.mp3", size=4_000_000, bit_rate=96, length=162)
        bot.slskd.parse_results = MagicMock(return_value=[oversize, mp3, big])
        ranked = bot.pipeline.rank([], _make_track(), PROFILE_CHAT)
        assert [r.username for r in ranked] == ["big", "mp3", "over"]


# ---------------------------------------------------------------------------
# /format
# ---------------------------------------------------------------------------


def _update(user_id=CHAT, chat_id=CHAT):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message = AsyncMock()
    return update


def _callback(data, user_id=CHAT, chat_id=CHAT):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    update.effective_chat.id = chat_id
    return update


class TestFormatSetting:
    def test_default_is_original(self):
        assert _make_bot()._send_format(CHAT) == "original"

    @pytest.mark.asyncio
    async def test_callback_persists_and_leaves_other_settings_alone(self, tmp_path):
        config = _make_config(str(tmp_path))
        config.auto_mode = True
        bot = _make_bot(config)
        bot._set_delivery(CHAT, "chat")
        await bot.handle_callback(_callback("format:mp3", user_id=OTHER), _make_context())
        fresh = _make_bot(config)
        assert fresh._send_format(CHAT) == "mp3"
        assert fresh._delivery_mode(CHAT) == "chat"
        assert fresh._is_auto(CHAT) is True

    @pytest.mark.asyncio
    async def test_unknown_format_is_ignored(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        await bot.handle_callback(_callback("format:wav"), _make_context())
        assert SettingsRepository(bot.db).get_send_format(CHAT) is None

    @pytest.mark.asyncio
    async def test_cmd_format_shows_current_and_every_choice(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path), chat_users={CHAT}))
        bot._set_send_format(CHAT, "opus")
        update = _update()
        await bot.cmd_format(update, _make_context())
        args, kwargs = update.message.reply_text.call_args
        assert "<b>Opus 192 kbps</b>" in args[0]
        assert "library" not in args[0]
        buttons = kwargs["reply_markup"].inline_keyboard[0]
        assert [b.callback_data for b in buttons] == ["format:original", "format:mp3", "format:opus"]
        assert buttons[2].text.startswith("✅")

    @pytest.mark.asyncio
    async def test_library_chat_is_told_the_format_applies_to_chat_delivery(self):
        bot = _make_bot()
        update = _update(user_id=OTHER, chat_id=OTHER)
        await bot.cmd_format(update, _make_context())
        assert "applies when tracks are sent in the chat" in update.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_locked_account_can_pick_a_format_and_stays_locked(self, tmp_path):
        config = _make_config(str(tmp_path), chat_users={CHAT})
        bot = _make_bot(config)
        await bot.handle_callback(_callback("format:opus"), _make_context())
        assert _make_bot(config)._send_format(CHAT) == "opus"
        assert SettingsRepository(bot.db).get_delivery_mode(CHAT) is None
        assert bot._delivery_mode(CHAT) == "chat"
        update = _update()
        await bot.cmd_deliver(update, _make_context())
        args, kwargs = update.message.reply_text.call_args
        assert "fixed" in args[0]
        assert "reply_markup" not in kwargs

    def test_command_in_menu_help_and_handlers(self):
        from music_downloader.bot.handlers import _register_commands, create_bot

        with patch("music_downloader.bot.handlers.Application") as app_cls:
            builder = app_cls.builder.return_value
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            app = create_bot(_make_config())
        commands = {cmd for c in app.add_handler.call_args_list for cmd in getattr(c.args[0], "commands", ())}
        assert "format" in commands

        menu_app = MagicMock()
        menu_app.bot = AsyncMock()
        asyncio.run(_register_commands(menu_app))
        assert "format" in {c.command for c in menu_app.bot.set_my_commands.call_args.args[0]}

    @pytest.mark.asyncio
    async def test_help_lists_format(self):
        update = _update()
        await _make_bot().cmd_start(update, _make_context())
        assert "/format" in update.message.reply_text.call_args.args[0]

    def test_old_database_gets_the_column(self, tmp_path):
        db_path = str(tmp_path / "old.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY, auto_mode INTEGER NOT NULL DEFAULT 0, "
            "delivery_mode TEXT, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        conn.execute("INSERT INTO chat_settings (chat_id, auto_mode, delivery_mode) VALUES (5, 1, 'chat')")
        conn.commit()
        conn.close()
        db = Database(db_path)
        repo = SettingsRepository(db)
        assert repo.get_send_format(5) is None
        repo.set_send_format(5, "mp3", auto_mode=False)
        assert repo.get_send_format(5) == "mp3"
        assert repo.get_delivery_mode(5) == "chat"
        assert repo.get_auto_mode(5) is True
        db.close()


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------


def _fake_ffmpeg(cmd, **kwargs):
    with open(cmd[-1], "wb") as f:
        f.write(b"audio")


class TestTranscodeArgs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fmt", "codec", "bitrate", "suffix"),
        [("mp3", "libmp3lame", "320k", ".mp3"), ("opus", "libopus", "192k", ".ogg")],
    )
    async def test_ffmpeg_args_per_format(self, tmp_path, fmt, codec, bitrate, suffix):
        with patch("subprocess.run", side_effect=_fake_ffmpeg) as run:
            out = await pipeline_fetch.transcode(str(tmp_path / "in.flac"), fmt, "Bang Bang", "Nancy Sinatra")
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-c:a") + 1] == codec
        assert cmd[cmd.index("-b:a") + 1] == bitrate
        assert "title=Bang Bang" in cmd and "artist=Nancy Sinatra" in cmd
        assert out.endswith(suffix) and cmd[-1] == out
        if fmt == "mp3":
            assert cmd[cmd.index("-id3v2_version") + 1] == "3"
        os.unlink(out)

    @pytest.mark.asyncio
    async def test_pipeline_embeds_the_cover_into_the_transcode(self, tmp_path):
        bot = _make_bot()
        bot.pipeline.embed_artwork = AsyncMock()
        with patch("subprocess.run", side_effect=_fake_ffmpeg):
            out = await bot.pipeline.transcode(str(tmp_path / "in.flac"), "mp3", _make_track())
        bot.pipeline.embed_artwork.assert_awaited_once_with(out, _make_track())
        os.unlink(out)

    def test_already_in_format(self, tmp_path):
        junk = _file(tmp_path, "x.ogg", 100)
        assert pipeline_fetch.already_in_format("x.mp3", "mp3", "mp3")
        assert pipeline_fetch.already_in_format("x.opus", "opus", "opus")
        assert not pipeline_fetch.already_in_format("x.flac", "flac", "mp3")
        assert not pipeline_fetch.already_in_format("x.mp3", "mp3", "opus")
        assert not pipeline_fetch.already_in_format(junk, "ogg", "opus")  # not an Ogg Opus stream
        assert pipeline_fetch.already_in_format("x.flac", "flac", "original")


def _format_bot(fmt):
    bot = _chat_bot()
    bot._set_send_format(CHAT, fmt)
    return bot


class TestFormatDelivery:
    @pytest.mark.asyncio
    async def test_flac_sent_as_mp3_with_caption(self, tmp_path):
        bot = _format_bot("mp3")
        source = _file(tmp_path, size=31_000_000)
        _setup_download(bot, source)
        mp3 = _file(tmp_path, "out.mp3", 5000)
        bot.pipeline.transcode = AsyncMock(return_value=mp3)
        bot.pipeline.convert_to_opus = AsyncMock()
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(size=31_000_000), _status_msg())

        bot.pipeline.transcode.assert_awaited_once_with(source, "mp3", _make_track())
        bot.pipeline.convert_to_opus.assert_not_awaited()
        kwargs = context.bot.send_audio.call_args.kwargs
        assert kwargs["filename"] == "Nancy Sinatra - Bang Bang.mp3"
        assert "Sent as MP3 320 kbps (original 31 MB FLAC)" in kwargs["caption"]
        assert "Converted to Opus" not in kwargs["caption"]
        assert not os.path.exists(mp3)
        assert not os.path.exists(source)
        assert bot.history_repo.get_recent(1)[0].filename == "Nancy Sinatra - Bang Bang.mp3"

    @pytest.mark.asyncio
    async def test_file_already_in_the_format_goes_as_is(self, tmp_path):
        bot = _format_bot("mp3")
        source = _file(tmp_path, "track.mp3", size=8_000_000)
        _setup_download(bot, source)
        bot.pipeline.transcode = AsyncMock()
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(size=8_000_000, ext="mp3"), _status_msg())

        bot.pipeline.transcode.assert_not_awaited()
        kwargs = context.bot.send_audio.call_args.kwargs
        assert kwargs["filename"] == "Nancy Sinatra - Bang Bang.mp3"
        assert "Sent as" not in kwargs["caption"]

    @pytest.mark.asyncio
    async def test_original_format_never_transcodes(self, tmp_path):
        bot = _chat_bot()
        _setup_download(bot, _file(tmp_path, size=31_000_000))
        bot.pipeline.transcode = AsyncMock()
        context = _make_context()
        await bot._do_download(context, CHAT, _make_track(), _make_result(), _status_msg())
        bot.pipeline.transcode.assert_not_awaited()
        assert context.bot.send_audio.call_args.kwargs["filename"].endswith(".flac")

    @pytest.mark.asyncio
    async def test_transcode_over_the_cap_falls_back_to_the_opus_ladder(self, tmp_path):
        bot = _format_bot("mp3")
        source = _file(tmp_path, size=40_000_000)
        _setup_download(bot, source)
        too_big = _file(tmp_path, "out.mp3", 50_000_001)
        bot.pipeline.transcode = AsyncMock(return_value=too_big)
        ogg = _file(tmp_path, "out.ogg", 5000)
        bot.pipeline.convert_to_opus = AsyncMock(return_value=ogg)
        context = _make_context()

        await bot._do_download(context, CHAT, _make_track(), _make_result(size=40_000_000), _status_msg())

        bot.pipeline.convert_to_opus.assert_awaited_once_with(source, 192)
        kwargs = context.bot.send_audio.call_args.kwargs
        assert kwargs["filename"].endswith(".ogg")
        assert "Converted to Opus 192 kbps, original 40 MB FLAC" in kwargs["caption"]
        assert "Sent as" not in kwargs["caption"]
        assert not os.path.exists(too_big)

    @pytest.mark.asyncio
    async def test_failed_transcode_sends_the_original(self, tmp_path):
        bot = _format_bot("opus")
        _setup_download(bot, _file(tmp_path, size=10_000_000))
        bot.pipeline.transcode = AsyncMock(return_value=None)
        context = _make_context()
        await bot._do_download(context, CHAT, _make_track(), _make_result(size=10_000_000), _status_msg())
        kwargs = context.bot.send_audio.call_args.kwargs
        assert kwargs["filename"].endswith(".flac")
        assert "Sent as" not in kwargs["caption"]

    @pytest.mark.asyncio
    async def test_library_delivery_ignores_the_format(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        bot.processor = MagicMock()
        bot.processor.build_filename = MagicMock(side_effect=lambda a, t, ext="flac": f"{a} - {t}.{ext}")
        bot._set_send_format(OTHER, "mp3")
        _setup_download(bot, _file(tmp_path, size=10_000_000))
        bot.pipeline.transcode = AsyncMock()
        context = _make_context()
        await bot._do_download(context, OTHER, _make_track(), _make_result(size=10_000_000), _status_msg())
        bot.pipeline.transcode.assert_not_awaited()
        assert context.bot.send_audio.call_args.kwargs["filename"].endswith(".flac")


def test_compose_passes_every_variable_the_bot_reads():
    """docker-compose.yml has an explicit environment map and no env_file:
    a variable missing there never reaches the bot, whatever .env says."""
    import re
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    passed = set(compose["services"]["slskd-importer"]["environment"])
    source = (root / "src" / "music_downloader" / "config.py").read_text()
    read = set(re.findall(r'(?:os\.getenv|_get_required_env)\(\s*"([A-Z_]+)"', source))
    assert "TELEGRAM_UPLOAD_TIMEOUT_SECS" in read and len(read) > 20
    # HEALTH_PORT stays at the image's 8080: the Dockerfile HEALTHCHECK probes that port.
    assert read - passed - {"HEALTH_PORT"} == set()
