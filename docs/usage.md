# Usage

## What the chat looks like

Text the bot a song name. It shows the Spotify matches when there is more than one, searches Soulseek
through slskd, ranks the FLAC copies, and sends you the one you pick so you can listen and read the
lossless check before you save it. A file whose spectrum reaches the top of its range says "Lossless OK";
one whose spectrum stops at an MP3-style cutoff says "Possible transcode", "Likely transcode" or "Fake
lossless", with the frequency where it stops. Then you tap Save to library or Reject.

## Flow

1. Send a song name to the Telegram bot (text message)
2. Bot resolves the track on **Spotify** (artist, title, duration, album)
3. Bot searches **slskd** (Soulseek) for FLAC files matching the track
4. Results are **scored** by duration match, audio quality, source reliability, and filename relevance
5. Bot presents the top matches; you pick one (or enable auto-mode). The file is sent to the chat with its lossless check; you tap Save to library or Reject
6. File is renamed to `Artist - Title.flac`, tagged with the Spotify cover art, and placed in your output directory
7. Your existing tools (e.g., [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), Navidrome) pick it up from there

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| *(any text)* | Search for a song and show download options |
| `/import <spotify url>` | Import a Spotify playlist or album — review each track or auto-save all |
| `/cancel` | Cancel the active import or search |
| `/auto` | Toggle auto-download per chat: best match is downloaded and saved without picking or approval (persists across restarts) |
| `/deliver` | Switch this chat between library delivery (save after you approve) and chat delivery (the track is sent here, nothing is saved). Persists across restarts |
| `/status` | Show active searches and downloads |
| `/history` | Show recent download history |
| `/help` | Show help message |

## Chat delivery

Chat delivery is for someone who wants the song in Telegram and nowhere else. You pick the song exactly
as before, but nothing is written to the library or kept on disk: when the download finishes, the bot sends
the track into the chat with no Save or Reject buttons and deletes the downloaded file.

- A file of 50 MB or less is sent as it is, with the Spotify cover art embedded.
- A bigger file is converted to Opus at the highest of 192, 160, 128 or 96 kbps that fits under 50 MB, and
  the caption says so (for example "Converted to Opus 192 kbps, original 61 MB FLAC"). When even 96 kbps
  cannot fit, the bot says so and offers Retry and Try next result.
- Copies that fit under 50 MB rank ahead of copies that would have to be converted.
- `/auto` still decides whether you pick first; with both on, the best match arrives in the chat with no taps.
- `/import` sends every track of the playlist or album to the chat, one after another.

Turn it on per chat with `/deliver`, or make it the default for some users with `TELEGRAM_CHAT_DELIVERY_USERS`.
In a group chat without a `/deliver` setting, the user who started the search or import decides where that track goes.
One bot token can only run one bot process, so both modes live in the same instance.

## Scoring Algorithm

Search results are ranked by:

1. **Duration match** (40 pts): Compared to Spotify duration. Within ±5s = perfect, ±10s = acceptable, >30s = excluded
2. **Audio quality** (25 pts): 24-bit scores 15, 16-bit 10; 88.2 kHz and above scores 10, 48 kHz 7, 44.1 kHz 6
3. **Source reliability** (20 pts): Free upload slots, fast upload speed, short queue
4. **Filename relevance** (15 pts): Artist and title words found in the filename

Results containing excluded keywords (live, remix, etc.) are automatically filtered out, unless the original track title also contains that keyword.

A FLAC downloaded from a song name, whether you picked the copy or `/auto` did, is checked for a lossy
cutoff before it is offered to you: a spectrum that stops around 16 kHz means the file was most likely
transcoded from MP3, and it is marked "Fake lossless". The check needs the analysis extra (installed in the
Docker image; `pip install 'telegram-slskd-local-bot[analysis]'` elsewhere). Auto-mode saves the file
without waiting for you, whatever the check says. Tracks from `/import` are downloaded and saved without
the check.
