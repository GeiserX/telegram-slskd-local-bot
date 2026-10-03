"""Every message the bot sends is HTML, and names from outside are escaped in it.

One test per flow family pushes a track named ``*NSYNC <&> _Test_ - Bye [Bye] `Bye``` through
it: the Markdown characters must arrive untouched, the HTML ones escaped, and no raw ``<`` from
the input may reach Telegram (it would either break the message or inject markup).
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import ParseMode
from telegram.error import NetworkError

from music_downloader.bot.handlers import MusicBot, PendingDownload, PendingSearch
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, FetchOutcome
from music_downloader.search.slskd_client import SearchResult

CHAT = 67890
ARTIST = "*NSYNC <&> _Test_"
TITLE = "Bye [Bye] `Bye`"
NAME = f"{ARTIST} - {TITLE}"
ESC_NAME = "*NSYNC &lt;&amp;&gt; _Test_ - Bye [Bye] `Bye`"
USER = "<peer>&co"
ESC_USER = "&lt;peer&gt;&amp;co"

# The tags the bot itself writes; anything else in the text is a leak.
_OWN_TAG = re.compile(r"</?(?:b|i|code|a)(?: [^<>]*)?>")


def _assert_html(text: str, *expected: str) -> None:
    for fragment in expected:
        assert fragment in text, f"{fragment!r} not in {text!r}"
    bare = _OWN_TAG.sub("", text)
    assert "<" not in bare and ">" not in bare, f"raw angle bracket in {text!r}"
    assert not re.search(r"&(?!lt;|gt;|amp;|quot;)", bare), f"raw ampersand in {text!r}"


def _make_config(chat_users=()):
    td = tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_upload_limit_bytes = 50_000_000
    config.telegram_allowed_users = {CHAT}
    config.telegram_chat_delivery_users = set(chat_users)
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
    return config


def _make_bot(chat_users=()):
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        bot = MusicBot(_make_config(chat_users))
    bot.processor = MagicMock()
    bot.processor.build_filename = MagicMock(side_effect=lambda a, t, ext="flac": f"{a} - {t}.{ext}")
    bot.processor.cleanup_download = MagicMock(return_value=True)
    bot.pipeline.embed_artwork = AsyncMock()
    return bot


def _track():
    return TrackInfo(
        artist=ARTIST,
        title=TITLE,
        album="<Album> & more",
        duration_ms=162_000,
        spotify_url="https://open.spotify.com/track/x?a=1&b=2",
        year="2000",
    )


def _result():
    return SearchResult(
        username=USER,
        filename=f"\\Music\\{NAME}.flac",
        size=30_000_000,
        bit_depth=16,
        sample_rate=44100,
        length=162,
    )


def _context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.application = MagicMock()
    context.application.create_task = MagicMock(side_effect=lambda coro, **kw: asyncio.ensure_future(coro))
    return context


def _msg():
    msg = AsyncMock()
    msg.message_id = 1
    return msg


def _update(text=""):
    update = MagicMock()
    update.effective_user.id = CHAT
    update.effective_chat.id = CHAT
    update.message = AsyncMock()
    update.message.text = text
    return update


def _edit_calls(msg):
    """(text, kwargs) for every edit of a status message."""
    return [(c.args[0], c.kwargs) for c in msg.edit_text.call_args_list]


class TestSearchList:
    @pytest.mark.asyncio
    async def test_result_list_escapes_track_and_filenames(self):
        bot = _make_bot()
        bot.pipeline.search = AsyncMock(return_value=[_result()])
        msg = _msg()
        await bot._do_slskd_search(_context(), CHAT, _track(), msg, generation=0, user_id=CHAT)

        edits = _edit_calls(msg)
        assert edits and all(kw.get("parse_mode") == ParseMode.HTML for _, kw in edits)
        _assert_html(edits[0][0], f"<b>{ESC_NAME}</b>", "Album: &lt;Album&gt; &amp; more")
        _assert_html(edits[-1][0], f"<b>{ESC_NAME}</b>", f"<code>{ESC_NAME}.flac</code>")

    def test_spotify_candidates_escape_names_and_link(self):
        text = MusicBot._format_spotify_results([_track(), _track()])
        _assert_html(text, f"<b>#1 {ESC_NAME}</b>", '<a href="https://open.spotify.com/track/x?a=1&amp;b=2">')


class TestDownloadStatus:
    @pytest.mark.asyncio
    async def test_download_status_escapes_peer_and_file(self):
        bot = _make_bot()
        context = _context()
        with patch.object(bot, "_do_download", new_callable=AsyncMock):
            await bot._launch_download(context, CHAT, _track(), _result(), 0, "s1")

        kwargs = context.bot.send_message.call_args.kwargs
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(kwargs["text"], ESC_NAME, f"<code>{ESC_USER}</code>", f"<code>{ESC_NAME}.flac</code>")


class TestApprovalCaption:
    @pytest.mark.asyncio
    async def test_saved_and_rejected_captions_are_escaped(self):
        bot = _make_bot()
        bot.pipeline.save = AsyncMock(return_value=f"/music/{NAME}.flac")
        bot.downloads["ok"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT, source_path="/x.flac")
        bot.downloads["no"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT)
        update = MagicMock()
        update.callback_query = AsyncMock()

        with patch.object(bot, "_dismiss_other_downloads", new_callable=AsyncMock):
            await bot._handle_approval(update, _context(), CHAT, "approve:ok")
        saved = update.callback_query.edit_message_caption.call_args.kwargs
        assert saved["parse_mode"] == ParseMode.HTML
        _assert_html(saved["caption"], f"<code>{ESC_NAME}.flac</code>")

        await bot._handle_approval(update, _context(), CHAT, "reject:no")
        rejected = update.callback_query.edit_message_caption.call_args.kwargs
        assert rejected["parse_mode"] == ParseMode.HTML
        _assert_html(rejected["caption"], f"Rejected: {ESC_NAME}")


class TestChatDeliveryCaption:
    @pytest.mark.asyncio
    async def test_sent_status_escapes_the_sent_filename(self, tmp_path):
        bot = _make_bot(chat_users={CHAT})
        source = tmp_path / "t.flac"
        source.write_bytes(b"x" * 1000)
        pending = PendingDownload(track=_track(), result=_result(), chat_id=CHAT, source_path=str(source))
        bot.downloads["d1"] = pending
        context, msg = _context(), _msg()

        assert await bot._deliver_download(context, CHAT, "d1", pending, msg, "Quality: x", "#1")

        assert context.bot.send_audio.call_args.kwargs["parse_mode"] == ParseMode.HTML
        text, kwargs = _edit_calls(msg)[-1]
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(text, f"<code>{ESC_NAME}.flac</code>")

    @pytest.mark.asyncio
    async def test_import_delivery_caption_escapes_the_track(self, tmp_path):
        bot = _make_bot(chat_users={CHAT})
        bot.import_repo = MagicMock()
        source = tmp_path / "t.flac"
        source.write_bytes(b"x" * 1000)
        context = _context()

        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._import_deliver(context, CHAT, 1, 2, "d1", _track(), _result(), str(source), _msg(), 0)

        kwargs = context.bot.send_audio.call_args.kwargs
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(kwargs["caption"], f"Import: {ESC_NAME}")


class TestImportHeading:
    @pytest.mark.asyncio
    async def test_import_heading_escapes_the_track(self):
        bot = _make_bot()
        bot.import_repo = MagicMock()
        bot.pipeline.search = AsyncMock(return_value=[])
        msg = _msg()
        await bot._do_import_slskd_search(_context(), CHAT, _track(), msg, 0, job_id=1, track_id=2)

        text, kwargs = _edit_calls(msg)[-1]
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(text, f"<b>Import track:</b> {ESC_NAME}")


class TestToggleCommands:
    """/deliver and /auto carry no outside values; they must still be HTML, not Markdown."""

    @pytest.mark.asyncio
    async def test_deliver_is_html(self):
        bot = _make_bot()
        update = _update()
        await bot.cmd_deliver(update, _context())
        args, kwargs = update.message.reply_text.call_args
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(args[0], "<b>Library</b>")
        assert "*" not in args[0]

    @pytest.mark.asyncio
    async def test_deliver_locked_is_html(self):
        bot = _make_bot(chat_users={CHAT})
        update = _update()
        await bot.cmd_deliver(update, _context())
        args, kwargs = update.message.reply_text.call_args
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(args[0], "<b>Chat</b> (fixed)")

    @pytest.mark.asyncio
    async def test_auto_is_html(self):
        bot = _make_bot()
        update = _update()
        await bot.cmd_auto(update, _context())
        args, kwargs = update.message.reply_text.call_args
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(args[0], "<b>OFF</b>")
        assert "*" not in args[0]


class TestHistory:
    @pytest.mark.asyncio
    async def test_history_escapes_filenames(self):
        bot = _make_bot()
        bot.history_repo.add(artist=ARTIST, title=TITLE, filename=f"{NAME}.flac", source_user=USER, status="success")
        update = _update()
        await bot.cmd_history(update, _context())
        args, kwargs = update.message.reply_text.call_args
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(args[0], f"<code>{ESC_NAME}.flac</code>")


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_escapes_searches_and_downloads(self):
        bot = _make_bot()
        bot.pending[CHAT] = PendingSearch(query=NAME, track=_track())
        bot.downloads["d1"] = PendingDownload(track=_track(), result=_result(), chat_id=CHAT)
        update = _update()
        await bot.cmd_status(update, _context())
        args, kwargs = update.message.reply_text.call_args
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(args[0], f"• {ESC_NAME}\n", f"• {ESC_NAME} ({ESC_NAME}.flac)")


class TestErrorMessages:
    @pytest.mark.asyncio
    async def test_failed_download_escapes_state_and_file(self):
        bot = _make_bot()
        bot.pipeline.fetch = AsyncMock(return_value=FetchOutcome(error=DOWNLOAD_FAILED, state="<Errored> & gone"))
        msg = _msg()
        await bot._do_download(_context(), CHAT, _track(), _result(), msg)

        text, kwargs = _edit_calls(msg)[-1]
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(text, "Download failed: &lt;Errored&gt; &amp; gone", f"<code>{ESC_NAME}.flac</code>")

    @pytest.mark.asyncio
    async def test_failed_import_search_escapes_the_track(self):
        bot = _make_bot()
        bot.import_repo = MagicMock()
        bot.pipeline.search = AsyncMock(side_effect=RuntimeError("boom"))
        msg = _msg()
        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._do_import_slskd_search(_context(), CHAT, _track(), msg, 0, job_id=1, track_id=2)

        text, kwargs = _edit_calls(msg)[-1]
        assert kwargs["parse_mode"] == ParseMode.HTML
        _assert_html(text, f"Search failed for {ESC_NAME}")

    @pytest.mark.asyncio
    async def test_send_error_reason_is_escaped(self, tmp_path):
        source = tmp_path / "t.flac"
        source.write_bytes(b"x")
        context = _context()
        context.bot.send_audio = AsyncMock(side_effect=NetworkError(f"refused {NAME}"))
        reason = await MusicBot._send_audio_file(context, CHAT, str(source), "t.flac", _track(), "c")
        _assert_html(reason, f"refused {ESC_NAME}")


def test_import_progress_header_is_escaped():
    """SimpleNamespace stands in for the import row the next-track message is built from."""
    row = SimpleNamespace(
        id=2, artist=ARTIST, title=TITLE, album="<Album>", duration_ms=1, spotify_url="u", year="2000"
    )
    bot = _make_bot()
    bot.import_repo = MagicMock()
    bot.import_repo.get_next_pending_track = MagicMock(return_value=row)
    bot.import_repo.get_job_progress = MagicMock(return_value=(0, 0, 0, 1))
    context = _context()

    async def run():
        with patch.object(bot, "_do_import_slskd_search", new_callable=AsyncMock):
            await bot._process_next_import_track(context, CHAT, 1, 0)

    asyncio.run(run())
    kwargs = context.bot.send_message.call_args.kwargs
    assert kwargs["parse_mode"] == ParseMode.HTML
    _assert_html(kwargs["text"], f"<b>{ESC_NAME}</b>", "Album: &lt;Album&gt; (2000)")
