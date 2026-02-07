"""
Configuration management for Music Downloader.
Loads and validates settings from environment variables.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class Config:
    """Configuration settings loaded from environment variables."""

    def __init__(self):
        """Initialize configuration from environment variables."""
        # =====================================================================
        # TELEGRAM BOT
        # =====================================================================
        self.telegram_bot_token = self._get_required_env("TELEGRAM_BOT_TOKEN")

        # Comma-separated Telegram user IDs allowed to use the bot
        # If empty, anyone can use it (not recommended)
        allowed_users_str = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        self.telegram_allowed_users = self._parse_id_set(allowed_users_str)

        # =====================================================================
        # SPOTIFY API (Client Credentials flow — no user login needed)
        # =====================================================================
        self.spotify_client_id = self._get_required_env("SPOTIFY_CLIENT_ID")
        self.spotify_client_secret = self._get_required_env("SPOTIFY_CLIENT_SECRET")

        # =====================================================================
        # SLSKD (Soulseek) CONNECTION
        # =====================================================================
        self.slskd_host = self._get_required_env("SLSKD_HOST")
        self.slskd_api_key = self._get_required_env("SLSKD_API_KEY")

        # =====================================================================
        # PATHS
        # =====================================================================
        # Where slskd stores completed downloads (mounted volume)
        self.download_dir = os.getenv("DOWNLOAD_DIR", "/downloads")

        # Where to place the final renamed files (e.g., WCYR-FLAC directory)
        self.output_dir = os.getenv("OUTPUT_DIR", "/music")

        # =====================================================================
        # DOWNLOAD BEHAVIOR
        # =====================================================================
        # Auto-download best match without user confirmation
        self.auto_mode = os.getenv("AUTO_MODE", "false").lower() == "true"

        # Maximum number of search results to show the user
        self.max_results = int(os.getenv("MAX_RESULTS", "5"))

        # Duration tolerance in seconds when matching Spotify duration
        self.duration_tolerance_secs = int(os.getenv("DURATION_TOLERANCE_SECS", "5"))

        # How long to wait for slskd search results (seconds)
        self.search_timeout_secs = int(os.getenv("SEARCH_TIMEOUT_SECS", "30"))

        # How long to wait for a download to complete (seconds)
        self.download_timeout_secs = int(os.getenv("DOWNLOAD_TIMEOUT_SECS", "600"))

        # Keywords in file paths that indicate unwanted versions
        exclude_kw = os.getenv(
            "EXCLUDE_KEYWORDS",
            "live,remix,acoustic,karaoke,instrumental,cover,demo,radio edit,tribute,remaster",
        )
        self.exclude_keywords = [kw.strip().lower() for kw in exclude_kw.split(",") if kw.strip()]

        # File naming template: {artist} - {title}
        self.filename_template = os.getenv("FILENAME_TEMPLATE", "{artist} - {title}")

        # =====================================================================
        # LOGGING
        # =====================================================================
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level == "WARN":
            log_level = "WARNING"
        self.log_level = getattr(logging, log_level, logging.INFO)

        # =====================================================================
        # HEALTH CHECK
        # =====================================================================
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))

        logger.info("Configuration loaded successfully")
        if self.auto_mode:
            logger.info("AUTO_MODE enabled — best match will be downloaded automatically")
        if self.telegram_allowed_users:
            logger.info(f"Bot restricted to {len(self.telegram_allowed_users)} allowed user(s)")
        else:
            logger.warning("TELEGRAM_ALLOWED_USERS is empty — bot is open to anyone!")

    def _get_required_env(self, key: str) -> str:
        """Get a required environment variable."""
        value = os.getenv(key)
        if not value:
            raise ValueError(
                f"Required environment variable '{key}' is not set. "
                "Please set it in your .env file or container environment."
            )
        return value

    @staticmethod
    def _parse_id_set(id_str: str) -> set[int]:
        """Parse comma-separated ID string into a set of integers."""
        if not id_str or not id_str.strip():
            return set()
        return {int(uid.strip()) for uid in id_str.split(",") if uid.strip()}


def setup_logging(config: Config):
    """Configure logging for the application."""
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("spotipy").setLevel(logging.WARNING)
