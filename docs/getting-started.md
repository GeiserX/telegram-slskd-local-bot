# Getting started

<p>
  <a href="https://pypi.org/project/telegram-slskd-local-bot/"><img src="https://img.shields.io/pypi/pyversions/telegram-slskd-local-bot?style=flat-square" alt="Python versions"/></a>
  <a href="https://codecov.io/gh/GeiserX/telegram-slskd-local-bot"><img src="https://codecov.io/gh/GeiserX/telegram-slskd-local-bot/graph/badge.svg" alt="codecov"/></a>
</p>

## Prerequisites

- A running [slskd](https://github.com/slskd/slskd) instance with an API key
- A [Spotify Developer](https://developer.spotify.com/dashboard) app (free — Client ID + Secret)
- A [Telegram bot](https://core.telegram.org/bots#botfather) token (via @BotFather)

The bot at [@slskdimporterbot](https://t.me/slskdimporterbot) is a private, allow-listed instance and will not answer you; deploy your own.

## Docker Compose

```yaml
services:
  slskd-importer:
    image: drumsergio/telegram-slskd-local-bot:0.13.1
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
      # Read-write: the bot deletes rejected files and sweeps abandoned ones
      - /path/to/slskd/downloads:/downloads
      - /path/to/music/library:/music
      # SQLite history and import state
      - ./data:/data
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

## From PyPI

Without Docker, install the package with the analysis extra, which the lossless check needs, and put `ffmpeg` on the `PATH`, which the bot uses to convert large files to Opus for the chat. Create the virtual environment in the same folder as `.env`. The bot looks for `.env` in the folders above its installed package, so a `.venv` beside `.env` finds it and one elsewhere does not.

```bash
mkdir slskd-importer && cd slskd-importer
python3 -m venv .venv && . .venv/bin/activate
pip install 'telegram-slskd-local-bot[analysis]'
curl -fsSL https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/.env.example -o .env
# edit .env: the tokens, TELEGRAM_ALLOWED_USERS, SLSKD_HOST and SLSKD_API_KEY; point DOWNLOAD_DIR
# (slskd's completed-downloads folder) and OUTPUT_DIR (your library) at real folders and add
# DATA_DIR=./data, because the defaults /downloads, /music and /data are container paths
slskd-importer run
```

Running from a checkout, tests and releases: [Development](development.md).
