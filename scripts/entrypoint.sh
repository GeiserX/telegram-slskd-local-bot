#!/bin/sh
set -e

echo "Music Downloader - Starting..."
echo "Version: $(python -c 'from music_downloader import __version__; print(__version__)')"

# Execute the provided command or default to running the bot
exec "$@"
