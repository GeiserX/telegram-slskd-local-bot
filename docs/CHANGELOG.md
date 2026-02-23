# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
