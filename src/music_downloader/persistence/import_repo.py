from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .database import Database


class JobStatus(StrEnum):
    pending = "pending"
    active = "active"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"


class TrackStatus(StrEnum):
    pending = "pending"
    searching = "searching"
    awaiting_approval = "awaiting_approval"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


@dataclass
class ImportJob:
    id: int
    chat_id: int
    spotify_url: str
    name: str
    total_tracks: int
    completed_tracks: int
    failed_tracks: int
    skipped_tracks: int
    status: str
    created_at: str
    updated_at: str


@dataclass
class ImportTrack:
    id: int
    job_id: int
    position: int
    artist: str
    title: str
    album: str
    duration_ms: int
    spotify_url: str
    year: str
    status: str
    error_message: str
    created_at: str
    updated_at: str


class ImportRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db._get_connection()

    def create_job(self, chat_id: int, spotify_url: str, name: str, total_tracks: int) -> int:
        cursor = self._conn.execute(
            """INSERT INTO import_jobs (chat_id, spotify_url, name, total_tracks)
            VALUES (?, ?, ?, ?)""",
            (chat_id, spotify_url, name, total_tracks),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_job(self, job_id: int) -> ImportJob | None:
        cursor = self._conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        return ImportJob(**dict(row)) if row else None

    def get_active_job(self, chat_id: int) -> ImportJob | None:
        cursor = self._conn.execute(
            "SELECT * FROM import_jobs WHERE chat_id = ? AND status IN ('pending', 'active') LIMIT 1",
            (chat_id,),
        )
        row = cursor.fetchone()
        return ImportJob(**dict(row)) if row else None

    def update_job_status(self, job_id: int, status: JobStatus) -> None:
        self._conn.execute(
            "UPDATE import_jobs SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status.value, job_id),
        )
        self._conn.commit()

    def increment_completed(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE import_jobs SET completed_tracks = completed_tracks + 1, updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

    def increment_failed(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE import_jobs SET failed_tracks = failed_tracks + 1, updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

    def increment_skipped(self, job_id: int) -> None:
        self._conn.execute(
            "UPDATE import_jobs SET skipped_tracks = skipped_tracks + 1, updated_at = datetime('now') WHERE id = ?",
            (job_id,),
        )
        self._conn.commit()

    def add_tracks(self, job_id: int, tracks: list[dict]) -> None:
        self._conn.executemany(
            """INSERT INTO import_tracks (job_id, position, artist, title, album, duration_ms, spotify_url, year)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    job_id,
                    t["position"],
                    t["artist"],
                    t["title"],
                    t.get("album", ""),
                    t.get("duration_ms", 0),
                    t.get("spotify_url", ""),
                    t.get("year", ""),
                )
                for t in tracks
            ],
        )
        self._conn.commit()

    def get_next_pending_track(self, job_id: int) -> ImportTrack | None:
        cursor = self._conn.execute(
            "SELECT * FROM import_tracks WHERE job_id = ? AND status = 'pending' ORDER BY position LIMIT 1",
            (job_id,),
        )
        row = cursor.fetchone()
        return ImportTrack(**dict(row)) if row else None

    def update_track_status(self, track_id: int, status: TrackStatus, error_message: str = "") -> None:
        self._conn.execute(
            "UPDATE import_tracks SET status = ?, error_message = ?, updated_at = datetime('now') WHERE id = ?",
            (status.value, error_message, track_id),
        )
        self._conn.commit()

    def get_tracks_by_job(self, job_id: int) -> list[ImportTrack]:
        cursor = self._conn.execute(
            "SELECT * FROM import_tracks WHERE job_id = ? ORDER BY position",
            (job_id,),
        )
        return [ImportTrack(**dict(row)) for row in cursor.fetchall()]

    def get_job_progress(self, job_id: int) -> tuple[int, int, int, int]:
        cursor = self._conn.execute(
            "SELECT completed_tracks, failed_tracks, skipped_tracks, total_tracks FROM import_jobs WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if not row:
            return (0, 0, 0, 0)
        return (row["completed_tracks"], row["failed_tracks"], row["skipped_tracks"], row["total_tracks"])
