# telegram-slskd-local-bot

Automated music discovery and download via Telegram bot. Resolves track metadata from Spotify, searches and downloads FLAC files from Soulseek (via [slskd](https://github.com/slskd/slskd)), renames them to `Artist - Title.flac`, and places them in your music library. Docker-ready.

## How It Works

```
You: "Nancy Sinatra Bang Bang"
Bot: Found: Nancy Sinatra - Bang Bang (My Baby Shot Me Down) (2:42)
     Searching slskd for FLAC...
     #1 [free] 2:42 | 16bit/44.1kHz | 30MB
     #2 [free] 2:41 | 16bit/44.1kHz | 28MB
     [Download #1] [Download #2] [Auto-pick best] [Cancel]
You: (taps Download #1)
Bot: Downloaded! Nancy Sinatra - Bang Bang (My Baby Shot Me Down).flac -> /music/
```

### Flow

1. Send a song name to the Telegram bot (text message)
2. Bot resolves the track on **Spotify** (artist, title, duration, album)
3. Bot searches **slskd** (Soulseek) for FLAC files matching the track
4. Results are **scored** by duration match, audio quality, source reliability, and filename relevance
5. Bot presents the top matches — you **pick one** (or enable auto-mode)
6. File is downloaded, **renamed** to `Artist - Title.flac`, and placed in your output directory
7. Your existing tools (e.g., [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), Navidrome) pick it up from there

## Quick Start

### Prerequisites

- A running [slskd](https://github.com/slskd/slskd) instance with an API key
- A [Spotify Developer](https://developer.spotify.com/dashboard) app (free — Client ID + Secret)
- A [Telegram bot](https://core.telegram.org/bots#botfather) token (via @BotFather)

### Docker Compose

```yaml
services:
  slskd-importer:
    image: drumsergio/telegram-slskd-local-bot:0.1.0
    container_name: slskd_importer
    restart: unless-stopped
    environment:
      TELEGRAM_BOT_TOKEN: "your-bot-token"
      TELEGRAM_ALLOWED_USERS: "your-telegram-user-id"
      SPOTIFY_CLIENT_ID: "your-spotify-client-id"
      SPOTIFY_CLIENT_SECRET: "your-spotify-client-secret"
      SLSKD_HOST: "http://your-slskd-host:5030"
      SLSKD_API_KEY: "your-slskd-api-key"
    volumes:
      - /path/to/slskd/downloads:/downloads:ro
      - /path/to/music/library:/music
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

### Local Development

```bash
# Clone the repo
git clone https://github.com/GeiserX/telegram-slskd-local-bot.git
cd telegram-slskd-local-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env with your credentials

# Run
python -m music_downloader run
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | No | *(open)* | Comma-separated Telegram user IDs allowed to use the bot |
| `SPOTIFY_CLIENT_ID` | Yes | — | Spotify Developer app Client ID |
| `SPOTIFY_CLIENT_SECRET` | Yes | — | Spotify Developer app Client Secret |
| `SLSKD_HOST` | Yes | — | slskd instance URL (e.g., `http://192.168.1.100:5030`) |
| `SLSKD_API_KEY` | Yes | — | slskd API key (Settings > Security > API Keys) |
| `DOWNLOAD_DIR` | No | `/downloads` | Where slskd stores completed downloads (container path) |
| `OUTPUT_DIR` | No | `/music` | Where to place renamed FLAC files (container path) |
| `AUTO_MODE` | No | `false` | Auto-download best match without asking |
| `MAX_RESULTS` | No | `5` | Maximum search results shown to user |
| `DURATION_TOLERANCE_SECS` | No | `5` | Duration match tolerance in seconds |
| `SEARCH_TIMEOUT_SECS` | No | `30` | slskd search timeout |
| `DOWNLOAD_TIMEOUT_SECS` | No | `600` | Download completion timeout |
| `EXCLUDE_KEYWORDS` | No | `live,remix,...` | Comma-separated keywords to filter out |
| `FILENAME_TEMPLATE` | No | `{artist} - {title}` | Output filename template |
| `LOG_LEVEL` | No | `INFO` | Logging level |
| `HEALTH_PORT` | No | `8080` | Health check HTTP port |

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| *(any text)* | Search for a song and show download options |
| `/auto` | Toggle auto-download mode on/off |
| `/status` | Show active searches and downloads |
| `/history` | Show recent download history |
| `/help` | Show help message |

## Scoring Algorithm

Search results are ranked by:

1. **Duration match** (40 pts): Compared to Spotify duration. Within ±5s = perfect, ±10s = acceptable, >30s = excluded
2. **Audio quality** (25 pts): Prefers 16-bit/44.1kHz (CD quality) for consistency
3. **Source reliability** (20 pts): Free upload slots, fast upload speed, short queue
4. **Filename relevance** (15 pts): Artist and title words found in the filename

Results containing excluded keywords (live, remix, etc.) are automatically filtered out, unless the original track title also contains that keyword.

## Architecture

```
┌──────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Telegram Bot    │────▶│  Spotify API │     │  slskd (Soulseek)│
│  (user input)    │     │  (metadata)  │     │  (search/download)│
└──────────────────┘     └──────────────┘     └──────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Music Downloader Service                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐│
│  │ Resolver │─▶│ Searcher │─▶│  Scorer  │─▶│ File Processor   ││
│  │(Spotify) │  │ (slskd)  │  │(ranking) │  │(rename + move)   ││
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                                                      │
                                                      ▼
                                              ┌──────────────┐
                                              │ Music Library │
                                              │  (FLAC files) │
                                              └──────────────┘
```

## License

[GPL-3.0](LICENSE)

## Links

- **Repository**: https://github.com/GeiserX/telegram-slskd-local-bot
- **Telegram Bot**: [@slskdimporterbot](https://t.me/slskdimporterbot)
- **Changelog**: [docs/CHANGELOG.md](docs/CHANGELOG.md)
