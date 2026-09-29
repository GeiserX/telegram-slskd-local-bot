<p align="center"><img src="https://raw.githubusercontent.com/GeiserX/telegram-slskd-local-bot/main/docs/images/banner.svg" alt="telegram-slskd-local-bot banner" width="900"/></p>

<h1 align="center">telegram-slskd-local-bot</h1>

  <a href="https://pypi.org/project/telegram-slskd-local-bot/"><img src="https://img.shields.io/pypi/v/telegram-slskd-local-bot?style=flat-square" alt="PyPI"/></a>
  <a href="https://github.com/GeiserX/telegram-slskd-local-bot/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/GeiserX/telegram-slskd-local-bot/tests.yml?label=tests" alt="Tests"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/GeiserX/telegram-slskd-local-bot" alt="License"/></a>
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

You need a running [slskd](https://github.com/slskd/slskd) with an API key, a [Spotify Developer](https://developer.spotify.com/dashboard) app and a [Telegram bot](https://core.telegram.org/bots#botfather) token. Put them in the [compose file](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/installation.md), then:

```bash
docker compose up -d
```

Send the bot a song name, for example "Nancy Sinatra Bang Bang".

## Documentation

- [Installation](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/installation.md): prerequisites, Docker Compose, local development
- [Configuration](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/configuration.md): environment variables
- [Usage](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/usage.md): an example chat, the flow, bot commands, the scoring algorithm
- [Architecture](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/architecture.md)
- [Changelog](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/CHANGELOG.md)
- Repository: https://github.com/GeiserX/telegram-slskd-local-bot
- Telegram bot: [@slskdimporterbot](https://t.me/slskdimporterbot) is the author's personal instance, allow-listed, so it won't respond to other users. Deploy your own to try it.

## Related Projects

**Music Pipeline:**

- [slskd-transform](https://github.com/GeiserX/slskd-transform) — Bulk upgrade lossy to lossless FLAC via Soulseek
- [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher) — Automated multi-format audio transcoding
- [quality-gate-encoder](https://github.com/GeiserX/quality-gate-encoder) (formerly jellyfin-encoder) — Automatic 720p HEVC/AV1 transcoding for Jellyfin

**Telegram Bots:**

- [paperless-telegram-bot](https://github.com/GeiserX/paperless-telegram-bot) — Manage Paperless-NGX documents through Telegram
- [AskePub](https://github.com/GeiserX/AskePub) — Telegram bot for ePub annotation with GPT-4
- [telegram-delay-channel-cloner](https://github.com/GeiserX/telegram-delay-channel-cloner) — Relay messages between channels with delay
- [jellyfin-telegram-channel-sync](https://github.com/GeiserX/jellyfin-telegram-channel-sync) — Sync Jellyfin access with Telegram membership
- [Telegram-Archive](https://github.com/GeiserX/Telegram-Archive) — Automated Telegram backup with local web viewer

## License

[GPL-3.0](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/LICENSE)
