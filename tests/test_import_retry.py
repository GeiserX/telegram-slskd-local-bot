"""A failed chat send in a review-mode /import offers Retry and Try next, like a plain download (tsb-eu1)."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

from music_downloader.bot.handlers import MusicBot, PendingDownload, PendingSearch
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

CHAT = 67890
USER = 12345


def _make_config():
    td = tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {USER}
    config.telegram_chat_delivery_users = {USER}  # locked to chat delivery
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


def _track():
    return TrackInfo(
        artist="Nancy Sinatra", title="Bang Bang", album="X", duration_ms=162_000, spotify_url="u", year="1966"
    )


def _result(idx=0):
    return SearchResult(username=f"user{idx}", filename=f"\\Music\\track{idx}.flac", size=30_000_000, length=162)


def _context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


def _tap(data):
    update = MagicMock()
    update.effective_chat.id = CHAT
    query = update.callback_query
    query.data = data
    query.from_user.id = USER
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_caption = AsyncMock()
    return update


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def _failed_send(results=2):
    """A review-mode import whose chat send just failed: returns (bot, job_id, track_id, status_msg)."""
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        bot = MusicBot(_make_config())
    job_id = bot.import_repo.create_job(CHAT, "https://open.spotify.com/playlist/x", "P", 1)
    bot.import_repo.add_tracks(job_id, [{"position": 1, "artist": "Nancy Sinatra", "title": "Bang Bang"}])
    track_id = bot.import_repo.get_next_pending_track(job_id).id
    bot._active_import[CHAT] = job_id
    bot._import_user[CHAT] = USER
    generation = bot._chat_generation.get(CHAT, 0)
    bot._import_pending[CHAT] = PendingSearch(
        query="q", track=_track(), results=[_result(i) for i in range(results)], search_id="s1"
    )
    source = os.path.join(bot.config.download_dir, "track0.flac")
    with open(source, "wb") as f:
        f.write(b"x")
    bot.downloads["d1"] = PendingDownload(
        track=_track(),
        result=_result(0),
        chat_id=CHAT,
        source_path=source,
        search_id="s1",
        user_id=USER,
        job_id=job_id,
        track_id=track_id,
        generation=generation,
    )
    bot._send_to_chat = AsyncMock(return_value=("send_failed", "Could not send the file to Telegram: boom"))
    bot._process_next_import_track = AsyncMock()
    status_msg = MagicMock(edit_text=AsyncMock())
    await bot._import_deliver(
        _context(), CHAT, job_id, track_id, "d1", _track(), _result(0), source, status_msg, generation
    )
    return bot, job_id, track_id, status_msg


class TestFailedSend:
    async def test_entry_is_kept_without_its_source(self):
        bot, *_ = await _failed_send()
        assert "d1" in bot.downloads
        assert bot.downloads["d1"].source_path is None

    async def test_offers_retry_next_mark_failed_and_skip(self):
        bot, job_id, track_id, status_msg = await _failed_send()
        markup = status_msg.edit_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["retry:d1", "next:d1", f"ir:{job_id}:{track_id}", f"is:{job_id}:{track_id}"]
        bot._process_next_import_track.assert_not_awaited()

    async def test_no_next_button_on_the_last_copy(self):
        _bot, job_id, track_id, status_msg = await _failed_send(results=1)
        markup = status_msg.edit_text.await_args.kwargs["reply_markup"]
        assert _callbacks(markup) == ["retry:d1", f"ir:{job_id}:{track_id}", f"is:{job_id}:{track_id}"]


class TestButtons:
    async def test_retry_reenters_the_import_download_for_that_track(self):
        bot, job_id, track_id, _ = await _failed_send()
        bot._do_import_download = AsyncMock()
        await bot.handle_callback(_tap("retry:d1"), _context())
        await asyncio.sleep(0)
        args = bot._do_import_download.await_args.args
        # (context, chat_id, track, result, status_msg, generation, job_id, track_id, dl_id)
        assert (args[1], args[2], args[3], args[5], args[6], args[7], args[8]) == (
            CHAT,
            _track(),
            _result(0),
            0,
            job_id,
            track_id,
            "d1",
        )
        assert bot.downloads["d1"].job_id == job_id

    async def test_try_next_picks_the_next_ranked_copy(self):
        bot, job_id, track_id, _ = await _failed_send()
        bot._do_import_download = AsyncMock()
        bot._do_download = AsyncMock()
        await bot.handle_callback(_tap("next:d1"), _context())
        await asyncio.sleep(0)
        bot._do_download.assert_not_awaited()
        args = bot._do_import_download.await_args.args
        assert args[3] == _result(1)
        assert (args[6], args[7]) == (job_id, track_id)
        new_entry = bot.downloads[args[8]]
        assert new_entry.result_index == 1 and new_entry.job_id == job_id and new_entry.track_id == track_id

    async def test_retry_after_cancel_does_nothing(self):
        bot, *_ = await _failed_send()
        bot._do_import_download = AsyncMock()
        cancel = MagicMock()
        cancel.effective_chat.id = CHAT
        cancel.effective_user.id = USER
        cancel.message.reply_text = AsyncMock()
        await bot.cmd_cancel(cancel, _context())
        await bot.handle_callback(_tap("retry:d1"), _context())
        await bot.handle_callback(_tap("next:d1"), _context())
        await asyncio.sleep(0)
        bot._do_import_download.assert_not_awaited()

    async def test_retry_from_a_stale_generation_does_nothing(self):
        bot, *_ = await _failed_send()
        bot._do_import_download = AsyncMock()
        bot._chat_generation[CHAT] = bot._chat_generation.get(CHAT, 0) + 1  # superseded, entry still there
        update = _tap("retry:d1")
        await bot.handle_callback(update, _context())
        await asyncio.sleep(0)
        bot._do_import_download.assert_not_awaited()
        assert "no longer running" in update.callback_query.edit_message_text.await_args.args[0]

    async def test_skip_forgets_the_track_entries(self):
        bot, job_id, track_id, _ = await _failed_send()
        await bot.handle_callback(_tap(f"is:{job_id}:{track_id}"), _context())
        assert "d1" not in bot.downloads
