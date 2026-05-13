"""Tests for Spotify playlist and album resolver."""

from unittest.mock import MagicMock

import pytest
import spotipy

from music_downloader.metadata.playlist import MAX_IMPORT_TRACKS, PlaylistInfo, PlaylistResolver


@pytest.fixture
def mock_spotify():
    sp = MagicMock()
    resolver = MagicMock()
    resolver.sp = sp
    return resolver


@pytest.fixture
def playlist_resolver(mock_spotify):
    return PlaylistResolver(mock_spotify)


def _make_playlist_track(artist="Artist", title="Song", album="Album", duration_ms=200000):
    return {
        "track": {
            "name": title,
            "artists": [{"name": artist}],
            "album": {"name": album, "release_date": "2023-01-01"},
            "duration_ms": duration_ms,
            "external_urls": {"spotify": "https://open.spotify.com/track/abc123"},
        }
    }


def _make_album_track(artist="Artist", title="Song", duration_ms=200000):
    return {
        "name": title,
        "artists": [{"name": artist}],
        "duration_ms": duration_ms,
        "external_urls": {"spotify": "https://open.spotify.com/track/abc123"},
    }


class TestIsSpotifyUrl:
    def test_playlist_url(self):
        assert PlaylistResolver.is_spotify_url("https://open.spotify.com/playlist/abc123") is True

    def test_album_url(self):
        assert PlaylistResolver.is_spotify_url("https://open.spotify.com/album/xyz789") is True

    def test_playlist_uri(self):
        assert PlaylistResolver.is_spotify_url("spotify:playlist:abc123") is True

    def test_album_uri(self):
        assert PlaylistResolver.is_spotify_url("spotify:album:xyz789") is True

    def test_track_url_not_matched(self):
        assert PlaylistResolver.is_spotify_url("https://open.spotify.com/track/abc123") is False

    def test_random_text(self):
        assert PlaylistResolver.is_spotify_url("hello world") is False


class TestExtractUrlType:
    def test_playlist_url(self):
        assert PlaylistResolver.extract_url_type("https://open.spotify.com/playlist/abc123") == "playlist"

    def test_album_url(self):
        assert PlaylistResolver.extract_url_type("https://open.spotify.com/album/xyz789") == "album"

    def test_playlist_uri(self):
        assert PlaylistResolver.extract_url_type("spotify:playlist:abc123") == "playlist"

    def test_album_uri(self):
        assert PlaylistResolver.extract_url_type("spotify:album:xyz789") == "album"

    def test_unknown_url(self):
        assert PlaylistResolver.extract_url_type("https://example.com/something") is None

    def test_track_url(self):
        assert PlaylistResolver.extract_url_type("https://open.spotify.com/track/abc") is None


class TestExtractId:
    def test_extract_from_playlist_url(self, playlist_resolver):
        import re

        url_re = re.compile(r"spotify\.com/playlist/([a-zA-Z0-9]+)")
        uri_re = re.compile(r"spotify:playlist:([a-zA-Z0-9]+)")
        result = PlaylistResolver._extract_id(
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", url_re, uri_re
        )
        assert result == "37i9dQZF1DXcBWIGoYBM5M"

    def test_extract_from_uri(self, playlist_resolver):
        import re

        url_re = re.compile(r"spotify\.com/playlist/([a-zA-Z0-9]+)")
        uri_re = re.compile(r"spotify:playlist:([a-zA-Z0-9]+)")
        result = PlaylistResolver._extract_id("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", url_re, uri_re)
        assert result == "37i9dQZF1DXcBWIGoYBM5M"

    def test_raises_on_bad_url(self, playlist_resolver):
        import re

        url_re = re.compile(r"spotify\.com/playlist/([a-zA-Z0-9]+)")
        uri_re = re.compile(r"spotify:playlist:([a-zA-Z0-9]+)")
        with pytest.raises(ValueError, match="Could not extract ID"):
            PlaylistResolver._extract_id("https://example.com/nothing", url_re, uri_re)


