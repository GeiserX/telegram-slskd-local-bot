"""Extended tests for __main__ - covering run_health_server and cmd_run."""

import os
from unittest.mock import MagicMock, patch

from music_downloader.__main__ import cmd_run, run_health_server


class TestRunHealthServer:
    def test_creates_and_runs_server(self):
        with patch("music_downloader.__main__.uvicorn") as mock_uvicorn:
            mock_server = MagicMock()
            mock_uvicorn.Config.return_value = MagicMock()
            mock_uvicorn.Server.return_value = mock_server
            run_health_server(8080)
            mock_server.run.assert_called_once()


class TestCmdRun:
    def test_cmd_run_starts_bot(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "t",
            "SPOTIFY_CLIENT_ID": "s",
            "SPOTIFY_CLIENT_SECRET": "ss",
            "SLSKD_HOST": "h",
            "SLSKD_API_KEY": "k",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("music_downloader.__main__.create_bot") as mock_create,
            patch("music_downloader.__main__.threading.Thread") as mock_thread,
        ):
            mock_app = MagicMock()
            mock_create.return_value = mock_app
            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance
            args = MagicMock()
            cmd_run(args)
            mock_thread_instance.start.assert_called_once()
            mock_app.run_polling.assert_called_once()
