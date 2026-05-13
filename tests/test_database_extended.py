"""Extended tests for database and scorer uncovered lines."""

import os
import tempfile

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult

# ============================================================
# Database tests
# ============================================================


class TestDatabaseNormalCreation:
    """Test normal database creation and schema."""

    def test_database_normal_creation(self, tmp_path):
        """Create DB in a temp dir, verify tables exist."""
        db_path = str(tmp_path / "test.db")
        db = Database(db_path)

        cursor = db.connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        assert "download_history" in tables
        assert "import_jobs" in tables
        assert "import_tracks" in tables
        db.close()

    def test_database_schema_version(self, tmp_path):
        """Verify user_version is set to 1."""
        db_path = str(tmp_path / "version.db")
        db = Database(db_path)

        cursor = db.connection.execute("PRAGMA user_version")
        version = cursor.fetchone()[0]
        assert version == 1
        db.close()


class TestDatabaseCorruptRecovery:
    """Test corrupt database recovery (lines 76-85)."""

    def test_database_corrupt_file_recreated(self, tmp_path):
        """Write garbage to a file, then create Database with that path - should recreate."""
        db_path = str(tmp_path / "corrupt.db")

        # Write garbage that looks like a sqlite header but is corrupt
        with open(db_path, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\xff" * 200)

        db = Database(db_path)

        # Should have recreated cleanly with valid schema
        cursor = db.connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        assert "download_history" in tables
        assert "import_jobs" in tables
        assert "import_tracks" in tables
        db.close()

    def test_database_corrupt_nonexistent_parent(self):
        """Corrupt recovery with nested parent dirs that don't exist yet."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "deep", "nested", "corrupt.db")

            # Create the path so we can write garbage
            os.makedirs(os.path.dirname(db_path))
            with open(db_path, "wb") as f:
                f.write(b"SQLite format 3\x00" + b"\xde\xad" * 100)

            db = Database(db_path)
            cursor = db.connection.execute("PRAGMA user_version")
            assert cursor.fetchone()[0] == 1
            db.close()


class TestDatabaseClose:
    """Test close() method (lines 97-98)."""

    def test_database_close(self, tmp_path):
        """Verify close() works without error."""
        db_path = str(tmp_path / "close.db")
        db = Database(db_path)
        db.close()

    def test_database_double_close(self, tmp_path):
        """Double close should not raise."""
        db_path = str(tmp_path / "dblclose.db")
        db = Database(db_path)
        db.close()
        db.close()  # Should not raise due to contextlib.suppress


# ============================================================
# Scorer tests — uncovered branches
# ============================================================


def _make_track(duration_ms: int = 162000) -> TrackInfo:
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="Album",
        duration_ms=duration_ms,
        spotify_url="",
        year="1966",
    )


def _make_result(
    filename: str = "Nancy Sinatra - Bang Bang.flac",
    length: int | None = 162,
    bit_depth: int | None = None,
    sample_rate: int | None = None,
    has_free_slot: bool = False,
    upload_speed: int = 0,
    queue_length: int = 0,
) -> SearchResult:
    return SearchResult(
        username="user1",
        filename=f"\\Music\\{filename}",
        size=30_000_000,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        length=length,
        has_free_slot=has_free_slot,
        upload_speed=upload_speed,
        queue_length=queue_length,
    )


class TestScorerDurationBranches:
    """Cover duration scoring branches (lines 111, 113, 115)."""

    def test_duration_diff_6_to_10(self):
        """Duration diff between tolerance (5) and 10s => line 111 branch."""
        scorer = ResultScorer(duration_tolerance_secs=5)
        track = _make_track(duration_ms=162000)  # 162s
        # diff = 8s (between 5 and 10)
        result = _make_result(length=170)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1
        assert scored[0].score > 0

    def test_duration_diff_11_to_30(self):
        """Duration diff between 10 and 30s => line 113 branch."""
        scorer = ResultScorer(duration_tolerance_secs=5)
        track = _make_track(duration_ms=162000)  # 162s
        # diff = 20s (between 10 and 30)
        result = _make_result(length=182)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1
        assert scored[0].score > 0

    def test_duration_diff_beyond_30_with_max_override(self):
        """Duration diff > 30s but within max_duration_diff => line 115 branch (pass)."""
        scorer = ResultScorer(duration_tolerance_secs=5)
        track = _make_track(duration_ms=162000)  # 162s
        # diff = 40s (> 30 but within max_duration_diff=60)
        result = _make_result(length=202)
        scored = scorer.score_results([result], track, max_duration_diff=60)
        assert len(scored) == 1
        # Gets 0 duration points but is not excluded
        assert scored[0].score >= 0


class TestScorerQualityBranches:
    """Cover audio quality edge cases (lines 130, 136, 140)."""

    def test_bit_depth_below_16(self):
        """bit_depth < 16 => line 130 (score += 5.0)."""
        scorer = ResultScorer()
        track = _make_track()
        result = _make_result(bit_depth=8, sample_rate=44100)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1

    def test_sample_rate_48000(self):
        """sample_rate == 48000 => line 136 (score += 7.0)."""
        scorer = ResultScorer()
        track = _make_track()
        result = _make_result(bit_depth=16, sample_rate=48000)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1

    def test_sample_rate_below_44100(self):
        """sample_rate not matching any known tier => line 140 (score += 3.0)."""
        scorer = ResultScorer()
        track = _make_track()
        result = _make_result(bit_depth=16, sample_rate=22050)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1


class TestScorerQueueBranch:
    """Cover queue_length branch (lines 153-154)."""

    def test_queue_length_between_1_and_4(self):
        """queue_length 1-4 => line 153-154 (score += 2.0)."""
        scorer = ResultScorer()
        track = _make_track()
        result = _make_result(queue_length=3)
        scored = scorer.score_results([result], track)
        assert len(scored) == 1
        # Compare with queue_length=0 to confirm lower score
        result_empty_q = _make_result(queue_length=0, filename="Nancy Sinatra - Bang Bang v2.flac")
        scored_both = scorer.score_results([result, result_empty_q], track)
        empty_q_score = next(r for r in scored_both if "v2" in r.filename).score
        short_q_score = next(r for r in scored_both if "v2" not in r.filename).score
        assert empty_q_score > short_q_score
