"""Spotify playlist and album resolver."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import spotipy

from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo

logger = logging.getLogger(__name__)

MAX_IMPORT_TRACKS = 500

_PLAYLIST_RE = re.compile(r"spotify\.com/playlist/([a-zA-Z0-9]+)")
_ALBUM_RE = re.compile(r"spotify\.com/album/([a-zA-Z0-9]+)")
_PLAYLIST_URI_RE = re.compile(r"spotify:playlist:([a-zA-Z0-9]+)")
_ALBUM_URI_RE = re.compile(r"spotify:album:([a-zA-Z0-9]+)")


@dataclass
class PlaylistInfo:
    """Resolved playlist/album metadata."""

    name: str
    owner: str
    total_tracks: int
    spotify_url: str
    tracks: list[TrackInfo]
    is_album: bool


class PlaylistResolver:
    """Resolves full track lists from Spotify playlist/album URLs."""

    def __init__(self, spotify: SpotifyResolver) -> None:
        self.spotify = spotify

    def resolve(self, url: str) -> PlaylistInfo | None:
        """Resolve a Spotify playlist or album URL to full track list."""
        url_type = self.extract_url_type(url)
        if url_type is None:
            raise ValueError(f"Not a recognized Spotify playlist/album URL: {url}")

        if url_type == "playlist":
            playlist_id = self._extract_id(url, _PLAYLIST_RE, _PLAYLIST_URI_RE)
            return self._resolve_playlist(playlist_id)

        album_id = self._extract_id(url, _ALBUM_RE, _ALBUM_URI_RE)
        return self._resolve_album(album_id)

    def _resolve_playlist(self, playlist_id: str) -> PlaylistInfo | None:
        try:
            playlist = self.spotify.sp.playlist(playlist_id)
            name = playlist["name"]
            owner = playlist["owner"]["display_name"]
            spotify_url = playlist["external_urls"].get("spotify", "")

            results = self.spotify.sp.playlist_tracks(playlist_id)
            items: list[dict] = []
            while results:
                items.extend(results["items"])
                results = self.spotify.sp.next(results) if results.get("next") else None

            tracks: list[TrackInfo] = []
            for item in items:
                track = item.get("track")
                if track is None:
                    continue
                artists = track.get("artists")
                if not artists:
                    continue
                tracks.append(
                    TrackInfo(
                        artist=artists[0]["name"],
                        title=track["name"],
                        album=track.get("album", {}).get("name", ""),
                        duration_ms=track.get("duration_ms", 0),
                        spotify_url=track.get("external_urls", {}).get("spotify", ""),
                        year=track.get("album", {}).get("release_date", "")[:4],
                    )
                )

            if len(tracks) > MAX_IMPORT_TRACKS:
                logger.warning("Playlist '%s' has %d tracks, capping at %d", name, len(tracks), MAX_IMPORT_TRACKS)
                tracks = tracks[:MAX_IMPORT_TRACKS]

            return PlaylistInfo(
                name=name,
                owner=owner,
                total_tracks=len(tracks),
                spotify_url=spotify_url,
                tracks=tracks,
                is_album=False,
            )
        except spotipy.SpotifyException:
            logger.exception(f"Failed to resolve playlist: {playlist_id}")
            return None

    def _resolve_album(self, album_id: str) -> PlaylistInfo | None:
        try:
            album = self.spotify.sp.album(album_id)
            name = album["name"]
            artists = album.get("artists")
            artist = artists[0]["name"] if artists else "Unknown Artist"
            year = album.get("release_date", "")[:4]
            spotify_url = album["external_urls"].get("spotify", "")

            results = self.spotify.sp.album_tracks(album_id)
            raw_tracks: list[dict] = []
            while results:
                raw_tracks.extend(results["items"])
                results = self.spotify.sp.next(results) if results.get("next") else None

            tracks: list[TrackInfo] = []
            for t in raw_tracks:
                t_artists = t.get("artists")
                if not t_artists:
                    continue
                tracks.append(
                    TrackInfo(
                        artist=t_artists[0]["name"],
                        title=t["name"],
                        album=name,
                        duration_ms=t.get("duration_ms", 0),
                        spotify_url=t.get("external_urls", {}).get("spotify", ""),
                        year=year,
                    )
                )

            if len(tracks) > MAX_IMPORT_TRACKS:
                logger.warning("Album '%s' has %d tracks, capping at %d", name, len(tracks), MAX_IMPORT_TRACKS)
                tracks = tracks[:MAX_IMPORT_TRACKS]

            return PlaylistInfo(
                name=name,
                owner=artist,
                total_tracks=len(tracks),
                spotify_url=spotify_url,
                tracks=tracks,
                is_album=True,
            )
        except spotipy.SpotifyException:
            logger.exception(f"Failed to resolve album: {album_id}")
            return None

    @staticmethod
    def is_spotify_url(text: str) -> bool:
        """Check if text contains a Spotify playlist or album URL."""
        return bool(
            _PLAYLIST_RE.search(text)
            or _ALBUM_RE.search(text)
            or _PLAYLIST_URI_RE.search(text)
            or _ALBUM_URI_RE.search(text)
        )

    @staticmethod
    def extract_url_type(url: str) -> str | None:
        """Return 'playlist', 'album', or None."""
        if _PLAYLIST_RE.search(url) or _PLAYLIST_URI_RE.search(url):
            return "playlist"
        if _ALBUM_RE.search(url) or _ALBUM_URI_RE.search(url):
            return "album"
        return None

    @staticmethod
    def _extract_id(url: str, url_re: re.Pattern[str], uri_re: re.Pattern[str]) -> str:
        match = url_re.search(url) or uri_re.search(url)
        if not match:
            raise ValueError(f"Could not extract ID from URL: {url}")
        return match.group(1)
