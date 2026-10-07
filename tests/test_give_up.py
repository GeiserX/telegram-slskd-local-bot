"""A transfer the bot gives up on is cancelled in slskd, and what of it landed is deleted.

Seen in production: the download timed out, slskd kept transferring, and the
files landed later with nobody to pick them up. Leftovers are swept after
ORPHAN_SWEEP_HOURS, apart from DOWNLOAD_CLEANUP_HOURS (downloads waiting on a button).
"""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.pipeline import fetch as pipeline_fetch
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.slskd_client import DownloadStatus, SearchResult, SlskdClient

REMOTE = "\\Music\\Album\\01 - Track.flac"


def _result():
    return SearchResult(username="peer", filename=REMOTE, size=30_000_000, length=200)


def _slskd(wait):
    slskd = MagicMock(spec=SlskdClient)
    slskd.enqueue_download.return_value = True
    slskd.wait_for_download = AsyncMock(side_effect=wait)
    slskd.cancel_downloads.return_value = 1
    return slskd


def _processor(tmp_path):
    return FileProcessor(str(tmp_path / "downloads"), str(tmp_path / "music"))


def _landed(tmp_path, folder: str = "Album", age_secs: float = 0.0) -> str:
    """A file where slskd puts REMOTE: <downloads>/<remote folder name>/<file>."""
    path = tmp_path / "downloads" / folder / "01 - Track.flac"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + b"\0" * 64)
    if age_secs:
        old = time.time() - age_secs
        os.utime(path, (old, old))
    return str(path)


class TestCancelDownloads:
    def _client(self, directories):
        with patch("slskd_api.SlskdClient"):
            client = SlskdClient("http://localhost:5030", "k")
        client.client.transfers.get_downloads = MagicMock(return_value={"directories": directories})
        client.client.transfers.cancel_download = MagicMock(return_value=True)
        return client

    def test_cancels_every_transfer_of_the_file_with_remove_true(self):
        client = self._client(
            [
                {"files": [{"filename": REMOTE, "id": "old", "state": "Completed, TimedOut"}]},
                {"files": [{"filename": REMOTE, "id": "new", "state": "InProgress"}, {"filename": "\\x", "id": "x"}]},
            ]
        )
        assert client.cancel_downloads("peer", REMOTE) == 2
        assert [c.args + (c.kwargs["remove"],) for c in client.client.transfers.cancel_download.call_args_list] == [
            ("peer", "old", True),
            ("peer", "new", True),
        ]

    def test_one_failing_cancel_does_not_stop_the_others_and_nothing_raises(self):
        client = self._client([{"files": [{"filename": REMOTE, "id": "a"}, {"filename": REMOTE, "id": "b"}]}])
        client.client.transfers.cancel_download.side_effect = [ConnectionError("down"), True]
        assert client.cancel_downloads("peer", REMOTE) == 1

    def test_slskd_down_returns_zero(self):
        client = self._client([])
        client.client.transfers.get_downloads.side_effect = ConnectionError("down")
        assert client.cancel_downloads("peer", REMOTE) == 0


