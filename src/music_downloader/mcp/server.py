"""The MCP server: McpTools registered as MCP tools, served over stdio or streamable HTTP."""

import asyncio
import contextlib
import hmac
import json
import logging
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer

from music_downloader import __version__
from music_downloader.config import Config
from music_downloader.mcp.tools import McpTools

logger = logging.getLogger(__name__)

SERVER_NAME = "telegram-slskd-local-bot"
HTTP_PATH = "/mcp"
INSTRUCTIONS = (
    "Find a song on Soulseek through slskd and save it. Chain the ids: resolve_track(query) gives track ids, "
    "search_copies(track_id) gives copy ids ranked best first, download(copy_id) fetches one and saves it to the "
    "library (or leaves it in the downloads folder with deliver='path'). album_listing(copy_id) and "
    "album_download(copy_id) do the same for the whole folder the copy came from. Ids expire after an hour."
)


def build_server(tools: McpTools) -> MCPServer:
    """An MCPServer whose tools call *tools*; every result is a JSON object."""
    server = MCPServer(name=SERVER_NAME, version=__version__, instructions=INSTRUCTIONS)

    @server.tool()
    async def resolve_track(query: str) -> dict[str, Any]:
        """Spotify candidates for a free-text query ("artist - title" works best), each with a track id."""
        return await tools.resolve_track(query)

    @server.tool()
    async def search_copies(
        track_id: str, profile: Literal["library", "chat"] = "library", limit: int = 10
    ) -> dict[str, Any]:
        """Soulseek copies of a resolved track, best first, each with a copy id.

        profile="library" puts lossless first; "chat" ranks for size and sound (what fits the upload cap).
        """
        return await tools.search_copies(track_id, profile, limit)

    @server.tool()
    async def download(copy_id: str, ctx: Context, deliver: Literal["library", "path"] = "library") -> dict[str, Any]:
        """Download a copy and wait for it (up to DOWNLOAD_TIMEOUT_SECS), reporting progress.

        deliver="library" renames it, embeds the cover and moves it into the library; "path" leaves the file
        in DOWNLOAD_DIR and returns its path. Both return the lossless check verdict for lossless formats.
        """
        return await tools.download(copy_id, deliver, progress=ctx.report_progress)

    @server.tool()
    async def album_listing(copy_id: str) -> dict[str, Any]:
        """The audio files of the peer folder a copy came from (usually its whole release): names, formats,
        sizes and the total. answered=false with a reason when the peer is offline or does not answer."""
        return await tools.album_listing(copy_id)

    @server.tool()
    async def album_download(
        copy_id: str, ctx: Context, deliver: Literal["library", "path"] = "library"
    ) -> dict[str, Any]:
        """Download every audio file of the folder a copy came from, reporting progress per file.

        deliver="library" saves each file into the library as it lands (named from its tags, the album cover
        embedded); "path" leaves them in DOWNLOAD_DIR. Returns one outcome per file; a failed file never
        stops the others. Each file waits up to DOWNLOAD_TIMEOUT_SECS, the album up to ALBUM_TIMEOUT_SECS.
        """
        return await tools.album_download(copy_id, deliver, progress=ctx.report_progress)

    @server.tool()
    async def history(limit: int = 20) -> dict[str, Any]:
        """The most recent downloads (saved, delivered, failed or rejected), newest first."""
        return await tools.history(limit)

    @server.tool()
    async def library_has(artist: str, title: str) -> dict[str, Any]:
        """Whether the library already holds a file that looks like this track, with the closest matches."""
        return await tools.library_has(artist, title)

    @server.tool()
    async def wishlist_add(track_id: str, wanted: Literal["any", "better"] = "any") -> dict[str, Any]:
        """Search a resolved track again every WISHLIST_CHECK_HOURS until a copy turns up.

        wanted="better" waits for a copy above the best one search_copies found. Hits are announced in the
        owner's Telegram chat (the first id in TELEGRAM_ALLOWED_USERS) by the bot's wishlist checker.
        """
        return await tools.wishlist_add(track_id, wanted)

    @server.tool()
    async def wishlist_list() -> dict[str, Any]:
        """Every wish on the wishlist, from every chat."""
        return await tools.wishlist_list()

    @server.tool()
    async def wishlist_remove(id: int) -> dict[str, Any]:
        """Remove a wish by its id (from wishlist_list)."""
        return await tools.wishlist_remove(id)

    @server.tool()
    async def status() -> dict[str, Any]:
        """Whether slskd answers, downloads waiting or running, wishes, the upload cap and the version."""
        return await tools.status()

    return server


class BearerAuth:
    """ASGI wrapper: HTTP requests without "Authorization: Bearer <token>" get a 401 (constant-time compare)."""

    def __init__(self, app, token: str):
        if not token:
            raise ValueError("BearerAuth needs a token")
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            supplied = next((v for k, v in scope.get("headers", []) if k.lower() == b"authorization"), b"")
            if not hmac.compare_digest(supplied, self._expected):
                body = json.dumps({"error": "unauthorized"}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def http_app(tools: McpTools, token: str, host: str = "0.0.0.0"):
    """The streamable HTTP app at HTTP_PATH behind BearerAuth."""
    app = build_server(tools).streamable_http_app(streamable_http_path=HTTP_PATH, host=host)
    return BearerAuth(app, token)


async def serve_http(pipeline, config: Config) -> None:
    """Serve MCP over streamable HTTP on MCP_HOST:MCP_PORT until cancelled, sharing *pipeline* with the bot."""
    import uvicorn

    class _Server(uvicorn.Server):
        # The bot owns SIGINT/SIGTERM; uvicorn must not take them over.
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    app = http_app(McpTools(pipeline), config.mcp_token, config.mcp_host)
    server = _Server(uvicorn.Config(app, host=config.mcp_host, port=config.mcp_port, log_level="warning"))
    logger.info("MCP server on http://%s:%d%s (bearer token required)", config.mcp_host, config.mcp_port, HTTP_PATH)
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await server.shutdown()
        raise
    except SystemExit:
        # uvicorn exits when it cannot bind; the bot keeps running without MCP.
        logger.error("MCP server could not start on %s:%d", config.mcp_host, config.mcp_port)
    except Exception:
        # The bot keeps running; without this the task would end without a word.
        logger.exception("MCP server stopped")


def run_stdio(config: Config) -> None:
    """`python -m music_downloader mcp`: serve MCP over stdin/stdout with its own Pipeline."""
    from music_downloader.persistence.album_repo import ALBUM_OWNER_STDIO
    from music_downloader.pipeline import Pipeline

    pipeline = Pipeline(config)
    # Its albums are its own: the bot's startup recovery leaves them alone.
    pipeline.album_owner = ALBUM_OWNER_STDIO
    server = build_server(McpTools(pipeline))

    async def main() -> None:
        # library_has reads the library index: keep it fresh when no bot runs next to this.
        index_task = asyncio.create_task(pipeline.library_index_loop())
        try:
            await server.run_stdio_async()
        finally:
            index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await index_task

    asyncio.run(main())
