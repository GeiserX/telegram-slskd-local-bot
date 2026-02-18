"""
Embed album artwork from Spotify into a single audio file.

Reusable by both the batch embedder and the Telegram bot.
"""

import logging

import httpx
import mutagen.flac
import mutagen.mp4
import spotipy

logger = logging.getLogger(__name__)


def fetch_spotify_artwork(sp: spotipy.Spotify, artist: str, title: str) -> bytes | None:
    """Search Spotify for a track and return album artwork bytes (JPEG)."""
    query = f"{artist} {title}"
    try:
        results = sp.search(q=query, type="track", limit=1)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            logger.debug("No Spotify results for artwork: %s", query)
            return None
        images = tracks[0].get("album", {}).get("images", [])
        if not images:
            return None
        url = images[0]["url"]
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        logger.debug("Spotify artwork fetch failed for: %s - %s", artist, title, exc_info=True)
        return None


def embed_artwork_into_file(filepath: str, image_data: bytes) -> bool:
    """Embed JPEG artwork into a FLAC or M4A file. Returns True on success."""
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    try:
        if ext == "flac":
            f = mutagen.flac.FLAC(filepath)
            if f.pictures:
                return False
            pic = mutagen.flac.Picture()
            pic.type = 3  # Cover (front)
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = image_data
            f.clear_pictures()
            f.add_picture(pic)
            f.save()
            return True
        elif ext in ("m4a", "mp4", "alac", "aac"):
            f = mutagen.mp4.MP4(filepath)
            if f.tags and f.tags.get("covr"):
                return False
            f.tags["covr"] = [
                mutagen.mp4.MP4Cover(image_data, imageformat=mutagen.mp4.MP4Cover.FORMAT_JPEG)
            ]
            f.save()
            return True
        else:
            logger.debug("Unsupported format for artwork embedding: %s", ext)
            return False
    except Exception:
        logger.debug("Failed to embed artwork into %s", filepath, exc_info=True)
        return False
