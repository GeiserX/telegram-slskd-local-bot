# CLAUDE.md — telegram-slskd-local-bot

## Overview
Telegram bot that automates music discovery and download. Resolves track metadata from Spotify, searches Soulseek via slskd, ranks lossless copies first (lossy ones listed below), downloads the pick, renames it to `Artist - Title.<ext>`, and places it in a music library directory. Chat delivery sends the track into Telegram instead and ranks by quality for size. The lossless and lossy extension sets live in `src/music_downloader/formats.py`; derive from them, never hardcode `flac` to mean lossless.

## Tech Stack
- Python 3.11+
- python-telegram-bot (Telegram Bot API)
- spotipy (Spotify Web API, Client Credentials flow)
- slskd-api (Soulseek/slskd REST API)
- stdlib http.server (health check endpoint)
- mutagen (audio metadata)
- Docker (image: `drumsergio/telegram-slskd-local-bot`)
- Published on PyPI
- pytest + pytest-asyncio for testing
- uv for dependency management
- ruff for linting/formatting

## Development
```bash
# Install
pip install -e ".[dev]"
# Or with uv
uv sync

# Lint and format (always run before committing)
ruff check src/
ruff format src/

# Test
pytest
# Or with uv
uv run pytest
```

Configuration via `.env` (see `.env.example`).

