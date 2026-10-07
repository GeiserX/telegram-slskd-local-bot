# Usage

## What the chat looks like

Text the bot a song name. It shows the Spotify matches when there is more than one, searches Soulseek
through slskd, ranks the copies with every lossless one of known length first, and sends you the one you pick so you can listen and read the
lossless check before you save it. A file whose spectrum reaches the top of its range says "Lossless OK";
one whose spectrum stops at an MP3-style cutoff says "Possible transcode", "Likely transcode" or "Fake
lossless", with the frequency where it stops. Then you tap Save to library or Reject.

## Flow

1. Send a song name to the Telegram bot (text message)
2. Bot resolves the track on **Spotify** (artist, title, duration, album)
3. Bot searches **slskd** (Soulseek) for audio files matching the track, lossless and lossy
4. Results are **scored** by duration match, audio quality, source reliability, and filename relevance; every lossless copy of known length is listed before every lossy one
5. Bot presents the top matches; you pick one (or enable auto-mode). The file is sent to the chat with its lossless check; you tap Save to library or Reject
6. File is renamed to `Artist - Title` with its own extension (`.flac`, `.mp3`...), tagged with the Spotify cover art, and placed in your output directory
7. Your existing tools (e.g., [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher), Navidrome) pick it up from there

## Telegram Bot Commands

