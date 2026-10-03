<p align="center"><img src="https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/docs/images/banner.svg" alt="telegram-slskd-local-bot" width="900"/></p>

<h1 align="center">telegram-slskd-local-bot</h1>

<p align="center">
  <a href="https://pypi.org/project/telegram-slskd-local-bot/"><img src="https://img.shields.io/pypi/v/telegram-slskd-local-bot?style=flat-square" alt="PyPI"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/telegram-slskd-local-bot/tests.yml?style=flat-square&label=tests" alt="Tests"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/telegram-slskd-local-bot?style=flat-square" alt="License"/></a>
  <a href="https://hub.docker.com/r/drumsergio/telegram-slskd-local-bot"><img src="https://img.shields.io/docker/pulls/drumsergio/telegram-slskd-local-bot?style=flat-square&logo=docker" alt="Docker Pulls"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/stargazers"><img src="https://img.shields.io/github/stars/GeiserX/telegram-slskd-local-bot?style=flat-square" alt="Stars"/></a>
</p>

**telegram-slskd-local-bot** is a Telegram bot, run in Docker next to your [slskd](https://github.com/slskd/slskd), that turns a song name typed on your phone into a FLAC in your music library. It looks the song up on Spotify, searches Soulseek for lossless copies, ranks them by duration, bit depth and source, and sends you the file in the chat so you can listen to it and read its lossless check before it is saved as `Artist - Title.flac`. Doing the same by hand in the slskd web UI means reading file lists, guessing which copy is the studio version and finding out after the fact that the FLAC was an MP3.

## Features

- Send a song name; the bot resolves artist, title, album and duration on Spotify and searches Soulseek for FLAC copies through your slskd.
- Ranks copies by duration against Spotify, bit depth and sample rate (hi-res first), free slots, speed and file name; drops live, remix and karaoke cuts unless the title has them.
- Sends you the track in the chat before it is saved: the FLAC itself, or an Opus copy of the whole song when the file is over Telegram's 50 MB limit.
- Checks the spectrum of a FLAC found from a song name (by you or `/auto`) and flags an MP3-style cutoff ("Possible transcode", "Likely transcode", "Fake lossless") before Save. `/import` skips it.
- Warns when similar files are already in the library before it searches.
- Saves as `Artist - Title.flac` with the Spotify cover art embedded, in the folder your player or [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher) watches.
- `/import` a Spotify playlist or album and review each track or auto-save them all; `/status` shows the progress.
- Chat delivery (`/deliver`, or fixed per account with `TELEGRAM_CHAT_DELIVERY_USERS`) sends the track into the chat under 50 MB and saves nothing anywhere, so you can share the bot without sharing your library.
- `/auto` per chat saves the best match without a tap; a failed transfer gets Retry and Try next result; downloads show live progress.
- Answers only allow-listed Telegram users, and denies everyone while the list is empty.
- Sweeps abandoned downloads after `DOWNLOAD_CLEANUP_HOURS` and never touches a transfer in flight.

## Quick start

You need a running [slskd](https://github.com/slskd/slskd) with an API key, a [Spotify Developer](https://developer.spotify.com/dashboard) app and a [Telegram bot](https://core.telegram.org/bots#botfather) token, on an amd64 host (the image has no arm64 build).

```bash
mkdir slskd-importer && cd slskd-importer
curl -fsSLO https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/.env.example -o .env
# edit .env: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS (your Telegram user id), SPOTIFY_CLIENT_ID and
# SPOTIFY_CLIENT_SECRET, SLSKD_HOST and SLSKD_API_KEY, SLSKD_DOWNLOAD_PATH (slskd's completed-downloads
# folder) and MUSIC_OUTPUT_PATH (your library); the last two must be writable by uid 1000
mkdir -p data && sudo chown 1000:1000 data    # the container runs as uid 1000
docker compose up -d                          # starts drumsergio/telegram-slskd-local-bot:0.13.1
```

The container is `slskd_importer`, and the log line `Music Downloader v0.13.1 starting` means it is up. A message to your bot (for example `Nancy Sinatra Bang Bang`) is answered with the Spotify matches to pick from, or with the ranked FLAC copies when there is only one match; an empty `TELEGRAM_ALLOWED_USERS` answers nobody, and the denial reply shows your id. [Getting started](https://geiserx.github.io/telegram-slskd-local-bot/getting-started/) has the other install paths.

## Documentation

The docs are at [geiserx.github.io/telegram-slskd-local-bot](https://geiserx.github.io/telegram-slskd-local-bot/).

- [Getting started](https://geiserx.github.io/telegram-slskd-local-bot/getting-started/): prerequisites, Docker Compose, running from PyPI
- [Configuration](https://geiserx.github.io/telegram-slskd-local-bot/configuration/): every environment variable and its default
- [Usage](https://geiserx.github.io/telegram-slskd-local-bot/usage/): what the chat shows, the commands, how results are scored
- [How it works](https://geiserx.github.io/telegram-slskd-local-bot/how-it-works/): the path from a message to a file in the library
- [Troubleshooting](https://geiserx.github.io/telegram-slskd-local-bot/troubleshooting/): the four failures people hit and what to report in an issue
- [Development](https://geiserx.github.io/telegram-slskd-local-bot/development/): running from a checkout, tests, releases

Release notes are in the [changelog](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/CHANGELOG.md). Bugs and questions go to the [issues](https://github.com/GeiserX/telegram-slskd-local-bot/issues); security problems to the [security policy](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/SECURITY.md), never a public issue.

## Related projects

Music pipeline: [slskd-transform](https://github.com/GeiserX/slskd-transform), [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), [quality-gate-encoder](https://github.com/GeiserX/quality-gate-encoder). Other Telegram bots: [paperless-telegram-bot](https://github.com/GeiserX/paperless-telegram-bot), [jellyfin-telegram-channel-sync](https://github.com/GeiserX/jellyfin-telegram-channel-sync), [Telegram-Archive](https://github.com/GeiserX/Telegram-Archive). The rest are in [Related projects](https://geiserx.github.io/telegram-slskd-local-bot/related/).

## License

[GPL-3.0-or-later](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/LICENSE)
