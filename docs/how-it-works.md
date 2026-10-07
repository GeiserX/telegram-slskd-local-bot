# How it works

Two front ends drive one pipeline. The Telegram bot reads your messages and taps; the [MCP server](mcp.md)
takes tool calls from an agent such as Claude Code. Both call the same code to resolve a track, search and rank
copies, download and check them, and save to the library, and both write the same SQLite history and wishlist.
The steps below are the Telegram side; the MCP tools run the same steps without the buttons.

**Resolve.** When the song name looks like something already in your library, the bot shows the similar files and asks before it goes on. Then the name goes to Spotify: the bot takes artist, title, album and duration from the match, and when Spotify returns more than one distinct track it shows them and lets you pick. `/import` does the same for every track of a Spotify playlist or album. Over [MCP](mcp.md#how-a-track-is-resolved), a song Spotify has no confident match for is looked up on MusicBrainz, and then on Soulseek by its file names.

**Search and score.** The bot asks your slskd to search Soulseek and keeps every audio file, lossless and lossy. Each copy is scored on how close its duration is to Spotify's, its audio quality (bit depth and sample rate for a lossless file, bitrate for a lossy one), the uploader's free slot, speed and queue, and how well the file name matches; live, remix and other excluded versions are dropped unless the Spotify title has the same word. Every lossless copy is listed before every lossy one, so a lossy copy shows up only below the lossless ones, or alone when the song has no lossless copy. The one exception is a lossless copy of a different version (more than 30 seconds off Spotify's length), which ranks among the lossy copies by score. The best `MAX_RESULTS` copies are shown as buttons.

**Listen and approve.** The copy you pick is downloaded through slskd. For a download started from a song name (picked by you or by `/auto`) the bot checks the spectrum of a lossless file for a lossy cutoff, then sends the file to the chat with the verdict: the file itself when it is under the upload cap (Telegram's 50 MB, or 2000 MB with a [local Bot API server](getting-started.md#send-files-over-50-mb-a-local-bot-api-server)), an Opus copy of the whole song when it is over, and a one-minute preview only when even the Opus copy is too big. You tap Save to library or Reject. In auto-mode the best copy is saved without asking. `/import` tracks are downloaded and saved without the spectrum check; with auto-save on they go straight to the library.

**Save and sweep.** A saved file is renamed with `FILENAME_TEMPLATE` (`Artist - Title` by default, plus the downloaded file's own extension), tagged with the Spotify cover art and written to `OUTPUT_DIR`; the download in `DOWNLOAD_DIR` is deleted, and so is a rejected one. Every hour the bot also deletes files in `DOWNLOAD_DIR` that nobody picked up for `DOWNLOAD_CLEANUP_HOURS`, and never touches a transfer still in flight.

In a chat-delivery chat (`/deliver`) the copies are ranked by quality for their size instead, with no lossless-first split, and the track itself is sent, converted to Opus when over the cap or to MP3 or Opus when `/format` asks for it, and nothing is written to `OUTPUT_DIR`; the download is deleted once it is sent.

**Take the whole album.** A track that was saved or sent offers the folder it came from on the same peer. The
bot lists that folder through slskd, asks before it fetches, then queues every audio file in one request and
waits for them one after another, each delivered as it lands: saved to the library with names from its tags
and the album's cover (files the library already has are skipped), or sent into the chat in the `/format`
format. The job is kept in SQLite file by file, so `/cancel` and a restart both know what arrived.

**Wait for a copy.** A track with no copy, or with no good one, can go on the wishlist. A checker in the bot
process wakes every hour, takes each wish whose `WISHLIST_CHECK_HOURS` have passed and runs one search for it,
with `WISHLIST_PAUSE_SECS` between two searches, skipping a chat whose own search or download is running. A copy
that qualifies is fetched and delivered in a chat with `/auto` on, or listed with pick buttons in any other
chat, in a message of its own. The wish ends when a copy from it is saved or sent. Wishes added over MCP go to
the owner's chat.

```
┌──────────────────┐     ┌──────────────┐     ┌──────────────────┐
│ Telegram or MCP  │────▶│  Spotify API │     │  slskd (Soulseek)│
│  (user input)    │     │  (metadata)  │     │  (search/download)│
└──────────────────┘     └──────────────┘     └──────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Music Downloader Service                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐│
│  │ Resolver │─▶│ Searcher │─▶│  Scorer  │─▶│ File Processor   ││
│  │(Spotify) │  │ (slskd)  │  │(ranking) │  │(rename + move)   ││
│  └──────────┘  └──────────┘  └──────────┘  └──────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                                                      │
                                                      ▼
                                              ┌──────────────┐
                                              │ Music Library │
                                              │ (audio files) │
                                              └──────────────┘
```