## Architecture
- `src/music_downloader/` — main application package
  - `config.py` — all environment variables and their handling (incl. the upload cap: `BYTES_PER_MB`, `DEFAULT_UPLOAD_LIMIT_BYTES`, `TELEGRAM_MAX_UPLOAD_MB`; the local Bot API server: `TELEGRAM_API_BASE_URL`, `TELEGRAM_UPLOAD_TIMEOUT_SECS`, `LOCAL_SERVER_MAX_UPLOAD_MB`; MCP: `MCP_PORT`, `MCP_HOST`, `MCP_TOKEN`, `telegram_owner_id`)
  - `pipeline/` — everything between a query and a file ready to hand over; never imports `telegram` (`tests/test_pipeline_no_telegram.py` enforces it)
    - `__init__.py` — `Pipeline`: owns the Spotify resolver, slskd client, scorer, file processor, repos and library index; `resolve`, `search`, `rank`, `fetch`, `save`, `discard`, `find_similar`, `library_index_loop`
    - `resolve.py` — Spotify lookup, "Artist - Title" parsing, synthetic tracks for direct search
    - `search.py` — query cleanup and fallbacks, the title guard, `rank()` (lossless-first or chat order, unknown-length copies never lead) returning `RankedResults` (`.hidden` = copies the guard dropped)
    - `fetch.py` — enqueue/wait/locate/lossless check as `FetchOutcome`; Opus conversion and the bitrate ladder that fits the cap; the `/format` send formats (`SEND_FORMATS`, `already_in_format`, `transcode`)
    - `library.py` — artwork, history rows, deleting sources, the orphan sweep loop
    - `wishlist.py` — the wishlist checker (`check_due`: due wishes searched one at a time, hits handed to the front end's callback, `skip` leaves a busy chat for the next tick); `Pipeline.wishlist_add/find/list/remove/check_due/loop`; tiers come from `search/scorer.py` `quality_tier`
  - `mcp/` — the MCP front end, never imports `telegram`: `tools.py` (`McpTools`: each tool over a `Pipeline`, dicts out, track/copy ids in a TTL map), `server.py` (`build_server` on the `mcp` SDK's `MCPServer`, `BearerAuth`, `serve_http` started by `create_bot` when `MCP_PORT` is set, `run_stdio` for `python -m music_downloader mcp`)
  - `bot/` — the Telegram front end: `handlers.py` (updates, per-chat state, HTML messages, sending files, `create_bot`), `keyboards.py`, `poll_request.py` (reports each successful `getUpdates` to the health state)
  - `persistence/` — SQLite in `DATA_DIR/importer.db`: `database.py` (schema), `history_repo.py`, `import_repo.py`, `settings_repo.py`, `pending_repo.py` (searches, wishlist-checker lists (`wish_searches`) and downloads waiting on a button, written through from the bot's dicts so buttons survive restarts), `library_index.py` (audio files under `OUTPUT_DIR` for the duplicate check; rebuilt at startup and hourly, updated on save, queried in a thread), `wishlist_repo.py` (tracks to search again: `any` copy or `better` than a baseline tier)
  - `health.py` — `/health` state: Telegram polling in the last 120 s and slskd answering in the last 60 s
  - `search/scorer.py` — search result scoring algorithm; `search/slskd_client.py` — slskd API wrapper
  - `processor/` — file renaming/moving and the lossless spectrum check
- `scripts/` — utility scripts
- `tests/` — test suite
- `docker-compose.yml` — full stack deployment
- `Dockerfile` — container build
- `.pre-commit-config.yaml` — code quality hooks

## Scoring Algorithm

Search results are ranked by 4 factors (total 100 points):
1. **Duration match** (40 pts): Compared to Spotify reference duration. A lossy file without a length gets one estimated from size and bitrate (`ResultScorer.effective_length`); a copy whose length stays unknown earns 0 here
2. **Audio quality** (25 pts): depends on the profile passed to `score_results(profile=...)`
   - `library`: lossless scores by bit depth and sample rate (hi-res preferred); lossy scores by bitrate tier. `pipeline/search.rank` then puts every lossless result before every lossy one, except a lossless result of a different version (length off by more than `SAME_VERSION_MAX_DIFF_SECS`) or of unknown length, which stays among the lossy ones in score order.
   - `chat` (chat delivery): perceived quality versus size, no lossless/lossy split. Lossless and lossy >= 256 kbps share the top tier (lossy bitrates are scaled by codec first: Opus x2, AAC/Vorbis x1.5), minus a size cost that grows from 5 MB to 50 MB, or to the upload cap when that is smaller (`CHAT_SIZE_PENALTY_FULL_BYTES`); files over the cap (50,000,000 bytes by default, 2000 MB with a local Bot API server) score as the Opus they become and sort after every file that fits; unknown-length files sort last. Constants and their reasons are at the top of `search/scorer.py`.
3. **Source reliability** (20 pts): Free slots, upload speed, queue
4. **Filename relevance** (15 pts): Artist/title word matching

Exclude keywords filter out live/remix/etc unless the original title contains them. Before scoring, the title guard (`pipeline/search.title_guard`) hides copies whose file and parent folder names share no word with the title (brackets, version noise and a/the/of/and/feat/ft dropped); it never empties the list and is skipped for direct search.

## Soulseek (slskd) Search Patterns

- **Single query, local filtering**: Never append format keywords (e.g. "flac") to the slskd search query -- Soulseek matches keywords against full file paths, which is unreliable. Instead, search with `artist title` and filter results locally by file extension (every format in `formats.AUDIO_EXTENSIONS`, lossless ranked first)
- **Search lifecycle**: `search_text()` -> poll `state()` -> `stop()` on timeout -> grab partial results from `search_responses()` -> `delete()` cleanup
- **Async wrapping**: All synchronous `slskd-api` calls must be wrapped with `asyncio.to_thread()` to avoid blocking the Telegram bot event loop
- **Timeouts**: Hard timeout via `asyncio.wait_for()` around the entire search+poll loop; `searches.stop()` actively cancels the server-side search on timeout

## Telegram UX Patterns

- **HTML everywhere**: every message is sent with `parse_mode=ParseMode.HTML`; dynamic text (track names, filenames, paths from Soulseek) must go through `_esc()`
- **Safe edits**: Always use the `_safe_edit()` wrapper (catches `BadRequest`, `TimedOut`, `NetworkError`) instead of raw `msg.edit_text()`
- **Result identification**: Download messages must include `#number` labels matching the result list so users can tell concurrent downloads apart
- **Spotify results cap**: Show max 5 results to the user; fetch 10 from the API for filtering headroom
- **Spotify artist filter**: When query contains `artist - title`, filter Spotify results by artist substring match before dedup to remove noise; fall back to unfiltered if the filter empties the list

## Deployment

### Release Steps

1. In a PR, bump the version in [`pyproject.toml`](pyproject.toml), the image pin in this repo's [`docker-compose.yml`](docker-compose.yml), [`docs/getting-started.md`](docs/getting-started.md) and the README quick start, and add the [`docs/CHANGELOG.md`](docs/CHANGELOG.md) entry. Merge it.
2. Tag the merge commit with the new version (`vX.Y.Z` below is that version) and push only that tag: `git tag vX.Y.Z <sha> && git push origin refs/tags/vX.Y.Z`
3. The tag runs [`docker-publish.yml`](.github/workflows/docker-publish.yml), which pushes `drumsergio/telegram-slskd-local-bot:<tag>` to Docker Hub, and [`release.yml`](.github/workflows/release.yml), which publishes the GitHub release and PyPI. Wait for both to pass.
4. Production deploys through GitOps: in the watchtower Gitea repo, set `slskd-importer/docker-compose.yml` to `drumsergio/telegram-slskd-local-bot:vX.Y.Z@sha256:<digest>`, commit and push. The webhook redeploys the stack. Take the digest from `docker buildx imagetools inspect drumsergio/telegram-slskd-local-bot:vX.Y.Z --format '{{.Manifest.Digest}}'`. Docker resolves the image by digest, so changing only the tag deploys nothing new. Don't run `docker compose up` by hand as well. It races the webhook.
5. Verify on watchtower: `slskd_importer` is healthy and its log says `Music Downloader vX.Y.Z starting`.

### Versioning Rules

- **NEVER** force-retag an existing version. Each release gets a unique semver tag.
- **Patch** (`v0.3.0` -> `v0.3.1`): Bug fixes, UX tweaks, small changes
- **Minor** (`v0.3.x` -> `v0.4.0`): New features, significant behavior changes
- **Major** (`v0.x.y` -> `v1.0.0`): Breaking changes
- Check the latest tag before tagging: `git describe --tags --abbrev=0`
- The README quick start fetches this repo's `docker-compose.yml`, so its pin must name the new version before the tag goes out.
- The image installs from [`requirements.txt`](requirements.txt), not `uv.lock`. A security fix in a transitive package (an alert on `uv.lock`) needs a floor in `requirements.txt` as well, or the image keeps the old version.

## External Dependencies

- **Spotify API**: Client Credentials flow (no user login). Used only for metadata resolution.
- **slskd API**: REST API with API key auth. Used for search, download, and file management.
- **Telegram Bot API**: Long-polling mode. Restricted to allowed user IDs via `TELEGRAM_ALLOWED_USERS`. Cloud API by default (50 MB uploads); with `TELEGRAM_API_BASE_URL` a local Bot API server (compose profile `bigfiles`, 2000 MB uploads), wired in `bot/handlers.py` `_configure_api_server`.

## Testing Strategy

- **Unit:** Scorer, config parsing, file handler, TrackInfo
- **Integration:** slskd client, Spotify resolver (with mocks)
- **Coverage target:** 80%

## Key Rules
- Never hardcode API keys (Telegram, Spotify, slskd); use environment variables
- Allowed Telegram users must be explicitly configured
- Docker images use semver tags, never `:latest`
- License is GPL-3.0
- Follow PEP 8, use type hints, prefer f-strings
- Telegram bot: [@slskdimporterbot](https://t.me/slskdimporterbot)

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