class TestResolve:
    def test_resolve_playlist_url(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "My Playlist",
            "owner": {"display_name": "User"},
            "external_urls": {"spotify": "https://open.spotify.com/playlist/abc"},
        }
        mock_spotify.sp.playlist_tracks.return_value = {
            "items": [_make_playlist_track()],
            "next": None,
        }
        mock_spotify.sp.next.return_value = None

        result = playlist_resolver.resolve("https://open.spotify.com/playlist/abc123")

        assert result is not None
        assert result.name == "My Playlist"
        assert result.is_album is False
        mock_spotify.sp.playlist.assert_called_once_with("abc123")

    def test_resolve_album_url(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "My Album",
            "artists": [{"name": "The Band"}],
            "release_date": "2023-05-01",
            "external_urls": {"spotify": "https://open.spotify.com/album/xyz"},
        }
        mock_spotify.sp.album_tracks.return_value = {
            "items": [_make_album_track()],
            "next": None,
        }
        mock_spotify.sp.next.return_value = None

        result = playlist_resolver.resolve("https://open.spotify.com/album/xyz789")

        assert result is not None
        assert result.name == "My Album"
        assert result.is_album is True
        mock_spotify.sp.album.assert_called_once_with("xyz789")

    def test_resolve_raises_on_invalid_url(self, playlist_resolver):
        with pytest.raises(ValueError, match="Not a recognized Spotify"):
            playlist_resolver.resolve("https://example.com/not-spotify")


