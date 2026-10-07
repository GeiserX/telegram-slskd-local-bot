"""
Configuration management for Music Downloader.
Loads and validates settings from environment variables.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Telegram's bot upload cap is 50,000,000 bytes, python-telegram-bot's
# constants.FileSizeLimit.FILESIZE_UPLOAD (tests pin the two together; this
# module must not import telegram, the pipeline reads it). Telegram counts
# 1 MB as 1,000,000 bytes, so TELEGRAM_MAX_UPLOAD_MB scales that unit.
BYTES_PER_MB = 1_000_000
DEFAULT_MAX_UPLOAD_MB = 50
DEFAULT_UPLOAD_LIMIT_BYTES = DEFAULT_MAX_UPLOAD_MB * BYTES_PER_MB
# A local Bot API server (TELEGRAM_API_BASE_URL) accepts uploads up to 2000 MB,
# and a 2 GB upload needs far longer than PTB's default timeouts allow.
LOCAL_SERVER_MAX_UPLOAD_MB = 2000
LOCAL_SERVER_UPLOAD_TIMEOUT_SECS = 600


class Config:
    """Configuration settings loaded from environment variables."""

    def __init__(self, require_telegram: bool = True):
        """Initialize configuration from environment variables.

        *require_telegram* False is for the stdio MCP server, which never talks
        to Telegram: TELEGRAM_BOT_TOKEN may then be unset (None).
        """
        # =====================================================================
        # TELEGRAM BOT
        # =====================================================================
        if require_telegram:
            self.telegram_bot_token = self._get_required_env("TELEGRAM_BOT_TOKEN")
        else:
            self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN") or None

        # Comma-separated Telegram user IDs allowed to use the bot
        # If empty, anyone can use it (not recommended)
        allowed_users_str = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        self.telegram_allowed_users = self._parse_id_set(allowed_users_str)
        # The first id listed is the owner: wishes added over MCP notify that
        # private chat (a private chat's id is the user's id).
        listed = [uid.strip() for uid in allowed_users_str.split(",") if uid.strip()]
        self.telegram_owner_id = int(listed[0]) if listed else None

        # Comma-separated Telegram user IDs whose chats default to chat delivery:
        # the track is sent into the chat (under the upload cap) and nothing is saved.
        # /deliver overrides this per chat.
        chat_delivery_str = os.getenv("TELEGRAM_CHAT_DELIVERY_USERS", "")
        self.telegram_chat_delivery_users = self._parse_id_set(chat_delivery_str)

        # A local Bot API server (e.g. http://telegram-bot-api:8081); empty
        # means Telegram's cloud API.
        self.telegram_api_base_url = os.getenv("TELEGRAM_API_BASE_URL", "").strip().rstrip("/")

        # Largest file the bot may send, in MB of 1,000,000 bytes. 50 is the
        # cloud Bot API's cap; a local Bot API server raises it to 2000, which
        # becomes the default when TELEGRAM_API_BASE_URL is set. Empty = default.
        default_mb = LOCAL_SERVER_MAX_UPLOAD_MB if self.telegram_api_base_url else DEFAULT_MAX_UPLOAD_MB
        self.telegram_max_upload_mb = max(1, int(os.getenv("TELEGRAM_MAX_UPLOAD_MB") or default_mb))
        self.telegram_upload_limit_bytes = self.telegram_max_upload_mb * BYTES_PER_MB

        # Read/write timeouts (seconds) for requests to Telegram, uploads
        # included. None keeps python-telegram-bot's defaults (the cloud API
        # case); with a local server the default fits a 2 GB upload on a LAN.
        timeout = os.getenv("TELEGRAM_UPLOAD_TIMEOUT_SECS") or (
            LOCAL_SERVER_UPLOAD_TIMEOUT_SECS if self.telegram_api_base_url else None
        )
        self.telegram_upload_timeout_secs = None if timeout is None else max(1, int(timeout))

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

        # Where to store SQLite database (persistent volume)
        self.data_dir = os.getenv("DATA_DIR", "/data")

        # =====================================================================
        # DOWNLOAD BEHAVIOR
        # =====================================================================
        # Auto-download best match without user confirmation
        self.auto_mode = os.getenv("AUTO_MODE", "false").lower() == "true"

        # Maximum number of search results to show per page (floor of 1 —
        # a zero would divide by zero in results pagination)
        self.max_results = max(1, int(os.getenv("MAX_RESULTS", "10")))

        # Duration tolerance in seconds when matching Spotify duration
        self.duration_tolerance_secs = int(os.getenv("DURATION_TOLERANCE_SECS", "5"))

        # How long to wait for slskd search results (seconds)
        self.search_timeout_secs = int(os.getenv("SEARCH_TIMEOUT_SECS", "30"))

        # How long to wait for a download to complete (seconds)
        self.download_timeout_secs = int(os.getenv("DOWNLOAD_TIMEOUT_SECS", "600"))

        # The most an album fetch may take in total (each file still gets at
        # most DOWNLOAD_TIMEOUT_SECS); files left when it runs out fail.
        self.album_timeout_secs = max(1, int(os.getenv("ALBUM_TIMEOUT_SECS") or "7200"))

        # Hours a download waiting on a button (Save, Reject, Retry) is kept,
        # with its file, before it expires (0: never expires).
        self.download_cleanup_hours = max(0, int(os.getenv("DOWNLOAD_CLEANUP_HOURS", "24")))

        # Hours before a file in DOWNLOAD_DIR that nothing waits on is deleted
        # by the hourly sweep (0 disables). Such a file is a leftover: a
        # superseded search, a restart, a transfer that finished after the bot
        # gave up. Files of downloads waiting on a button are never swept, and
        # transfers in flight are safe (fresh mtime).
        self.orphan_sweep_hours = max(0, int(os.getenv("ORPHAN_SWEEP_HOURS") or "6"))

        # The lossless gate, library deliveries only: a lossless copy whose
        # spectrum shows it was made from a lossy file is deleted and the next
        # ranked copy tried; after LOSSLESS_GATE_MAX_REJECTIONS rejected copies
        # the next one is kept whatever it is, and the user is told.
        self.lossless_gate = (os.getenv("LOSSLESS_GATE") or "true").strip().lower() != "false"
        self.lossless_gate_max_rejections = max(0, int(os.getenv("LOSSLESS_GATE_MAX_REJECTIONS") or "3"))

        # Wishlist: hours between two searches for the same wish, and seconds
        # between two wish searches in one pass (Soulseek etiquette).
        self.wishlist_check_hours = max(1, int(os.getenv("WISHLIST_CHECK_HOURS") or "24"))
        self.wishlist_pause_secs = max(0, int(os.getenv("WISHLIST_PAUSE_SECS") or "20"))

        # The library sweep (docs/sweep.md): off unless LIBRARY_SWEEP_USERS
        # names someone. Those accounts (library delivery only) get /sweep and
        # the sweep's reports; the schedule is read in the container's time zone.
        self.library_sweep_users = self._parse_id_set(os.getenv("LIBRARY_SWEEP_USERS", ""))
        self.library_sweep_schedule = os.getenv("LIBRARY_SWEEP_SCHEDULE", "").strip()
        from music_downloader.pipeline.sweep_rules import parse_schedule

        parse_schedule(self.library_sweep_schedule)  # a typo stops the start, not the first sweep
        self.library_sweep_pause_secs = max(0, int(os.getenv("LIBRARY_SWEEP_PAUSE_SECS") or "20"))
        self.library_sweep_keep_days = max(0, int(os.getenv("LIBRARY_SWEEP_KEEP_DAYS") or "7"))

        # Keywords in file paths that indicate unwanted versions
        exclude_kw = os.getenv(
            "EXCLUDE_KEYWORDS",
            "live,remix,acoustic,karaoke,instrumental,cover,demo,radio edit,tribute,remaster",
        )
        self.exclude_keywords = [kw.strip().lower() for kw in exclude_kw.split(",") if kw.strip()]

        # File naming template: {artist}, {title}, and {track} (two-digit track
        # number of an album file; dropped with its separator when unknown)
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

        # =====================================================================
        # MCP SERVER (streamable HTTP, in the bot process)
        # =====================================================================
        # Unset = no MCP over HTTP (`python -m music_downloader mcp` still
        # serves stdio). A port needs MCP_TOKEN: every request must carry
        # "Authorization: Bearer <MCP_TOKEN>".
        mcp_port = os.getenv("MCP_PORT", "").strip()
        self.mcp_port = int(mcp_port) if mcp_port else None
        # Loopback by default, which also turns on the MCP SDK's Host/Origin
        # check; the container sets 0.0.0.0 in docker-compose.yml.
        self.mcp_host = os.getenv("MCP_HOST", "").strip() or "127.0.0.1"
        self.mcp_token = os.getenv("MCP_TOKEN", "").strip() or None
        if self.mcp_port is not None and not self.mcp_token:
            raise ValueError("MCP_PORT is set but MCP_TOKEN is empty: the MCP server refuses to run without a token.")

        logger.info("Configuration loaded successfully")
        if self.auto_mode:
            logger.info("AUTO_MODE enabled — best match will be downloaded automatically")
        if self.telegram_allowed_users:
            logger.info(f"Bot restricted to {len(self.telegram_allowed_users)} allowed user(s)")
        else:
            logger.warning("TELEGRAM_ALLOWED_USERS is empty — bot will deny all commands until configured")
        if self.telegram_chat_delivery_users:
            logger.info(f"Chat delivery is the default for {len(self.telegram_chat_delivery_users)} user(s)")

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
