"""Tests for __main__ module."""

import sys
from io import BytesIO
from unittest.mock import patch

import pytest

from music_downloader.__main__ import HealthHandler, main


class TestHealthHandler:
    def test_health_endpoint(self):
        """HealthHandler responds 200 on /health."""
        from unittest.mock import MagicMock

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        HealthHandler.do_GET(handler)

        handler.send_response.assert_called_once_with(200)
        handler.wfile.seek(0)
        assert b'{"status":"healthy"}' in handler.wfile.read()

    def test_not_found(self):
        """HealthHandler responds 404 on unknown paths."""
        from unittest.mock import MagicMock

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/unknown"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.end_headers = MagicMock()

        HealthHandler.do_GET(handler)

        handler.send_response.assert_called_once_with(404)


class TestMain:
    def test_version_flag(self):
        with patch.object(sys, "argv", ["slskd-importer", "--version"]), pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0

    def test_default_command_is_run(self):
        """Without a subcommand, main defaults to 'run'."""
        with patch.object(sys, "argv", ["slskd-importer"]), patch("music_downloader.__main__.cmd_run") as mock_run:
            main()
            mock_run.assert_called_once()

    def test_run_subcommand(self):
        with (
            patch.object(sys, "argv", ["slskd-importer", "run"]),
            patch("music_downloader.__main__.cmd_run") as mock_run,
        ):
            main()
            mock_run.assert_called_once()

    def test_unknown_command(self):
        with patch.object(sys, "argv", ["slskd-importer", "unknown"]), pytest.raises(SystemExit):
            main()