class TestFetchGivesUp:
    async def test_a_timeout_cancels_the_transfer_and_deletes_what_landed(self, tmp_path):
        landed = []

        async def wait(**kw):
            landed.append(_landed(tmp_path))  # slskd finishes just as the bot gives up
            return None

        slskd = _slskd(wait)
        outcome = await pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
        assert (outcome.error, outcome.state) == (DOWNLOAD_FAILED, "Timeout")
        slskd.cancel_downloads.assert_called_once_with("peer", REMOTE)
        assert not os.path.exists(landed[0])

    async def test_an_older_copy_of_the_same_file_is_left_alone(self, tmp_path):
        # Still waiting on a Save button from an earlier download: not this transfer's file.
        older = _landed(tmp_path, age_secs=3600)
        slskd = _slskd(AsyncMock(return_value=None))
        await pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
        slskd.cancel_downloads.assert_called_once()
        assert os.path.exists(older)

    async def test_a_file_of_the_same_name_from_another_folder_is_left_alone(self, tmp_path):
        # Another download landing meanwhile ("01 - Track.flac" of a different album).
        other = []

        async def wait(**kw):
            other.append(_landed(tmp_path, folder="Other Album"))
            return None

        slskd = _slskd(wait)
        await pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
        slskd.cancel_downloads.assert_called_once()
        assert os.path.exists(other[0])

    async def test_a_failed_transfer_is_dropped_from_slskd(self, tmp_path):
        slskd = _slskd(AsyncMock(return_value=DownloadStatus("peer", REMOTE, "Completed, Errored")))
        outcome = await pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
        assert outcome.state == "Completed, Errored"
        slskd.cancel_downloads.assert_called_once_with("peer", REMOTE)

    async def test_a_cancelled_fetch_cancels_the_transfer_without_waiting_on_slskd(self, tmp_path):
        started = asyncio.Event()

        async def wait(**kw):
            started.set()
            await asyncio.sleep(3600)

        slskd = _slskd(wait)
        threads = []
        real = pipeline_fetch.abandon_in_background

        def capture(*args):
            threads.append(real(*args))
            return threads[-1]

        with patch.object(pipeline_fetch, "abandon_in_background", side_effect=capture):
            task = asyncio.create_task(
                pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
            )
            await asyncio.wait_for(started.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        [thread] = threads
        thread.join(5)
        slskd.cancel_downloads.assert_called_once_with("peer", REMOTE)

    async def test_a_finished_download_is_not_cancelled(self, tmp_path):
        path = _landed(tmp_path)
        slskd = _slskd(AsyncMock(return_value=DownloadStatus("peer", REMOTE, "Completed, Succeeded", transfer_id="t")))
        outcome = await pipeline_fetch.fetch(slskd, _processor(tmp_path), _result(), 60, AsyncMock(), None)
        assert outcome.ok and outcome.path == path
        slskd.cancel_downloads.assert_not_called()
        assert os.path.exists(path)


class TestSweepSettings:
    _REQUIRED = {
        "TELEGRAM_BOT_TOKEN": "t",
        "SPOTIFY_CLIENT_ID": "i",
        "SPOTIFY_CLIENT_SECRET": "s",
        "SLSKD_HOST": "http://x",
        "SLSKD_API_KEY": "k",
    }

    def _config(self, monkeypatch, **extra):
        for name in ("ORPHAN_SWEEP_HOURS", "LOSSLESS_GATE", "LOSSLESS_GATE_MAX_REJECTIONS"):
            monkeypatch.delenv(name, raising=False)
        for k, v in {**self._REQUIRED, **extra}.items():
            monkeypatch.setenv(k, v)
        from music_downloader.config import Config

        return Config()

    def test_orphan_sweep_hours_defaults_to_6_apart_from_the_button_expiry(self, monkeypatch):
        config = self._config(monkeypatch)
        assert (config.orphan_sweep_hours, config.download_cleanup_hours) == (6, 24)
        assert self._config(monkeypatch, ORPHAN_SWEEP_HOURS="0").orphan_sweep_hours == 0
        assert self._config(monkeypatch, ORPHAN_SWEEP_HOURS="-3").orphan_sweep_hours == 0
        assert self._config(monkeypatch, ORPHAN_SWEEP_HOURS="").orphan_sweep_hours == 6

    def test_lossless_gate_is_on_by_default_and_false_turns_it_off(self, monkeypatch):
        config = self._config(monkeypatch)
        assert (config.lossless_gate, config.lossless_gate_max_rejections) == (True, 3)
        assert self._config(monkeypatch, LOSSLESS_GATE="false").lossless_gate is False
        assert self._config(monkeypatch, LOSSLESS_GATE="FALSE").lossless_gate is False
        assert self._config(monkeypatch, LOSSLESS_GATE_MAX_REJECTIONS="5").lossless_gate_max_rejections == 5

    async def test_the_pipeline_sweeps_files_after_orphan_sweep_hours(self):
        from tests.test_chat_delivery import _make_bot, _make_config

        config = _make_config()
        config.orphan_sweep_hours, config.download_cleanup_hours = 6, 24
        bot = _make_bot(config)
        with patch("music_downloader.pipeline._library.orphan_sweep_loop", new_callable=AsyncMock) as loop:
            await bot.pipeline.orphan_sweep_loop(set)
        assert loop.await_args.args[1] == 6

    async def test_the_sweep_runs_when_only_orphan_sweep_hours_is_set(self):
        from music_downloader.bot.handlers import create_bot
        from tests.test_orphan_sweep import _handlers_config

        config = _handlers_config()
        config.download_cleanup_hours, config.orphan_sweep_hours = 0, 6
        with (
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as mock_app_cls,
        ):
            builder = MagicMock()
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            mock_app_cls.builder.return_value = builder
            create_bot(config)
            post_init = builder.post_init.call_args[0][0]
            post_shutdown = builder.post_shutdown.call_args[0][0]
        app = MagicMock()
        app.bot = AsyncMock()
        before = asyncio.all_tasks()
        await post_init(app)
        started = asyncio.all_tasks() - before
        assert "_orphan_sweep_loop" in {t.get_coro().__name__ for t in started}
        await post_shutdown(app)
