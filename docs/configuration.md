# Configuration

## Environment variables

The Docker image runs as uid 1000, so the three host folders mounted into it (`SLSKD_DOWNLOAD_PATH`, `MUSIC_OUTPUT_PATH`, `DATA_PATH`) must be writable by that uid.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather. The stdio MCP server (`python -m music_downloader mcp`) does not need it |
| `TELEGRAM_ALLOWED_USERS` | Yes | *(deny all)* | Comma-separated Telegram user IDs allowed to use the bot. Empty means the bot denies everyone (fail-closed) — the denial reply shows you your ID. The first ID listed is the owner: wishes added over [MCP](mcp.md) are announced in that chat |
| `TELEGRAM_CHAT_DELIVERY_USERS` | No | *(empty)* | Comma-separated Telegram user IDs fixed to chat delivery: the track is sent into the chat (under the upload cap, converted to Opus when bigger) and nothing is saved anywhere. These accounts cannot switch to the library with `/deliver`, so only accounts outside the list ever write to it. In a group chat it matches the user who started the search or import |
| `TELEGRAM_MAX_UPLOAD_MB` | No | `50`, or `2000` with `TELEGRAM_API_BASE_URL` | The largest file the bot sends into a chat, in MB of 1,000,000 bytes (Telegram's unit). 50 is the cloud Bot API's limit, 50,000,000 bytes; a local Bot API server accepts up to 2000. Chat delivery, the chat ranking, the Opus conversion and the previews all use this one value. Leave it empty to get the right default for the server the bot talks to. The bot reads a file into memory to upload it, so on a small machine keep the cap below the RAM you can spare (for example `500` with 2 GB) |
| `TELEGRAM_API_BASE_URL` | No | *(cloud API)* | Base URL of a local Bot API server, for example `http://telegram-bot-api:8081` (the `bigfiles` service in the compose file). Set, it raises the upload cap to 2000 MB and the request timeouts to 600 s. The bot must be logged out of the cloud server once first: [steps](getting-started.md#send-files-over-50-mb-a-local-bot-api-server) |
| `TELEGRAM_API_ID` | Compose only, with `bigfiles` | — | `api_id` of an application created at [my.telegram.org](https://my.telegram.org) (API development tools). Read by the `telegram-bot-api` service, not by the bot |
| `TELEGRAM_API_HASH` | Compose only, with `bigfiles` | — | `api_hash` of the same application. Read by the `telegram-bot-api` service, not by the bot |
| `TELEGRAM_UPLOAD_TIMEOUT_SECS` | No | `600` with `TELEGRAM_API_BASE_URL`, python-telegram-bot's defaults otherwise | Read and write timeout in seconds for requests to Telegram, uploads included. Raise it if big uploads time out on a slow link. Polling for updates keeps its own timeout |
| `SPOTIFY_CLIENT_ID` | Yes | — | Spotify Developer app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Yes | — | Spotify Developer app Client Secret |
| `SLSKD_HOST` | Yes | — | slskd instance URL (e.g., `http://192.168.1.100:5030`) |
| `SLSKD_API_KEY` | Yes | — | slskd API key (Settings > Security > API Keys) |
| `DOWNLOAD_DIR` | No | `/downloads` | Where slskd stores completed downloads (container path) |
| `OUTPUT_DIR` | No | `/music` | Where to place renamed audio files (container path) |
| `DATA_DIR` | No | `/data` | Where the SQLite history and import state live (container path) |
| `SLSKD_DOWNLOAD_PATH` | Compose only | `./downloads` | Host folder mounted at `/downloads`: slskd's completed-downloads folder |
| `MUSIC_OUTPUT_PATH` | Compose only | `./music` | Host folder mounted at `/music`: your library |
| `DATA_PATH` | Compose only | `./data` | Host folder mounted at `/data` |
| `AUTO_MODE` | No | `false` | Default auto-download state for chats that never toggled `/auto`: best match is downloaded and saved without asking. Running `/deliver` in a chat that never toggled `/auto` fixes that chat's auto-download state at the current default, so a later change to `AUTO_MODE` no longer applies to it |
| `MAX_RESULTS` | No | `10` | Maximum search results shown to user |
| `DURATION_TOLERANCE_SECS` | No | `5` | Duration match tolerance in seconds |
| `SEARCH_TIMEOUT_SECS` | No | `30` | slskd search timeout |
| `DOWNLOAD_TIMEOUT_SECS` | No | `600` | Download completion timeout |
| `ALBUM_TIMEOUT_SECS` | No | `7200` | The most a whole album download may take. Each file still gets at most `DOWNLOAD_TIMEOUT_SECS`; files left when this runs out fail as timed out (slskd keeps their transfers) |
| `WISHLIST_CHECK_HOURS` | No | `24` | Hours between two searches for the same wished track (`/wishlist`) |
| `WISHLIST_PAUSE_SECS` | No | `20` | Seconds between two wish searches in one pass, to keep the load on Soulseek peers low |
| `DOWNLOAD_CLEANUP_HOURS` | No | `24` | Hours before abandoned files in `DOWNLOAD_DIR` are auto-deleted (hourly sweep; `0` disables). In-flight transfers are never touched. Downloads still waiting on a button (Save, Reject, Retry) survive a restart for this long, then expire with their file. Result lists older than this are dropped at the next start |
| `EXCLUDE_KEYWORDS` | No | `live,remix,...` | Comma-separated keywords to filter out |
| `FILENAME_TEMPLATE` | No | `{artist} - {title}` | Output filename template. Variables: `{artist}`, `{title}`, and `{track}`, the two-digit track number of a file saved from an album download (`03`). Without `{track}` the number never reaches the name; for a file with no number the placeholder is dropped with the separator next to it. The file's own extension is always added |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `HEALTH_PORT` | No | `8080` | Health check HTTP port. `GET /health` answers 200 only while the bot is polling Telegram (a successful poll in the last 120 s) and slskd answered in the last 60 s; otherwise 503 with a JSON body naming the failed check. `GET /ready` is a constant 200 |
| `MCP_PORT` | No | *(off)* | Serve the [MCP](mcp.md) tools over streamable HTTP from the bot process, at `http://<host>:<port>/mcp`. Unset means no MCP over HTTP; `python -m music_downloader mcp` serves stdio either way |
| `MCP_HOST` | No | `127.0.0.1` | Address the MCP HTTP server binds to. The default accepts local connections only and turns on the MCP SDK's Host header check; the compose file sets `0.0.0.0`, which the container needs so the published port reaches it |
| `MCP_TOKEN` | With `MCP_PORT` | — | Bearer token every MCP HTTP request must carry (`Authorization: Bearer <token>`). The bot refuses to start with `MCP_PORT` set and no token |

