# Configuration

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | Yes | *(deny all)* | Comma-separated Telegram user IDs allowed to use the bot. Empty means the bot denies everyone (fail-closed) — the denial reply shows you your ID |
| `SPOTIFY_CLIENT_ID` | Yes | — | Spotify Developer app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Yes | — | Spotify Developer app Client Secret |
| `SLSKD_HOST` | Yes | — | slskd instance URL (e.g., `http://192.168.1.100:5030`) |
| `SLSKD_API_KEY` | Yes | — | slskd API key (Settings > Security > API Keys) |
| `DOWNLOAD_DIR` | No | `/downloads` | Where slskd stores completed downloads (container path) |
| `OUTPUT_DIR` | No | `/music` | Where to place renamed FLAC files (container path) |
| `AUTO_MODE` | No | `false` | Default auto-download state for chats that never toggled `/auto`: best match is downloaded and saved without asking |
| `MAX_RESULTS` | No | `10` | Maximum search results shown to user |
| `DURATION_TOLERANCE_SECS` | No | `5` | Duration match tolerance in seconds |
| `SEARCH_TIMEOUT_SECS` | No | `30` | slskd search timeout |
| `DOWNLOAD_TIMEOUT_SECS` | No | `600` | Download completion timeout |
| `DOWNLOAD_CLEANUP_HOURS` | No | `24` | Hours before abandoned files in `DOWNLOAD_DIR` are auto-deleted (hourly sweep; `0` disables). In-flight transfers are never touched |
| `EXCLUDE_KEYWORDS` | No | `live,remix,...` | Comma-separated keywords to filter out |
| `FILENAME_TEMPLATE` | No | `{artist} - {title}` | Output filename template |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `HEALTH_PORT` | No | `8080` | Health check HTTP port |

