# How it works

**Resolve.** When the song name looks like something already in your library, the bot shows the similar files and asks before it goes on. Then the name goes to Spotify: the bot takes artist, title, album and duration from the match, and when Spotify returns more than one distinct track it shows them and lets you pick. `/import` does the same for every track of a Spotify playlist or album.

**Search and score.** The bot asks your slskd to search Soulseek and keeps the FLAC files; only when there is none does it fall back to other audio formats. Each copy is scored on how close its duration is to Spotify's, its bit depth and sample rate, the uploader's free slot, speed and queue, and how well the file name matches; live, remix and other excluded versions are dropped unless the Spotify title has the same word. The best `MAX_RESULTS` copies are shown as buttons.

**Listen and approve.** The copy you pick is downloaded through slskd. The bot checks its spectrum for a lossy cutoff, then sends the file to the chat with the verdict: the FLAC itself when it is under Telegram's 50 MB limit, an Opus copy of the whole song when it is over, and a one-minute preview only when even the Opus copy is too big. You tap Save to library or Reject. In auto-mode, and for auto-save imports, the best copy is saved without asking.

**Save and sweep.** A saved file is renamed with `FILENAME_TEMPLATE` (`Artist - Title.flac` by default), tagged with the Spotify cover art and written to `OUTPUT_DIR`; the download in `DOWNLOAD_DIR` is deleted, and so is a rejected one. Every hour the bot also deletes files in `DOWNLOAD_DIR` that nobody picked up for `DOWNLOAD_CLEANUP_HOURS`, and never touches a transfer still in flight.

```
┌──────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  Telegram Bot    │────▶│  Spotify API │     │  slskd (Soulseek)│
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
                                              │  (FLAC files) │
                                              └──────────────┘
```

