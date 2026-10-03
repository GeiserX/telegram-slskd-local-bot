# Development

## Set up

```bash
git clone https://github.com/GeiserX/telegram-slskd-local-bot.git
cd telegram-slskd-local-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,analysis]"
cp .env.example .env
# edit .env with your own tokens, slskd address and folders
python -m music_downloader run
```

`ffmpeg` and `ffprobe` must be on the `PATH` for the Opus conversion and the tests that use it.
`uv sync --all-extras` works instead of the `venv` and `pip` lines; `uv.lock` is committed.

## Architecture

The code under `src/music_downloader/` has three layers. The pipeline knows nothing about Telegram, the
Telegram bot is one front end on top of it, and SQLite holds everything that must outlive a restart.

### The pipeline (`music_downloader.pipeline`)

`Pipeline` (in `pipeline/__init__.py`) owns the Spotify resolver, the slskd client, the scorer, the file
processor, the database repositories and the library index. It covers everything between "we have a query"
and "we have a file ready to hand over". Its methods take and return plain dataclasses and async callables:

| Module | What it does |
|--------|--------------|
| `pipeline/resolve.py` | Free text to Spotify candidates (`lookup`), "Artist - Title" parsing, the synthetic track a direct search uses |
| `pipeline/search.py` | The search query helpers (version-noise stripping, keyword-reduction fallbacks), the title guard, and `rank`, which turns slskd responses into a `RankedResults` list for the `library` or `chat` profile |
| `pipeline/fetch.py` | `fetch`: enqueue, wait, find the file on disk, run the lossless check, all returned as a `FetchOutcome`. Also the Opus conversion and the bitrate ladder that fits the upload cap |
| `pipeline/library.py` | Cover art, the download-history row, deleting sources, the hourly orphan sweep |
| `pipeline/wishlist.py` | The wishlist checker: which wishes are due, which copies satisfy one (`search/scorer.py` `quality_tier`), the sequential search pass that hands hits to the front end's delivery callback |

The pieces it drives sit next to it: `metadata/` (Spotify, playlists), `search/slskd_client.py` and
`search/scorer.py`, `processor/` (renaming and moving files, the lossless analyzer), `tools/embed_artwork.py`.
`tests/test_pipeline_no_telegram.py` imports every pipeline module in a fresh interpreter and fails if any of
them pulls in `telegram`.

### Front ends

- **Telegram** (`music_downloader.bot`, this repository): `bot/handlers.py` parses updates, keeps the
  per-chat state, edits messages (always HTML, every outside value through `_esc`) and sends files.
  `bot/keyboards.py` builds the buttons. `bot/poll_request.py` reports every successful `getUpdates` poll to
  the health state. `create_bot()` wires it up and starts the background tasks: the orphan sweep, the
  library index rescan, the slskd health probe and the wishlist checker.
- **Other front ends**: because the pipeline is Telegram-free, another front end (an MCP server, for
  example, kept as its own project) can import `music_downloader.pipeline` and drive the same `Pipeline`
  without going through the bot.

### State in SQLite

Everything lives in one database, `importer.db` in `DATA_DIR` (`persistence/database.py` creates every table
with `CREATE TABLE IF NOT EXISTS`):

| Table | Holds | Written by |
|-------|-------|------------|
| `download_history` | One row per finished, failed, delivered or rejected download (`/history`) | `history_repo.py` |
| `import_jobs`, `import_tracks` | `/import` jobs and the state of each track | `import_repo.py` |
| `chat_settings` | Per-chat `/auto` and `/deliver` choices | `settings_repo.py` |
| `pending_searches` | One row per chat with a live result list: the query, the track, the ranked results (JSON), the page, the profile and how many copies the title guard hid | `pending_repo.py` |
| `pending_downloads` | One row per download waiting on Save, Reject, Retry or Try next: track and result (JSON), the file path, the slskd transfer id, the import job if any | `pending_repo.py` |
| `wishlist` | One row per wished track: chat, track (JSON), profile, `any` or `better` than a baseline quality tier, last check, number of checks, last notification | `wishlist_repo.py` |
| `library_index` | One row per audio file under `OUTPUT_DIR`, subfolders included: relative path, stem, accent-free lowercase stem, extension, mtime | `library_index.py` |

The bot keeps the pending rows in two dicts (`WriteThroughDict`): every set and delete is written through, so
after a restart the buttons on old messages still work. A button whose row is gone answers that it expired
after a restart. At startup and in the hourly sweep, pending downloads older than `DOWNLOAD_CLEANUP_HOURS`
are dropped with their files and their slskd transfers.

The library index replaces walking `OUTPUT_DIR` on every message. It is rebuilt in a thread at startup and
every hour (whether or not the orphan sweep is on), each save adds its own row, and the duplicate check runs
in a thread: a `LIKE` prefilter on the accent-free stem keeps at most 300 rows, best first, and only those go
through the fuzzy match (threshold 0.6, the same as before).

### Health

`health.py` holds two timestamps. `GET /health` (served by `__main__.py` on `HEALTH_PORT`) answers 200 only
while the Telegram application runs, a `getUpdates` poll succeeded in the last 120 s, and slskd answered
`application/state` in the last 60 s (a background probe asks every 20 s). Otherwise it answers 503 with a
JSON body naming the failed check, for example `{"status": "unhealthy", "failed": "slskd", "reason":
"application/state last answered 75 s ago"}`. `GET /ready` is a constant 200. The Docker `HEALTHCHECK`
calls `/health`.

## Test and lint

```bash
ruff check . && ruff format --check .
python -m pytest tests/ -v --tb=short
python -m pytest tests/ --cov=src/music_downloader --cov-report=term-missing
```

The same three commands run on every pull request as the `ruff` and `test` checks, plus CodeQL as `Analyze`.
`pre-commit install` runs ruff and the YAML and whitespace checks before each commit.

## Build the image

```bash
docker build -t drumsergio/telegram-slskd-local-bot:dev .
```

The image installs the packages of the `analysis` extra (they are in `requirements.txt`), `ffmpeg` and
`libsndfile1`; the plain PyPI package installs none of them.

## Release

1. Bump `version` in `pyproject.toml`, the image pin in `docker-compose.yml` and in
   [Getting started](getting-started.md), and add the section to the
   [changelog](https://github.com/GeiserX/telegram-slskd-local-bot/blob/main/docs/CHANGELOG.md).
2. Push to `main`, then push a new tag `vX.Y.Z`. Never move an existing tag.
3. The tag runs two workflows: Docker Publish pushes `drumsergio/telegram-slskd-local-bot:X.Y.Z` to Docker
   Hub, and GitHub Release creates the release from the changelog section and uploads the package to PyPI.

## Docs

The site is built by MkDocs from `docs/`. `pip install -r docs/requirements-docs.txt && mkdocs build --strict`
builds it locally; the same strict build runs on every pull request and a push to `main` deploys it.
