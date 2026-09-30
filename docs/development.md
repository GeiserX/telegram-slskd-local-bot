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
