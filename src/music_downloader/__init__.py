"""telegram-slskd-local-bot - Automated music discovery and download via Telegram bot."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("telegram-slskd-local-bot")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
