"""Tests for the search result scoring engine."""

import pytest

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult


@pytest.fixture
def scorer():
    """Create a scorer with default settings."""
    return ResultScorer(duration_tolerance_secs=5)


@pytest.fixture
def track():
    """Create a reference track (Nancy Sinatra - Bang Bang)."""
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang (My Baby Shot Me Down)",
        album="How Does That Grab You?",
        duration_ms=162000,  # 2:42
        spotify_url="https://open.spotify.com/track/xxx",
        year="1966",
    )


def make_result(
    filename: str = "Nancy Sinatra - Bang Bang.flac",
    length: int = 162,
    bit_depth: int = 16,
    sample_rate: int = 44100,
    size: int = 30_000_000,
    has_free_slot: bool = True,
    upload_speed: int = 1_000_000,
    queue_length: int = 0,
) -> SearchResult:
    """Helper to create a SearchResult."""
    return SearchResult(
        username="testuser",
        filename=f"\\Music\\Nancy Sinatra\\{filename}",
        size=size,
        bit_rate=900,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        length=length,
        has_free_slot=has_free_slot,
        upload_speed=upload_speed,
        queue_length=queue_length,
    )


class TestResultScorer:
    """Tests for ResultScorer."""

    def test_perfect_match_scores_high(self, scorer, track):
        """A perfect match should score high."""
        result = make_result(length=162)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1
        assert scored[0].score > 70

    def test_duration_mismatch_excluded(self, scorer, track):
        """A result with very different duration should be excluded."""
        result = make_result(length=300)  # 5 minutes vs 2:42
        scored = scorer.score_results([result], track)
        assert len(scored) == 0

    def test_live_version_excluded(self, scorer, track):
        """Results with 'live' in filename should be excluded."""
        result = make_result(filename="Nancy Sinatra - Bang Bang (Live at Radio City).flac")
        scored = scorer.score_results([result], track)
        assert len(scored) == 0

    def test_remix_excluded(self, scorer, track):
        """Results with 'remix' in filename should be excluded."""
        result = make_result(filename="Nancy Sinatra - Bang Bang (DJ Remix).flac")
        scored = scorer.score_results([result], track)
        assert len(scored) == 0

    def test_keyword_in_original_title_not_excluded(self, scorer):
        """If the original title contains the keyword, don't exclude it."""
        # Track title actually contains "Acoustic"
        acoustic_track = TrackInfo(
            artist="Some Artist",
            title="My Acoustic Song",
            album="Album",
            duration_ms=200_000,
            spotify_url="",
            year="2024",
        )
        result = make_result(
            filename="Some Artist - My Acoustic Song.flac",
            length=200,
        )
        scored = scorer.score_results([result], acoustic_track)
        assert len(scored) == 1

    def test_close_duration_preferred(self, scorer, track):
        """Results closer in duration should score higher."""
        exact = make_result(length=162)
        close = make_result(length=165, filename="Nancy Sinatra - Bang Bang v2.flac")
        scored = scorer.score_results([exact, close], track)
        assert scored[0].score > scored[1].score

    def test_free_slot_preferred(self, scorer, track):
        """Results with free upload slots should score higher."""
        free = make_result(has_free_slot=True, filename="Nancy Sinatra - Bang Bang A.flac")
        busy = make_result(has_free_slot=False, filename="Nancy Sinatra - Bang Bang B.flac")
        scored = scorer.score_results([free, busy], track)
        free_score = next(r for r in scored if "A.flac" in r.filename).score
        busy_score = next(r for r in scored if "B.flac" in r.filename).score
        assert free_score > busy_score

    def test_cd_quality_preferred(self, scorer, track):
        """16-bit/44.1kHz should be preferred over hi-res for consistency."""
        cd = make_result(bit_depth=16, sample_rate=44100, filename="Nancy Sinatra - Bang CD.flac")
        hires = make_result(bit_depth=24, sample_rate=96000, filename="Nancy Sinatra - Bang HiRes.flac")
        scored = scorer.score_results([cd, hires], track)
        cd_score = next(r for r in scored if "CD.flac" in r.filename).score
        hires_score = next(r for r in scored if "HiRes.flac" in r.filename).score
        assert cd_score > hires_score

    def test_deduplication(self, scorer, track):
        """Duplicate basenames should be deduplicated (keep highest score)."""
        good = make_result(has_free_slot=True, upload_speed=5_000_000)
        bad = make_result(has_free_slot=False, upload_speed=100_000)
        bad.username = "slowuser"
        scored = scorer.score_results([good, bad], track)
        assert len(scored) == 1
        assert scored[0].username == "testuser"

    def test_no_duration_info_gets_neutral_score(self, scorer, track):
        """Results without duration info should get a moderate score, not excluded."""
        result = make_result(length=None)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1
        assert scored[0].score > 0
