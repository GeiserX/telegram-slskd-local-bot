"""The wishlist: search a track again later, for any copy or for a better one than today's."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

from music_downloader.bot.handlers import MusicBot
from music_downloader.persistence.database import Database
from music_downloader.persistence.pending_repo import PendingDownload, PendingSearch
from music_downloader.persistence.wishlist_repo import Wish, WishlistRepository
from music_downloader.pipeline import wishlist as pw
from music_downloader.pipeline.search import RankedResults
from music_downloader.search.scorer import (
    PROFILE_CHAT,
    PROFILE_LIBRARY,
    TIER_LABELS,
    TIER_LOSSLESS_16,
    TIER_LOSSLESS_24,
    TIER_LOSSY_128,
    TIER_LOSSY_192,
    TIER_LOSSY_256,
    TIER_LOSSY_UNDER_128,
    ResultScorer,
    quality_tier,
)
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import CHAT, OTHER, _make_bot, _make_config, _make_context, _make_track

DAY = 86400


def _config(tmp_path, **kw):
    config = _make_config(str(tmp_path), **kw)
    config.wishlist_check_hours = 24
    config.wishlist_pause_secs = 20
    return config


def _mp3(kbps, idx=0, ext="mp3", length=162):
    return SearchResult(
        username=f"peer{idx}",
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {idx}.{ext}",
        size=kbps * 1000 * length // 8,
        bit_rate=kbps,
        length=length,
    )


def _flac(bit_depth, idx=0):
    return SearchResult(
        username=f"peer{idx}",
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {idx}.flac",
        size=30_000_000,
        bit_depth=bit_depth,
        sample_rate=44100,
        length=162,
    )


def _repo(tmp_path):
    return WishlistRepository(Database(str(tmp_path / "w.db")))


def _wish(repo, wanted="any", baseline=None, chat_id=CHAT, created_at=0.0, **kw):
    return repo.add(
        Wish(
            chat_id=chat_id,
            user_id=chat_id,
            track=_make_track(),
            profile=PROFILE_LIBRARY,
            wanted=wanted,
            baseline_tier=baseline,
            created_at=created_at,
            **kw,
        )
    )


def _callback(data, message=None, user_id=CHAT, chat_id=CHAT):
    update = MagicMock()
    update.callback_query = AsyncMock()
    update.callback_query.from_user.id = user_id
    update.callback_query.data = data
    update.callback_query.message = message or MagicMock(text_html="", reply_markup=None)
    update.effective_chat.id = chat_id
    return update


def _update(chat_id=CHAT):
    update = MagicMock()
    update.effective_user.id = chat_id
    update.effective_chat.id = chat_id
    update.message = AsyncMock()
    return update


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# Repository and schema
# ---------------------------------------------------------------------------


class TestRepository:
    def test_round_trip(self, tmp_path):
        repo = _repo(tmp_path)
        added = _wish(repo, "better", TIER_LOSSY_192, created_at=123.5)
        assert added.id is not None
        got = repo.get(added.id)
        assert got == added
        assert got.track == _make_track()
        assert (got.wanted, got.baseline_tier, got.profile, got.checks) == ("better", TIER_LOSSY_192, "library", 0)
        assert got.last_checked_at is None and got.notified_at is None

        repo.mark_checked(added.id, 500.0)
        repo.mark_notified(added.id, 900.0)
        got = repo.get(added.id)
        assert (got.last_checked_at, got.notified_at, got.checks) == (900.0, 900.0, 2)

    def test_list_and_remove_are_per_chat(self, tmp_path):
        repo = _repo(tmp_path)
        mine = _wish(repo)
        theirs = _wish(repo, chat_id=OTHER)
        assert [w.id for w in repo.list_for_chat(CHAT)] == [mine.id]
        assert [w.id for w in repo.list_all()] == [mine.id, theirs.id]
        assert repo.remove(CHAT, theirs.id) is False
        assert repo.get(theirs.id) is not None
        assert repo.remove(CHAT, mine.id) is True
        assert repo.get(mine.id) is None

    def test_old_database_gets_the_table(self, tmp_path):
        db_path = str(tmp_path / "old.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY, auto_mode INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO chat_settings (chat_id, auto_mode) VALUES (5, 1)")
        conn.commit()
        conn.close()
        db = Database(db_path)
        repo = WishlistRepository(db)
        wish = _wish(repo, chat_id=5)
        assert repo.list_for_chat(5) == [wish]
        assert db.connection.execute("SELECT auto_mode FROM chat_settings WHERE chat_id = 5").fetchone()[0] == 1
        db.close()


# ---------------------------------------------------------------------------
# Quality tiers
# ---------------------------------------------------------------------------


class TestQualityTiers:
    def test_one_ordered_scale(self):
        ladder = [_mp3(96), _mp3(128), _mp3(192), _mp3(320), _flac(16), _flac(24)]
        assert [quality_tier(r) for r in ladder] == [
            TIER_LOSSY_UNDER_128,
            TIER_LOSSY_128,
            TIER_LOSSY_192,
            TIER_LOSSY_256,
            TIER_LOSSLESS_16,
            TIER_LOSSLESS_24,
        ]
        assert sorted(TIER_LABELS) == list(range(6))

    def test_codec_factor(self):
        assert quality_tier(_mp3(128, ext="opus")) == TIER_LOSSY_256
        assert quality_tier(_mp3(128, ext="m4a")) == TIER_LOSSY_192
        assert quality_tier(_mp3(128)) == TIER_LOSSY_128

    def test_unknown_and_estimated_bitrates(self):
        no_rate = SearchResult(username="u", filename="a.mp3", size=0)
        assert quality_tier(no_rate) == TIER_LOSSY_128
        estimated = SearchResult(username="u", filename="a.mp3", size=320 * 1000 * 200 // 8, length=200)
        assert quality_tier(estimated) == TIER_LOSSY_256
        assert quality_tier(SearchResult(username="u", filename="a.flac", size=1)) == TIER_LOSSLESS_16

    def test_perceived_points_follow_the_tiers(self):
        points = ResultScorer._perceived_points
        assert [points(_mp3(k)) for k in (96, 128, 192, 320)] == [1.0, 10.0, 20.0, 25.0]
        assert points(_mp3(128, ext="opus")) == 25.0
        assert points(_flac(16)) == 25.0


# ---------------------------------------------------------------------------
# The checker (pipeline, Telegram-free)
# ---------------------------------------------------------------------------


def _search_returning(*results):
    return AsyncMock(return_value=RankedResults(results))


class TestChecker:
    @pytest.mark.asyncio
    async def test_due_versus_not_due(self, tmp_path):
        repo = _repo(tmp_path)
        old = _wish(repo, created_at=0.0)
        _wish(repo, created_at=2 * DAY - 10)
        search = _search_returning()
        n = await pw.check_due(repo, search, AsyncMock(), DAY, 0, sleep=AsyncMock(), now=lambda: 2 * DAY)
        assert n == 1
        assert search.await_count == 1
        assert search.call_args.args[0] == "Nancy Sinatra Bang Bang"
        got = repo.get(old.id)
        assert (got.last_checked_at, got.checks, got.notified_at) == (2 * DAY, 1, None)

    def test_notified_at_holds_the_wish_back(self):
        wish = Wish(chat_id=1, track=_make_track(), profile="library", wanted="any", created_at=0.0)
        assert pw.is_due(wish, 2 * DAY, DAY)
        wish.last_checked_at = 0.5 * DAY
        wish.notified_at = 1.5 * DAY
        assert not pw.is_due(wish, 2 * DAY, DAY)

    @pytest.mark.asyncio
    async def test_any_is_satisfied_by_any_copy(self, tmp_path):
        repo = _repo(tmp_path)
        wish = _wish(repo, "any")
        deliver = AsyncMock(return_value=False)
        await pw.check_due(repo, _search_returning(_mp3(96)), deliver, DAY, 0, sleep=AsyncMock(), now=lambda: DAY)
        assert [r.bit_rate for r in deliver.call_args.args[1]] == [96]
        assert deliver.call_args.args[0].id == wish.id

    @pytest.mark.asyncio
    async def test_better_needs_a_strictly_higher_tier(self, tmp_path):
        repo = _repo(tmp_path)
        wish = _wish(repo, "better", TIER_LOSSY_256)
        deliver = AsyncMock(return_value=False)
        same = _search_returning(_mp3(320, 1), _mp3(128, 2, ext="opus"), _mp3(192, 3))
        await pw.check_due(repo, same, deliver, DAY, 0, sleep=AsyncMock(), now=lambda: DAY)
        deliver.assert_not_awaited()
        assert repo.get(wish.id).checks == 1

        better = _search_returning(_mp3(320, 1), _flac(16, 2), _flac(24, 3))
        await pw.check_due(repo, better, deliver, DAY, 0, sleep=AsyncMock(), now=lambda: 3 * DAY)
        assert [quality_tier(r) for r in deliver.call_args.args[1]] == [TIER_LOSSLESS_16, TIER_LOSSLESS_24]

    @pytest.mark.asyncio
    async def test_notified_wish_is_not_sent_again_this_period(self, tmp_path):
        repo = _repo(tmp_path)
        wish = _wish(repo)
        deliver = AsyncMock(return_value=False)
        search = _search_returning(_mp3(320))
        await pw.check_due(repo, search, deliver, DAY, 0, sleep=AsyncMock(), now=lambda: DAY)
        assert repo.get(wish.id).notified_at == DAY
        await pw.check_due(repo, search, deliver, DAY, 0, sleep=AsyncMock(), now=lambda: DAY + 3600)
        assert deliver.await_count == 1
        await pw.check_due(repo, search, deliver, DAY, 0, sleep=AsyncMock(), now=lambda: 2 * DAY)
        assert deliver.await_count == 2

    @pytest.mark.asyncio
    async def test_fulfilled_wish_is_removed(self, tmp_path):
        repo = _repo(tmp_path)
        wish = _wish(repo)
        await pw.check_due(
            repo, _search_returning(_mp3(320)), AsyncMock(return_value=True), DAY, 0, sleep=AsyncMock(), now=lambda: DAY
        )
        assert repo.get(wish.id) is None

    @pytest.mark.asyncio
    async def test_a_failed_write_does_not_stop_the_pass(self, tmp_path):
        repo = _repo(tmp_path)
        first = _wish(repo)
        second = _wish(repo, chat_id=OTHER)
        real_remove = repo.remove

        def remove(chat_id, wish_id):
            if wish_id == first.id:
                raise sqlite3.OperationalError("database is locked")
            return real_remove(chat_id, wish_id)

        repo.remove = remove
        deliver = AsyncMock(return_value=True)
        n = await pw.check_due(repo, _search_returning(_mp3(320)), deliver, DAY, 0, sleep=AsyncMock(), now=lambda: DAY)
        assert n == 2 and deliver.await_count == 2
        assert repo.get(second.id) is None
        # The removal failed, but notified_at holds the wish back for a period.
        assert repo.get(first.id).notified_at == DAY
        assert not pw.is_due(repo.get(first.id), DAY + 3600, DAY)

    @pytest.mark.asyncio
    async def test_a_skipped_wish_is_not_searched_and_stays_due(self, tmp_path):
        repo = _repo(tmp_path)
        wish = _wish(repo)
        search = _search_returning()
        n = await pw.check_due(
            repo, search, AsyncMock(), DAY, 0, sleep=AsyncMock(), now=lambda: DAY, skip=lambda w: True
        )
        assert n == 0 and search.await_count == 0
        assert repo.get(wish.id).checks == 0 and pw.is_due(repo.get(wish.id), DAY, DAY)

    @pytest.mark.asyncio
    async def test_searches_are_sequential_with_the_pause_between(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        events = []

        async def search(query, track, profile, **kw):
            events.append(("search", profile))
            return RankedResults()

        async def sleep(secs):
            events.append(("sleep", secs))

        _wish(bot.pipeline.wishlist_repo)
        bot.pipeline.wishlist_repo.add(
            Wish(chat_id=OTHER, track=_make_track(), profile=PROFILE_CHAT, wanted="any", created_at=0.0)
        )
        bot.pipeline.search = search
        assert await bot.pipeline.wishlist_check_due(AsyncMock(), sleep=sleep) == 2
        assert events == [("search", PROFILE_LIBRARY), ("sleep", 20), ("search", PROFILE_CHAT)]

    def test_pipeline_validates_wanted(self, tmp_path):
        pipeline = _make_bot(_config(tmp_path)).pipeline
        with pytest.raises(ValueError):
            pipeline.wishlist_add(CHAT, CHAT, _make_track(), PROFILE_LIBRARY, "maybe")
        with pytest.raises(ValueError):
            pipeline.wishlist_add(CHAT, CHAT, _make_track(), PROFILE_LIBRARY, "better")
        wish = pipeline.wishlist_add(CHAT, CHAT, _make_track(), PROFILE_LIBRARY, "any", baseline_tier=3)
        assert wish.baseline_tier is None
        assert pipeline.wishlist_list(CHAT) == [wish]
        assert pipeline.wishlist_remove(CHAT, wish.id) is True


# ---------------------------------------------------------------------------
# Telegram: the two buttons, /wishlist, delivery
# ---------------------------------------------------------------------------


async def _search_and_capture(bot, results):
    bot.slskd.search = AsyncMock(return_value=[])
    bot.slskd.parse_results = MagicMock(return_value=list(results))
    with patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock) as edit:
        await bot._do_slskd_search(_make_context(), CHAT, _make_track(), AsyncMock(), generation=0, user_id=CHAT)
    return edit.call_args


def _wish_button(markup, action):
    return next(c for c in _callbacks(markup) if c.startswith(f"wish:{action}:"))


class TestButtons:
    @pytest.mark.asyncio
    async def test_nothing_found_offers_tell_me_and_adds_an_any_wish(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        call = await _search_and_capture(bot, [])
        markup = call.kwargs["reply_markup"]
        assert "direct:search" in _callbacks(markup)
        data = _wish_button(markup, "any")

        update = _callback(data, MagicMock(text_html="No results found", reply_markup=markup))
        await bot.handle_callback(update, _make_context())
        [wish] = bot.pipeline.wishlist_list(CHAT)
        assert (wish.wanted, wish.baseline_tier, wish.profile, wish.user_id) == ("any", None, PROFILE_LIBRARY, CHAT)
        assert wish.track == _make_track()
        args, kwargs = update.callback_query.edit_message_text.call_args
        assert args[0].startswith("No results found\n\n🔔 I'll tell you when it appears")
        assert _callbacks(kwargs["reply_markup"]) == ["direct:search"]

    @pytest.mark.asyncio
    async def test_result_list_offers_wait_and_adds_a_better_wish(self, tmp_path):
        bot = _make_bot(_config(tmp_path, chat_users={CHAT}))
        call = await _search_and_capture(bot, [_mp3(320, 1), _mp3(128, 2)])
        markup = call.kwargs["reply_markup"]
        data = _wish_button(markup, "better")

        update = _callback(data, MagicMock(text_html="<b>list</b>", reply_markup=markup))
        await bot.handle_callback(update, _make_context())
        [wish] = bot.pipeline.wishlist_list(CHAT)
        assert (wish.wanted, wish.baseline_tier, wish.profile) == ("better", TIER_LOSSY_256, PROFILE_CHAT)
        args, kwargs = update.callback_query.edit_message_text.call_args
        assert args[0] == (
            "<b>list</b>\n\n⏳ Waiting for a copy better than lossy 256+ kbps, searched again every 24 h (/wishlist)."
        )
        remaining = _callbacks(kwargs["reply_markup"])
        assert not any(c.startswith("wish:") for c in remaining)
        assert any(c.startswith("dl:") for c in remaining)

    @pytest.mark.asyncio
    async def test_better_baseline_is_the_best_copy_on_the_list_not_number_one(self, tmp_path):
        bot = _make_bot(_config(tmp_path, chat_users={CHAT}))
        call = await _search_and_capture(bot, [_flac(16, 2), _mp3(320, 1)])
        assert [r.extension for r in bot.pending[CHAT].results] == ["mp3", "flac"]  # chat ranking: MP3 first
        markup = call.kwargs["reply_markup"]
        update = _callback(_wish_button(markup, "better"), MagicMock(text_html="list", reply_markup=markup))
        await bot.handle_callback(update, _make_context())
        [wish] = bot.pipeline.wishlist_list(CHAT)
        assert wish.baseline_tier == TIER_LOSSLESS_16
        assert "better than lossless 16-bit" in update.callback_query.edit_message_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_a_track_already_on_the_wishlist_is_not_added_twice(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        existing = _wish(bot.pipeline.wishlist_repo, "any")
        call = await _search_and_capture(bot, [_mp3(320, 1)])
        markup = call.kwargs["reply_markup"]
        update = _callback(_wish_button(markup, "better"), MagicMock(text_html="list", reply_markup=markup))
        await bot.handle_callback(update, _make_context())
        assert [w.id for w in bot.pipeline.wishlist_list(CHAT)] == [existing.id]
        assert update.callback_query.edit_message_text.call_args.args[0] == (
            "list\n\nThis track is already on the wishlist (/wishlist)."
        )

    @pytest.mark.asyncio
    async def test_auto_mode_list_also_offers_wait(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        bot._set_auto(CHAT, True)
        bot._launch_download = AsyncMock()
        call = await _search_and_capture(bot, [_mp3(320)])
        assert _wish_button(call.kwargs["reply_markup"], "better").endswith(bot.pending[CHAT].search_id)

    @pytest.mark.asyncio
    async def test_stale_or_top_tier_adds_nothing(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        await _search_and_capture(bot, [_flac(24)])
        await bot.handle_callback(_callback("wish:better:deadbeef"), _make_context())
        await bot.handle_callback(_callback(f"wish:better:{bot.pending[CHAT].search_id}"), _make_context())
        assert bot.pipeline.wishlist_list(CHAT) == []


class TestWishlistCommand:
    @pytest.mark.asyncio
    async def test_lists_and_removes(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        repo = bot.pipeline.wishlist_repo
        first = _wish(repo, "any")
        second = _wish(repo, "better", TIER_LOSSY_192)
        repo.mark_checked(second.id, 1_700_000_000)
        _wish(repo, chat_id=OTHER)

        update = _update()
        await bot.cmd_wishlist(update, _make_context())
        args, kwargs = update.message.reply_text.call_args
        assert (
            "<b>#1</b> Nancy Sinatra - Bang Bang\n    Waiting for any copy · last checked not yet · 0 checks" in args[0]
        )
        assert "Waiting for a copy better than lossy 192 kbps · last checked 2023-11-1" in args[0]
        assert "· 1 check" in args[0]
        assert "#3" not in args[0]
        assert _callbacks(kwargs["reply_markup"]) == [f"wish:rm:{first.id}", f"wish:rm:{second.id}"]

        update = _callback(f"wish:rm:{first.id}")
        await bot.handle_callback(update, _make_context())
        assert [w.id for w in bot.pipeline.wishlist_list(CHAT)] == [second.id]
        args, kwargs = update.callback_query.edit_message_text.call_args
        assert "#2" not in args[0] and "better than lossy 192 kbps" in args[0]
        assert _callbacks(kwargs["reply_markup"]) == [f"wish:rm:{second.id}"]

    @pytest.mark.asyncio
    async def test_empty_and_other_chats_wish_is_untouchable(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        theirs = _wish(bot.pipeline.wishlist_repo, chat_id=OTHER)
        await bot.handle_callback(_callback(f"wish:rm:{theirs.id}"), _make_context())
        assert bot.pipeline.wishlist_repo.get(theirs.id) is not None
        update = _update()
        await bot.cmd_wishlist(update, _make_context())
        assert "empty" in update.message.reply_text.call_args.args[0]

    def test_command_in_menu_and_handlers(self, tmp_path):
        from music_downloader.bot.handlers import _register_commands, create_bot

        with (
            patch("music_downloader.bot.handlers.Application") as app_cls,
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
        ):
            builder = app_cls.builder.return_value
            for chain in ("token", "post_init", "post_shutdown"):
                getattr(builder, chain).return_value = builder
            app = create_bot(_config(tmp_path))
        commands = {cmd for c in app.add_handler.call_args_list for cmd in getattr(c.args[0], "commands", ())}
        assert "wishlist" in commands
        menu_app = MagicMock()
        menu_app.bot = AsyncMock()
        asyncio.run(_register_commands(menu_app))
        assert "wishlist" in {c.command for c in menu_app.bot.set_my_commands.call_args.args[0]}


class TestDelivery:
    def _bot(self, tmp_path, auto):
        bot = _make_bot(_config(tmp_path))
        bot._set_auto(CHAT, auto)
        bot._launch_download = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[_mp3(320, 1), _flac(16, 2)])
        context = _make_context()
        context.bot.send_message.return_value = MagicMock(message_id=77)
        return bot, context

    @staticmethod
    def _wish_list(bot):
        [(key, search)] = bot.wish_searches.items()
        assert key == (CHAT, search.search_id)
        return search

    @pytest.mark.asyncio
    async def test_wish_of_a_deauthorized_user_is_dropped_without_a_search(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        bot.config.telegram_allowed_users = {wish.user_id + 1}  # the owner removed this account
        await bot.pipeline.wishlist_check_due(
            lambda w, m: bot._deliver_wish(context, w, m), skip=bot._wish_skip, sleep=AsyncMock()
        )
        bot.slskd.search.assert_not_awaited()
        context.bot.send_message.assert_not_called()
        assert bot.pipeline.wishlist_repo.get(wish.id) is None
        # Direct delivery of such a wish is refused too.
        wish2 = _wish(bot.pipeline.wishlist_repo, "any")
        assert await bot._deliver_wish(context, wish2, [_mp3(320, 1)]) is False
        context.bot.send_message.assert_not_called()
        assert bot.pipeline.wishlist_repo.get(wish2.id) is None

    @pytest.mark.asyncio
    async def test_auto_chat_fetches_the_best_match_and_keeps_the_wish_until_it_is_saved(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=True)
        wish = _wish(bot.pipeline.wishlist_repo, "better", TIER_LOSSY_192)
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())

        bot._launch_download.assert_awaited_once()
        args, kwargs = bot._launch_download.call_args
        listed = self._wish_list(bot)
        assert CHAT not in bot.pending  # the chat's own search state is untouched
        assert args[:6] == (context, CHAT, wish.track, listed.results[0], 0, listed.search_id)
        assert listed.results[0].is_lossless  # library ranking: lossless leads
        assert listed.wish_id == wish.id and kwargs["user_id"] == CHAT
        # Launched is not fulfilled: a failed fetch must leave the wish in place.
        assert bot.pipeline.wishlist_repo.get(wish.id).notified_at is not None

        dl = PendingDownload(
            track=wish.track, result=listed.results[0], chat_id=CHAT, source_path="/x.flac", search_id=listed.search_id
        )
        bot.downloads["d1"] = dl
        bot.pipeline.save = AsyncMock(return_value=None)
        await bot._auto_save(CHAT, "d1", dl, AsyncMock(), "", "#1")
        assert bot.pipeline.wishlist_repo.get(wish.id) is not None
        bot.downloads["d1"] = dl
        bot.pipeline.save = AsyncMock(return_value="/music/Nancy Sinatra - Bang Bang.flac")
        await bot._auto_save(CHAT, "d1", dl, AsyncMock(), "", "#1")
        assert bot.pipeline.wishlist_repo.get(wish.id) is None
        assert listed.wish_id is None

    @pytest.mark.asyncio
    async def test_other_chats_get_the_list_with_stop_waiting(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        wish = _wish(bot.pipeline.wishlist_repo, "better", TIER_LOSSY_256)
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())

        bot._launch_download.assert_not_awaited()
        kwargs = context.bot.send_message.call_args.kwargs
        assert kwargs["chat_id"] == CHAT
        assert "a copy better than lossy 256+ kbps turned up" in kwargs["text"]
        callbacks = _callbacks(kwargs["reply_markup"])
        listed = self._wish_list(bot)
        assert f"wish:stop:{wish.id}" in callbacks
        assert f"dl:{listed.search_id}:0" in callbacks
        assert not any(c.startswith("wish:better:") for c in callbacks)
        assert [r.is_lossless for r in listed.results] == [True]  # only copies above the baseline
        stored = bot.pipeline.wishlist_repo.get(wish.id)
        assert stored.notified_at is not None and stored.checks == 1

        update = _callback(f"wish:stop:{wish.id}", MagicMock(text_html="list", reply_markup=kwargs["reply_markup"]))
        await bot.handle_callback(update, context)
        assert bot.pipeline.wishlist_repo.get(wish.id) is None
        args, edit_kwargs = update.callback_query.edit_message_text.call_args
        assert args[0] == "list\n\n🔕 Stopped waiting for this track."
        assert isinstance(edit_kwargs["reply_markup"], InlineKeyboardMarkup)
        assert not any(c.startswith("wish:") for c in _callbacks(edit_kwargs["reply_markup"]))

    @pytest.mark.asyncio
    async def test_own_search_and_wish_list_never_replace_each_other(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        await _search_and_capture(bot, [_mp3(128, 3)])
        own = bot.pending[CHAT]
        own_id = own.search_id
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
        listed = self._wish_list(bot)
        assert bot.pending[CHAT].search_id == own_id  # the wish did not take over the chat's list

        # The owner's own list still works after the notification...
        await bot.handle_callback(_callback(f"dl:{own_id}:0"), context)
        args = bot._launch_download.call_args.args
        assert (args[3], args[5]) == (own.results[0], own_id)

        # ...and the owner's next search neither marks the notification superseded nor stales it.
        bot._do_search = AsyncMock()
        bot.pipeline.find_similar = AsyncMock(return_value=None)
        update = _update()
        update.message.text = "Daft Punk - One More Time"
        await bot.handle_text(update, context)
        edited = [c.kwargs.get("message_id") for c in context.bot.edit_message_text.call_args_list]
        assert 77 not in edited
        await _search_and_capture(bot, [_mp3(96, 4)])
        await bot.handle_callback(_callback(f"dl:{listed.search_id}:0"), context)
        args = bot._launch_download.call_args.args
        assert (args[3], args[5]) == (listed.results[0], listed.search_id)
        assert bot.pipeline.wishlist_repo.get(wish.id) is not None

    @pytest.mark.asyncio
    async def test_saving_or_sending_a_copy_from_the_list_fulfils_the_wish(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        await _search_and_capture(bot, [_mp3(128, 3)])
        own_id = bot.pending[CHAT].search_id
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
        listed = self._wish_list(bot)

        # Library: Save on the preview of a copy picked from the wish list.
        bot.downloads["d1"] = PendingDownload(
            track=wish.track, result=listed.results[0], chat_id=CHAT, source_path="/x.flac", search_id=listed.search_id
        )
        bot.pipeline.save = AsyncMock(return_value="/music/Nancy Sinatra - Bang Bang.flac")
        await bot.handle_callback(_callback("approve:d1"), context)
        assert bot.pipeline.wishlist_repo.get(wish.id) is None
        assert (CHAT, listed.search_id) not in bot.wish_searches  # its keyboard is dismissed
        assert bot.pending[CHAT].search_id == own_id  # the chat's own list is not

        # Chat delivery: the copy was sent into the chat.
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
        listed = self._wish_list(bot)
        dl = PendingDownload(
            track=wish.track, result=listed.results[0], chat_id=CHAT, source_path="/x.flac", search_id=listed.search_id
        )
        bot._send_to_chat = AsyncMock(return_value=("sent", "Nancy Sinatra - Bang Bang.flac"))
        bot.pipeline.discard = AsyncMock()
        await bot._deliver_download(context, CHAT, "d2", dl, AsyncMock(), "", "#1")
        assert bot.pipeline.wishlist_repo.get(wish.id) is None

    @pytest.mark.asyncio
    async def test_paging_a_wish_list_keeps_stop_waiting(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        bot.slskd.parse_results = MagicMock(return_value=[_mp3(320, i) for i in range(7)])
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
        listed = self._wish_list(bot)
        update = _callback(f"dl_page:{listed.search_id}:1")
        await bot.handle_callback(update, context)
        callbacks = _callbacks(update.callback_query.edit_message_text.call_args.kwargs["reply_markup"])
        assert f"wish:stop:{wish.id}" in callbacks
        assert not any(c.startswith("wish:better:") for c in callbacks)
        assert listed.page == 1

    @pytest.mark.asyncio
    async def test_wish_lists_survive_a_restart(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
        listed = self._wish_list(bot)
        bot.wish_searches[(CHAT, "old")] = PendingSearch(query="q", track=wish.track, search_id="old", created_at=0.0)
        again = _make_bot(bot.config)
        restored = again.wish_searches[(CHAT, listed.search_id)]
        assert (restored.wish_id, restored.results, restored.message_id) == (wish.id, listed.results, 77)
        assert (CHAT, "old") not in again.wish_searches  # older than DOWNLOAD_CLEANUP_HOURS

    @pytest.mark.asyncio
    async def test_a_busy_chat_is_left_alone_until_the_next_tick(self, tmp_path):
        bot, context = self._bot(tmp_path, auto=False)
        wish = _wish(bot.pipeline.wishlist_repo, "any")
        busy_during_search = []

        async def slskd_search(*a, **k):
            busy_during_search.append(bot._chat_busy(wish))
            return []

        bot.slskd.search = slskd_search
        await _search_and_capture_with(bot)
        assert busy_during_search and all(busy_during_search) and not bot._chat_busy(wish)

        bot._searching[CHAT] = 1
        deliver = AsyncMock(return_value=False)
        assert await bot.pipeline.wishlist_check_due(deliver, sleep=AsyncMock(), skip=bot._chat_busy) == 0
        assert bot.pipeline.wishlist_repo.get(wish.id).checks == 0
        del bot._searching[CHAT]
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[_mp3(320, 1)])
        assert await bot.pipeline.wishlist_check_due(deliver, sleep=AsyncMock(), skip=bot._chat_busy) == 1
        deliver.assert_awaited_once()


async def _search_and_capture_with(bot):
    """Run the bot's own slskd search with whatever bot.slskd.search is."""
    bot.slskd.parse_results = MagicMock(return_value=[])
    with patch("music_downloader.bot.handlers._safe_edit", new_callable=AsyncMock):
        await bot._do_slskd_search(_make_context(), CHAT, _make_track(), AsyncMock(), generation=0, user_id=CHAT)