class TestResolvePlaylist:
    def test_basic_playlist(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "Chill Vibes",
            "owner": {"display_name": "DJ Cool"},
            "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"},
        }
        mock_spotify.sp.playlist_tracks.return_value = {
            "items": [
                _make_playlist_track("Artist1", "Song1"),
                _make_playlist_track("Artist2", "Song2"),
            ],
            "next": None,
        }

        result = playlist_resolver._resolve_playlist("p1")

        assert result.name == "Chill Vibes"
        assert result.owner == "DJ Cool"
        assert result.total_tracks == 2
        assert result.spotify_url == "https://open.spotify.com/playlist/p1"
        assert result.is_album is False
        assert result.tracks[0].artist == "Artist1"
        assert result.tracks[1].title == "Song2"

    def test_pagination(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "Big Playlist",
            "owner": {"display_name": "Owner"},
            "external_urls": {"spotify": ""},
        }
        page1 = {
            "items": [_make_playlist_track("A1", "S1")],
            "next": "https://api.spotify.com/v1/next-page",
        }
        page2 = {
            "items": [_make_playlist_track("A2", "S2")],
            "next": None,
        }
        mock_spotify.sp.playlist_tracks.return_value = page1
        mock_spotify.sp.next.return_value = page2

        result = playlist_resolver._resolve_playlist("p2")

        assert result.total_tracks == 2
        assert result.tracks[0].artist == "A1"
        assert result.tracks[1].artist == "A2"
        mock_spotify.sp.next.assert_called_once_with(page1)

    def test_skips_track_with_no_artists(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "P",
            "owner": {"display_name": "O"},
            "external_urls": {"spotify": ""},
        }
        items = [
            _make_playlist_track("Good", "Track"),
            {"track": {"name": "No Artist", "artists": [], "album": {}, "duration_ms": 0, "external_urls": {}}},
        ]
        mock_spotify.sp.playlist_tracks.return_value = {"items": items, "next": None}

        result = playlist_resolver._resolve_playlist("p3")

        assert result.total_tracks == 1
        assert result.tracks[0].artist == "Good"

    def test_skips_none_track(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "P",
            "owner": {"display_name": "O"},
            "external_urls": {"spotify": ""},
        }
        items = [
            _make_playlist_track("Good", "Track"),
            {"track": None},
        ]
        mock_spotify.sp.playlist_tracks.return_value = {"items": items, "next": None}

        result = playlist_resolver._resolve_playlist("p4")

        assert result.total_tracks == 1

    def test_caps_at_max_import_tracks(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.return_value = {
            "name": "Huge",
            "owner": {"display_name": "O"},
            "external_urls": {"spotify": ""},
        }
        items = [_make_playlist_track("A", f"S{i}") for i in range(MAX_IMPORT_TRACKS + 50)]
        mock_spotify.sp.playlist_tracks.return_value = {"items": items, "next": None}

        result = playlist_resolver._resolve_playlist("p5")

        assert result.total_tracks == MAX_IMPORT_TRACKS
        assert len(result.tracks) == MAX_IMPORT_TRACKS

    def test_spotify_exception_returns_none(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.playlist.side_effect = spotipy.SpotifyException(http_status=404, code=-1, msg="Not found")

        result = playlist_resolver._resolve_playlist("bad_id")

        assert result is None


class TestResolveAlbum:
    def test_basic_album(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "Abbey Road",
            "artists": [{"name": "The Beatles"}],
            "release_date": "1969-09-26",
            "external_urls": {"spotify": "https://open.spotify.com/album/a1"},
        }
        mock_spotify.sp.album_tracks.return_value = {
            "items": [
                _make_album_track("The Beatles", "Come Together"),
                _make_album_track("The Beatles", "Something"),
            ],
            "next": None,
        }

        result = playlist_resolver._resolve_album("a1")

        assert result.name == "Abbey Road"
        assert result.owner == "The Beatles"
        assert result.total_tracks == 2
        assert result.spotify_url == "https://open.spotify.com/album/a1"
        assert result.is_album is True
        assert result.tracks[0].title == "Come Together"
        assert result.tracks[0].year == "1969"
        assert result.tracks[0].album == "Abbey Road"

    def test_album_no_artists_uses_unknown(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "Mystery Album",
            "artists": [],
            "release_date": "2020",
            "external_urls": {"spotify": ""},
        }
        mock_spotify.sp.album_tracks.return_value = {
            "items": [_make_album_track("X", "Y")],
            "next": None,
        }

        result = playlist_resolver._resolve_album("a2")

        assert result.owner == "Unknown Artist"

    def test_pagination(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "Double Album",
            "artists": [{"name": "Band"}],
            "release_date": "2022",
            "external_urls": {"spotify": ""},
        }
        page1 = {
            "items": [_make_album_track("Band", "T1")],
            "next": "https://api.spotify.com/v1/next",
        }
        page2 = {
            "items": [_make_album_track("Band", "T2")],
            "next": None,
        }
        mock_spotify.sp.album_tracks.return_value = page1
        mock_spotify.sp.next.return_value = page2

        result = playlist_resolver._resolve_album("a3")

        assert result.total_tracks == 2
        assert result.tracks[1].title == "T2"
        mock_spotify.sp.next.assert_called_once_with(page1)

    def test_skips_track_with_no_artists(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "A",
            "artists": [{"name": "B"}],
            "release_date": "2023",
            "external_urls": {"spotify": ""},
        }
        items = [
            _make_album_track("Good", "Track"),
            {"name": "Bad", "artists": [], "duration_ms": 0, "external_urls": {}},
        ]
        mock_spotify.sp.album_tracks.return_value = {"items": items, "next": None}

        result = playlist_resolver._resolve_album("a4")

        assert result.total_tracks == 1

    def test_caps_at_max_import_tracks(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.return_value = {
            "name": "Massive",
            "artists": [{"name": "Prog"}],
            "release_date": "2021",
            "external_urls": {"spotify": ""},
        }
        items = [_make_album_track("Prog", f"T{i}") for i in range(MAX_IMPORT_TRACKS + 10)]
        mock_spotify.sp.album_tracks.return_value = {"items": items, "next": None}

        result = playlist_resolver._resolve_album("a5")

        assert result.total_tracks == MAX_IMPORT_TRACKS
        assert len(result.tracks) == MAX_IMPORT_TRACKS

    def test_spotify_exception_returns_none(self, playlist_resolver, mock_spotify):
        mock_spotify.sp.album.side_effect = spotipy.SpotifyException(http_status=403, code=-1, msg="Forbidden")

        result = playlist_resolver._resolve_album("bad_id")

        assert result is None


class TestPlaylistInfo:
    def test_dataclass_fields(self):
        from music_downloader.metadata.spotify import TrackInfo

        track = TrackInfo("A", "B", "C", 180000, "", "2023")
        info = PlaylistInfo(
            name="Test",
            owner="Owner",
            total_tracks=1,
            spotify_url="https://open.spotify.com/playlist/x",
            tracks=[track],
            is_album=False,
        )
        assert info.name == "Test"
        assert info.owner == "Owner"
        assert info.total_tracks == 1
        assert info.is_album is False
        assert len(info.tracks) == 1
