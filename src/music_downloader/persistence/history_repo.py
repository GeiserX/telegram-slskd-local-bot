from __future__ import annotations

from dataclasses import dataclass

from .database import Database


@dataclass
class HistoryRecord:
    id: int
    artist: str
    title: str
    album: str
    filename: str
    source_user: str
    remote_path: str
    status: str
    duration_secs: int
    file_size: int
    created_at: str


class HistoryRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    def add(
        self,
        artist: str,
        title: str,
        filename: str,
        source_user: str,
        status: str,
        album: str = "",
        remote_path: str = "",
        duration_secs: int = 0,
        file_size: int = 0,
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO download_history (artist, title, album, filename, source_user, remote_path, status, duration_secs, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artist, title, album, filename, source_user, remote_path, status, duration_secs, file_size),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_recent(self, limit: int = 50) -> list[HistoryRecord]:
        cursor = self._conn.execute(
            "SELECT * FROM download_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [HistoryRecord(**dict(row)) for row in cursor.fetchall()]

    def count(self) -> int:
        cursor = self._conn.execute("SELECT COUNT(*) FROM download_history")
        return cursor.fetchone()[0]
