"""
Entry point for Music Downloader.
Can be run as: python -m music_downloader
"""

import argparse
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from music_downloader import __version__
from music_downloader.bot.handlers import create_bot
from music_downloader.config import Config, setup_logging

logger = logging.getLogger(__name__)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress access logs


def _start_health_server(port: int):
    server = HTTPServer(("127.0.0.1", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def cmd_run(args):
    """Run the Telegram bot with health check endpoint."""
    config = Config()
    setup_logging(config)

    logger.info(f"Music Downloader v{__version__} starting...")

    # Start health check server in background
    _start_health_server(config.health_port)
    logger.info(f"Health check endpoint running on port {config.health_port}")

    # Start the Telegram bot (blocking)
    bot_app = create_bot(config)
    logger.info("Starting Telegram bot polling...")
    bot_app.run_polling(drop_pending_updates=True)


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

    args = parser.parse_args()

    if args.command is None:
        # Default to 'run'
        args.command = "run"

    if args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
