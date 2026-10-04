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
| `resolve_track(query)` | Spotify candidates for a free-text query, each with a track id, artist, title, album, year and duration | `resolve_track(query="Nancy Sinatra - Bang Bang")` |
| `search_copies(track_id, profile="library", limit=10)` | Soulseek copies of that track, best first, each with a copy id, format, quality, quality tier, size, duration, source user, score, `lossless` and `fits_cap` (under `TELEGRAM_MAX_UPLOAD_MB`). `profile="library"` puts lossless first; `"chat"` ranks for sound per megabyte. Searches "artist title", then the title alone | `search_copies(track_id="t3fa9c1", profile="library", limit=5)` |
| `download(copy_id, deliver="library")` | Downloads the copy and waits for it, up to `DOWNLOAD_TIMEOUT_SECS`, sending progress notifications. `deliver="library"` renames it, embeds the cover, moves it into `OUTPUT_DIR` and records it in the history; `"path"` leaves the file in `DOWNLOAD_DIR` and returns its path. Both return the lossless check verdict (FLAC, WAV and AIFF; `null` otherwise) | `download(copy_id="c81d0e2", deliver="library")` |
| `history(limit=20)` | The latest downloads, newest first, with their status (`success`, `delivered`, `failed`...) | `history(limit=10)` |
| `library_has(artist, title)` | Whether the library holds a file that looks like this track (the bot's duplicate check), with the closest matches | `library_has(artist="Nancy Sinatra", title="Bang Bang")` |
| `wishlist_add(track_id, wanted="any")` | Searches the track again every `WISHLIST_CHECK_HOURS`. `"any"` waits for any copy; `"better"` waits for a copy above the best one `search_copies` found for that track | `wishlist_add(track_id="t3fa9c1", wanted="better")` |
| `wishlist_list()` | Every wish, from every chat | `wishlist_list()` |
| `wishlist_remove(id)` | Removes a wish by the id `wishlist_list` shows | `wishlist_remove(id=4)` |
| `status()` | Whether slskd answers, downloads waiting on a Telegram button, MCP downloads running, the number of wishes, the upload cap and the version | `status()` |

A typical chain: `resolve_track` → `search_copies` with the track id → `download` with the copy id → `history`.

A wish added over MCP belongs to the owner, the first id in `TELEGRAM_ALLOWED_USERS`. The bot's wishlist
checker announces it in that private chat, and with `/auto` on there it fetches the copy as an auto search
does. The stdio server runs no checker: its wishes wait for the bot, which reads the same database when both
use the same `DATA_DIR`.

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
        "TELEGRAM_BOT_TOKEN": "123456:ABC...",
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

`TELEGRAM_BOT_TOKEN` is required because the configuration is shared with the bot; the stdio server never
talks to Telegram. Logs go to stderr.

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
