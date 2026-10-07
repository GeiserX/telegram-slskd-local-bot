"""Album delivery in Telegram: the button after a save or send, the listing, Get all, progress, /cancel, a restart."""

import asyncio
import dataclasses
import datetime
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from music_downloader.bot.handlers import ALBUM_EXPIRED, RESTART_EXPIRED, MusicBot
from music_downloader.persistence.album_repo import (
    ALBUM_CANCELLED,
    ALBUM_INTERRUPTED,
    ALBUM_RUNNING,
    AlbumJob,
    FileOutcome,
)
from music_downloader.persistence.pending_repo import PendingDownload
from music_downloader.pipeline import album as _album
from music_downloader.search.slskd_client import SearchResult
from tests.test_album import FOLDER, PEER, TRACK, _land, _raw_folder, _slskd, _waiter
from tests.test_chat_delivery import CHAT, _make_config, _status_msg

DL = "abc12345"


@pytest.fixture(autouse=True)
def _no_spotify_cover():
    with patch.object(_album, "fetch_spotify_album_artwork", return_value=None):
        yield


def _bot(tmp_path, chat=False):
    config = _make_config(str(tmp_path), chat_users={CHAT} if chat else None)
    config.album_timeout_secs = 7200
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        bot = MusicBot(config)
    bot.slskd = _slskd()
    return bot


def _chosen():
    return SearchResult(PEER, f"{FOLDER}\\06 - Echoes.mp3", 22_000_000, bit_rate=320, length=1411)


def _offer(bot, dl_id=DL):
    dl = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, user_id=CHAT)
    bot.downloads[dl_id] = dl
    return bot._offer_album(dl_id, dl)


def _context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=_status_msg())
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


def _query(data, date=None):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.from_user.id = CHAT
    query.message.date = date or datetime.datetime.now(datetime.UTC)
    # A text message: the caption edit fails and _edit_approval_message falls back to the text.
    query.edit_message_caption = AsyncMock(side_effect=RuntimeError("no caption"))
    query.edit_message_text = AsyncMock()
    return query


def _update(query):
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = CHAT
    update.effective_user.id = CHAT
    update.message.reply_text = AsyncMock()
    return update


async def _tap(bot, context, data, date=None):
    query = _query(data, date)
    await bot.handle_callback(_update(query), context)
    return query


def _texts(query):
    return [c.kwargs["text"] for c in query.edit_message_text.call_args_list]


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _status_texts(msg):
    return [c.args[0] for c in msg.edit_text.call_args_list]


async def _finish(bot):
    run = bot._album_runs[CHAT]
    await asyncio.wait_for(run.task, 10)


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------


class TestOffer:
    async def test_library_save_offers_the_album_and_keeps_the_row(self, tmp_path):
        bot = _bot(tmp_path)
        bot.pipeline.embed_artwork = AsyncMock()
        source = tmp_path / "downloads" / "06 - Echoes.mp3"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"ID3" + b"\0" * 64)
        bot.downloads[DL] = PendingDownload(
            track=TRACK, result=_chosen(), chat_id=CHAT, source_path=str(source), user_id=CHAT, transfer_id="t1"
        )
        other = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, source_path=None)
        bot.downloads["other123"] = other
        query = await _tap(bot, _context(), f"approve:{DL}")

        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        assert _buttons(markup) == [("\U0001f4bf Whole album from this source", f"alb:{DL}")]
        assert "Saved" in _texts(query)[-1]
        row = bot.downloads[DL]
        assert row.delivered and row.source_path is None and row.transfer_id == ""
        # The other pending download of the chat is dismissed as before; the offer stays.
        assert "other123" not in bot.downloads
        # And the row is in SQLite: a restart keeps the button.
        assert bot.pending_repo.load_downloads()[DL].delivered is True

    async def test_chat_send_offers_the_album(self, tmp_path):
        bot = _bot(tmp_path, chat=True)
        bot._send_to_chat = AsyncMock(return_value=("sent", "Pink Floyd - Echoes.mp3"))
        bot.pipeline.discard = AsyncMock()
        dl = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, source_path="/x.mp3", user_id=CHAT)
        bot.downloads[DL] = dl
        status = _status_msg()
        assert await bot._deliver_download(_context(), CHAT, DL, dl, status, "q", "#1") is True
        assert "Sent" in status.edit_text.call_args.args[0]
        assert _buttons(status.edit_text.call_args.kwargs["reply_markup"]) == [
            ("\U0001f4bf Whole album from this source", f"alb:{DL}")
        ]
        assert bot.downloads[DL].delivered

    async def test_auto_save_offers_the_album(self, tmp_path):
        bot = _bot(tmp_path)
        bot.pipeline.save = AsyncMock(return_value=str(tmp_path / "music" / "Pink Floyd - Echoes.mp3"))
        dl = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, source_path="/x.mp3")
        bot.downloads[DL] = dl
        status = _status_msg()
        await bot._auto_save(CHAT, DL, dl, status, "q", "#1")
        assert f"alb:{DL}" in str(_buttons(status.edit_text.call_args.kwargs["reply_markup"]))
        assert bot.downloads[DL].delivered

    async def test_a_new_search_and_cancel_leave_the_offer(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        bot.downloads["live1234"] = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT)
        bot._cancel_chat_operations(CHAT)
        assert list(bot.downloads) == [DL]

    def test_import_tracks_and_bare_names_get_no_offer(self, tmp_path):
        bot = _bot(tmp_path)
        dl = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, job_id=3)
        bot.downloads[DL] = dl
        assert bot._offer_album(DL, dl) is None and DL not in bot.downloads
        bare = PendingDownload(track=TRACK, result=SearchResult(PEER, "Echoes.mp3", 1), chat_id=CHAT)
        bot.downloads[DL] = bare
        assert bot._offer_album(DL, bare) is None and DL not in bot.downloads


