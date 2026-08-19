"""Tests for real auto-download mode and unattended imports (v0.12.0)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.bot.handlers import MusicBot, PendingDownload
from music_downloader.bot.keyboards import build_import_confirm_keyboard
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.settings_repo import SettingsRepository
from music_downloader.search.slskd_client import SearchResult


def _make_config(td=None):
    td = td or tempfile.mkdtemp()
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


def _make_bot(config=None):
    with (
        patch("music_downloader.bot.handlers.SpotifyResolver"),
        patch("music_downloader.bot.handlers.SlskdClient"),
    ):
        return MusicBot(config or _make_config())


def _make_track():
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="X",
        duration_ms=162_000,
        spotify_url="u",
        year="1966",
    )


def _make_result(idx=0):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\track{idx}.flac",
        size=30_000_000,
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


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------


class TestSettingsPersistence:
    def test_repo_roundtrip_and_default(self, tmp_path):
        repo = SettingsRepository(Database(str(tmp_path / "db.sqlite")))
        assert repo.get_auto_mode(1) is None
        repo.set_auto_mode(1, True)
        assert repo.get_auto_mode(1) is True
        repo.set_auto_mode(1, False)
        assert repo.get_auto_mode(1) is False

    def test_toggle_survives_bot_restart(self, tmp_path):
        config = _make_config(str(tmp_path))
        bot1 = _make_bot(config)
        bot1._set_auto(67890, True)

        bot2 = _make_bot(config)  # same data_dir = same DB
        assert bot2._is_auto(67890) is True

    def test_config_default_applies_until_toggled(self, tmp_path):
        config = _make_config(str(tmp_path))
        config.auto_mode = True
        bot = _make_bot(config)
        assert bot._is_auto(111) is True  # env default
        bot._set_auto(111, False)  # explicit toggle wins
        assert bot._is_auto(111) is False


# ---------------------------------------------------------------------------
# Auto-download flow
# ---------------------------------------------------------------------------


class TestAutoDownloadFlow:
    @pytest.mark.asyncio
    async def test_auto_search_skips_picker_and_downloads_best(self):
        bot = _make_bot()
        bot._set_auto(67890, True)
        results = [_make_result(0), _make_result(1)]
        bot.slskd.search = AsyncMock(return_value=[{"files": []}])
        bot.slskd.parse_results = MagicMock(return_value=results)
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=results)

        with patch.object(bot, "_launch_download", new_callable=AsyncMock) as mock_launch:
            await bot._do_slskd_search(_make_context(), 67890, _make_track(), AsyncMock(), generation=0)

        mock_launch.assert_awaited_once()
        args = mock_launch.call_args[0]
        assert args[3] == results[0]  # best match
        assert args[4] == 0  # index

    @pytest.mark.asyncio
    async def test_manual_mode_still_shows_picker(self):
        """Positive control: auto off keeps the keyboard flow."""
        bot = _make_bot()
        results = [_make_result(0)]
        bot.slskd.search = AsyncMock(return_value=[{"files": []}])
        bot.slskd.parse_results = MagicMock(return_value=results)
        bot.scorer = MagicMock()
        bot.scorer.score_results = MagicMock(return_value=results)

        with (
            patch.object(bot, "_launch_download", new_callable=AsyncMock) as mock_launch,
            patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            await bot._do_slskd_search(_make_context(), 67890, _make_track(), AsyncMock(), generation=0)

        mock_launch.assert_not_awaited()
        assert mock_edit.call_args.kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_auto_save_processes_without_approval(self, tmp_path):
        bot = _make_bot()
        source = tmp_path / "track.flac"
        source.write_bytes(b"flac")
        pending = PendingDownload(track=_make_track(), result=_make_result(), chat_id=67890, source_path=str(source))
        bot.downloads["abc"] = pending
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value=str(tmp_path / "Nancy Sinatra - Bang Bang.flac"))
        bot.processor.cleanup_download = MagicMock(return_value=True)
        status_msg = AsyncMock()

        with patch.object(bot, "_embed_spotify_artwork", new_callable=AsyncMock):
            await bot._auto_save(67890, "abc", pending, status_msg, "quality", "#1")

        assert "abc" not in bot.downloads
        bot.processor.process_file.assert_called_once()
        records = bot.history_repo.get_recent(5)
        assert records and records[0].status == "success"
        assert "Auto-saved" in status_msg.edit_text.call_args[0][0]


# ---------------------------------------------------------------------------
# Unattended imports
# ---------------------------------------------------------------------------


class TestImportAutoSave:
    def test_confirm_keyboard_offers_both_modes(self):
        kb = build_import_confirm_keyboard(7)
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "ic:7" in callbacks
        assert "ic:7:auto" in callbacks
        assert "ix:7" in callbacks

    @pytest.mark.asyncio
    async def test_auto_import_saves_and_continues(self, tmp_path):
        bot = _make_bot()
        bot._import_auto[67890] = True
        bot.import_repo = MagicMock()
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value=str(tmp_path / "out.flac"))
        bot.processor.cleanup_download = MagicMock(return_value=True)

        with (
            patch.object(bot, "_embed_spotify_artwork", new_callable=AsyncMock),
            patch.object(bot, "_process_next_import_track", new_callable=AsyncMock) as mock_next,
            patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        ):
            await bot._import_auto_save(
                _make_context(), 67890, 1, 2, "dl1", _make_track(), _make_result(), "/tmp/src.flac", AsyncMock(), 0
            )

        from music_downloader.persistence.import_repo import TrackStatus

        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.completed)
        mock_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_import_failure_marks_failed_and_continues(self):
        bot = _make_bot()
        bot._import_auto[67890] = True
        bot.import_repo = MagicMock()

        with (
            patch.object(bot, "_process_next_import_track", new_callable=AsyncMock) as mock_next,
            patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        ):
            await bot._import_auto_fail(
                _make_context(), 67890, 1, 2, "dl1", AsyncMock(), 0, "❌ failed — continuing.", "Timeout"
            )

        from music_downloader.persistence.import_repo import TrackStatus

        bot.import_repo.complete_track.assert_called_once_with(1, 2, TrackStatus.failed, "Timeout")
        mock_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_shows_import_progress(self):
        bot = _make_bot()
        bot._active_import[67890] = 5
        bot._import_auto[67890] = True
        bot.import_repo = MagicMock()
        bot.import_repo.get_job_progress = MagicMock(return_value=(3, 1, 0, 10))

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 67890
        update.message = AsyncMock()
        update.effective_message = update.message

        await bot.cmd_status(update, _make_context())

        text = update.message.reply_text.call_args[0][0]
        assert "Import (auto-save)" in text
        assert "4/10" in text


class TestAutoBranchCoverage:
    @pytest.mark.asyncio
    async def test_auto_save_failure_records_process_failed(self, tmp_path):
        bot = _make_bot()
        source = tmp_path / "t.flac"
        source.write_bytes(b"x")
        pending = PendingDownload(track=_make_track(), result=_make_result(), chat_id=67890, source_path=str(source))
        bot.downloads["zz"] = pending
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value=None)  # save fails
        status_msg = AsyncMock()

        await bot._auto_save(67890, "zz", pending, status_msg, "q", "#1")

        records = bot.history_repo.get_recent(5)
        assert records and records[0].status == "process_failed"
        assert "failed to save" in status_msg.edit_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_import_download_auto_enqueue_failure_routes_to_auto_fail(self):
        bot = _make_bot()
        bot._import_auto[67890] = True
        bot.slskd.enqueue_download = MagicMock(return_value=False)

        with patch.object(bot, "_import_auto_fail", new_callable=AsyncMock) as mock_fail:
            await bot._do_import_download(
                _make_context(),
                67890,
                _make_track(),
                _make_result(),
                AsyncMock(),
                0,
                job_id=1,
                track_id=2,
                dl_id="d1",
            )
        mock_fail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_import_download_auto_timeout_routes_to_auto_fail(self):
        bot = _make_bot()
        bot._import_auto[67890] = True
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock(return_value=None)  # timeout

        with patch.object(bot, "_import_auto_fail", new_callable=AsyncMock) as mock_fail:
            await bot._do_import_download(
                _make_context(),
                67890,
                _make_track(),
                _make_result(),
                AsyncMock(),
                0,
                job_id=1,
                track_id=2,
                dl_id="d1",
            )
        mock_fail.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_import_download_auto_success_routes_to_auto_save(self, tmp_path):
        from music_downloader.search.slskd_client import DownloadStatus

        bot = _make_bot()
        bot._import_auto[67890] = True
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock(
            return_value=DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        )
        source = tmp_path / "s.flac"
        source.write_bytes(b"x")
        bot.processor = MagicMock()
        bot.processor.find_downloaded_file = MagicMock(return_value=str(source))
        bot.downloads["d1"] = PendingDownload(track=_make_track(), result=_make_result(), chat_id=67890)

        with patch.object(bot, "_import_auto_save", new_callable=AsyncMock) as mock_save:
            await bot._do_import_download(
                _make_context(),
                67890,
                _make_track(),
                _make_result(),
                AsyncMock(),
                0,
                job_id=1,
                track_id=2,
                dl_id="d1",
            )
        mock_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_import_auto_save_failure_marks_failed_and_continues(self):
        bot = _make_bot()
        bot.import_repo = MagicMock()
        bot.processor = MagicMock()
        bot.processor.process_file = MagicMock(return_value=None)

        with (
            patch.object(bot, "_process_next_import_track", new_callable=AsyncMock) as mock_next,
            patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)),
        ):
            await bot._import_auto_save(
                _make_context(), 67890, 1, 2, "d1", _make_track(), _make_result(), "/tmp/s.flac", AsyncMock(), 0
            )

        from music_downloader.persistence.import_repo import TrackStatus

        assert bot.import_repo.complete_track.call_args[0][2] == TrackStatus.failed
        mock_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ic_auto_callback_sets_unattended_mode(self):
        bot = _make_bot()
        bot.import_repo = MagicMock()
        bot.import_repo.get_job_for_chat = MagicMock(return_value=MagicMock(id=1))

        update = MagicMock()
        update.callback_query = AsyncMock()
        context = _make_context()
        context.application.create_task = MagicMock(return_value=MagicMock())

        with patch("music_downloader.bot.handlers.asyncio.to_thread", side_effect=lambda fn, *a, **k: fn(*a, **k)):
            await bot._handle_import_callback(update, context, 67890, "ic:1:auto")

        assert bot._import_auto.get(67890) is True

    @pytest.mark.asyncio
    async def test_do_download_auto_routes_to_auto_save(self, tmp_path):
        from music_downloader.search.slskd_client import DownloadStatus

        bot = _make_bot()
        bot._set_auto(67890, True)
        bot.slskd.enqueue_download = MagicMock(return_value=True)
        bot.slskd.wait_for_download = AsyncMock(
            return_value=DownloadStatus(username="u", filename="f", state="Completed, Succeeded")
        )
        source = tmp_path / "s.flac"
        source.write_bytes(b"x")
        bot.processor = MagicMock()
        bot.processor.find_downloaded_file = MagicMock(return_value=str(source))
        bot.processor.build_filename = MagicMock(return_value="n.flac")

        with patch.object(bot, "_auto_save", new_callable=AsyncMock) as mock_auto:
            await bot._do_download(_make_context(), 67890, _make_track(), _make_result(), AsyncMock(), 0, "sid")
        mock_auto.assert_awaited_once()