| Command | Description |
|---------|-------------|
| *(any text)* | Search for a song and show download options |
| `/import <spotify url>` | Import a Spotify playlist or album — review each track or auto-save all |
| `/cancel` | Cancel the active import, album download or search |
| `/auto` | Toggle auto-download per chat: best match is downloaded and saved without picking or approval (persists across restarts) |
| `/deliver` | Switch this chat between library delivery (save after you approve) and chat delivery (the track is sent here, nothing is saved). Persists across restarts |
| `/format` | Pick the format of tracks sent into this chat: Original (default), MP3 320 kbps or Opus 192 kbps. Chat delivery only; persists across restarts |
| `/wishlist` | Tracks this chat is waiting for, each with a Remove button (see [Wishlist](#wishlist)) |
| `/status` | Show active searches and downloads |
| `/sweep` | Only for accounts in `LIBRARY_SWEEP_USERS`: look for better copies of the library's songs now; `/sweep status`, `/sweep reviews`, `/sweep force` (see [Library sweep](sweep.md)) |
| `/history` | Show recent download history |
| `/help` | Show help message |

## Wishlist

A track can wait for a copy that does not exist yet:

- When nothing is found, tap **🔔 Tell me when it appears**: any copy will do.
- Under every result list, tap **⏳ Wait for a better copy**: only a copy of a higher quality tier than
  the best one on the list counts. The tiers, lowest first: lossy under 128 kbps, 128, 192, 256 kbps or more (MP3-equivalent,
  so Opus 128 counts as 256), lossless 16-bit, lossless 24-bit.

The bot searches each wished track again once every `WISHLIST_CHECK_HOURS` (default 24), one search at a
time with `WISHLIST_PAUSE_SECS` (default 20) between them. It leaves a chat alone while that chat's own search,
download or import runs, and tries again an hour later. When a copy turns up, a chat with `/auto` on gets it
fetched and delivered like an auto search. Any other chat gets the list of copies that qualify, with the usual
pick buttons and **Stop waiting**. That list is a message of its own. It does not replace the chat's current
result list, and a new search does not replace it. The wish is done once a copy from it is saved or sent into
the chat; until then it stays and is not sent again before the next period, so a failed download leaves it
waiting. A track already on the wishlist is not added twice. `/wishlist` lists the chat's wishes with when
each was last checked.

## Chat delivery

Chat delivery is for someone who wants the song in Telegram and nowhere else. You pick the song exactly
as before, but nothing is written to the library or kept on disk: when the download finishes, the bot sends
the track into the chat with no Save or Reject buttons and deletes the downloaded file.

The upload cap is Telegram's 50 MB (50,000,000 bytes), or 2000 MB when the bot talks to a
[local Bot API server](getting-started.md#send-files-over-50-mb-a-local-bot-api-server). `TELEGRAM_MAX_UPLOAD_MB`
overrides either (see [Configuration](configuration.md)).

- A file at or under the cap is sent as it is, with the Spotify cover art embedded.
- A bigger file is converted to Opus at the highest of 192, 160, 128 or 96 kbps that fits under the cap, and
  the caption says so (for example "Converted to Opus 192 kbps, original 61 MB FLAC"). When even 96 kbps
  cannot fit, the bot says so and offers Retry and Try next result.
- `/format` picks MP3 320 kbps or Opus 192 kbps instead of the original: a file in another format is
  converted before sending and the caption says so (for example "Sent as MP3 320 kbps (original 31 MB FLAC)");
  a file already in that format goes as it is. When the converted file is still over the cap, the Opus steps
  above apply. Library delivery always keeps the original file.
- Copies are ranked by quality for their size, with no lossless-first split (see [Scoring](#scoring-algorithm)).
- Copies that fit under the cap rank ahead of copies that would have to be converted.
- `/auto` still decides whether you pick first; with both on, the best match arrives in the chat with no taps.
- `/import` sends every track of the playlist or album to the chat, one after another.

Turn it on per chat with `/deliver`, or fix it for some accounts with `TELEGRAM_CHAT_DELIVERY_USERS`: those
accounts always get chat delivery and `/deliver` cannot move them to the library, which keeps the library yours.
In a group chat without a `/deliver` setting, the user who started the search or import decides where that track goes.
One bot token can only run one bot process, so both modes live in the same instance.

## Album delivery

The best copy of a song usually sits inside the whole release on the same peer. Once a track is saved to the
library or sent into the chat, its message gets a **💿 Whole album from this source** button. It is the only way
in: the result list keeps its usual buttons.

1. Tap it. The bot lists the folder the track came from on that peer and edits the message to show it, for
   example "1971 - Meddle on vinylhoarder: 6 audio files, 268 MB, FLAC", with the first six file names. Only
   audio files count; covers, cue sheets and logs are left out. When the peer does not answer within 30 seconds,
   or is offline, the message says so and offers **Retry**.
2. Tap **Get all N**, or **Cancel** to go back to the album button. One status message follows the download
   file by file, with the file being fetched, its progress, and how many arrived, were skipped or failed so far.
3. The bot delivers each file as it lands, the way this chat delivers when you tap **Get all**:
    - **Library delivery**: saved into `OUTPUT_DIR` with no preview, named from its tags (or its file name when
      the tags are missing) through `FILENAME_TEMPLATE`, where `{track}` puts the track number in. Every file gets
      the album's Spotify cover. A file the library already has under the same name, accents and case aside, is
      skipped and its download deleted; the chosen track itself is usually one of them. The check goes by name
      only: a different track saved earlier under that exact name (an "Intro" from another album) counts too.
      Two files of this album with the same title are both kept, the second as "Artist - Title (1)".
    - **Chat delivery**: sent into the chat under the upload cap and in the `/format` format, converted when
      needed exactly like a single track, with the caption "03/09 Artist - Title".
4. A file that fails (the peer refused it, the transfer timed out, it was too big to send) is listed and skipped;
   the others go on. The status message ends with the summary: saved or sent N of M, how many were already in
   the library, and each failed file with the reason.

As with one track, the bot deletes each download once it is saved or sent and removes its finished transfer
from slskd. A file that could not be sent stays in `DOWNLOAD_DIR` for the hourly sweep. A file gives up when its
transfer moves no byte for `DOWNLOAD_TIMEOUT_SECS`; one still queued at the peer keeps waiting. The whole album
waits at most `ALBUM_TIMEOUT_SECS`, two hours by default (see [Configuration](configuration.md)). Files still
waiting when that runs out fail as timed out, and the summary says so. Every file the bot gives up on (timed
out, stalled, failed or cancelled) has its transfer cancelled in slskd, and whatever of it already landed is deleted.

One album runs per chat at a time, and a new search does not stop it. `/cancel` does: the bot stops waiting,
keeps every file that already arrived, and cancels the transfers still queued in slskd. After a restart the bot tells the chat how far an interrupted album got; files that landed
while it was down are saved to the library (library delivery) or left for the hourly sweep (chat delivery), and
slskd keeps the rest. The album button expires with its track
after `DOWNLOAD_CLEANUP_HOURS`. `/import` tracks do not get one.

## Scoring Algorithm

The bot keeps every audio format. Lossless means FLAC, WAV, AIFF, ALAC, APE, WavPack, TTA and TAK; lossy means
MP3, AAC, M4A, Ogg, Opus and WMA. M4A counts as lossy because the extension cannot tell ALAC from AAC.

Before scoring, the **title guard** drops copies named after another track. The track title is reduced to
its words: version noise ("- 2011 Remaster"), anything in parentheses or brackets, punctuation and accents go,
and so do the words a, the, of, and, feat and ft. A copy whose file name and parent folder name contain none of
the remaining words is hidden, and the results header says how many, for example "Found 4 matches, all
lossless (12 unrelated hidden)". A title of one word keeps that word, and a title made only of those common
words keeps them. The guard never empties the list: when it would hide every copy, it is skipped and the log
says so. A direct search skips it, as it skips the other filters.

Search results are ranked by:

1. **Duration match** (40 pts): Compared to Spotify duration. Within ±5s = perfect, ±10s = acceptable, >30s = excluded.
   Many peers share files without a length. For a lossy file with a known bitrate the length is estimated from
   size and bitrate (size × 8 / (kbps × 1000)) and scored like a reported one, so a 4-minute MP3 is excluded from
   a 23-minute search. A copy whose length stays unknown (a lossless file without one, or a lossy file without a
   bitrate) earns no duration points and never leads the list: in library delivery it ranks among the lossy
   copies, in chat delivery after every copy of known length.
2. **Audio quality** (25 pts), which depends on where the track goes:
    - **Library delivery**: every lossless copy is listed before every lossy one, except a lossless copy of a
      different version (more than 30 s off Spotify's length, kept only by the last-resort search) or without a
      known length, which ranks among the lossy copies by score. A lossless copy scores by
      bit depth (24-bit 15, 16-bit 10) and sample rate (88.2 kHz and above 10, 48 kHz 7, 44.1 kHz 6). A lossy
      copy scores by bitrate: 256 kbps or more 25, 192 kbps 20, 128 kbps 10, under 128 kbps 1. The bitrate is
      first scaled to what it sounds like in MP3 terms: Opus counts double, AAC and Vorbis one and a half times
      (an Opus file at 128 kbps is top tier, an AAC one at 128 kbps is a step below).
    - **Chat delivery**: no lossless-first split; the points measure quality for the size. A lossless file and
      a lossy one at 256 kbps or more both start at 25 (192 kbps 20, 128 kbps 10, under 128 kbps 1), then lose
      up to 5 points as the file grows from 5 MB to 50 MB, or to the upload cap when that is smaller, and
      with a local Bot API server up to 20 more points from 50 MB to the cap, on a logarithmic scale so the
      first doubling already counts
      (1 MB = 1,000,000 bytes, as Telegram counts). With a 2000 MB cap a 300 MB file pays the same 5 points as a
      50 MB one. A 10 MB MP3 at 320 kbps beats a 35 MB CD-quality
      FLAC of the same song, and a 20 MB FLAC beats a 20 MB MP3 at 128 kbps. A copy over the cap scores as the
      Opus it will be sent as: its tier minus 8.
3. **Source reliability** (20 pts): Free upload slots, fast upload speed, short queue. In chat delivery these
   count half, so quality for its size can outweigh a fast peer
4. **Filename relevance** (15 pts): Artist and title words found in the filename

Results containing excluded keywords (live, remix, etc.) are automatically filtered out, unless the original track title also contains that keyword.

A lossless file downloaded from a song name, whether you picked the copy or `/auto` did, is checked for a lossy
cutoff before it is offered to you: a spectrum that stops around 16 kHz means the file was most likely
transcoded from MP3, and it is marked "Fake lossless". The check needs the analysis extra (installed in the
Docker image; `pip install 'telegram-slskd-local-bot[analysis]'` elsewhere). It reads FLAC, WAV and AIFF; an APE,
WavPack, TTA, TAK or ALAC file says "Lossless check: not checked", and a lossy file gets no check. Without the
analysis extra every lossless file says "not checked". Auto-mode saves the file
without waiting for you once the check passes.

**The lossless gate.** A copy going to the library (picked by you, by `/auto`, or an `/import` track) that the
check shows was made from a lossy file never reaches it: the bot deletes the copy and downloads the next one on
the result list, and the status message says which copy was rejected and why:

```
🚫 #1 rejected: transcoded from lossy, cutoff 14.0 kHz
⬇️ Downloading #2...
```

The line is drawn at a cutoff below 16 kHz for a 44.1 or 48 kHz file, and below 19.5 kHz for a file above
48 kHz, which is a hi-res file upsampled from a lossy one. A cutoff between those and the top of the range
("Possible transcode", "Likely transcode" at 17 kHz and up) is shown but not rejected. After
`LOSSLESS_GATE_MAX_REJECTIONS` rejected copies (3) the next copy is kept whatever it is, with a line saying it
would have been rejected. When every copy on the list is rejected nothing is kept. In both cases the message
offers "Wait for a better copy". The wishlist ranks copies by the format they advertise, so it cannot tell a
fake FLAC from a real one: after fakes it waits for a copy of a higher tier. Chat delivery is not gated, and
neither are album downloads. `LOSSLESS_GATE=false` turns the gate off.
