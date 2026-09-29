# Usage

## Example chat

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

## Flow

1. Send a song name to the Telegram bot (text message)
2. Bot resolves the track on **Spotify** (artist, title, duration, album)
3. Bot searches **slskd** (Soulseek) for FLAC files matching the track
4. Results are **scored** by duration match, audio quality, source reliability, and filename relevance
5. Bot presents the top matches — you **pick one** (or enable auto-mode)
6. File is downloaded, **renamed** to `Artist - Title.flac`, and placed in your output directory
7. Your existing tools (e.g., [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), Navidrome) pick it up from there

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| *(any text)* | Search for a song and show download options |
| `/import <spotify url>` | Import a Spotify playlist or album — review each track or auto-save all |
| `/cancel` | Cancel the active import or search |
| `/auto` | Toggle auto-download per chat: best match is downloaded and saved without picking or approval (persists across restarts) |
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

