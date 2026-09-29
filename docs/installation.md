# Installation

<p>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white" alt="Python"/></a>
  <a href="https://hub.docker.com/r/drumsergio/telegram-slskd-local-bot"><img src="https://img.shields.io/docker/image-size/drumsergio/telegram-slskd-local-bot/latest" alt="Docker Image Size"/></a>
  <a href="https://codecov.io/gh/GeiserX/telegram-slskd-local-bot"><img src="https://codecov.io/gh/GeiserX/telegram-slskd-local-bot/graph/badge.svg" alt="codecov"/></a>
</p>

## Prerequisites

- A running [slskd](https://github.com/slskd/slskd) instance with an API key
- A [Spotify Developer](https://developer.spotify.com/dashboard) app (free — Client ID + Secret)
- A [Telegram bot](https://core.telegram.org/bots#botfather) token (via @BotFather)

## Docker Compose

```yaml
services:
  slskd-importer:
    image: drumsergio/telegram-slskd-local-bot:0.12.0
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
    logging:
      driver: "json-file"
      options:
        max-size: "50m"
        max-file: "3"
```

## Local Development

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

