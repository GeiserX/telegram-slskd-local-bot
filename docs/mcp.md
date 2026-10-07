# MCP server

The bot also speaks the [Model Context Protocol](https://modelcontextprotocol.io), so an agent such as Claude
Code or Claude Desktop can find a song, pick a copy and save it without Telegram. It drives the same pipeline
as the bot: the same Spotify lookup, the same slskd search and ranking, the same lossless check, the same
library, history and wishlist.

There are two ways to reach it:

- Over stdio, `python -m music_downloader mcp` starts a server on stdin and stdout for an agent on the same
  machine. It reads the same [environment variables](configuration.md) or `.env` as the bot.
- Over HTTP, with `MCP_PORT` set, the bot process also serves MCP over streamable HTTP at
  `http://<host>:<MCP_PORT>/mcp`. Every request needs `Authorization: Bearer <MCP_TOKEN>`. This server shares
  the bot's database, slskd client and wishlist checker.

## Tools

Every tool returns a JSON object. Tracks and copies come back with short ids (`t3fa9c1`, `c81d0e2`) that the
next call takes; an id is valid for an hour.

| Tool | What it does | Example call |
|------|--------------|--------------|
| `resolve_track(query, artist, title, duration_secs)` | Track candidates for a free-text query, or for an artist and title, confident ones first, each with a track id, artist, title, album, year, duration, `source` (`spotify`, `musicbrainz` or `soulseek`), `source_id` and `confident`. Spotify first, MusicBrainz when Spotify has no confident candidate, the best Soulseek copy when neither has one. See [How a track is resolved](#how-a-track-is-resolved) | `resolve_track(artist="Graham Central Station", title="Jam", duration_secs=219.9)` |
| `search_copies(track_id, profile="library", limit=10)` | Soulseek copies of that track, best first, each with a copy id, format, quality, quality tier, size, duration, source user, score, `lossless` and `fits_cap` (under `TELEGRAM_MAX_UPLOAD_MB`). `profile="library"` puts lossless first; `"chat"` ranks for sound per megabyte. Searches "artist title", then the title alone | `search_copies(track_id="t3fa9c1", profile="library", limit=5)` |
| `download(copy_id, deliver="library")` | Downloads the copy and waits for it, up to `DOWNLOAD_TIMEOUT_SECS`, sending progress notifications. `deliver="library"` renames it, embeds the cover, moves it into `OUTPUT_DIR` and records it in the history; `"path"` leaves the file in `DOWNLOAD_DIR` and returns its path. Both return the lossless check verdict (FLAC, WAV and AIFF; `null` otherwise) | `download(copy_id="c81d0e2", deliver="library")` |
| `album_listing(copy_id)` | The audio files of the peer folder the copy came from, usually its whole release: name, format, quality, size and duration of each, the total size and the formats present. `answered` is false, with a `reason` (`no_answer`, `unreachable`), when the peer is offline or does not answer within 30 s; `no_audio` when the folder holds no audio | `album_listing(copy_id="c81d0e2")` |
| `album_download(copy_id, deliver="library")` | Downloads every audio file of that folder (one slskd request for the lot), with progress per file. `deliver="library"` saves each file into `OUTPUT_DIR` as it lands, named from its tags (or its file name when the tags are missing), with the album's Spotify cover and a history row noted `album`; a file the library already has under that name is skipped (`skipped` true, `path` the library's file) and its download deleted. `"path"` leaves the files in `DOWNLOAD_DIR`. A file gives up when its transfer moves no byte for `DOWNLOAD_TIMEOUT_SECS`; the whole album waits up to `ALBUM_TIMEOUT_SECS`. Returns one outcome per file (`ok`, `path`, `skipped`, `error`, `state`) and the counts; a failed file never stops the others | `album_download(copy_id="c81d0e2", deliver="library")` |
| `history(limit=20)` | The latest downloads, newest first, with their status (`success`, `delivered`, `failed`...) | `history(limit=10)` |
| `library_has(artist, title)` | Whether the library holds a file that looks like this track (the bot's duplicate check), with the closest matches | `library_has(artist="Nancy Sinatra", title="Bang Bang")` |
| `wishlist_add(track_id, wanted="any")` | Searches the track again every `WISHLIST_CHECK_HOURS`. `"any"` waits for any copy; `"better"` waits for a copy above the best one `search_copies` found for that track | `wishlist_add(track_id="t3fa9c1", wanted="better")` |
| `wishlist_list()` | Every wish, from every chat | `wishlist_list()` |
| `wishlist_remove(id)` | Removes a wish by the id `wishlist_list` shows | `wishlist_remove(id=4)` |
| `status()` | Whether slskd answers, downloads waiting on a Save, Reject or Retry button in Telegram, MCP downloads running, the number of wishes, the upload cap and the version | `status()` |

## How a track is resolved

`resolve_track` asks Spotify first. A candidate is `confident` when its artist and title are the asked ones
once spelling is folded and, if you pass `duration_secs` (the length of the file you have), when it lasts within
8 seconds of it. Folding ignores accents, apostrophes (straight, curly or missing), `&` against "and", a leading
"The", and suffixes that name the same recording: " - Single Edit", " - 2011 Remaster", "(feat. ...)",
"(From ...)", "Mono". A suffix that names another recording, such as " - Live", "(Remix)", " - Acoustic" or
"Karaoke", keeps the titles apart. The artist matches when one name holds the other, so "Kevin Rowland & Dexys
Midnight Runners" matches "Dexys Midnight Runners".

When no Spotify candidate is confident, the server asks [MusicBrainz](https://musicbrainz.org) for recordings
of that artist and title. It sends no key, at most one request per second, with a User-Agent that names this
project. Recordings MusicBrainz marks as live or a remix are left out unless the asked title says so. Its
confident recordings come first, `source` `musicbrainz`, followed by the Spotify candidates.

When neither has a confident candidate, the server searches Soulseek for "artist title" and turns the best copy
into a candidate with `source` `soulseek`, `confident` false, and the artist, title and length read from the file
name. Its `search_copies` ranks the copies of that same search instead of searching again.

The fallbacks need both an artist and a title. Pass them as `artist` and `title`, write the query as
"Artist - Title", or use a free-text query whose artist Spotify recognises at its start or end. A query that is
only a title gets the Spotify candidates and nothing more.

Every candidate's track id works the same way in `search_copies`, `download`, `album_listing`,
`album_download` and `wishlist_add`, whatever its source. A wish keeps the track's `source` and `source_id`
(`spotify:track:<id>`, `musicbrainz:recording:<mbid>`, `soulseek:<user>:<path>`).

A typical chain: `resolve_track` → `search_copies` with the track id → `download` with the copy id → `history`.
For the whole release: `album_listing` with the same copy id, then `album_download`.

Errors come in two shapes. A bad argument, an expired id or a refused wish is a tool error (`is_error` true)
with a one-line reason. A `download` that ran and failed is a normal result with `ok` false, the error and the
slskd state. `wishlist_add` for a track the owner already waits for returns that wish with
`already_waiting` true instead of adding a second one.

A wish added over MCP belongs to the owner, the first id in `TELEGRAM_ALLOWED_USERS`. The bot's wishlist
checker announces it in that private chat, and with `/auto` on there it fetches the copy as an auto search
does. The stdio server runs no checker: its wishes wait for the bot, which reads the same database when both
use the same `DATA_DIR`.

An `album_download` whose peer cannot be listed returns `ok` false with the listing's `reason` and no job.
An album the stdio server runs is its own: a bot restart on the same `DATA_DIR` does not take it over. If the
stdio server stops mid-album, nothing saves the files that land after it; the hourly sweep removes them.

A file left with `deliver="path"` stays in `DOWNLOAD_DIR` until the sweep removes it after
`DOWNLOAD_CLEANUP_HOURS`, so copy it out before then.

## Connect over stdio

For Claude Code, add this to `.mcp.json` in your project (or run `claude mcp add`); Claude Desktop takes the
same block under `mcpServers` in `claude_desktop_config.json`. Point `command` at a Python that has the
package installed, from `pip install telegram-slskd-local-bot` or the `.venv` of a checkout.

```json
{
  "mcpServers": {
    "slskd": {
      "command": "python",
      "args": ["-m", "music_downloader", "mcp"],
      "env": {
        "TELEGRAM_ALLOWED_USERS": "123456789",
        "SPOTIFY_CLIENT_ID": "...",
        "SPOTIFY_CLIENT_SECRET": "...",
        "SLSKD_HOST": "http://192.168.1.100:5030",
        "SLSKD_API_KEY": "...",
        "DOWNLOAD_DIR": "/path/to/slskd/downloads",
        "OUTPUT_DIR": "/path/to/music",
        "DATA_DIR": "/path/to/data"
      }
    }
  }
}
```

The stdio server never talks to Telegram, so it needs no `TELEGRAM_BOT_TOKEN`. Logs go to stderr.

## Connect over HTTP

Set a port and a token in `.env`, uncomment the `ports` block in
[docker-compose.yml](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docker-compose.yml) and
recreate the container.

```bash
MCP_PORT=8765
MCP_TOKEN=$(openssl rand -hex 32)
```

Then point the client at the URL with the token. For Claude Code:

```bash
claude mcp add --transport http slskd http://192.168.1.10:8765/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

or in `.mcp.json`:

```json
{
  "mcpServers": {
    "slskd": {
      "type": "http",
      "url": "http://192.168.1.10:8765/mcp",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

A request without the right token gets `401`.

## Security

- **Keep it on your LAN.** The HTTP server has no TLS. Publish the port on the host's LAN address (or reach it
  over a VPN), never on a public interface.
- **The token is required.** The bot refuses to start with `MCP_PORT` set and `MCP_TOKEN` empty, and compares
  the token in constant time. Anyone with the token can download into your library.
- **No Telegram rules apply.** The MCP caller is the owner: `TELEGRAM_ALLOWED_USERS` does not gate it,
  `TELEGRAM_CHAT_DELIVERY_USERS` does not lock it to chat delivery, and it sees and removes every chat's
  wishes. Give the token only to agents you would give your library to.
