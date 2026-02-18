"""
Batch artwork embedder.

Scans a music directory for audio files missing embedded cover artwork,
looks up each track on Spotify, downloads the album art, and embeds it
into the file using mutagen.  Optionally mirrors artwork into AAC and
ALAC sibling directories that share the same flat naming scheme.

Usage:
    python -m music_downloader.tools.artwork_embedder \
        --music-dir /path/to/WCYR-FLAC \
        [--aac-dir /path/to/WCYR-AAC-256] \
        [--alac-dir /path/to/WCYR-ALAC] \
        [--report /path/to/missing_artwork.txt]
"""

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import httpx
import mutagen.flac
import mutagen.mp4
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

logger = logging.getLogger(__name__)

SPOTIFY_RATE_LIMIT_PAUSE = 2.0
ARTWORK_MIME = "image/jpeg"


def _parse_artist_title(filename: str) -> tuple[str, str] | None:
    """Extract (artist, title) from ``Artist - Title.ext`` filename."""
    stem = Path(filename).stem
    match = re.match(r"^(.+?)\s*-\s*(.+)$", stem)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _flac_has_art(path: str) -> bool:
    try:
        f = mutagen.flac.FLAC(path)
        return bool(f.pictures)
    except Exception:
        return False


def _m4a_has_art(path: str) -> bool:
    try:
        f = mutagen.mp4.MP4(path)
        covers = f.tags.get("covr", [])
        return bool(covers)
    except Exception:
        return False


def _fetch_spotify_artwork_url(sp: spotipy.Spotify, artist: str, title: str) -> str | None:
    """Search Spotify and return the best album artwork URL (largest available)."""
    query = f"{artist} {title}"
    try:
        results = sp.search(q=query, type="track", limit=1)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            return None
        images = tracks[0].get("album", {}).get("images", [])
        if not images:
            return None
        # images are sorted largest-first by Spotify
        return images[0]["url"]
    except Exception:
        logger.exception("Spotify search failed for: %s - %s", artist, title)
        return None


def _download_image(url: str) -> bytes | None:
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except Exception:
        logger.exception("Failed to download artwork from %s", url)
        return None


def _embed_flac(path: str, image_data: bytes) -> bool:
    try:
        f = mutagen.flac.FLAC(path)
        pic = mutagen.flac.Picture()
        pic.type = 3  # Cover (front)
        pic.mime = ARTWORK_MIME
        pic.desc = "Cover"
        pic.data = image_data
        f.clear_pictures()
        f.add_picture(pic)
        f.save()
        return True
    except Exception:
        logger.exception("Failed to embed artwork into FLAC: %s", path)
        return False


def _embed_m4a(path: str, image_data: bytes) -> bool:
    try:
        f = mutagen.mp4.MP4(path)
        f.tags["covr"] = [
            mutagen.mp4.MP4Cover(image_data, imageformat=mutagen.mp4.MP4Cover.FORMAT_JPEG)
        ]
        f.save()
        return True
    except Exception:
        logger.exception("Failed to embed artwork into M4A: %s", path)
        return False


def _sibling_m4a_path(flac_path: str, target_dir: str) -> str | None:
    """Return the .m4a path in *target_dir* with the same stem, or None if missing."""
    stem = Path(flac_path).stem
    candidate = os.path.join(target_dir, f"{stem}.m4a")
    return candidate if os.path.isfile(candidate) else None


def run(
    music_dir: str,
    aac_dir: str | None = None,
    alac_dir: str | None = None,
    report_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Scan *music_dir* for FLAC files without embedded artwork, fetch from
    Spotify, and embed.  Returns a summary dict.
    """
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set")
        sys.exit(1)

    sp = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
    )

    flac_files = sorted(f for f in os.listdir(music_dir) if f.lower().endswith(".flac"))
    logger.info("Found %d FLAC files in %s", len(flac_files), music_dir)

    missing: list[str] = []
    for fname in flac_files:
        fpath = os.path.join(music_dir, fname)
        if not _flac_has_art(fpath):
            missing.append(fname)

    logger.info("%d files missing artwork (of %d total)", len(missing), len(flac_files))

    stats = {"total": len(flac_files), "missing": len(missing), "embedded": 0, "failed": [], "skipped": []}
    failures: list[str] = []

    for i, fname in enumerate(missing, 1):
        parsed = _parse_artist_title(fname)
        if not parsed:
            logger.warning("[%d/%d] Cannot parse artist/title: %s", i, len(missing), fname)
            failures.append(fname)
            continue

        artist, title = parsed
        logger.info("[%d/%d] %s - %s", i, len(missing), artist, title)

        if dry_run:
            stats["skipped"].append(fname)
            continue

        url = _fetch_spotify_artwork_url(sp, artist, title)
        if not url:
            logger.warning("  -> No Spotify artwork found")
            failures.append(fname)
            time.sleep(0.3)
            continue

        image_data = _download_image(url)
        if not image_data:
            failures.append(fname)
            continue

        flac_path = os.path.join(music_dir, fname)
        if _embed_flac(flac_path, image_data):
            stats["embedded"] += 1
            logger.info("  -> Embedded artwork (%d KB)", len(image_data) // 1024)
        else:
            failures.append(fname)
            continue

        # Mirror to AAC / ALAC directories
        for label, target_dir in [("AAC", aac_dir), ("ALAC", alac_dir)]:
            if not target_dir:
                continue
            m4a_path = _sibling_m4a_path(flac_path, target_dir)
            if m4a_path and not _m4a_has_art(m4a_path):
                if _embed_m4a(m4a_path, image_data):
                    logger.info("  -> Also embedded into %s copy", label)

        # Respect Spotify rate limits
        time.sleep(SPOTIFY_RATE_LIMIT_PAUSE)

    stats["failed"] = failures

    # Write report
    if report_path and failures:
        with open(report_path, "w") as fh:
            fh.write(f"# Artwork embedding failures ({len(failures)} files)\n")
            fh.write(f"# Run date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for f in failures:
                fh.write(f"{f}\n")
        logger.info("Wrote failure report to %s (%d entries)", report_path, len(failures))

    logger.info(
        "Done: %d embedded, %d failed, %d already had artwork",
        stats["embedded"],
        len(failures),
        stats["total"] - stats["missing"],
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Batch embed album artwork from Spotify into audio files")
    parser.add_argument("--music-dir", required=True, help="Path to FLAC music directory")
    parser.add_argument("--aac-dir", default=None, help="Path to AAC-256 directory (optional)")
    parser.add_argument("--alac-dir", default=None, help="Path to ALAC directory (optional)")
    parser.add_argument("--report", default=None, help="Path to write failure report")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, don't embed")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("spotipy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    run(
        music_dir=args.music_dir,
        aac_dir=args.aac_dir,
        alac_dir=args.alac_dir,
        report_path=args.report,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
