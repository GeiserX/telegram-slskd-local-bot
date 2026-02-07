# telegram-slskd-local-bot - AI Assistant Configuration

> **Project Context:** This is an open-source project. Consider community guidelines and contribution standards.

## Persona

You assist developers working on telegram-slskd-local-bot.

Project description: Automated music discovery and download via Telegram bot. Resolves metadata from Spotify, searches and downloads FLAC from Soulseek (slskd), and organizes your library. Docker-ready.

## Tech Stack

- Python 3.11+
- python-telegram-bot (Telegram Bot API)
- spotipy (Spotify Web API, Client Credentials flow)
- slskd-api (Soulseek/slskd REST API)
- FastAPI + uvicorn (health check endpoint)
- mutagen (audio metadata)
- Docker

> **AI Assistance:** Let AI analyze the codebase and suggest additional technologies and approaches as needed.

## Repository & Infrastructure

- **Host:** GitHub
- **License:** GPL-3.0
- **Architecture:** Single Docker container running Telegram bot + health endpoint
- **Commits:** Follow [Conventional Commits](https://conventionalcommits.org) format
- **Versioning:** Follow [Semantic Versioning](https://semver.org) (semver)
- **CI/CD:** GitHub Actions
- **Deployment:** Docker via Portainer GitOps
- **Docker Image:** `drumsergio/telegram-slskd-local-bot`
- **Telegram Bot:** [@slskdimporterbot](https://t.me/slskdimporterbot)
- **Example Repo:** https://github.com/GeiserX/Telegram-Archive (use as reference for style/structure)

## AI Behavior Rules

- **Always enter Plan Mode** before making any changes - think through the approach first

## Git Workflow

- **Workflow:** Create feature branches and submit pull requests
- Do NOT commit directly to main branch
- Create descriptive branch names (e.g., `feat/batch-import`, `fix/duration-matching`)

## Deployment Workflow

### Versioning Rules

- **NEVER** force-retag an existing version. Each release gets a unique semver tag.
- **Patch** (`v0.3.0` -> `v0.3.1`): Bug fixes, UX tweaks, small changes
- **Minor** (`v0.3.x` -> `v0.4.0`): New features, significant behavior changes
- **Major** (`v0.x.y` -> `v1.0.0`): Breaking changes
- Check the latest tag before tagging: `git describe --tags --abbrev=0`
- Also update the image tag in `gitea/watchtower/slskd-importer/docker-compose.yml` to match

### Release Steps

1. Commit to `main`, create a **new** semver tag (e.g. `git tag v0.3.1`), push with `--tags`
2. GHA `docker-publish.yml` builds and pushes `drumsergio/telegram-slskd-local-bot:<tag>` to Docker Hub
3. Update `gitea/watchtower/slskd-importer/docker-compose.yml` with the new tag, commit and push to Gitea
4. Redeploy via Portainer API on **watchtower** (stack ID `195`, endpoint `2`)
5. Verify with `docker ps --filter name=slskd_importer` on watchtower

## Important Files to Read

Always read these files first to understand the project context:

- `README.md` — Features, configuration, deployment
- `src/music_downloader/config.py` — All environment variables and their handling
- `src/music_downloader/bot/handlers.py` — Telegram bot logic and conversation flow
- `src/music_downloader/search/scorer.py` — Search result scoring algorithm
- `.env.example` — Configuration reference
- `docker-compose.yml` — Deployment patterns

## Self-Improving Blueprint

> **Auto-update enabled:** As you work on this project, track patterns and update this configuration file to better reflect the project's conventions and preferences.

## Boundaries

### Always (do without asking)

- Create new files
- Rename/move files
- Rewrite large sections
- Change dependencies
- Touch CI pipelines
- Modify Docker config
- Change environment vars
- Update docs automatically
- Edit README

### Ask First

- Delete files
- Modify scoring algorithm weights significantly
- Update API contracts
- Skip tests temporarily

### Never

- Modify .env files or secrets
- Delete critical files without backup
- Force push to git
- Expose sensitive information in logs
- Hardcode credentials or API keys

## Code Style

- **Naming:** Follow idiomatic Python conventions
- **Logging:** Python logging with `logger = logging.getLogger(__name__)`

Follow these conventions:

- Follow PEP 8 style guidelines
- Use type hints for function signatures
- Prefer f-strings for string formatting
- Write self-documenting code
- Add comments for complex logic only
- Keep functions focused and testable
- **Linter/Formatter:** `ruff check src/` and `ruff format src/` -- always run before committing

## Key Architecture Decisions

### Scoring Algorithm

Search results are ranked by 4 factors (total 100 points):
1. **Duration match** (40 pts): Compared to Spotify reference duration
2. **Audio quality** (25 pts): Prefers 16-bit/44.1kHz CD quality
3. **Source reliability** (20 pts): Free slots, upload speed, queue
4. **Filename relevance** (15 pts): Artist/title word matching

Exclude keywords filter out live/remix/etc unless the original title contains them.

### External Dependencies

- **Spotify API**: Client Credentials flow (no user login). Used only for metadata resolution.
- **slskd API**: REST API with API key auth. Used for search, download, and file management.
- **Telegram Bot API**: Long-polling mode. Restricted to allowed user IDs.

### Telegram Message UX Patterns

- **Markdown escaping**: Dynamic text (filenames, paths from Soulseek) must be escaped with `_escape_md()` or wrapped in backtick code spans to avoid `BadRequest` from Telegram's Markdown parser
- **Safe edits**: Always use the `_safe_edit()` wrapper (catches `BadRequest`, `TimedOut`, `NetworkError`) instead of raw `msg.edit_text()`
- **Result identification**: Download messages must include `#number` labels matching the result list so users can tell concurrent downloads apart
- **Spotify results cap**: Show max 5 results to the user; fetch 10 from the API for filtering headroom
- **Spotify artist filter**: When query contains `artist - title`, filter Spotify results by artist substring match before dedup to remove noise; fall back to unfiltered if the filter empties the list

### Soulseek (slskd) Search Patterns

- **Single query, local filtering**: Never append format keywords (e.g. "flac") to the slskd search query -- Soulseek matches keywords against full file paths, which is unreliable. Instead, search with `artist title` and filter results locally by file extension (`.flac` preferred, fall back to other audio formats)
- **Search lifecycle**: `search_text()` -> poll `state()` -> `stop()` on timeout -> grab partial results from `search_responses()` -> `delete()` cleanup
- **Async wrapping**: All synchronous `slskd-api` calls must be wrapped with `asyncio.to_thread()` to avoid blocking the Telegram bot event loop
- **Timeouts**: Hard timeout via `asyncio.wait_for()` around the entire search+poll loop; `searches.stop()` actively cancels the server-side search on timeout

## Testing Strategy

### Frameworks

Use: pytest, pytest-asyncio

### Coverage Target: 80%

### Test Levels

- **Unit:** Scorer, config parsing, file handler, TrackInfo
- **Integration:** slskd client, Spotify resolver (with mocks)
- **E2e:** Full bot conversation flow (future)

## Security Configuration

### Secrets Management

- Environment Variables (never committed)
- GitHub Actions Secrets for CI/CD

### Authentication

- Telegram bot restricted to specific user IDs via `TELEGRAM_ALLOWED_USERS`
- Spotify Client Credentials (no user data access)
- slskd API key with readwrite role

## Security Notice

> **Do not commit secrets to the repository or to the live app.**
> Always use secure standards to transmit sensitive information.
> Use environment variables, secret managers, or secure vaults for credentials.

---

*Generated by [LynxPrompt](https://lynxprompt.com) CLI*
