"""Tests for MusicBot download flow, slskd search, large file handling, and create_bot."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.bot.handlers import MusicBot, create_bot
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.processor.flac_analyzer import FlacVerdict
from music_downloader.search.slskd_client import DownloadStatus, SearchResult

_tmp_dir = None


def _get_tmp_dir():
    global _tmp_dir
    if _tmp_dir is None:
        _tmp_dir = tempfile.mkdtemp()
    return _tmp_dir


def _make_config():
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
    config.download_dir = os.path.join(td, "downloads2")
    config.output_dir = os.path.join(td, "music2")
    config.filename_template = "{artist} - {title}"
    config.search_timeout_secs = 30
    config.download_timeout_secs = 600
    return config


def _make_track():
    return TrackInfo(
        artist="Nancy Sinatra", title="Bang Bang",
        album="How Does That Grab You?", duration_ms=162_000,
        spotify_url="https://open.spotify.com/track/xxx", year="1966",
    )


def _make_result(idx=0, ext="flac"):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {idx}.{ext}",
        size=30_000_000, bit_rate=900, bit_depth=16, sample_rate=44100,
        length=162, has_free_slot=True, upload_speed=1_000_000, queue_length=0,
    )


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock()
    context.bot.send_audio = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.bot.edit_message_reply_markup = AsyncMock()
    context.bot.edit_message_caption = AsyncMock()
    context.bot.edit_message_text = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(return_value=MagicMock())
    return context


class TestDoSlskdSearch:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_no_results_all_fallbacks(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[])
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=[])
        bot._chat_generation[123] = 0

        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.message_id = 1

        context = _make_context()
        await bot._do_slskd_search(context, 123, _make_track(), msg, 0)
        # Should have tried multiple fallback searches

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_finds_flac_results(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[{"responses": []}])
        results = [_make_result(0), _make_result(1)]
        bot.slskd.parse_results = MagicMock(return_value=results)
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=results)
        bot._chat_generation[123] = 0

        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.message_id = 1

        context = _make_context()
        await bot._do_slskd_search(context, 123, _make_track(), msg, 0)
        assert 123 in bot.pending

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_non_flac_fallback(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        call_count = 0

        def parse_side_effect(responses, flac_only=True):
            nonlocal call_count
            call_count += 1
            if not flac_only and call_count >= 2:
                return [_make_result(0, "mp3")]
            return []

        bot.slskd.parse_results = MagicMock(side_effect=parse_side_effect)
        score_count = 0

        def score_side_effect(results, track, **kwargs):
            nonlocal score_count
            score_count += 1
            return results if results else []

        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(side_effect=score_side_effect)
        bot._chat_generation[123] = 0

        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.message_id = 1

        context = _make_context()
        await bot._do_slskd_search(context, 123, _make_track(), msg, 0)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_stale_aborts_early(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[])
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=[])
        bot._chat_generation[123] = 5  # generation is ahead

        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.message_id = 1

        context = _make_context()
        await bot._do_slskd_search(context, 123, _make_track(), msg, 0)
        assert 123 not in bot.pending

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_exception_handled(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = AsyncMock()
        bot.slskd.search = AsyncMock(side_effect=Exception("Network error"))
        bot._chat_generation[123] = 0

        msg = AsyncMock()
        msg.edit_text = AsyncMock()
        msg.message_id = 1

        context = _make_context()
        await bot._do_slskd_search(context, 123, _make_track(), msg, 0)
        # Should not raise


class TestDoDownload:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_enqueue_fails(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=False)

        status_msg = AsyncMock()
        status_msg.edit_text = AsyncMock()
        status_msg.message_id = 1

        context = _make_context()
        await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_failed_status(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        failed_status = DownloadStatus(username="u", filename="f", state="Errored")
        bot.slskd.wait_for_download = AsyncMock(return_value=failed_status)

        status_msg = AsyncMock()
        status_msg.edit_text = AsyncMock()
        status_msg.message_id = 1

        context = _make_context()
        await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)
        assert any(h["status"] == "failed" for h in bot.history)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_timeout(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock(return_value=None)

        status_msg = AsyncMock()
        status_msg.edit_text = AsyncMock()
        status_msg.message_id = 1

        context = _make_context()
        await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_file_not_found_on_disk(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        completed = DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        bot.slskd.wait_for_download = AsyncMock(return_value=completed)
        bot.processor = MagicMock()
        bot.processor.find_downloaded_file = MagicMock(return_value=None)

        status_msg = AsyncMock()
        status_msg.edit_text = AsyncMock()
        status_msg.message_id = 1

        context = _make_context()
        await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)
        assert any(h["status"] == "file_not_found" for h in bot.history)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_successful_download_small_file(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        completed = DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        bot.slskd.wait_for_download = AsyncMock(return_value=completed)

        # Create a real small temp file
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            f.write(b"\x00" * 1000)
            source_path = f.name

        try:
            bot.processor = MagicMock()
            bot.processor.find_downloaded_file = MagicMock(return_value=source_path)
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.flac")
            bot._analyze_flac = AsyncMock(return_value=FlacVerdict(
                verdict="AUTHENTIC", cutoff_khz=22.05, nyquist_khz=22.05,
                sample_rate=44100, bit_depth=16,
            ))

            status_msg = AsyncMock()
            status_msg.edit_text = AsyncMock()
            status_msg.message_id = 1

            sent_msg = AsyncMock()
            sent_msg.message_id = 2

            context = _make_context()
            context.bot.send_audio = AsyncMock(return_value=sent_msg)

            result = _make_result()
            await bot._do_download(context, 123, _make_track(), result, status_msg)
            context.bot.send_audio.assert_called_once()
        finally:
            os.unlink(source_path)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_send_audio_bad_request_falls_back_to_document(self, mock_slskd_cls, mock_spotify):
        from telegram.error import BadRequest

        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        completed = DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        bot.slskd.wait_for_download = AsyncMock(return_value=completed)

        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            f.write(b"\x00" * 1000)
            source_path = f.name

        try:
            bot.processor = MagicMock()
            bot.processor.find_downloaded_file = MagicMock(return_value=source_path)
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.flac")
            bot._analyze_flac = AsyncMock(return_value=None)

            status_msg = AsyncMock()
            status_msg.edit_text = AsyncMock()
            status_msg.message_id = 1

            sent_msg = AsyncMock()
            sent_msg.message_id = 2

            context = _make_context()
            context.bot.send_audio = AsyncMock(side_effect=BadRequest("file too large"))
            context.bot.send_document = AsyncMock(return_value=sent_msg)

            await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)
            context.bot.send_document.assert_called_once()
        finally:
            os.unlink(source_path)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_download_exception(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(side_effect=Exception("unexpected"))

        status_msg = AsyncMock()
        status_msg.edit_text = AsyncMock()
        status_msg.message_id = 1

        context = _make_context()
        await bot._do_download(context, 123, _make_track(), _make_result(), status_msg)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_non_flac_skips_analysis(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot.slskd = MagicMock()
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        completed = DownloadStatus(username="u", filename="f", state="Completed")
        bot.slskd.wait_for_download = AsyncMock(return_value=completed)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"\x00" * 1000)
            source_path = f.name

        try:
            bot.processor = MagicMock()
            bot.processor.find_downloaded_file = MagicMock(return_value=source_path)
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.mp3")
            bot._analyze_flac = AsyncMock()

            status_msg = AsyncMock()
            status_msg.edit_text = AsyncMock()
            status_msg.message_id = 1

            sent_msg = AsyncMock()
            sent_msg.message_id = 2
            context = _make_context()
            context.bot.send_audio = AsyncMock(return_value=sent_msg)

            mp3_result = _make_result(ext="mp3")
            await bot._do_download(context, 123, _make_track(), mp3_result, status_msg)
            bot._analyze_flac.assert_not_called()
        finally:
            os.unlink(source_path)


class TestSendLargeFile:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_ogg_conversion_success_small_enough(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"\x00" * 1000)
            ogg_path = f.name

        try:
            bot._convert_to_ogg = AsyncMock(return_value=ogg_path)
            bot.processor = MagicMock()
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.ogg")

            sent_msg = AsyncMock()
            sent_msg.message_id = 2
            context = _make_context()
            context.bot.send_audio = AsyncMock(return_value=sent_msg)

            await bot._send_large_file(
                context, 123, _make_track(), _make_result(),
                "/fake/source.flac", 60_000_000, "Quality line", "#1", "dl1",
            )
            context.bot.send_audio.assert_called_once()
        finally:
            if os.path.exists(ogg_path):
                os.unlink(ogg_path)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_ogg_too_large_falls_back_to_preview(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())

        # Create a fake "large" OGG (we'll mock the size check)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"\x00" * 100)
            ogg_path = f.name

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"\x00" * 100)
            preview_path = f.name

        try:
            bot._convert_to_ogg = AsyncMock(return_value=ogg_path)
            bot._create_preview = AsyncMock(return_value=preview_path)
            bot.processor = MagicMock()
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.ogg")

            sent_msg = AsyncMock()
            sent_msg.message_id = 2
            context = _make_context()
            context.bot.send_audio = AsyncMock(return_value=sent_msg)

            # Patch os.path.getsize to return large size for OGG
            orig_getsize = os.path.getsize
            def mock_getsize(path):
                if path == ogg_path:
                    return 60_000_000  # > 50MB limit
                return orig_getsize(path)

            with patch("music_downloader.bot.handlers.os.path.getsize", side_effect=mock_getsize):
                await bot._send_large_file(
                    context, 123, _make_track(), _make_result(),
                    "/fake/source.flac", 70_000_000, "Quality", "#1", "dl1",
                )
        finally:
            for p in (ogg_path, preview_path):
                if os.path.exists(p):
                    os.unlink(p)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_ogg_conversion_fails_uses_preview(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"\x00" * 100)
            preview_path = f.name

        try:
            bot._convert_to_ogg = AsyncMock(return_value=None)
            bot._create_preview = AsyncMock(return_value=preview_path)
            bot.processor = MagicMock()
            bot.processor.build_filename = MagicMock(return_value="Artist - Song.ogg")

            sent_msg = AsyncMock()
            sent_msg.message_id = 2
            context = _make_context()
            context.bot.send_audio = AsyncMock(return_value=sent_msg)

            await bot._send_large_file(
                context, 123, _make_track(), _make_result(),
                "/fake/source.flac", 60_000_000, "Quality", "#1", "dl1",
            )
        finally:
            if os.path.exists(preview_path):
                os.unlink(preview_path)

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_both_conversions_fail(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        bot._convert_to_ogg = AsyncMock(return_value=None)
        bot._create_preview = AsyncMock(return_value=None)

        sent_msg = AsyncMock()
        sent_msg.message_id = 2
        context = _make_context()
        context.bot.send_message = AsyncMock(return_value=sent_msg)

        await bot._send_large_file(
            context, 123, _make_track(), _make_result(),
            "/fake/source.flac", 60_000_000, "Quality", "#1", "dl1",
        )
        context.bot.send_message.assert_called_once()


class TestAsyncHelpers:
    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_analyze_flac_runs(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.analyze_flac") as mock_analyze:
            mock_analyze.return_value = FlacVerdict(
                verdict="AUTHENTIC", cutoff_khz=22.05, nyquist_khz=22.05,
                sample_rate=44100, bit_depth=16,
            )
            result = await MusicBot._analyze_flac("/fake/path.flac")
            assert result is not None
            assert result.verdict == "AUTHENTIC"

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_analyze_flac_exception(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.analyze_flac") as mock_analyze:
            mock_analyze.side_effect = Exception("read error")
            result = await MusicBot._analyze_flac("/fake/path.flac")
            assert result is None

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_convert_to_ogg_runs(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.convert_to_ogg") as mock_conv:
            mock_conv.return_value = "/tmp/output.ogg"
            result = await MusicBot._convert_to_ogg("/fake/path.flac")
            assert result == "/tmp/output.ogg"

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_convert_to_ogg_exception(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.convert_to_ogg") as mock_conv:
            mock_conv.side_effect = Exception("ffmpeg error")
            result = await MusicBot._convert_to_ogg("/fake/path.flac")
            assert result is None

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_create_preview_runs(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.create_preview_clip") as mock_clip:
            mock_clip.return_value = "/tmp/preview.ogg"
            result = await MusicBot._create_preview("/fake/path.flac", 60.0)
            assert result == "/tmp/preview.ogg"

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_create_preview_exception(self, mock_slskd_cls, mock_spotify):
        with patch("music_downloader.bot.handlers.create_preview_clip") as mock_clip:
            mock_clip.side_effect = Exception("ffmpeg error")
            result = await MusicBot._create_preview("/fake/path.flac")
            assert result is None

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_embed_spotify_artwork_success(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        with patch("music_downloader.bot.handlers.fetch_spotify_artwork") as mock_fetch:
            mock_fetch.return_value = b"\xff\xd8\xff\xe0"
            with patch("music_downloader.bot.handlers.embed_artwork_into_file") as mock_embed:
                mock_embed.return_value = True
                await bot._embed_spotify_artwork("/fake/path.flac", _make_track())

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_embed_spotify_artwork_no_art(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        with patch("music_downloader.bot.handlers.fetch_spotify_artwork") as mock_fetch:
            mock_fetch.return_value = None
            await bot._embed_spotify_artwork("/fake/path.flac", _make_track())

    @patch("music_downloader.bot.handlers.SpotifyResolver")
    @patch("music_downloader.bot.handlers.SlskdClient")
    @pytest.mark.asyncio
    async def test_embed_spotify_artwork_exception(self, mock_slskd_cls, mock_spotify):
        bot = MusicBot(_make_config())
        with patch("music_downloader.bot.handlers.fetch_spotify_artwork") as mock_fetch:
            mock_fetch.side_effect = Exception("network error")
            # Should not raise
            await bot._embed_spotify_artwork("/fake/path.flac", _make_track())


class TestCreateBot:
    def test_creates_application(self):
        config = _make_config()
        with patch("music_downloader.bot.handlers.Application") as mock_app_cls:
            mock_builder = MagicMock()
            mock_app = MagicMock()
            mock_builder.token.return_value = mock_builder
            mock_builder.build.return_value = mock_app
            mock_app_cls.builder.return_value = mock_builder

            app = create_bot(config)
            assert app is mock_app
            mock_app.add_handler.assert_called()
            # Should have 5 command handlers + 1 callback + 1 message = 7
            assert mock_app.add_handler.call_count == 7