class TestExpired:
    @pytest.mark.parametrize("data", [f"alb:{DL}", f"albgo:{DL}", f"albno:{DL}"])
    async def test_a_button_without_its_row_expired(self, tmp_path, data):
        bot = _bot(tmp_path)
        query = await _tap(bot, _context(), data)
        assert _texts(query) == [ALBUM_EXPIRED]
        bot.slskd.browse_directory.assert_not_called()

    async def test_a_button_from_before_the_restart_says_so(self, tmp_path):
        bot = _bot(tmp_path)
        old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        query = await _tap(bot, _context(), f"alb:{DL}", date=old)
        assert _texts(query) == [RESTART_EXPIRED]

    async def test_a_track_still_waiting_on_save_is_no_album_offer(self, tmp_path):
        bot = _bot(tmp_path)
        bot.downloads[DL] = PendingDownload(track=TRACK, result=_chosen(), chat_id=CHAT, source_path="/x.flac")
        query = await _tap(bot, _context(), f"alb:{DL}")
        assert _texts(query) == [ALBUM_EXPIRED]


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListing:
    async def test_tap_lists_the_folder_and_asks(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        query = await _tap(bot, _context(), f"alb:{DL}")
        bot.slskd.browse_directory.assert_called_once_with(PEER, FOLDER)
        listing, text = _texts(query)
        assert "Listing the folder" in listing
        assert "<b>1971 - Meddle</b> on <code>vinylhoarder</code>: 3 audio files, 92 MB, FLAC, MP3" in text
        assert "<code>01 - One of These Days.flac</code>" in text and "cover.jpg" not in text
        assert _buttons(query.edit_message_text.call_args.kwargs["reply_markup"]) == [
            ("⬇️ Get all 3", f"albgo:{DL}"),
            ("Cancel", f"albno:{DL}"),
        ]

    def test_long_folders_show_six_names(self, tmp_path):
        bot = _bot(tmp_path)
        files = [_album.FolderFile(f"{FOLDER}\\{n:02d} - T.flac", 1_000_000, "flac") for n in range(1, 10)]
        text = bot._album_listing_text(_album.FolderListing(PEER, FOLDER, files))
        assert text.count("<code>0") == 6 and "… and 3 more" in text

    @pytest.mark.parametrize(
        ("error", "says"),
        [(TimeoutError(), "did not answer"), (requests.exceptions.ConnectionError("offline"), "Could not list")],
    )
    async def test_a_silent_peer_offers_retry(self, tmp_path, error, says):
        bot = _bot(tmp_path)
        bot.slskd.browse_directory.side_effect = error
        _offer(bot)
        query = await _tap(bot, _context(), f"alb:{DL}")
        assert says in _texts(query)[-1]
        assert _buttons(query.edit_message_text.call_args.kwargs["reply_markup"]) == [
            ("\U0001f504 Retry", f"alb:{DL}"),
            ("Cancel", f"albno:{DL}"),
        ]
        assert bot.downloads[DL].delivered  # Retry still works

    async def test_cancel_goes_back_to_the_offer(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        context = _context()
        await _tap(bot, context, f"alb:{DL}")
        query = await _tap(bot, context, f"albno:{DL}")
        assert _buttons(query.edit_message_text.call_args.kwargs["reply_markup"]) == [
            ("\U0001f4bf Whole album from this source", f"alb:{DL}")
        ]
        assert DL not in bot._album_listings and CHAT not in bot._album_runs
        bot.slskd.enqueue_files.assert_not_called()


# ---------------------------------------------------------------------------
# Get all
# ---------------------------------------------------------------------------


class TestGetAll:
    async def test_library_album_saves_skips_the_duplicate_and_lists_the_failure(self, tmp_path):
        bot = _bot(tmp_path)
        music = tmp_path / "music"
        music.mkdir(exist_ok=True)
        (music / "Pink Floyd - Echoes.mp3").write_bytes(b"old")
        bot.pipeline.library_index.add(str(music / "Pink Floyd - Echoes.mp3"))
        bot.slskd.wait_for_download = _waiter(tmp_path, {"02 - Pillow of Winds.flac": "Completed, Errored"})
        _offer(bot)
        context = _context()
        status = context.bot.send_message.return_value
        await _tap(bot, context, f"alb:{DL}")
        query = await _tap(bot, context, f"albgo:{DL}")
        assert "Getting 3 files" in _texts(query)[-1]
        assert DL not in bot.downloads  # the offer is spent
        await _finish(bot)

        assert sorted(os.listdir(music)) == ["Pink Floyd - Echoes.mp3", "Pink Floyd - One of These Days.flac"]
        assert (music / "Pink Floyd - Echoes.mp3").read_bytes() == b"old"
        # The duplicate's download is deleted, not left behind.
        assert not os.path.exists(tmp_path / "downloads" / "1971 - Meddle" / "06 - Echoes.mp3")
        edits = _status_texts(status)
        assert any("⬇️ 1/3 <code>01 - One of These Days.flac</code> · 40%" in e for e in edits)
        assert len(edits) == len(set(edits))  # no edit repeats the one before
        summary = edits[-1]
        assert "✅ Saved 1 of 3 to the library" in summary and str(music) not in summary
        assert "⏭ 1 already in the library, skipped" in summary
        assert "❌ Failed (1):\n• <code>02 - Pillow of Winds.flac</code>: Completed, Errored" in summary
        assert CHAT not in bot._album_runs
        assert {(r.title, r.status, r.note) for r in bot.history_repo.get_recent(10)} == {
            ("One of These Days", "success", "album")
        }

    async def test_chat_album_sends_each_file_in_the_chat_format(self, tmp_path):
        bot = _bot(tmp_path, chat=True)
        bot._set_send_format(CHAT, "mp3")
        bot.pipeline.embed_artwork = AsyncMock()
        transcoded = []

        async def transcode(path, fmt, track, art):
            out = tmp_path / f"out{len(transcoded)}.mp3"
            out.write_bytes(b"mp3")
            transcoded.append((os.path.basename(path), fmt, track.title, art))
            return str(out)

        bot.pipeline.transcode = AsyncMock(side_effect=transcode)
        bot.slskd.wait_for_download = _waiter(tmp_path, {"02 - Pillow of Winds.flac": "Completed, Errored"})
        context = _context()
        sends = []

        async def send_audio(**kwargs):
            sends.append((kwargs["filename"], kwargs["caption"]))
            if kwargs["filename"].endswith("Echoes.mp3"):
                raise RuntimeError("Request Entity Too Large")

        context.bot.send_audio = AsyncMock(side_effect=send_audio)
        status = context.bot.send_message.return_value
        _offer(bot)
        await _tap(bot, context, f"alb:{DL}")
        await _tap(bot, context, f"albgo:{DL}")
        await _finish(bot)

        assert [s[0] for s in sends] == ["Pink Floyd - One of These Days.mp3", "Pink Floyd - Echoes.mp3"]
        assert sends[0][1].startswith("01/03 Pink Floyd - One of These Days\n\U0001f3a7 Sent as MP3 320")
        assert sends[1][1] == "03/03 Pink Floyd - Echoes"  # already MP3: sent as it is
        assert len(transcoded) == 1 and transcoded[0][:3] == ("01 - One of These Days.flac", "mp3", "One of These Days")
        assert isinstance(transcoded[0][3], _album.AlbumArt)
        # The sent file is deleted; the one Telegram refused is left to the orphan sweep.
        downloads = tmp_path / "downloads" / "1971 - Meddle"
        assert sorted(os.listdir(downloads)) == ["06 - Echoes.mp3"]
        assert not (tmp_path / "music").exists() or os.listdir(tmp_path / "music") == []
        summary = _status_texts(status)[-1]
        assert "✅ Sent 1 of 3 to this chat" in summary and "already in the library" not in summary
        assert "• <code>06 - Echoes.mp3</code>: Telegram refused it" in summary
        assert {(r.title, r.status, r.note) for r in bot.history_repo.get_recent(10)} == {
            ("One of These Days", "delivered", "album"),
            ("Echoes", "send_failed", "album"),
        }

    async def test_timed_out_files_are_cancelled_in_slskd(self, tmp_path):
        bot = _bot(tmp_path)
        bot.slskd.wait_for_download = _waiter(tmp_path, {"02 - Pillow of Winds.flac": None})
        _offer(bot)
        context = _context()
        status = context.bot.send_message.return_value
        await _tap(bot, context, f"alb:{DL}")
        await _tap(bot, context, f"albgo:{DL}")
        await _finish(bot)
        summary = _status_texts(status)[-1]
        assert "• <code>02 - Pillow of Winds.flac</code>: Timeout" in summary
        assert (
            "⏳ 1 stopped moving or ran past the album's time limit: their transfers were cancelled in slskd" in summary
        )
        bot.slskd.cancel_downloads.assert_called_once_with(PEER, f"{FOLDER}\\02 - Pillow of Winds.flac")

    async def test_the_album_runs_outside_ptbs_tasks_so_a_stop_never_waits_on_it(self, tmp_path):
        bot = _bot(tmp_path)
        bot.slskd.wait_for_download = _waiter(tmp_path, {})
        _offer(bot)
        context = _context()
        await _tap(bot, context, f"alb:{DL}")
        await _tap(bot, context, f"albgo:{DL}")
        await _finish(bot)
        # Application.stop() awaits every application.create_task task.
        context.application.create_task.assert_not_called()

    async def test_a_failed_start_frees_the_chats_album_slot(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        context = _context()
        await _tap(bot, context, f"alb:{DL}")
        bot._run_album = MagicMock(side_effect=RuntimeError("no loop"))
        with pytest.raises(RuntimeError):
            await bot._handle_album_confirm(_update(_query(f"albgo:{DL}")), context, CHAT, f"albgo:{DL}")
        assert CHAT not in bot._album_runs

    def test_an_expired_offer_drops_its_listing(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        bot._album_listings[DL] = _album.FolderListing(PEER, FOLDER)
        bot.downloads[DL] = dataclasses.replace(bot.downloads[DL], created_at=0.0)
        bot._prune_pending()
        assert DL not in bot.downloads and DL not in bot._album_listings

    async def test_a_second_album_waits_for_the_first(self, tmp_path):
        bot = _bot(tmp_path)
        _offer(bot)
        context = _context()
        bot._album_runs[CHAT] = MagicMock()
        await _tap(bot, context, f"alb:{DL}")
        assert "already running" in context.bot.send_message.call_args.kwargs["text"]
        bot.slskd.browse_directory.assert_not_called()


class TestCancel:
    async def test_cancel_mid_album_keeps_what_landed_and_leaves_the_transfers(self, tmp_path):
        bot = _bot(tmp_path)
        reached = asyncio.Event()
        landed_first = _waiter(tmp_path, {})

        async def wait(username, filename, timeout_secs, progress_cb):
            if "02 - " in filename:
                reached.set()
                await asyncio.sleep(3600)
            return await landed_first(username, filename, timeout_secs, progress_cb)

        bot.slskd.wait_for_download = AsyncMock(side_effect=wait)
        _offer(bot)
        context = _context()
        status = context.bot.send_message.return_value
        await _tap(bot, context, f"alb:{DL}")
        await _tap(bot, context, f"albgo:{DL}")
        await asyncio.wait_for(reached.wait(), 5)
        assert "1/3 done" in bot._album_runs[CHAT].line

        update = _update(MagicMock())
        bot.import_repo.get_active_job = MagicMock(return_value=None)
        await bot.cmd_cancel(update, context)
        await _finish(bot)

        assert "Album download stopped" in update.message.reply_text.call_args.args[0]
        summary = _status_texts(status)[-1]
        assert "✅ Saved 1 of 3" in summary
        assert "⏹ Cancelled with 2 not fetched: their transfers were cancelled in slskd" in summary
        assert os.listdir(tmp_path / "music") == ["Pink Floyd - One of These Days.flac"]
        [job] = bot.pipeline.album_repo.list_by_status(ALBUM_CANCELLED)
        assert job.unfinished == [1, 2]
        # Only the saved file's finished transfer is forgotten; the unfinished ones are cancelled.
        assert {c.args for c in bot.slskd.remove_transfer.call_args_list} <= {(PEER, "tx-01")}
        assert [c.args[1].rsplit("\\", 1)[-1] for c in bot.slskd.cancel_downloads.call_args_list] == [
            "02 - Pillow of Winds.flac",
            "06 - Echoes.mp3",
        ]
        assert bot.slskd.wait_for_download.await_count == 2


class TestShutdown:
    async def test_post_shutdown_cancels_a_running_album_and_leaves_its_job_running(self, tmp_path):
        from music_downloader.bot.handlers import create_bot

        made = []
        config = _make_config(str(tmp_path))
        config.album_timeout_secs = 7200
        with (
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as app_cls,
            patch(
                "music_downloader.bot.handlers.MusicBot",
                side_effect=lambda *a, **k: made.append(MusicBot(*a, **k)) or made[-1],
            ),
        ):
            builder = MagicMock()
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            app_cls.builder.return_value = builder
            create_bot(config)
            post_shutdown = builder.post_shutdown.call_args[0][0]
        [bot] = made
        bot.slskd = _slskd()
        started = asyncio.Event()

        async def wait(username, filename, timeout_secs, progress_cb):
            started.set()
            await asyncio.sleep(3600)

        bot.slskd.wait_for_download = AsyncMock(side_effect=wait)
        _offer(bot)
        context = _context()
        await _tap(bot, context, f"alb:{DL}")
        await _tap(bot, context, f"albgo:{DL}")
        await asyncio.wait_for(started.wait(), 5)
        task = bot._album_runs[CHAT].task

        await asyncio.wait_for(post_shutdown(MagicMock()), 5)
        assert task.cancelled()
        # The job stays running: the next start recovers it.
        assert len(bot.pipeline.album_repo.list_by_status(ALBUM_RUNNING)) == 1


class TestRestart:
    async def test_interrupted_albums_are_reported_to_their_chats(self, tmp_path):
        bot = _bot(tmp_path)
        files = _album.parse_directory(_raw_folder(), FOLDER)
        # The chat album comes first: it must leave the landed file for the library album after it.
        sent = AlbumJob(PEER, FOLDER, files, TRACK, deliver="chat", chat_id=CHAT)
        bot.pipeline.album_repo.add(sent)
        library = AlbumJob(PEER, FOLDER, files, TRACK, chat_id=CHAT)
        library.outcomes[0] = FileOutcome(filename=files[0].filename, path="/music/a.flac")
        bot.pipeline.album_repo.add(library)
        bot.pipeline.album_repo.add(AlbumJob(PEER, FOLDER, files, TRACK, deliver="path"))  # MCP: no chat
        landed = _land(tmp_path, files[1].filename)  # finished while the bot was down

        telegram = AsyncMock()
        await bot._recover_albums(telegram)

        texts = [c.kwargs["text"] for c in telegram.send_message.call_args_list]
        assert [c.kwargs["chat_id"] for c in telegram.send_message.call_args_list] == [CHAT, CHAT]
        assert "restarted during the album <b>1971 - Meddle</b>" in texts[0]
        assert "0 of 3 sent, 0 failed, 3 not fetched" in texts[0]
        assert "2 of 3 saved, 0 failed, 1 not fetched" in texts[1]
        assert bot.pipeline.album_repo.list_by_status(ALBUM_RUNNING) == []
        assert len(bot.pipeline.album_repo.list_by_status(ALBUM_INTERRUPTED)) == 3
        # The chat album left the file where it landed (nobody is there to send it); the library album saved it.
        assert bot.pipeline.album_repo.get(sent.id).outcomes[1] is None
        assert bot.pipeline.album_repo.get(library.id).outcomes[1].ok
        assert not os.path.exists(landed)
