"""Tests for configuration loading."""

import os
from unittest.mock import patch

import pytest


class TestConfig:
    """Tests for Config class."""

    @pytest.fixture
    def env_vars(self):
        """Minimal required environment variables."""
        return {
            "TELEGRAM_BOT_TOKEN": "test-token-123",
            "SPOTIFY_CLIENT_ID": "test-client-id",
            "SPOTIFY_CLIENT_SECRET": "test-client-secret",
            "SLSKD_HOST": "http://localhost:5030",
            "SLSKD_API_KEY": "test-api-key",
        }

    def test_loads_required_vars(self, env_vars):
        """Config loads all required environment variables."""
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.telegram_bot_token == "test-token-123"
            assert config.spotify_client_id == "test-client-id"
            assert config.spotify_client_secret == "test-client-secret"
            assert config.slskd_host == "http://localhost:5030"
            assert config.slskd_api_key == "test-api-key"

    def test_default_values(self, env_vars):
        """Config uses sensible defaults for optional vars."""
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.auto_mode is False
            assert config.max_results == 5
            assert config.duration_tolerance_secs == 5
            assert config.search_timeout_secs == 30
            assert config.download_timeout_secs == 600
            assert config.log_level == 20  # logging.INFO
            assert config.health_port == 8080

    def test_auto_mode_enabled(self, env_vars):
        """Config parses AUTO_MODE=true correctly."""
        env_vars["AUTO_MODE"] = "true"
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.auto_mode is True

    def test_allowed_users_parsing(self, env_vars):
        """Config parses comma-separated user IDs."""
        env_vars["TELEGRAM_ALLOWED_USERS"] = "123,456,789"
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.telegram_allowed_users == {123, 456, 789}

    def test_allowed_users_empty(self, env_vars):
        """Config handles empty allowed users (open to all)."""
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.telegram_allowed_users == set()

    def test_missing_required_var_raises(self):
        """Config raises ValueError for missing required variables."""
        with patch.dict(os.environ, {}, clear=True):
            from music_downloader.config import Config

            with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
                Config()

    def test_exclude_keywords_parsing(self, env_vars):
        """Config parses exclude keywords correctly."""
        env_vars["EXCLUDE_KEYWORDS"] = "live,remix,demo"
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.exclude_keywords == ["live", "remix", "demo"]

    def test_custom_filename_template(self, env_vars):
        """Config accepts custom filename template."""
        env_vars["FILENAME_TEMPLATE"] = "{title} by {artist}"
        with patch.dict(os.environ, env_vars, clear=False):
            from music_downloader.config import Config

            config = Config()
            assert config.filename_template == "{title} by {artist}"
