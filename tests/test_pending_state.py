"""Pending searches and downloads survive a restart (SQLite write-through, prune, expired buttons)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import sqlite3
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

from music_downloader.bot.handlers import RESTART_EXPIRED, MusicBot, PendingDownload, PendingSearch
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.pending_repo import PendingRepository, WriteThroughDict
from music_downloader.pipeline import library
from music_downloader.search.slskd_client import SearchResult

CHAT = 67890
USER = 12345


def _make_config(td=None):
    td = td or tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_upload_limit_bytes = 50_000_000
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {USER}
    config.telegram_chat_delivery_users = set()
    config.auto_mode = False
    config.max_results = 5
    config.duration_tolerance_secs = 5
    config.exclude_keywords = ["live", "remix"]
    config.download_dir = os.path.join(td, "downloads")
    config.output_dir = os.path.join(td, "music")
    config.data_dir = os.path.join(td, "data")
    config.filename_template = "{artist} - {title}"
    config.search_timeout_secs = 30
    config.download_timeout_secs = 600
    config.download_cleanup_hours = 24
    os.makedirs(config.download_dir, exist_ok=True)
    return config


def _make_bot(config):
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        bot = MusicBot(config)
    bot.pipeline.forget_transfer = MagicMock()
    return bot


def _track():
    return TrackInfo(
        artist="Nancy Sinatra", title="Bang Bang", album="X", duration_ms=162_000, spotify_url="u", year="1966"
    )


def _result(idx=0):
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\track{idx}.flac",
        size=30_000_000,
        bit_depth=16,
        sample_rate=44100,
        length=162,
        has_free_slot=True,
    )


def _source(config, name="track0.flac"):
    path = os.path.join(config.download_dir, name)
    with open(path, "wb") as f:
        f.write(b"fLaC" + b"\0" * 64)
    return path


def _context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


def _tap(data, message_date=None):
    update = MagicMock()
    update.effective_chat.id = CHAT
    update.effective_user.id = USER
    query = update.callback_query
    query.data = data
    query.from_user.id = USER
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    query.message.date = message_date or datetime.datetime.now(datetime.UTC)
    return update


def _edited_text(query) -> str:
    """Every text the bot wrote into the tapped message (caption or text edits)."""
    texts = [c.kwargs.get("caption") or c.kwargs.get("text") for c in query.edit_message_caption.call_args_list]
    texts += [c.kwargs.get("text") or (c.args[0] if c.args else "") for c in query.edit_message_text.call_args_list]
    return "\n".join(t for t in texts if t)


class TestRepository:
    def test_download_round_trip_keeps_every_field(self, tmp_path):
        repo = PendingRepository(Database(str(tmp_path / "db.sqlite")))
        dl = PendingDownload(
            track=_track(),
            result=_result(3),
            chat_id=CHAT,
            source_path="/downloads/x.flac",
            status_message_id=11,
            approval_message_id=12,
            result_index=3,
            search_id="s1",
            user_id=USER,
            transfer_id="t-1",
            job_id=7,
            track_id=8,
            created_at=123.5,
        )
        repo.save_download("d1", dl)
        loaded = repo.load_downloads()["d1"]
        # generation is per process and deliberately not stored
        assert loaded == PendingDownload(**{**dl.__dict__, "generation": 0})

    def test_search_round_trip_keeps_every_field(self, tmp_path):
        repo = PendingRepository(Database(str(tmp_path / "db.sqlite")))
        search = PendingSearch(
            query="q",
            track=_track(),
            results=[_result(0), _result(1)],
            message_id=5,
            page=1,
            search_id="s9",
            profile="chat",
            created_at=99.0,
        )
        repo.save_search(CHAT, search)
        repo.save_search(1, PendingSearch(query="no track yet"))
        loaded = repo.load_searches()
        assert loaded[CHAT] == search
        assert loaded[1].track is None

    def test_old_database_gains_the_tables_in_place(self, tmp_path):
        path = str(tmp_path / "importer.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY, auto_mode INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO chat_settings (chat_id, auto_mode) VALUES (1, 1)")
        conn.commit()
        conn.close()
        db = Database(path)
        tables = {r[0] for r in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pending_downloads", "pending_searches"} <= tables
        assert db.connection.execute("SELECT auto_mode FROM chat_settings WHERE chat_id = 1").fetchone()[0] == 1

    def test_write_failure_is_logged_not_raised(self, caplog):
        def boom(*_):
            raise sqlite3.OperationalError("disk full")

        d = WriteThroughDict({}, boom, boom)
        d["k"] = 1
        assert d.pop("k") == 1
        assert "Could not persist pending state" in caplog.text


class TestRestart:
    async def test_save_button_works_on_a_second_bot(self):
        config = _make_config()
        bot1 = _make_bot(config)
        source = _source(config)
        bot1.downloads["d1"] = PendingDownload(
            track=_track(), result=_result(), chat_id=CHAT, source_path=source, user_id=USER, transfer_id="t1"
        )
        bot1.downloads["d1"].approval_message_id = 44
        bot1.downloads.save("d1")

        bot2 = _make_bot(config)  # same DATA_DIR = same database
        assert bot2.downloads["d1"].approval_message_id == 44
        bot2.pipeline.save = AsyncMock(return_value="/music/Nancy Sinatra - Bang Bang.flac")
        update = _tap("approve:d1")
        await bot2.handle_callback(update, _context())

        bot2.pipeline.save.assert_awaited_once_with(source, _track(), _result(), "t1")
        assert "Saved" in _edited_text(update.callback_query)
        assert "d1" not in bot2.downloads
        assert "d1" not in _make_bot(config).downloads, "the row must be gone from the database too"

    async def test_pick_button_works_on_a_second_bot(self):
        config = _make_config()
        bot1 = _make_bot(config)
        bot1.pending[CHAT] = PendingSearch(
            query="q", track=_track(), results=[_result(0), _result(1)], message_id=5, search_id="s1"
        )

        bot2 = _make_bot(config)
        bot2._launch_download = AsyncMock()
        await bot2.handle_callback(_tap("dl:s1:1"), _context())
        args = bot2._launch_download.await_args.args
        assert args[2:6] == (_track(), _result(1), 1, "s1")

    async def test_results_page_change_is_persisted(self):
        config = _make_config()
        bot1 = _make_bot(config)
        bot1.pending[CHAT] = PendingSearch(
            query="q", track=_track(), results=[_result(i) for i in range(12)], search_id="s1"
        )
        await bot1.handle_callback(_tap("dl_page:s1:1"), _context())
        assert _make_bot(config).pending[CHAT].page == 1

    async def test_retry_button_works_on_a_second_bot(self):
        config = _make_config()
        bot1 = _make_bot(config)
        bot1.downloads["d1"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT, user_id=USER)

        bot2 = _make_bot(config)
        bot2._do_download = AsyncMock()
        await bot2.handle_callback(_tap("retry:d1"), _context())
        await asyncio.sleep(0)
        bot2._do_download.assert_awaited_once()
        assert bot2._do_download.await_args.args[3] == _result()

    async def test_gone_entry_on_an_old_message_says_it_expired_with_the_restart(self):
        config = _make_config()
        bot = _make_bot(config)
        before_start = datetime.datetime.fromtimestamp(bot._started_at - 60, datetime.UTC)
        for data in ("approve:zz", "retry:zz", "next:zz", "dl:s1:0"):
            update = _tap(data, message_date=before_start)
            await bot.handle_callback(update, _context())
            assert RESTART_EXPIRED in _edited_text(update.callback_query), data

    async def test_gone_entry_on_a_new_message_keeps_the_old_wording(self):
        bot = _make_bot(_make_config())
        update = _tap("approve:zz", message_date=datetime.datetime.fromtimestamp(time.time() + 5, datetime.UTC))
        await bot.handle_callback(update, _context())
        assert _edited_text(update.callback_query) == "⏹ Cancelled"


class TestPrune:
    def test_startup_drops_expired_entries_with_their_files(self):
        config = _make_config()
        bot1 = _make_bot(config)
        old_file = _source(config, "old.flac")
        fresh_file = _source(config, "fresh.flac")
        old = time.time() - 25 * 3600
        bot1.downloads["old"] = PendingDownload(
            track=_track(), result=_result(), chat_id=CHAT, source_path=old_file, transfer_id="t-old", created_at=old
        )
        bot1.downloads["fresh"] = PendingDownload(
            track=_track(), result=_result(), chat_id=CHAT, source_path=fresh_file
        )
        bot1.pending[CHAT] = PendingSearch(query="q", created_at=old)
        bot1.pending[1] = PendingSearch(query="fresh")

        bot2 = _make_bot(config)
        assert set(bot2.downloads) == {"fresh"}
        assert set(bot2.pending) == {1}
        assert not os.path.exists(old_file)
        assert os.path.exists(fresh_file)

    def test_startup_drops_entries_whose_file_is_gone_and_import_entries(self):
        config = _make_config()
        bot1 = _make_bot(config)
        bot1.downloads["nofile"] = PendingDownload(
            track=_track(), result=_result(), chat_id=CHAT, source_path=os.path.join(config.download_dir, "gone.flac")
        )
        bot1.downloads["failed"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT)
        bot1.downloads["import"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT, job_id=1, track_id=2)
        assert set(_make_bot(config).downloads) == {"failed"}

    def test_hourly_sweep_prunes_through_the_hook(self):
        config = _make_config()
        bot = _make_bot(config)
        path = _source(config)
        bot.downloads["old"] = PendingDownload(
            track=_track(), result=_result(), chat_id=CHAT, source_path=path, created_at=time.time() - 25 * 3600
        )
        processor = MagicMock()
        processor.sweep_orphans = MagicMock(return_value=(0, 0))

        async def run():
            with (
                patch("music_downloader.pipeline.library.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
                contextlib.suppress(asyncio.CancelledError),
            ):
                await library.orphan_sweep_loop(processor, 24, lambda: set(), bot._prune_pending)

        asyncio.run(run())
        assert "old" not in bot.downloads
        assert not os.path.exists(path)
        processor.sweep_orphans.assert_called_once()
