"""
Entry point for Music Downloader.
Can be run as: python -m music_downloader
"""

import argparse
import logging
import sys
import threading

import uvicorn
from fastapi import FastAPI

from music_downloader import __version__
from music_downloader.bot.handlers import create_bot
from music_downloader.config import Config, setup_logging

logger = logging.getLogger(__name__)


def create_health_app() -> FastAPI:
    """Create a minimal FastAPI app for health checks."""
    app = FastAPI(title="Music Downloader", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    return app


def run_health_server(port: int):
    """Run the health check HTTP server in a background thread."""
    app = create_health_app()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()


def cmd_run(args):
    """Run the Telegram bot with health check endpoint."""
    config = Config()
    setup_logging(config)

    logger.info(f"Music Downloader v{__version__} starting...")

    # Start health check server in background
    health_thread = threading.Thread(target=run_health_server, args=(config.health_port,), daemon=True)
    health_thread.start()
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
