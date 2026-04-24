"""Tests for __main__ module."""

import sys
from unittest.mock import patch

import pytest

from music_downloader.__main__ import create_health_app, main


class TestCreateHealthApp:
    def test_creates_app(self):
        app = create_health_app()
        assert app.title == "Music Downloader"
        # Verify the health route exists
        routes = [r.path for r in app.routes]
        assert "/health" in routes


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
