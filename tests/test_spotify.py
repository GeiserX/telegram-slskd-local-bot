"""Tests for Spotify metadata resolver."""

import pytest

from music_downloader.metadata.spotify import TrackInfo


class TestTrackInfo:
    """Tests for the TrackInfo dataclass."""

    @pytest.fixture
    def track(self):
        return TrackInfo(
            artist="Nancy Sinatra",
            title="Bang Bang (My Baby Shot Me Down)",
            album="How Does That Grab You?",
            duration_ms=162000,
            spotify_url="https://open.spotify.com/track/xxx",
            year="1966",
        )

    def test_duration_secs(self, track):
        assert track.duration_secs == 162

    def test_duration_display(self, track):
        assert track.duration_display == "2:42"

    def test_filename(self, track):
        assert track.filename == "Nancy Sinatra - Bang Bang (My Baby Shot Me Down)"

    def test_str(self, track):
        assert str(track) == "Nancy Sinatra - Bang Bang (My Baby Shot Me Down) (2:42)"

    def test_short_duration(self):
        """Test duration display for sub-minute tracks."""
        track = TrackInfo("A", "B", "C", 45000, "", "2024")
        assert track.duration_display == "0:45"

    def test_long_duration(self):
        """Test duration display for long tracks."""
        track = TrackInfo("A", "B", "C", 600000, "", "2024")  # 10 minutes
        assert track.duration_display == "10:00"
