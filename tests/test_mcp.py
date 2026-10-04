"""The MCP front end: tools over a fake pipeline, the protocol in-process, the HTTP token, config, CLI."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.history_repo import HistoryRepository
from music_downloader.persistence.pending_repo import PendingRepository
from music_downloader.persistence.wishlist_repo import WishlistRepository
from music_downloader.pipeline import Pipeline
from music_downloader.pipeline import library as _library
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, FetchOutcome
from music_downloader.pipeline.search import RankedResults
from music_downloader.processor.lossless_analyzer import LosslessVerdict
from music_downloader.search.slskd_client import DownloadStatus, SearchResult

OWNER = 111
TOKEN = "s3cret-token"
TRACK = TrackInfo("Nancy Sinatra", "Bang Bang (Remastered)", "How Does That Grab You?", 162_000, "https://x/t", "1966")


def _flac(bit_depth=16, idx=0, size=30_000_000):
    return SearchResult(
        username=f"peer{idx}",
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {idx}.flac",
        size=size,
        bit_depth=bit_depth,
        sample_rate=44100,
        length=162,
        score=91.25,
    )


def _mp3(kbps=320, idx=1):
    return SearchResult(
        username=f"peer{idx}", filename=f"\\Music\\Bang Bang {idx}.mp3", size=6_500_000, bit_rate=kbps, length=162
    )


class FakePipeline:
    """The Pipeline surface McpTools uses, with real SQLite repos and canned slskd/Spotify answers."""

    wishlist_add = Pipeline.wishlist_add
    wishlist_find = Pipeline.wishlist_find
    wishlist_list = Pipeline.wishlist_list
    wishlist_remove = Pipeline.wishlist_remove

    def __init__(self, tmp_path, results=None):
        db = Database(str(tmp_path / "importer.db"))
        self.history_repo = HistoryRepository(db)
        self.wishlist_repo = WishlistRepository(db)
        self.pending_repo = PendingRepository(db)
        self.config = SimpleNamespace(telegram_owner_id=OWNER)
        self.upload_limit_bytes = 50_000_000
        self.output_dir = tmp_path / "music"
        self.results = results if results is not None else [_flac(16, 0), _mp3(320, 1), _flac(16, 2, 60_000_000)]
        self.search_queries = []
        self.saved = []
        self.slskd = MagicMock()
        self.slskd.is_up.return_value = True
        self.verdict = LosslessVerdict("AUTHENTIC", 21.5, 22.05, 44100, 16)
        self.fetch_outcome = None

    def resolve(self, query):
        return [TRACK] if query else []

    async def search(self, query, track, profile, **_):
        self.search_queries.append((query, profile))
        return RankedResults(self.results, 2)

    async def fetch(self, result, progress_cb, analyze=True):
        await progress_cb(DownloadStatus(result.username, result.filename, "InProgress", 50.0))
        await progress_cb(DownloadStatus(result.username, result.filename, "Completed, Succeeded", 100.0))
        if self.fetch_outcome is not None:
            return self.fetch_outcome
        return FetchOutcome(path=f"/downloads/{result.basename}", verdict=self.verdict, transfer_id="tx1")

    async def record_history(self, track, result, status, filename=None):
        await _library.record_history(self.history_repo, track, result, status, filename)

    async def save(self, source_path, track, result, transfer_id=""):
        self.saved.append((source_path, transfer_id))
        await self.record_history(track, result, "success")
        return str(self.output_dir / f"{track.artist} - {track.title}.{result.extension}")

    async def find_similar(self, query):
        return ["Nancy Sinatra - Bang Bang.flac"] if "bang bang" in query.lower() else []


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _tools(tmp_path, **kw):
    from music_downloader.mcp.tools import McpTools

    clock = Clock()
    pipeline = FakePipeline(tmp_path, **kw)
    return McpTools(pipeline, ttl_secs=60, clock=clock), pipeline, clock


class TestToolChain:
    @pytest.mark.asyncio
    async def test_resolve_search_download_history_chain_through_ids(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        resolved = await tools.resolve_track("nancy sinatra bang bang")
        [candidate] = resolved["candidates"]
        assert candidate["artist"] == "Nancy Sinatra" and candidate["duration_secs"] == 162
        assert candidate["id"].startswith("t")

        found = await tools.search_copies(candidate["id"], limit=2)
        assert pipeline.search_queries == [("Nancy Sinatra Bang Bang", "library")]
        assert found["total"] == 3 and found["hidden_by_title_guard"] == 2
        assert len(found["copies"]) == 2
        first = found["copies"][0]
        assert first["id"].startswith("c")
        assert first["format"] == "flac" and first["lossless"] is True
        assert first["size_bytes"] == 30_000_000 and first["fits_cap"] is True
        assert first["source"] == "peer0" and first["score"] == 91.2
        assert found["copies"][1]["lossless"] is False

        progress = []
        done = await tools.download(first["id"], progress=lambda *a: _record(progress, a))
        assert done["ok"] is True and done["deliver"] == "library"
        assert done["path"].endswith("Nancy Sinatra - Bang Bang (Remastered).flac")
        assert done["lossless_check"]["verdict"] == "AUTHENTIC"
        assert pipeline.saved == [("/downloads/Nancy Sinatra - Bang Bang 0.flac", "tx1")]
        assert [p[0] for p in progress] == [50.0, 100.0]

        hist = await tools.history(5)
        assert [(r["title"], r["status"]) for r in hist["downloads"]] == [("Bang Bang (Remastered)", "success")]

    @pytest.mark.asyncio
    async def test_search_falls_back_to_the_title_alone(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path, results=[])
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        found = await tools.search_copies(track_id, profile="chat")
        assert pipeline.search_queries == [("Nancy Sinatra Bang Bang", "chat"), ("Bang Bang", "chat")]
        assert found["copies"] == []

    @pytest.mark.asyncio
    async def test_fits_cap_follows_the_upload_cap(self, tmp_path):
        tools, _, _ = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        copies = (await tools.search_copies(track_id))["copies"]
        assert [c["fits_cap"] for c in copies] == [True, True, False]

    @pytest.mark.asyncio
    async def test_bad_profile_and_empty_query_are_tool_errors(self, tmp_path):
        tools, _, _ = _tools(tmp_path)
        with pytest.raises(ToolError):
            await tools.resolve_track("  ")
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        with pytest.raises(ToolError, match="profile"):
            await tools.search_copies(track_id, profile="loud")

    @pytest.mark.asyncio
    async def test_ids_expire_after_the_ttl(self, tmp_path):
        tools, _, clock = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        copy_id = (await tools.search_copies(track_id))["copies"][0]["id"]
        clock.now += 59
        await tools.search_copies(track_id)  # still valid just before the TTL
        clock.now += 2
        with pytest.raises(ToolError, match="expired"):
            await tools.search_copies(track_id)
        with pytest.raises(ToolError, match="expired"):
            await tools.download(copy_id)

    @pytest.mark.asyncio
    async def test_download_path_leaves_the_file_and_records_delivered(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        copy_id = (await tools.search_copies(track_id))["copies"][0]["id"]
        done = await tools.download(copy_id, deliver="path")
        assert done == {
            "ok": True,
            "deliver": "path",
            "path": "/downloads/Nancy Sinatra - Bang Bang 0.flac",
            "lossless_check": {
                "verdict": "AUTHENTIC",
                "cutoff_khz": 21.5,
                "nyquist_khz": 22.05,
                "sample_rate": 44100,
                "bit_depth": 16,
            },
        }
        assert pipeline.saved == []
        [row] = (await tools.history())["downloads"]
        assert row["status"] == "delivered" and row["filename"] == "/downloads/Nancy Sinatra - Bang Bang 0.flac"

    @pytest.mark.asyncio
    async def test_failed_download_is_reported_and_recorded(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.fetch_outcome = FetchOutcome(error=DOWNLOAD_FAILED, state="Timeout")
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        copy_id = (await tools.search_copies(track_id))["copies"][0]["id"]
        done = await tools.download(copy_id)
        assert done == {"ok": False, "error": "failed", "state": "Timeout"}
        assert [r["status"] for r in (await tools.history())["downloads"]] == ["failed"]
        assert tools.downloads_in_progress == 0

    @pytest.mark.asyncio
    async def test_bad_deliver_is_a_tool_error(self, tmp_path):
        tools, _, _ = _tools(tmp_path)
        with pytest.raises(ToolError, match="deliver"):
            await tools.download("c000000", deliver="chat")

    @pytest.mark.asyncio
    async def test_library_has(self, tmp_path):
        tools, _, _ = _tools(tmp_path)
        assert await tools.library_has("Nancy Sinatra", "Bang Bang") == {
            "found": True,
            "matches": ["Nancy Sinatra - Bang Bang.flac"],
        }
        assert (await tools.library_has("Nobody", "Nothing"))["found"] is False


async def _record(store, args):
    store.append(args)


class TestWishlistTools:
    @pytest.mark.asyncio
    async def test_add_any_list_remove(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        wish = (await tools.wishlist_add(track_id))["wish"]
        assert wish["chat_id"] == OWNER and wish["wanted"] == "any" and wish["baseline"] is None
        assert pipeline.wishlist_repo.get(wish["id"]).user_id == OWNER

        listed = (await tools.wishlist_list())["wishes"]
        assert [w["id"] for w in listed] == [wish["id"]]
        assert listed[0]["track"]["title"] == "Bang Bang (Remastered)"

        assert await tools.wishlist_remove(wish["id"]) == {"removed": True}
        assert await tools.wishlist_remove(wish["id"]) == {"removed": False}
        assert (await tools.wishlist_list())["wishes"] == []

    @pytest.mark.asyncio
    async def test_a_track_already_waited_for_is_not_added_twice(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        first = await tools.wishlist_add(track_id)
        assert first["already_waiting"] is False
        again = await tools.wishlist_add(track_id, "any")
        assert again == {"wish": first["wish"], "already_waiting": True}
        assert len(pipeline.wishlist_repo.list_all()) == 1

    @pytest.mark.asyncio
    async def test_list_and_remove_cover_every_chat(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        other = pipeline.wishlist_add(999, 999, TRACK, "chat", "any")
        assert [w["chat_id"] for w in (await tools.wishlist_list())["wishes"]] == [999]
        assert (await tools.wishlist_remove(other.id))["removed"] is True

    @pytest.mark.asyncio
    async def test_better_needs_a_search_and_takes_its_best_tier(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path, results=[_mp3(320, 1), _flac(16, 0)])
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        with pytest.raises(ToolError, match="search_copies"):
            await tools.wishlist_add(track_id, "better")
        await tools.search_copies(track_id, profile="chat")
        wish = (await tools.wishlist_add(track_id, "better"))["wish"]
        assert wish["baseline"] == "lossless 16-bit" and wish["profile"] == "chat"

    @pytest.mark.asyncio
    async def test_better_than_lossless_24_is_refused(self, tmp_path):
        tools, _, _ = _tools(tmp_path, results=[_flac(24, 0)])
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        await tools.search_copies(track_id)
        with pytest.raises(ToolError, match="nothing ranks higher"):
            await tools.wishlist_add(track_id, "better")

    @pytest.mark.asyncio
    async def test_no_owner_and_bad_wanted_are_tool_errors(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        track_id = (await tools.resolve_track("bang bang"))["candidates"][0]["id"]
        with pytest.raises(ToolError, match="wanted"):
            await tools.wishlist_add(track_id, "someday")
        pipeline.config.telegram_owner_id = None
        with pytest.raises(ToolError, match="TELEGRAM_ALLOWED_USERS"):
            await tools.wishlist_add(track_id)


class TestStatus:
    @pytest.mark.asyncio
    async def test_status(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.wishlist_add(OWNER, OWNER, TRACK, "library", "any")
        status = await tools.status()
        assert status["slskd_reachable"] is True
        assert status["pending_downloads"] == 0
        assert status["mcp_downloads_in_progress"] == 0
        assert status["wishes"] == 1
        assert status["upload_cap_mb"] == 50
        assert isinstance(status["version"], str)
        pipeline.slskd.is_up.return_value = False
        assert (await tools.status())["slskd_reachable"] is False


class TestProtocol:
    @pytest.mark.asyncio
    async def test_tools_over_mcp_with_progress(self, tmp_path):
        from music_downloader.mcp.server import build_server

        tools, _, _ = _tools(tmp_path)
        server = build_server(tools)
        progress = []

        async def on_progress(done, total, message):
            progress.append((done, total, message))

        async with Client(server) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {
                "resolve_track",
                "search_copies",
                "download",
                "history",
                "library_has",
                "wishlist_add",
                "wishlist_list",
                "wishlist_remove",
                "status",
            }
            resolved = await client.call_tool("resolve_track", {"query": "bang bang"})
            track_id = resolved.structured_content["candidates"][0]["id"]
            found = await client.call_tool("search_copies", {"track_id": track_id, "limit": 1})
            copy_id = found.structured_content["copies"][0]["id"]
            done = await client.call_tool("download", {"copy_id": copy_id}, progress_callback=on_progress)
            assert done.structured_content["ok"] is True
            expired = await client.call_tool("download", {"copy_id": "cdeadbe"})
            assert expired.is_error

        assert [p[0] for p in progress] == [50.0, 100.0]
        assert progress[0][1] == 100.0 and "InProgress" in progress[0][2]


class TestHttpToken:
    def _client(self, tmp_path):
        from music_downloader.mcp.server import http_app

        tools, _, _ = _tools(tmp_path)
        return TestClient(http_app(tools, TOKEN))

    _INIT = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    _HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

    def test_missing_and_wrong_tokens_are_rejected(self, tmp_path):
        with self._client(tmp_path) as client:
            missing = client.post("/mcp", json=self._INIT, headers=self._HEADERS)
            wrong = client.post("/mcp", json=self._INIT, headers={**self._HEADERS, "Authorization": "Bearer nope"})
            bare = client.post("/mcp", json=self._INIT, headers={**self._HEADERS, "Authorization": TOKEN})
        assert [r.status_code for r in (missing, wrong, bare)] == [401, 401, 401]
        assert missing.headers["www-authenticate"] == "Bearer"

    def test_right_token_reaches_the_server(self, tmp_path):
        with self._client(tmp_path) as client:
            ok = client.post("/mcp", json=self._INIT, headers={**self._HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert ok.status_code == 200
        assert "serverInfo" in ok.text

    def test_compare_is_constant_time(self):
        import music_downloader.mcp.server as server_mod

        with patch.object(server_mod.hmac, "compare_digest", return_value=True) as compare:
            asyncio.run(server_mod.BearerAuth(AsyncMock(), TOKEN)({"type": "http", "headers": []}, None, None))
        compare.assert_called_once_with(b"", b"Bearer " + TOKEN.encode())

    def test_empty_token_is_refused(self):
        from music_downloader.mcp.server import BearerAuth

        with pytest.raises(ValueError):
            BearerAuth(AsyncMock(), "")


class TestConfig:
    _REQUIRED = {
        "TELEGRAM_BOT_TOKEN": "t",
        "SPOTIFY_CLIENT_ID": "i",
        "SPOTIFY_CLIENT_SECRET": "s",
        "SLSKD_HOST": "http://x",
        "SLSKD_API_KEY": "k",
    }

    def _config(self, monkeypatch, **env):
        for key in ("MCP_PORT", "MCP_HOST", "MCP_TOKEN", "TELEGRAM_ALLOWED_USERS"):
            monkeypatch.delenv(key, raising=False)
        for k, v in {**self._REQUIRED, **env}.items():
            monkeypatch.setenv(k, v)
        from music_downloader.config import Config

        return Config()

    def test_defaults(self, monkeypatch):
        config = self._config(monkeypatch)
        assert config.mcp_port is None and config.mcp_host == "0.0.0.0" and config.mcp_token is None
        assert config.telegram_owner_id is None

    def test_port_host_token(self, monkeypatch):
        config = self._config(monkeypatch, MCP_PORT="8765", MCP_HOST="127.0.0.1", MCP_TOKEN=" abc ")
        assert (config.mcp_port, config.mcp_host, config.mcp_token) == (8765, "127.0.0.1", "abc")

    def test_port_without_token_refuses_to_start(self, monkeypatch):
        with pytest.raises(ValueError, match="MCP_TOKEN"):
            self._config(monkeypatch, MCP_PORT="8765", MCP_TOKEN="  ")

    def test_owner_is_the_first_listed_user(self, monkeypatch):
        assert self._config(monkeypatch, TELEGRAM_ALLOWED_USERS="900, 5,77").telegram_owner_id == 900


class TestEntryPoints:
    def test_mcp_subcommand_runs_stdio(self, monkeypatch):
        import music_downloader.__main__ as main_mod

        monkeypatch.setattr("sys.argv", ["slskd-importer", "mcp"])
        with (
            patch.object(main_mod, "Config") as config_cls,
            patch.object(main_mod, "setup_logging"),
            patch("music_downloader.mcp.server.run_stdio") as run_stdio,
        ):
            main_mod.main()
        run_stdio.assert_called_once_with(config_cls.return_value)

    @pytest.mark.asyncio
    async def test_bot_serves_http_on_its_own_pipeline_when_mcp_port_is_set(self):
        from music_downloader.bot.handlers import create_bot
        from tests.test_orphan_sweep import _handlers_config

        config = _handlers_config()
        config.mcp_port = 8765
        started = asyncio.Event()

        async def fake_serve(pipeline, cfg):
            fake_serve.pipeline = pipeline
            started.set()
            await asyncio.Event().wait()

        with (
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as app_cls,
            patch("music_downloader.mcp.server.serve_http", fake_serve),
        ):
            builder = MagicMock()
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            app_cls.builder.return_value = builder
            create_bot(config)
            post_init = builder.post_init.call_args[0][0]
            post_shutdown = builder.post_shutdown.call_args[0][0]

            app = MagicMock()
            app.bot = AsyncMock()
            await post_init(app)
            await asyncio.wait_for(started.wait(), 5)
            mcp_task = next(t for t in asyncio.all_tasks() if t.get_coro().__name__ == "fake_serve")
            await post_shutdown(app)
        assert mcp_task.cancelled()
        assert isinstance(fake_serve.pipeline, Pipeline)
