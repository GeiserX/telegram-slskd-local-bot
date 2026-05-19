from __future__ import annotations

import pytest

from src.music_downloader.persistence.database import Database
from src.music_downloader.persistence.import_repo import (
    ImportRepository,
    JobStatus,
    TrackStatus,
)


@pytest.fixture()
def repo(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield ImportRepository(db)
    db.close()


def _make_tracks(count: int, start_position: int = 1) -> list[dict]:
    return [
        {
            "position": start_position + i,
            "artist": f"Artist{i}",
            "title": f"Title{i}",
            "album": f"Album{i}",
            "duration_ms": 200_000 + i,
            "spotify_url": f"https://open.spotify.com/track/{i}",
            "year": "2024",
        }
        for i in range(count)
    ]


class TestCreateJob:
    def test_returns_job_id(self, repo):
        job_id = repo.create_job(
            chat_id=100, spotify_url="https://spotify/playlist/1", name="My Playlist", total_tracks=10
        )
        assert job_id == 1

    def test_creates_with_correct_fields(self, repo):
        job_id = repo.create_job(chat_id=42, spotify_url="https://spotify/playlist/abc", name="Test PL", total_tracks=5)
        job = repo.get_job_for_chat(job_id, chat_id=42)
        assert job is not None
        assert job.chat_id == 42
        assert job.spotify_url == "https://spotify/playlist/abc"
        assert job.name == "Test PL"
        assert job.total_tracks == 5
        assert job.completed_tracks == 0
        assert job.failed_tracks == 0
        assert job.skipped_tracks == 0
        assert job.status == "pending"


class TestAddTracks:
    def test_adds_tracks_and_verifies_count(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=3)
        tracks = _make_tracks(3)
        repo.add_tracks(job_id, tracks)
        all_tracks = repo.get_tracks_by_job(job_id)
        assert len(all_tracks) == 3

    def test_deduplication_by_position(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=3)
        tracks = _make_tracks(3)
        repo.add_tracks(job_id, tracks)
        # Insert same tracks again (same job_id + position = UNIQUE constraint)
        repo.add_tracks(job_id, tracks)
        all_tracks = repo.get_tracks_by_job(job_id)
        assert len(all_tracks) == 3


class TestGetJobForChat:
    def test_returns_job_for_correct_chat(self, repo):
        job_id = repo.create_job(chat_id=100, spotify_url="url", name="PL", total_tracks=5)
        job = repo.get_job_for_chat(job_id, chat_id=100)
        assert job is not None
        assert job.id == job_id

    def test_returns_none_for_wrong_chat(self, repo):
        job_id = repo.create_job(chat_id=100, spotify_url="url", name="PL", total_tracks=5)
        job = repo.get_job_for_chat(job_id, chat_id=999)
        assert job is None


class TestGetNextPendingTrack:
    def test_returns_tracks_in_position_order(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=3)
        tracks = _make_tracks(3, start_position=1)
        repo.add_tracks(job_id, tracks)

        first = repo.get_next_pending_track(job_id)
        assert first is not None
        assert first.position == 1

    def test_returns_none_when_no_pending(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=1)
        tracks = _make_tracks(1)
        repo.add_tracks(job_id, tracks)

        track = repo.get_next_pending_track(job_id)
        assert track is not None
        repo.complete_track(job_id, track.id, TrackStatus.completed)

        next_track = repo.get_next_pending_track(job_id)
        assert next_track is None


class TestCompleteTrack:
    def test_updates_track_status(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=2)
        repo.add_tracks(job_id, _make_tracks(2))

        track = repo.get_next_pending_track(job_id)
        assert track is not None
        repo.complete_track(job_id, track.id, TrackStatus.completed)

        all_tracks = repo.get_tracks_by_job(job_id)
        completed_track = next(t for t in all_tracks if t.id == track.id)
        assert completed_track.status == "completed"

    def test_increments_completed_counter(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=3)
        repo.add_tracks(job_id, _make_tracks(3))

        track = repo.get_next_pending_track(job_id)
        repo.complete_track(job_id, track.id, TrackStatus.completed)

        progress = repo.get_job_progress(job_id)
        assert progress == (1, 0, 0, 3)  # completed, failed, skipped, total

    def test_increments_failed_counter(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=2)
        repo.add_tracks(job_id, _make_tracks(2))

        track = repo.get_next_pending_track(job_id)
        repo.complete_track(job_id, track.id, TrackStatus.failed, error_message="timeout")

        progress = repo.get_job_progress(job_id)
        assert progress == (0, 1, 0, 2)

        all_tracks = repo.get_tracks_by_job(job_id)
        failed_track = next(t for t in all_tracks if t.id == track.id)
        assert failed_track.error_message == "timeout"

    def test_increments_skipped_counter(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=2)
        repo.add_tracks(job_id, _make_tracks(2))

        track = repo.get_next_pending_track(job_id)
        repo.complete_track(job_id, track.id, TrackStatus.skipped)

        progress = repo.get_job_progress(job_id)
        assert progress == (0, 0, 1, 2)


class TestUpdateJobStatus:
    def test_transitions_status(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        repo.update_job_status(job_id, JobStatus.active)

        job = repo.get_job_for_chat(job_id, chat_id=1)
        assert job is not None
        assert job.status == "active"

    def test_transition_to_completed(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        repo.update_job_status(job_id, JobStatus.completed)

        # Completed jobs don't show up in get_active_job
        active = repo.get_active_job(chat_id=1)
        assert active is None


class TestGetActiveJob:
    def test_returns_pending_job(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        active = repo.get_active_job(chat_id=1)
        assert active is not None
        assert active.id == job_id

    def test_returns_active_job(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        repo.update_job_status(job_id, JobStatus.active)
        active = repo.get_active_job(chat_id=1)
        assert active is not None
        assert active.id == job_id

    def test_does_not_return_completed_job(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        repo.update_job_status(job_id, JobStatus.completed)
        active = repo.get_active_job(chat_id=1)
        assert active is None

    def test_does_not_return_cancelled_job(self, repo):
        job_id = repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        repo.update_job_status(job_id, JobStatus.cancelled)
        active = repo.get_active_job(chat_id=1)
        assert active is None

    def test_scoped_to_chat_id(self, repo):
        repo.create_job(chat_id=1, spotify_url="url", name="PL", total_tracks=5)
        active = repo.get_active_job(chat_id=999)
        assert active is None