@pytest.mark.asyncio
async def test_the_bot_checker_skips_busy_chats():
    from music_downloader.bot.handlers import create_bot
    from music_downloader.pipeline import Pipeline
    from tests.test_orphan_sweep import _handlers_config

    seen = {}

    async def fake_loop(self, deliver, skip=None):
        seen["skip"] = skip
        await asyncio.Event().wait()

    with (
        patch("music_downloader.pipeline.SpotifyResolver"),
        patch("music_downloader.pipeline.SlskdClient"),
        patch("music_downloader.bot.handlers.Application") as app_cls,
        patch.object(Pipeline, "wishlist_loop", fake_loop),
    ):
        builder = MagicMock()
        for chain in ("token", "post_init", "post_shutdown"):
            getattr(builder, chain).return_value = builder
        app_cls.builder.return_value = builder
        create_bot(_handlers_config())
        app = MagicMock()
        app.bot = AsyncMock()
        await builder.post_init.call_args[0][0](app)
        await asyncio.sleep(0)
        await builder.post_shutdown.call_args[0][0](app)
    assert getattr(seen["skip"], "__func__", None) is MusicBot._wish_skip


class TestTwoWishListsInOneChat:
    """Two wishes found in the same check: each list is decided on its own."""

    @staticmethod
    def _two_lists(bot, context):
        async def run():
            repo = bot.pipeline.wishlist_repo
            first = _wish(repo, "any")
            second = _wish(repo, "any", created_at=1.0)
            await bot.pipeline.wishlist_check_due(lambda w, m: bot._deliver_wish(context, w, m), sleep=AsyncMock())
            lists = {s.wish_id: s for s in bot.wish_searches.values()}
            return first, second, lists[first.id], lists[second.id]

        return run()

    @staticmethod
    def _download(bot, dl_id, search, source="/x.flac"):
        bot.downloads[dl_id] = PendingDownload(
            track=_make_track(),
            result=search.results[0],
            chat_id=CHAT,
            source_path=source,
            approval_message_id=int(dl_id[1:]),
            search_id=search.search_id,
        )

    @staticmethod
    def _running(bot, search):
        task = asyncio.get_running_loop().create_future()
        bot._track_task(CHAT, task, search.search_id)
        return task

    @pytest.mark.asyncio
    async def test_approving_one_wish_cancels_only_that_wish(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        bot._launch_download = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[_mp3(320, 1), _flac(16, 2)])
        context = _make_context()
        context.bot.send_message.return_value = MagicMock(message_id=77)
        first, second, first_list, second_list = await self._two_lists(bot, context)

        # One copy of each wish is waiting for Save; a second copy of each is still downloading.
        self._download(bot, "d1", first_list)
        self._download(bot, "d2", second_list)
        self._download(bot, "d3", first_list, source=None)
        first_task = self._running(bot, first_list)
        second_task = self._running(bot, second_list)
        bot.pipeline.save = AsyncMock(return_value="/music/Nancy Sinatra - Bang Bang.flac")
        bot.pipeline.remove_file = MagicMock()

        await bot.handle_callback(_callback("approve:d1"), context)

        # The first wish is done: its other copy is dismissed and its running download stops.
        assert bot.pipeline.wishlist_repo.get(first.id) is None
        assert (CHAT, first_list.search_id) not in bot.wish_searches
        assert "d3" not in bot.downloads and first_task.cancelled()
        # The second wish is untouched: its list, its copy and its running download all stay.
        assert bot.pipeline.wishlist_repo.get(second.id) is not None
        assert (CHAT, second_list.search_id) in bot.wish_searches
        assert "d2" in bot.downloads and not second_task.cancelled()
        edited = [c.kwargs.get("message_id") for c in context.bot.edit_message_caption.call_args_list]
        assert 3 in edited and 2 not in edited

    @pytest.mark.asyncio
    async def test_a_new_search_leaves_a_wish_list_download_alone(self, tmp_path):
        bot = _make_bot(_config(tmp_path))
        bot._launch_download = AsyncMock()
        bot.slskd.search = AsyncMock(return_value=[])
        bot.slskd.parse_results = MagicMock(return_value=[_mp3(320, 1), _flac(16, 2)])
        context = _make_context()
        context.bot.send_message.return_value = MagicMock(message_id=77)
        first, second, first_list, second_list = await self._two_lists(bot, context)
        self._download(bot, "d2", second_list, source=None)
        bot.downloads["d2"].status_message_id = 500
        wish_task = self._running(bot, second_list)
        own_task = asyncio.get_running_loop().create_future()
        bot._track_task(CHAT, own_task, "own12345")

        bot._do_search = AsyncMock()
        bot.pipeline.find_similar = AsyncMock(return_value=None)
        update = _update()
        update.message.text = "Daft Punk - One More Time"
        await bot.handle_text(update, context)

        assert own_task.cancelled() and not wish_task.cancelled()
        assert "d2" in bot.downloads
        superseded = [c.kwargs.get("message_id") for c in context.bot.edit_message_text.call_args_list]
        assert 500 not in superseded
