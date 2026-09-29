<p align="center"><img src="https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/docs/images/banner.svg" alt="telegram-slskd-local-bot" width="900"/></p>

<h1 align="center">telegram-slskd-local-bot</h1>

<p align="center">
  <a href="https://pypi.org/project/telegram-slskd-local-bot/"><img src="https://img.shields.io/pypi/v/telegram-slskd-local-bot?style=flat-square" alt="PyPI"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/telegram-slskd-local-bot/tests.yml?label=tests" alt="Tests"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/telegram-slskd-local-bot" alt="License"/></a>
  <a href="https://hub.docker.com/r/drumsergio/telegram-slskd-local-bot"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-slskd-local-bot" alt="Docker Pulls"/></a>
</p>

<p align="center"><strong>Automated music discovery and download via Telegram bot. Resolves track metadata from Spotify, searches and downloads FLAC files from Soulseek (via <a href="https://github.com/slskd/slskd">slskd</a>), renames them to <code>Artist - Title.flac</code>, and places them in your music library. Docker-ready.</strong></p>

## Features

- Send a song name, or `/import` a Spotify playlist or album.
- Resolves artist, title, duration and album on Spotify, then searches slskd for FLAC files.
- Ranks results by duration match, audio quality, source reliability and file name relevance, and drops live and remix versions unless the original title has them.
- You pick from the top matches, or turn on `/auto` per chat to save the best one without asking.
- Renames downloads to `Artist - Title.flac` and moves them into your library, ready for tools like [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher) or Navidrome.
- Only answers allow-listed Telegram users, and denies everyone when the list is empty.
- Sweeps abandoned downloads after `DOWNLOAD_CLEANUP_HOURS` and never touches transfers in flight.

## Quick start

You need a running [slskd](https://github.com/slskd/slskd) with an API key, a [Spotify Developer](https://developer.spotify.com/dashboard) app and a [Telegram bot](https://core.telegram.org/bots#botfather) token; run this in an empty folder, since it writes `docker-compose.yml` and `.env`.

```bash
curl -fsSLO https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/.env.example -o .env
mkdir -p data && sudo chown 1000:1000 data   # the container runs as uid 1000
docker compose up -d
```

Before the last command, fill in `.env`: the tokens, your Telegram user id in `TELEGRAM_ALLOWED_USERS` (an empty list answers nobody), and `SLSKD_DOWNLOAD_PATH` and `MUSIC_OUTPUT_PATH`, slskd's download folder and your library, both writable by uid 1000. Then send the bot a song name, for example "Nancy Sinatra Bang Bang"; [Getting started](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/getting-started.md) has the other install paths.

## Documentation

- [Getting started](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/getting-started.md): prerequisites, Docker Compose, local development
- [Configuration](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/configuration.md): environment variables
- [Usage](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/usage.md): an example chat, the flow, bot commands, the scoring algorithm
- [How it works](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/how-it-works.md)
- [Troubleshooting](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/troubleshooting.md)
- [Changelog](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/CHANGELOG.md)

## Related projects

Music pipeline: [slskd-transform](https://github.com/GeiserX/slskd-transform), [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), [quality-gate-encoder](https://github.com/GeiserX/quality-gate-encoder). Other Telegram bots: [paperless-telegram-bot](https://github.com/GeiserX/paperless-telegram-bot), [jellyfin-telegram-channel-sync](https://github.com/GeiserX/jellyfin-telegram-channel-sync), [Telegram-Archive](https://github.com/GeiserX/Telegram-Archive). More in [Related projects](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/related.md).

## License

[GPL-3.0-or-later](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/LICENSE)
