#!/bin/sh
set -e

echo "slskd-importer - Starting..."
echo "Version: $(python -c 'from music_downloader import __version__; print(__version__)')"

# Execute the provided command or default to running the bot
exec "$@"
