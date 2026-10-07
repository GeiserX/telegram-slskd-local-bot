# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.18.0] - 2026-10-07

### Added

- The lossless gate keeps lossless files made from lossy ones out of the
  library. A copy headed for the library (a track you save, an `/import` track,
  the MCP `download` with `deliver="library"`) whose spectrum stops below
  16 kHz (44.1 or 48 kHz files) or below 19.5 kHz (hi-res files, an upsampled
  fake) is deleted and the next copy on the result list downloaded instead. The
  status message says which copy was rejected and why ("#1 rejected: transcoded
  from lossy, cutoff 14.0 kHz"), and so does the MCP result (`rejected`). After
  `LOSSLESS_GATE_MAX_REJECTIONS` rejected copies (3) the next copy is kept and
  flagged; when nothing is kept, or a flagged copy is, the bot offers "Wait for
  a better copy". `LOSSLESS_GATE=false` turns it off. Chat delivery and album
  downloads are not gated. Rejected copies are recorded in the history as
  `lossy_source`
- `ORPHAN_SWEEP_HOURS` (default 6): leftover files in `DOWNLOAD_DIR` that
  nothing waits on are deleted after this many hours, at startup and hourly

### Changed

- `/import` tracks headed for the library now run the lossless check, which
  the gate needs
- `DOWNLOAD_CLEANUP_HOURS` now only sets how long a download waiting on a
  button is kept; the sweep of leftover files follows `ORPHAN_SWEEP_HOURS`.
  `DOWNLOAD_CLEANUP_HOURS=0` no longer turns the sweep off: set
  `ORPHAN_SWEEP_HOURS=0` for that

### Fixed

- A download the bot gave up on kept running in slskd: after a timeout the
  files landed later with nobody to pick them up. Every transfer the bot gives
  up on (timed out, failed, or cancelled, for one track or an album file) is
  now cancelled in slskd and removed from its list, and whatever of it already
  landed in `DOWNLOAD_DIR` is deleted

## [0.17.1] - 2026-10-04

### Fixed

- slskd reports a transfer as complete a moment before it moves the file from
  its incomplete folder into the downloads folder, so the bot sometimes answered
  "Downloaded file not found on disk" for a file that landed a second later.
  The lookup now keeps looking for up to 10 seconds, for single tracks and for
  album files alike
- Album files in WAV or AIFF were always named from their file name: their ID3
  tags (artist, title, album, track number) are now read like every other format
- The album duplicate check compared names without the format, so a library
  MP3 made the bot discard the lossless copy of the same track. A lossy library
  copy no longer counts against a lossless album file; a lossless or same-format
  copy still does
- `{track}` in `FILENAME_TEMPLATE` with a file that has no number no longer eats
  leading punctuation from the artist or title, and cleans up "({track})" and
  "{track}. " separators too
- "Artist - Album - 03 - Title.mp3" file names now parse their track number

## [0.17.0] - 2026-10-04

### Added

- **Whole album from this source.** Once a track is saved to the library or
  sent into the chat, its message offers the folder it came from on the same
  peer, which is usually the complete release in the same quality. The tap
  lists the folder (file count, size, formats, the first six names) and asks;
  a peer that does not answer gets Retry. Get all queues every audio file in
  one slskd request and follows them in one status message. Library delivery
  saves each file as it lands, named from its tags through
  `FILENAME_TEMPLATE` (`{track}` adds the track number) with the album's
  Spotify cover, and skips files the library already has. Chat delivery sends
  each file in the `/format` format under the upload cap, captioned
  "03/09 Artist - Title". A failed file is listed with its reason and the
  rest go on; the summary says how many arrived, were skipped or failed
- `ALBUM_TIMEOUT_SECS` (default 7200): the most a whole album may take. An
  album file gives up only when its transfer moves no byte for
  `DOWNLOAD_TIMEOUT_SECS`, so a slow peer is waited on
- `/cancel` stops a running album: what arrived stays and slskd keeps the
  transfers still queued. A new search does not stop it
- After a restart the bot tells the chat how far an interrupted album got,
  and saves the files that landed while it was down (library delivery; a
  chat-delivery album leaves them for the hourly sweep). Albums a stdio MCP
  server runs are its own: the bot's restart leaves them alone
- MCP: `album_listing` and `album_download` take the folder of a copy id;
  `album_download` reports a skipped file with `skipped` true

### Changed

- A saved or sent track keeps its pending row (no file, no transfer) for the
  album button until `DOWNLOAD_CLEANUP_HOURS`; `/status` and the MCP
  `status` do not count it as a pending download

### Fixed

- When slskd lists a file twice (an earlier attempt's finished or failed
  transfer next to a new one), the download wait follows the running one
  instead of the old record, which failed a retry within seconds

## [0.16.2] - 2026-10-04

### Fixed

- The chat ranking's size cost above 50 MB was linear to the cap and worth 10
  points, so a 907 MB hi-res copy from a fast peer still outranked 57 MB MP3
  320 copies from slow peers. The cost is now logarithmic and worth 20 points
  at the cap, and the chat profile counts the peer's slot, speed and queue
  at half weight, so a tenfold size difference beats any source advantage

## [0.16.1] - 2026-10-04

### Fixed

- With a 2000 MB cap the chat ranking's size cost stopped at 50 MB, so eight
  908 MB hi-res copies of a 23-minute track tied with its 57 MB MP3 320 and
  won on source points. The cost now keeps growing from 50 MB to the cap

## [0.16.0] - 2026-10-04

### Added

- **Files up to 2 GB in the chat.** The bot can talk to a local Telegram Bot
  API server (`TELEGRAM_API_BASE_URL`) instead of Telegram's cloud server.
  The upload cap then defaults to 2000 MB instead of 50 MB, the bot sends
  originals as they are instead of converting them to Opus, and requests get 600 s timeouts
  (`TELEGRAM_UPLOAD_TIMEOUT_SECS`). The chat ranking keeps its size cost
  between 5 MB and 50 MB, so a 300 MB FLAC is not buried under small MP3s.
  Getting started has the steps, including the one-time `logOut` from the
  cloud server.
- **Compose profile `bigfiles`.** `docker compose --profile bigfiles up -d`
  also starts `aiogram/telegram-bot-api:10.3` with its own data volume, reading
  `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from `.env`. Without the profile
  nothing changes.
- **`/format` picks the send format per account.** Original (default),
  MP3 320 kbps or Opus 192 kbps for every track sent into the chat. A file
  already in that format goes as it is; any other is converted with the
  Spotify cover embedded, and the caption says so. Library saves always keep
  the original. If the conversion fails, the bot sends what it would have sent
  before.
- **A wishlist.** "Tell me when it appears" when nothing is found, and "Wait
  for a better copy" under every result list, which waits for a copy of a
  higher quality tier than the best one listed (lossy under 128 kbps, 128,
  192, 256 kbps or more, lossless 16-bit, lossless 24-bit). The bot searches
  each wish again every `WISHLIST_CHECK_HOURS` (default 24), one search at a
  time with `WISHLIST_PAUSE_SECS` between them, and skips a chat while its own
  search or download runs. A chat with `/auto` on gets the copy fetched; any
  other chat gets the list to pick from, in a message that neither replaces nor
  is replaced by the chat's own searches. A wish ends when a copy from it is
  saved or sent, not when the download starts. A track is never on the list
  twice. `/wishlist` lists the wishes with a Remove button each.
- **An MCP front end.** An agent such as Claude Code can resolve a track,
  search copies, download one into the library or to a path, read the
  history, check the library and manage the wishlist, through the same
  pipeline as the bot. Over stdio with `python -m music_downloader mcp`, or over
  streamable HTTP from the bot process with `MCP_PORT` and a bearer token in
  `MCP_TOKEN`. The bot announces wishes added over MCP in the owner's chat, the
  first id in `TELEGRAM_ALLOWED_USERS`. The stdio server needs no
  `TELEGRAM_BOT_TOKEN`. `MCP_HOST` defaults to `127.0.0.1`; the compose file
  sets `0.0.0.0` for the container.

## [0.15.0] - 2026-10-04

### Changed

- **The pipeline left the Telegram handler.** Resolving a track on Spotify,
  searching and ranking, downloading and checking, and saving to the library
  now live in `music_downloader.pipeline`, which never imports `telegram`.
  `bot/handlers.py` keeps the Telegram side: reading messages, editing them,
  sending files. A test fails if a pipeline module ever imports `telegram`, so
  another front end can drive the same pipeline. One side effect: Save on a
  preview now moves the file in a background thread, as auto-mode already did,
  so a big copy no longer stalls the bot. An import track whose save fails now
  gets a "process failed" row in `/history`, as a plain search already did.
- **Every message is HTML.** Markdown broke whenever a track, file or
  Soulseek user name held `*`, `_`, `[` or a backtick. Messages now use
  Telegram's HTML mode and every outside value is escaped, so no name can
  break a message.
- **Long tracks rank the right file first.** A 23-minute search used to list
  unrelated album tracks first, because a copy with no length earned flat
  duration points. Now a lossy copy without a length gets one estimated from
  its size and bitrate and is scored like any other. A copy whose length stays
  unknown earns no duration points and never leads the list. A title guard
  also hides copies whose file and folder names share no word with the title,
  and the header says how many, for example "(12 unrelated hidden)". The guard
  never empties the list and skips direct search.
- **The duplicate check reads an index.** It used to list `OUTPUT_DIR` and
  fuzzy-match every file on each message, blocking the bot, and it ignored
  subfolders. A `library_index` table in SQLite now holds every audio file
  under `OUTPUT_DIR`, subfolders included. The bot rebuilds it in a thread at
  startup and every hour, and each save adds its row. A query looks at no more
  than 300 rows, in a thread. The warning and its threshold are unchanged. A
  rebuild that finds no audio file (a missing or unmounted `OUTPUT_DIR`) keeps
  the previous index instead of emptying it.
- **The upload cap is Telegram's real one.** Telegram's bot upload limit is
  50,000,000 bytes, not 50 MiB (52,428,800), so a file just under 50 MiB was
  treated as fitting and Telegram refused it. The cap now comes from
  `TELEGRAM_MAX_UPLOAD_MB` (default 50, 1 MB = 1,000,000 bytes, Telegram's
  unit). The send paths, the chat ranking, the Opus bitrate choice and the
  messages all read it, and sizes in the result list use the same unit.

### Added

- **Buttons survive a restart.** Searches and downloads waiting on a button
  are kept in SQLite (`pending_searches`, `pending_downloads`), so Save,
  Reject, Retry, Try next and the result pages still work after the bot
  restarts. A button whose entry expired says "This button expired after a
  restart". Downloads still waiting after `DOWNLOAD_CLEANUP_HOURS` are dropped
  with their file (counted from when the file landed; a download still running
  is never dropped). Result lists older than that are dropped at the next start.
- **`/health` checks something.** It used to answer 200 no matter what. It now
  answers 200 only while the bot polls Telegram (a successful poll in the last
  120 s) and slskd answers (in the last 60 s), and 503 with the failed check
  otherwise. `/ready` stays a constant 200.
- **slskd's transfer list is tidied.** Once a download is saved, sent,
  rejected, expired or dropped by a new search, its finished transfer is
  removed from slskd too.
- **Retry after a failed chat send in an import.** In a review-mode import
  sent to the chat, a failed send now offers Retry, Try next result, Mark
  failed and Skip instead of stalling the import.
- `TELEGRAM_MAX_UPLOAD_MB`, see above.
- A troubleshooting entry for slskd writing downloads as root, which stops the
  bot from deleting them: set `SLSKD_UMASK=0002` on slskd and run the bot with
  the root group (`user: "1000:0"`).

### Fixed

- A failed download inside a review-mode import offered a Retry that ran the
  plain search flow, so the import track never finished. It now retries
  inside the import.
- Mark failed and Skip on an import track now drop that track's waiting
  downloads and their files.

## [0.14.2] - 2026-10-03

### Changed

- Lossy copies are tiered by what their bitrate sounds like, not by the raw
  number: Opus counts double and AAC or Vorbis one and a half times, so an
  Opus file at 128 kbps ranks with an MP3 at 256 kbps instead of with an MP3
  at 128 kbps. Applies to both the library and the chat ranking
- Cover art is now embedded into WAV and AIFF saves as well (an ID3 chunk,
  the same frame as MP3)

### Fixed

- The auto-save import status lines escape the artist and title, so a name
  with Markdown characters no longer breaks that edit

## [0.14.1] - 2026-10-03

### Fixed

- Re-release of 0.14.0. The `v0.14.0` tag was cut from the 0.13.1 commit by
  mistake, so the image and release published under that name carried none
  of the changes below; they have been removed. 0.14.1 is the first build
  with them

## [0.14.0] - 2026-10-03

### Changed

- **Lossless first, lossy below.** Before, the search kept FLAC only and fell
  back to other formats when no FLAC scored. Now every audio format is kept
  from the start. In library delivery every lossless copy (FLAC, WAV, AIFF,
  ALAC, APE, WavPack, TTA, TAK) is listed before every lossy one (MP3, AAC,
  M4A, Ogg, Opus, WMA), so a song with no lossless copy still offers the best
  lossy copies, ranked by bitrate. A lossless copy of a different version,
  more than 30 seconds off the Spotify length, does not jump ahead: it ranks
  among the lossy copies by score. The list header counts both, for example "Found
  150 matches (142 lossless, 8 lossy)", and every line shows its format.
  Auto-mode still picks a lossless copy whenever there is one. A lossy file
  saves under its own extension with the cover art embedded (MP3, M4A, Ogg
  Vorbis and Opus).
- **Chat delivery ranks by quality for the size.** A chat in chat delivery no
  longer prefers hi-res: a lossless file that fits under 50 MB and a lossy one
  at 256 kbps or more count the same, 192 kbps a step below, 128 kbps well
  below, and every file then loses a few points as it grows toward 50 MB. A
  10 MB MP3 at 320 kbps now beats a 35 MB FLAC of the same song. Copies over
  50 MB still come after every copy that fits, ranked as the Opus they will be
  sent as.
- **"Lossless" where the project said "FLAC".** Messages, docs and code now say
  lossless when they mean any lossless format, and FLAC only for the codec.
  The spectrum check, now called the lossless check, also reads WAV and AIFF;
  APE, WavPack, TTA, TAK and ALAC files say "Lossless check: not checked"
  instead of nothing. The module `processor/flac_analyzer.py` is now
  `processor/lossless_analyzer.py`, and `SlskdClient.parse_results` no longer
  takes `flac_only`: it always returns every audio format.

## [0.13.1] - 2026-10-03

### Changed

- Accounts listed in `TELEGRAM_CHAT_DELIVERY_USERS` are now fixed to chat
  delivery: `/deliver` shows the mode without a toggle and the switch to the
  library is refused, so sharing the bot with someone never lets them write to
  your library. 0.13.0 let a listed account flip itself to the library

## [0.13.0] - 2026-10-03

### Added

- **Chat delivery.** A chat can now get the song in Telegram instead of the
  library. You pick the song as before; when the download finishes, the bot
  sends the track into the chat, saves nothing anywhere and deletes the
  downloaded file. A file over Telegram's 50 MB bot limit is converted to Opus
  at the highest of 192, 160, 128 or 96 kbps that fits, and the caption says
  so; copies that fit under 50 MB rank ahead of copies that would need
  converting. `/deliver` switches a chat between library and chat delivery
  (persisted across restarts), and `TELEGRAM_CHAT_DELIVERY_USERS` makes chat
  delivery the default for the users it lists. It works with `/auto` (the best
  match arrives with no taps) and with `/import`. One bot token can run only
  one bot process, so both modes share the same instance
- `/history` marks delivered tracks with 📨

## [0.12.1] - 2026-09-30

### Security

- The image now installs urllib3 2.8.0 or later. 0.12.0 shipped urllib3 2.7.0,
  which three advisories cover: an HTTPS proxy TLS
  setting that can be ignored (GHSA-8988-9cw3-xx77), an unbounded chunk-size
  line held in memory (GHSA-vxq7-64xx-v4gw) and an infinite loop in chunked
  deflate streaming (GHSA-gh4c-6fx4-qh6g). `uv.lock` moves to 2.8.0 as well

## [0.12.0] - 2026-08-19

### Added

- **Auto-download mode is now real.** `/auto` (per chat, persisted across
  restarts; `AUTO_MODE` is the default for chats that never toggled) skips the
  picker and the approval step: the best-ranked match is downloaded, saved to
  the library, tagged with artwork, and confirmed in one message
- **Unattended playlist imports.** The import confirm screen offers
  "Review each track" or "Auto-save all"; auto-save imports run start-to-finish
  with no taps and no Telegram preview uploads, marking failed tracks and
  continuing instead of pausing
- `/status` shows import progress (mode, processed/total, saved/failed/skipped)
- **Automatic orphan cleanup**: an hourly sweep deletes files in
  `DOWNLOAD_DIR` older than `DOWNLOAD_CLEANUP_HOURS` (default 24, `0`
  disables) and prunes emptied per-user folders. In-flight downloads are
  protected explicitly, and mtime-based age keeps actively-written slskd
  transfers safe. Production had accumulated 4.8 GB of abandoned files
  before this existed

## [0.11.0] - 2026-08-18

### Fixed

- A hung (not down) slskd could block the bot forever: the HTTP client now has
  a 15s per-request timeout and all synchronous slskd calls run off the event
  loop (`asyncio.to_thread`)
- Crashes are no longer invisible: a global error handler logs the traceback
  and tells the user something went wrong
- `/status` crashed while a search was awaiting a Spotify pick, and showed
  every chat's activity to every user — now guarded and scoped per chat
- Edited Telegram messages crashed every handler (`update.message` is `None`);
  handlers now only react to new messages
- Download IDs restarted at 1 after a restart, letting a stale Reject button
  delete the wrong file — IDs are now random and unique
- Result keyboards from an older search could download from the current result
  list under the old labels — callbacks now carry a search id that must match
- `/import` could brick itself permanently (job row written before confirm,
  `/cancel` only checked memory) — `/cancel` now falls back to the database and
  stale jobs are cancelled at startup
- A full or read-only disk deleted the whole database (`OperationalError` is a
  `DatabaseError` subclass); only genuine corruption now triggers recovery,
  and the old file is moved aside instead of deleted
- Reject/dismiss failed silently on a read-only `/downloads` mount; deletes are
  now guarded and the shipped compose mounts the volume read-write
- Telegram flood control (`RetryAfter`) is waited out instead of escaping
- `MAX_RESULTS=0` no longer divides by zero

### Added

- Live download progress: the "Downloading…" message updates with percent,
  speed, and queued state instead of staying frozen for up to 10 minutes
- "No results" replies now offer the direct Soulseek search escape hatch
  instead of dead-ending
- Superseded searches/downloads are marked "⏹ Superseded by a newer request"
  instead of sitting frozen forever
- The command menu is registered with Telegram (`/` autocomplete), and
  `/import` and `/cancel` are finally listed in `/help` and the README
- The unauthorized reply includes your Telegram user ID so you can copy it
  straight into `TELEGRAM_ALLOWED_USERS`

### Changed

- README/docs corrected: empty `TELEGRAM_ALLOWED_USERS` denies everyone
  (fail-closed), `MAX_RESULTS` defaults to 10, the quickstart pins the current
  image, and `AUTO_MODE` is documented as reserved (it currently has no effect)

## [0.10.0] – [0.10.5] - 2026-05-19 – 2026-05-24

### Added

- Direct Soulseek search prompts for `Artist - Title` so files are saved
  exactly as requested

### Fixed

- Spotify result ranking: artist-name matches in the query are boosted with
  word-level matching (original artists surface above tribute/karaoke noise)
- Single-source version via `importlib.metadata`; packaging fixed with
  setuptools `find_packages`
- Hardened error handling and removed dead code (0.10.0)

## [0.9.0] – [0.9.6] - 2026-05-13 – 2026-05-19

### Added

- Playlist/album import (`/import <spotify url>`) with per-track review
- Direct Soulseek search flow embedded in the Spotify keyboard
- Hi-res audio (24-bit/96kHz) preferred over CD quality in ranking
- PyPI publishing and Codecov coverage reporting

### Fixed

- Searches always stop before fetching responses from slskd (empty-result bug)
- Downloaded files always cleaned up after approve/reject/dismiss
- Duration filtering skipped for direct searches

## [0.8.0] – [0.8.3] - 2026-04-01 – 2026-04-10

### Added

- App icon, Docker Hub README sync, SECURITY.md, Dependabot

### Fixed

- FLAC Vorbis comment tags deduplicated on save (legitimate multi-value tags
  preserved)
- Pagination buttons no longer fail on stale callback queries

## [0.7.0] - 2026-02-23

### Added

- Cancel-on-new-message: sending a new query while mid-search or mid-download
  cancels all in-flight operations for that chat instantly (generation counter
  + asyncio task cancellation)
- Large file OGG conversion: files >50 MB are converted to OGG Opus and sent
  in full; only trimmed to ~1 min if the OGG still exceeds 50 MB
- `convert_to_ogg()` utility (ffmpeg-based, handles any audio format)
- ffmpeg added as Docker system dependency for reliable audio conversion
- Dismiss-on-approve: saving one download to library automatically cancels all
  other pending downloads for the same chat (buttons removed, messages updated)
- `approval_message_id` tracking on `PendingDownload` for programmatic message edits

### Changed

- Results keyboard is now locked after selecting a download (no duplicate picks)
- Preview clips use ffmpeg → OGG Opus instead of soundfile (handles all formats)
- Default preview trim duration changed from 30 s to 60 s
- Stale approve/reject buttons now show "⏹ Cancelled" instead of silently
  disappearing

### Fixed

- "File too large for Telegram" text-only fallback no longer appears; files are
  always sent as playable audio (OGG conversion or trimmed clip)

## [0.4.0] - 2026-02-09

### Added

- FLAC authenticity analysis via spectral cutoff detection on downloaded files
  - Verdicts: AUTHENTIC, WARNING, SUSPICIOUS, FAKE shown before save approval
  - Uses Welch's PSD method to detect lossy-to-lossless transcodes
- Fallback to `send_document` when `send_audio` fails (BadRequest edge cases)
- New dependencies: numpy, scipy, soundfile for spectral analysis
- 9 new tests for FLAC analyzer (synthetic audio generation with controlled cutoffs)

### Changed

- Large file message improved with quality info and analysis results
- Download preview now shows FLAC authenticity verdict alongside quality info
- Dockerfile: added libsndfile1 system dependency for soundfile

## [0.3.5] - 2026-02-08

### Added

- Three-tier search fallback: full query -> title-only -> keyword reduction + album year
- Stale search cleanup before each new search (fixes slskd API caching bug)
- Configurable MAX_RESULTS environment variable for FLAC result display count

### Changed

- Default FLAC results display increased from 5 to 10

## [0.1.0] - 2026-02-07

### Added

- Initial release
- Telegram bot interface for song search and download
- Spotify metadata resolution (track name, artist, duration, album)
- slskd integration for FLAC search and download via Soulseek
- Scoring algorithm: duration matching, quality analysis, keyword filtering
- File processor: rename to "Artist - Title.flac" and place in output directory
- Auto-download mode (toggle with `/auto`)
- Download history and status commands
- FastAPI health check endpoint
- Docker support with security hardening
- GitHub Actions CI/CD (tests, lint, Docker publish, CodeQL, releases)
