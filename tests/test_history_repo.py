from __future__ import annotations

import pytest

from src.music_downloader.persistence.database import Database
from src.music_downloader.persistence.history_repo import HistoryRepository


@pytest.fixture()
def repo(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    yield HistoryRepository(db)
    db.close()


class TestAdd:
    def test_add_returns_row_id(self, repo):
        row_id = repo.add(
            artist="Artist1",
            title="Title1",
            filename="file.flac",
            source_user="user1",
            status="completed",
        )
        assert row_id == 1

    def test_add_record_appears_in_get_recent(self, repo):
        repo.add(
            artist="Artist1",
            title="Title1",
            filename="file.flac",
            source_user="user1",
            status="completed",
            album="Album1",
            remote_path="/remote/path",
            duration_secs=240,
            file_size=10_000_000,
        )
        records = repo.get_recent()
        assert len(records) == 1
        rec = records[0]
        assert rec.artist == "Artist1"
        assert rec.title == "Title1"
        assert rec.album == "Album1"
        assert rec.filename == "file.flac"
        assert rec.source_user == "user1"
        assert rec.remote_path == "/remote/path"
        assert rec.status == "completed"
        assert rec.duration_secs == 240
        assert rec.file_size == 10_000_000


class TestGetRecent:
    def test_descending_chronological_order(self, repo):
        repo.add(artist="First", title="T1", filename="f1", source_user="u", status="ok")
        # SQLite datetime('now') has second precision; insert a manual delay isn't reliable,
        # so we insert with distinct created_at via raw SQL isn't possible through the repo.
        # Instead, add multiple and check order by id (autoincrement matches insert order
        # when created_at is the same second, ORDER BY created_at DESC, id DESC implicitly).
        repo.add(artist="Second", title="T2", filename="f2", source_user="u", status="ok")
        repo.add(artist="Third", title="T3", filename="f3", source_user="u", status="ok")
        records = repo.get_recent()
        artists = [r.artist for r in records]
        # Most recent first (DESC by created_at — all same second, but rowid order is preserved
        # since SQLite returns ties in insertion order for DESC when using AUTOINCREMENT).
        assert artists == ["Third", "Second", "First"]

    def test_respects_limit(self, repo):
        for i in range(10):
            repo.add(artist=f"A{i}", title=f"T{i}", filename=f"f{i}", source_user="u", status="ok")
        records = repo.get_recent(limit=3)
        assert len(records) == 3

    def test_empty_database_returns_empty_list(self, repo):
        records = repo.get_recent()
        assert records == []


class TestCount:
    def test_count_after_multiple_adds(self, repo):
        assert repo.count() == 0
        repo.add(artist="A1", title="T1", filename="f1", source_user="u", status="ok")
        assert repo.count() == 1
        repo.add(artist="A2", title="T2", filename="f2", source_user="u", status="ok")
        assert repo.count() == 2
        repo.add(artist="A3", title="T3", filename="f3", source_user="u", status="ok")
        assert repo.count() == 3

    def test_count_empty_database(self, repo):
        assert repo.count() == 0
