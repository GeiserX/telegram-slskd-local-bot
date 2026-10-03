"""
Embed album artwork from Spotify into a single audio file.

Reusable by both the batch embedder and the Telegram bot.
"""

import base64
import logging

import httpx
import mutagen
import mutagen.aiff
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import mutagen.mp4
import mutagen.oggopus
import mutagen.oggvorbis
import mutagen.wave
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


def _cover_picture(image_data: bytes) -> mutagen.flac.Picture:
    """A front-cover JPEG picture block (FLAC, and base64-wrapped in Ogg)."""
    pic = mutagen.flac.Picture()
    pic.type = 3  # Cover (front)
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    pic.data = image_data
    return pic


def embed_artwork_into_file(filepath: str, image_data: bytes) -> bool:
    """Embed JPEG artwork into a FLAC, M4A, MP3, Ogg Vorbis, Opus, WAV or AIFF file.

    Returns True when artwork was written; False when the file already has
    artwork, the format has no artwork support here, or mutagen cannot read it.
    """
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    try:
        if ext == "flac":
            f = mutagen.flac.FLAC(filepath)
            if f.pictures:
                return False
            f.clear_pictures()
            f.add_picture(_cover_picture(image_data))
            f.save()
            return True
        elif ext in ("m4a", "mp4", "alac", "aac"):
            f = mutagen.mp4.MP4(filepath)
            if f.tags and f.tags.get("covr"):
                return False
            f.tags["covr"] = [mutagen.mp4.MP4Cover(image_data, imageformat=mutagen.mp4.MP4Cover.FORMAT_JPEG)]
            f.save()
            return True
        elif ext == "mp3":
            f = mutagen.mp3.MP3(filepath)
            if f.tags is None:
                f.add_tags()
            if f.tags.getall("APIC"):
                return False
            f.tags.add(mutagen.id3.APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_data))
            f.save()
            return True
        elif ext in ("wav", "aiff", "aif"):
            # Both carry an ID3 chunk, the same APIC frame as MP3.
            f = mutagen.wave.WAVE(filepath) if ext == "wav" else mutagen.aiff.AIFF(filepath)
            if f.tags is None:
                f.add_tags()
            if f.tags.getall("APIC"):
                return False
            f.tags.add(mutagen.id3.APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_data))
            f.save()
            return True
        elif ext in ("ogg", "opus"):
            f = mutagen.File(filepath, options=[mutagen.oggvorbis.OggVorbis, mutagen.oggopus.OggOpus])
            if f is None:
                return False
            if f.tags is None:
                f.add_tags()
            if f.tags.get("metadata_block_picture"):
                return False
            f.tags["metadata_block_picture"] = [base64.b64encode(_cover_picture(image_data).write()).decode("ascii")]
            f.save()
            return True
        else:
            logger.debug("Unsupported format for artwork embedding: %s", ext)
            return False
    except Exception:
        logger.debug("Failed to embed artwork into %s", filepath, exc_info=True)
        return False
