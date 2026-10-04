"""
Entry point for Music Downloader.
Can be run as: python -m music_downloader
"""

import argparse
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from music_downloader import __version__
from music_downloader.bot.handlers import create_bot
from music_downloader.config import Config, setup_logging
from music_downloader.health import HealthState

logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    """/health: 200 only while Telegram polling and slskd both work (503 + reason otherwise).

    /ready: a constant 200, for anything that only needs the process up.
    """

    def do_GET(self):
        if self.path == "/health":
            healthy, body = self.server.health.report()
            self._send_json(200 if healthy else 503, body)
        elif self.path == "/ready":
            self._send_json(200, {"status": "ready"})
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, separators=(",", ":")).encode())

    def log_message(self, format, *args):
        pass  # Suppress access logs


def _start_health_server(port: int, health: HealthState | None = None):
    server = HTTPServer(("127.0.0.1", port), HealthHandler)
    server.health = health or HealthState()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def cmd_run(args):
    """Run the Telegram bot with health check endpoint."""
    config = Config()
    setup_logging(config)

    logger.info(f"Music Downloader v{__version__} starting...")

    # Start health check server in background
    health = HealthState()
    _start_health_server(config.health_port, health)
    logger.info(f"Health check endpoint running on port {config.health_port}")

    # Start the Telegram bot (blocking)
    bot_app = create_bot(config, health)
    logger.info("Starting Telegram bot polling...")
    bot_app.run_polling(drop_pending_updates=True)


def cmd_mcp(args):
    """Serve the MCP tools over stdin/stdout (logs go to stderr)."""
    from music_downloader.mcp.server import run_stdio

    config = Config()
    setup_logging(config)
    logger.info(f"Music Downloader v{__version__} MCP server on stdio")
    run_stdio(config)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="slskd-importer",
        description="Automated music discovery and download via Telegram bot.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 'run' command (default)
    subparsers.add_parser("run", help="Start the bot and health server")
    subparsers.add_parser("mcp", help="Serve the MCP tools over stdio")

    args = parser.parse_args()

    if args.command is None:
        # Default to 'run'
        args.command = "run"

    if args.command == "run":
        cmd_run(args)
    elif args.command == "mcp":
        cmd_mcp(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
