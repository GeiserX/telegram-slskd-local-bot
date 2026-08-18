# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
