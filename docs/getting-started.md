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
    image: drumsergio/telegram-slskd-local-bot:0.20.2
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

## Send files over 50 MB: a local Bot API server

Telegram's cloud Bot API refuses any file a bot sends over 50 MB, so chat delivery converts bigger files to
Opus. A [local Bot API server](https://github.com/tdlib/telegram-bot-api) running next to the bot accepts up
to 2000 MB. The repo's `docker-compose.yml` already has one, `telegram-bot-api`, behind the `bigfiles` profile.
The compose file above does not. If you started from it, copy the `telegram-bot-api` service and the
`telegram-bot-api-data` volume from the repo's
[docker-compose.yml](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docker-compose.yml) into
yours. Without them `--profile bigfiles` starts only the bot, which then cannot reach `telegram-bot-api`.

1. Log in at [my.telegram.org](https://my.telegram.org), open **API development tools** and create an application. Copy its
   `api_id` and `api_hash`.
2. Put them in `.env`:

    ```bash
    TELEGRAM_API_ID=1234567
    TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
    ```

3. Log the bot out of Telegram's cloud server, **once**, before its first start against the local server.
   A bot token is logged in to one server at a time; without this step the local server may not get the
   bot's updates.

    ```bash
    curl https://api.telegram.org/bot<your-bot-token>/logOut
    ```

    The answer is `{"ok":true,"result":true}`.

4. Point the bot at the local server in `.env`:

    ```bash
    TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081
    ```

5. Start both containers:

    ```bash
    docker compose --profile bigfiles up -d
    ```

    The bot's log says `Talking to local Bot API server at http://telegram-bot-api:8081; upload cap 2000 MB`.

The server runs as uid 101 and keeps its state in the `telegram-bot-api-data` volume. The named volume in the
compose file gets that owner on its own; if you swap it for a host folder, give the folder to uid 101 first
(`sudo chown -R 101:101 /path/to/folder`).

What changes:

- The upload cap becomes 2000 MB (`TELEGRAM_MAX_UPLOAD_MB` still overrides it).
- The bot sends the original file as it is, both the preview before Save and chat delivery: a 120 MB hi-res
  FLAC arrives as that FLAC, with no Opus conversion. `/format` still converts when you ask for MP3 or Opus.
- Chat ranking only pushes copies over 2000 MB to the end; the size cost still grows from 5 MB to 50 MB.
- Telegram clients play audio of any size, so the big file plays in the chat like a small one.
- Requests to Telegram get 600 s read and write timeouts, enough for a 2 GB upload on a LAN
  (`TELEGRAM_UPLOAD_TIMEOUT_SECS` changes it).
- The bot reads the whole file into memory to upload it, so a 1.5 GB FLAC needs 1.5 GB of free RAM while it
  is sent. On a small box set `TELEGRAM_MAX_UPLOAD_MB` below the RAM you can spare, for example `500` on a
  machine with 2 GB.

To go back to the cloud server, log the bot out of the local server with `logOut`, remove
`TELEGRAM_API_BASE_URL` from `.env` and start the bot again with `docker compose up -d`. Telegram can take up
to 10 minutes to accept the token on the cloud server again.

```bash
docker compose exec telegram-bot-api wget -qO- http://127.0.0.1:8081/bot<your-bot-token>/logOut
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
