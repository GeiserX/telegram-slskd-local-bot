"""Tests for the automatic orphan sweep of the downloads directory."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.processor.file_handler import FileProcessor


def _handlers_config():
    """Config shaped for MusicBot construction (mirrors the handlers test fixtures)."""
    import tempfile
    from unittest.mock import MagicMock

    td = tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "i"
    config.spotify_client_secret = "s"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "k"
    config.telegram_allowed_users = {12345}
    config.auto_mode = False
    config.max_results = 5
    config.duration_tolerance_secs = 5
    config.exclude_keywords = []
    config.download_dir = os.path.join(td, "downloads")
    config.output_dir = os.path.join(td, "music")
    config.data_dir = os.path.join(td, "data")
    config.filename_template = "{artist} - {title}"
    config.search_timeout_secs = 30
    config.download_timeout_secs = 600
    config.download_cleanup_hours = 24
    return config


def _make_processor(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return FileProcessor(download_dir=str(downloads), output_dir=str(tmp_path / "music")), downloads


def _make_old_file(path, hours_old=48, content=b"flac-bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - hours_old * 3600
    os.utime(path, (old, old))
    return path


class TestSweepOrphans:
    def test_deletes_old_keeps_fresh(self, tmp_path):
        proc, downloads = _make_processor(tmp_path)
        old = _make_old_file(downloads / "user1" / "old.flac")
        fresh = downloads / "user2" / "fresh.flac"
        fresh.parent.mkdir()
        fresh.write_bytes(b"downloading")

        deleted, freed = proc.sweep_orphans(24)

        assert deleted == 1
        assert freed == len(b"flac-bytes")
        assert not old.exists()
        assert fresh.exists(), "a file inside the TTL window must survive"

    def test_protected_paths_survive_regardless_of_age(self, tmp_path):
        proc, downloads = _make_processor(tmp_path)
        inflight = _make_old_file(downloads / "user1" / "inflight.flac", hours_old=72)

        deleted, _ = proc.sweep_orphans(24, protected_paths={str(inflight)})

        assert deleted == 0
        assert inflight.exists()

    def test_prunes_empty_user_dirs_but_never_the_root(self, tmp_path):
        proc, downloads = _make_processor(tmp_path)
        _make_old_file(downloads / "user1" / "album" / "old.flac")

        proc.sweep_orphans(24)

        assert not (downloads / "user1").exists(), "emptied per-user tree should be pruned"
        assert downloads.exists(), "the downloads root itself must survive"

    def test_zero_hours_disables_sweep(self, tmp_path):
        proc, downloads = _make_processor(tmp_path)
        old = _make_old_file(downloads / "old.flac")

        assert proc.sweep_orphans(0) == (0, 0)
        assert old.exists()

    def test_symlinks_are_skipped(self, tmp_path):
        proc, downloads = _make_processor(tmp_path)
        outside = tmp_path / "precious.flac"
        outside.write_bytes(b"library file")
        link = downloads / "link.flac"
        link.symlink_to(outside)
        old = time.time() - 72 * 3600
        os.utime(link, (old, old), follow_symlinks=False)

        deleted, _ = proc.sweep_orphans(24)

        assert deleted == 0
        assert outside.exists()

    def test_missing_download_dir_is_a_noop(self, tmp_path):
        proc = FileProcessor(download_dir=str(tmp_path / "nope"), output_dir=str(tmp_path / "music"))
        assert proc.sweep_orphans(24) == (0, 0)


class TestCleanupHoursConfig:
    _REQUIRED = {
        "TELEGRAM_BOT_TOKEN": "t",
        "SPOTIFY_CLIENT_ID": "i",
        "SPOTIFY_CLIENT_SECRET": "s",
        "SLSKD_HOST": "http://x",
        "SLSKD_API_KEY": "k",
    }

    def _config(self, monkeypatch, **extra):
        for k, v in {**self._REQUIRED, **extra}.items():
            monkeypatch.setenv(k, v)
        from music_downloader.config import Config

        return Config()

    def test_default_is_24(self, monkeypatch):
        monkeypatch.delenv("DOWNLOAD_CLEANUP_HOURS", raising=False)
        assert self._config(monkeypatch).download_cleanup_hours == 24

    def test_zero_disables_and_negatives_clamp(self, monkeypatch):
        assert self._config(monkeypatch, DOWNLOAD_CLEANUP_HOURS="0").download_cleanup_hours == 0
        assert self._config(monkeypatch, DOWNLOAD_CLEANUP_HOURS="-5").download_cleanup_hours == 0


class TestSweepLoopLifecycle:
    @pytest.mark.asyncio
    async def test_loop_sweeps_and_survives_errors(self):
        """One failing sweep must not kill the hourly loop."""
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
        ):
            from music_downloader.bot.handlers import MusicBot

            bot = MusicBot(_handlers_config())
        bot.processor = MagicMock()
        bot.processor.sweep_orphans = MagicMock(side_effect=[RuntimeError("boom"), (2, 1024)])

        with patch("music_downloader.bot.handlers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            with pytest.raises(asyncio.CancelledError):
                await bot._orphan_sweep_loop()

        assert bot.processor.sweep_orphans.call_count == 2

    @pytest.mark.asyncio
    async def test_post_init_starts_task_and_post_shutdown_cancels_it(self):
        from music_downloader.bot.handlers import create_bot

        config = _handlers_config()
        with (
            patch("music_downloader.bot.handlers.SpotifyResolver"),
            patch("music_downloader.bot.handlers.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as mock_app_cls,
        ):
            mock_builder = MagicMock()
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(mock_builder, chain).return_value = mock_builder
            mock_builder.build.return_value = MagicMock()
            mock_app_cls.builder.return_value = mock_builder
            create_bot(config)
            post_init = mock_builder.post_init.call_args[0][0]
            post_shutdown = mock_builder.post_shutdown.call_args[0][0]

        app = MagicMock()
        app.bot = AsyncMock()

        before = asyncio.all_tasks()
        await post_init(app)
        started = asyncio.all_tasks() - before
        assert len(started) == 1, "post_init must start exactly the sweep task"

        await post_shutdown(app)
        assert started.pop().cancelled(), "post_shutdown must cancel the sweep task"
