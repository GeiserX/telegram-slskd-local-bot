"""Extended tests for Spotify metadata resolver - covering SpotifyResolver class."""

from unittest.mock import MagicMock, patch

import pytest

from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo


class TestSpotifyResolver:
    @pytest.fixture
    def resolver(self):
        with patch("music_downloader.metadata.spotify.SpotifyClientCredentials"):
            with patch("music_downloader.metadata.spotify.spotipy.Spotify") as mock_sp:
                r = SpotifyResolver("test-id", "test-secret")
                r.sp = mock_sp.return_value
                return r

    def test_search_returns_track(self, resolver):
        resolver.sp.search.return_value = {
            "tracks": {
                "items": [
                    {
                        "artists": [{"name": "Nancy Sinatra"}],
                        "name": "Bang Bang",
                        "album": {
                            "name": "How Does That Grab You?",
                            "release_date": "1966-01-01",
                            "images": [],
                        },
                        "duration_ms": 162000,
                        "external_urls": {"spotify": "https://open.spotify.com/track/xxx"},
                    }
                ]
            }
        }
        result = resolver.search("Nancy Sinatra Bang Bang")
        assert result is not None
        assert result.artist == "Nancy Sinatra"
        assert result.title == "Bang Bang"
        assert result.year == "1966"

    def test_search_no_results(self, resolver):
        resolver.sp.search.return_value = {"tracks": {"items": []}}
        result = resolver.search("nonexistent song xyz")
        assert result is None

    def test_search_exception(self, resolver):
        resolver.sp.search.side_effect = Exception("API error")
        result = resolver.search("test")
        assert result is None

    def test_search_multiple_returns_list(self, resolver):
        resolver.sp.search.return_value = {
            "tracks": {
                "items": [
                    {
                        "artists": [{"name": "Artist1"}],
                        "name": "Song1",
                        "album": {"name": "Album1", "release_date": "2024-01-01", "images": []},
                        "duration_ms": 180000,
                        "external_urls": {"spotify": "https://open.spotify.com/track/1"},
                    },
                    {
                        "artists": [{"name": "Artist2"}],
                        "name": "Song2",
                        "album": {"name": "Album2", "release_date": "2023-06-15", "images": []},
                        "duration_ms": 200000,
                        "external_urls": {"spotify": "https://open.spotify.com/track/2"},
                    },
                ]
            }
        }
        results = resolver.search_multiple("test", limit=5)
        assert len(results) == 2
        assert results[0].artist == "Artist1"
        assert results[1].artist == "Artist2"

    def test_search_multiple_empty(self, resolver):
        resolver.sp.search.return_value = {"tracks": {"items": []}}
        results = resolver.search_multiple("nonexistent")
        assert results == []

    def test_search_multiple_exception(self, resolver):
        resolver.sp.search.side_effect = Exception("API error")
        results = resolver.search_multiple("test")
        assert results == []

    def test_search_missing_spotify_url(self, resolver):
        resolver.sp.search.return_value = {
            "tracks": {
                "items": [
                    {
                        "artists": [{"name": "Artist"}],
                        "name": "Song",
                        "album": {"name": "Album", "release_date": "2024"},
                        "duration_ms": 180000,
                        "external_urls": {},
                    }
                ]
            }
        }
        result = resolver.search("test")
        assert result is not None
        assert result.spotify_url == ""

    def test_search_missing_release_date(self, resolver):
        resolver.sp.search.return_value = {
            "tracks": {
                "items": [
                    {
                        "artists": [{"name": "Artist"}],
                        "name": "Song",
                        "album": {"name": "Album"},
                        "duration_ms": 180000,
                        "external_urls": {"spotify": "url"},
                    }
                ]
            }
        }
        result = resolver.search("test")
        assert result is not None
        assert result.year == ""
