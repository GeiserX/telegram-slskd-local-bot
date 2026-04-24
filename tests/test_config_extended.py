"""Extended tests for config module - covering setup_logging and edge cases."""

import logging
import os
from unittest.mock import patch

from music_downloader.config import Config, setup_logging


class TestSetupLogging:
    def test_configures_logging(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "SPOTIFY_CLIENT_ID": "s",
            "SPOTIFY_CLIENT_SECRET": "ss",
            "SLSKD_HOST": "h",
            "SLSKD_API_KEY": "k",
            "LOG_LEVEL": "DEBUG",
        }, clear=False):
            config = Config()
            setup_logging(config)
            assert logging.getLogger("httpx").level == logging.WARNING
            assert logging.getLogger("httpcore").level == logging.WARNING
            assert logging.getLogger("telegram").level == logging.WARNING
            assert logging.getLogger("spotipy").level == logging.WARNING


class TestConfigWarnLevel:
    def test_warn_maps_to_warning(self):
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "t",
            "SPOTIFY_CLIENT_ID": "s",
            "SPOTIFY_CLIENT_SECRET": "ss",
            "SLSKD_HOST": "h",
            "SLSKD_API_KEY": "k",
            "LOG_LEVEL": "WARN",
        }, clear=False):
            config = Config()
            assert config.log_level == logging.WARNING


class TestConfigParseIdSet:
    def test_empty_string(self):
        assert Config._parse_id_set("") == set()

    def test_whitespace_only(self):
        assert Config._parse_id_set("   ") == set()

    def test_single_id(self):
        assert Config._parse_id_set("123") == {123}

    def test_multiple_ids(self):
        assert Config._parse_id_set("1,2,3") == {1, 2, 3}

    def test_with_spaces(self):
        assert Config._parse_id_set(" 1 , 2 , 3 ") == {1, 2, 3}

    def test_trailing_comma(self):
        assert Config._parse_id_set("1,2,") == {1, 2}
