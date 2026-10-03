"""
Telegram bot handlers for music search and download.
"""

import asyncio
import contextlib
import datetime
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from telegram import BotCommand, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from music_downloader.bot.keyboards import (
    build_approve_keyboard,
    build_auto_mode_keyboard,
    build_delivery_mode_keyboard,
    build_direct_search_keyboard,
    build_duplicate_keyboard,
    build_import_confirm_keyboard,
    build_import_skip_keyboard,
    build_import_track_keyboard,
    build_results_keyboard,
    build_retry_keyboard,
    build_retry_next_keyboard,
    build_spotify_keyboard,
)
from music_downloader.config import Config
from music_downloader.metadata.playlist import PlaylistResolver
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.import_repo import JobStatus, TrackStatus
from music_downloader.pipeline import Pipeline
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, ENQUEUE_FAILED, opus_bitrates_that_fit
from music_downloader.pipeline.resolve import parse_query_artist_title, synthetic_track
from music_downloader.pipeline.search import (
    build_reduced_queries,
    clean_search_title,
    extract_latin_keywords,
    has_non_latin_script,
)
from music_downloader.processor.lossless_analyzer import not_checked_display
from music_downloader.search.scorer import CHAT_SIZE_LIMIT_BYTES, PROFILE_CHAT, PROFILE_LIBRARY
from music_downloader.search.slskd_client import DownloadStatus, SearchResult

logger = logging.getLogger(__name__)

# Telegram bot API file size limit: 50 MB (one value, shared with the chat ranking profile)
TELEGRAM_FILE_LIMIT = CHAT_SIZE_LIMIT_BYTES

# Delivery modes: "library" saves to OUTPUT_DIR after approval; "chat" sends the
# track into the chat as the deliverable and saves nothing anywhere.
DELIVERY_LIBRARY = "library"
DELIVERY_CHAT = "chat"


def _escape_md(text: str) -> str:
    """Escape Markdown V1 special characters for safe display."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _retry_after_seconds(exc: RetryAfter) -> float:
    """RetryAfter.retry_after is an int today and a timedelta under PTB_TIMEDELTA."""
    value = exc.retry_after
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    return float(value)


async def _safe_edit(msg: Message, text: str, **kwargs) -> bool:
    """Edit a Telegram message, swallowing common failures.

    Waits out flood control (RetryAfter) once before giving up.
    Returns True on success, False if the edit failed (logged as warning).
    """
    for attempt in (1, 2):
        try:
            await msg.edit_text(text, **kwargs)
            return True
        except RetryAfter as exc:
            if attempt == 2:
                logger.warning(f"Telegram edit still flood-limited after waiting: {exc}")
                return False
            logger.warning(f"Telegram flood control on edit, retrying in {exc.retry_after}s")
            await asyncio.sleep(_retry_after_seconds(exc) + 0.5)
        except BadRequest as exc:
            logger.warning(f"Telegram edit failed (BadRequest): {exc}")
            return False
        except TimedOut:
            logger.warning("Telegram edit timed out")
            return False
        except NetworkError as exc:
            logger.warning(f"Telegram edit network error: {exc}")
            return False
    return False


async def _safe_query_edit(query, text: str, **kwargs) -> bool:
    """Edit a callback query message, swallowing transient Telegram errors.

    Waits out flood control (RetryAfter) once before giving up.
    """
    for attempt in (1, 2):
        try:
            await query.edit_message_text(text, **kwargs)
            return True
        except RetryAfter as exc:
            if attempt == 2:
                logger.warning(f"Telegram query edit still flood-limited after waiting: {exc}")
                return False
            logger.warning(f"Telegram flood control on query edit, retrying in {exc.retry_after}s")
            await asyncio.sleep(_retry_after_seconds(exc) + 0.5)
        except BadRequest as exc:
            logger.warning(f"Telegram query edit failed (BadRequest): {exc}")
            return False
        except TimedOut:
            logger.warning("Telegram query edit timed out")
            return False
        except NetworkError as exc:
            logger.warning(f"Telegram query edit network error: {exc}")
            return False
    return False


@dataclass
class PendingSearch:
    """Holds state for an active search session."""

    query: str
    track: TrackInfo | None = None
    results: list[SearchResult] = field(default_factory=list)
    message_id: int | None = None
    page: int = 0
    # Unique id binding result keyboards to this search; stale buttons from an
    # earlier search must never resolve against a newer result list.
    search_id: str = ""


@dataclass
class PendingDownload:
    """Tracks a single file download waiting for approval."""

    track: TrackInfo
    result: SearchResult
    chat_id: int
    source_path: str | None = None  # Path in /downloads
    status_message_id: int | None = None
    approval_message_id: int | None = None  # Message with approve/reject buttons
    result_index: int = 0  # Position in ranked results list
    search_id: str = ""  # Search this download came from (see PendingSearch)
    # Who started it: TELEGRAM_CHAT_DELIVERY_USERS is keyed by user id, which
    # differs from the chat id outside private chats.
    user_id: int | None = None


def _component(name: str) -> property:
    """A pipeline component exposed on the bot, so assigning ``bot.slskd`` replaces it in the pipeline too."""
    return property(
        lambda self: getattr(self.pipeline, name),
        lambda self, value: setattr(self.pipeline, name, value),
    )


class MusicBot:
    """Telegram bot for music discovery and download."""

    def __init__(self, config: Config, pipeline: Pipeline | None = None):
        self.config = config
        # Everything that is not Telegram: Spotify, slskd, ranking, files, repos.
        self.pipeline = pipeline or Pipeline(config)
        # Per-chat auto-mode cache; the durable value lives in chat_settings
        # (config.auto_mode is only the default for chats that never toggled).
        self._auto_mode_cache: dict[int, bool] = {}
        # Per-chat stored delivery mode cache (None = never set, env list decides).
        self._delivery_cache: dict[int, str | None] = {}

        # Per-user pending searches (chat_id -> PendingSearch)
        self.pending: dict[int, PendingSearch] = {}

        # Active downloads keyed by short unique ID
        # download_id -> PendingDownload
        self.downloads: dict[str, PendingDownload] = {}

        # Per-chat Spotify candidates when multiple tracks match (chat_id -> list[TrackInfo])
        self._spotify_candidates: dict[int, list[TrackInfo]] = {}
        # Current page for Spotify browsing (chat_id -> page)
        self._spotify_page: dict[int, int] = {}

        # Awaiting direct search metadata: chat_id -> original query
        self._awaiting_direct_metadata: dict[int, str] = {}

        # Per-chat cancellation: generation counter bumped on each new text message.
        # Running search/download flows check their generation against the current
        # value and abort silently when superseded by a newer request.
        self._chat_generation: dict[int, int] = {}
        # Background tasks (downloads) tracked per chat for cancellation.
        self._active_tasks: dict[int, set[asyncio.Task]] = {}
        # Orphan-sweep loop handle; owned here and cancelled on shutdown.
        self._sweep_task: asyncio.Task | None = None

        # Active import tracking (chat_id -> job_id)
        self._active_import: dict[int, int] = {}
        # Separate pending search state for import flows (avoids clobbering self.pending)
        self._import_pending: dict[int, PendingSearch] = {}
        # Import runs unattended for these chats (chosen on the confirm keyboard)
        self._import_auto: dict[int, bool] = {}
        # Who started the import in each chat (decides the env-list delivery default)
        self._import_user: dict[int, int] = {}

        # A restart cannot leave a genuinely running import behind, but it does
        # leave pending/active rows that would block /import for that chat forever.
        stale_jobs = self.import_repo.cancel_stale_jobs()
        if stale_jobs:
            logger.warning("Cancelled %d stale import job(s) left over from a previous run", stale_jobs)

    # The pipeline's components, reachable (and replaceable) through the bot:
    # /status, /history and the import flow read them here.
    spotify = _component("spotify")
    slskd = _component("slskd")
    scorer = _component("scorer")
    processor = _component("processor")
    db = _component("db")
    history_repo = _component("history_repo")
    import_repo = _component("import_repo")
    settings_repo = _component("settings_repo")
    playlist_resolver = _component("playlist_resolver")

    def _profile(self, chat_id: int | None, user_id: int | None = None) -> str:
        """Ranking profile for this chat: chat delivery weighs quality against size."""
        chat = chat_id is not None and self._is_chat_delivery(chat_id, user_id)
        return PROFILE_CHAT if chat else PROFILE_LIBRARY

    def _is_auto(self, chat_id: int) -> bool:
        """Whether auto-download is on for this chat (persisted; config is the default)."""
        if chat_id not in self._auto_mode_cache:
            stored = self.settings_repo.get_auto_mode(chat_id)
            self._auto_mode_cache[chat_id] = self.config.auto_mode if stored is None else stored
        return self._auto_mode_cache[chat_id]

    def _set_auto(self, chat_id: int, enabled: bool) -> None:
        self._auto_mode_cache[chat_id] = enabled
        self.settings_repo.set_auto_mode(chat_id, enabled)

    def _delivery_mode(self, chat_id: int, user_id: int | None = None) -> str:
        """Delivery mode for this chat: stored value, else the env user list, else library.

        ``user_id`` is the user who started the operation; outside private chats
        it, not the chat id, is what the env list matches.
        """
        if self._is_locked_to_chat(chat_id, user_id):
            return DELIVERY_CHAT
        if chat_id not in self._delivery_cache:
            self._delivery_cache[chat_id] = self.settings_repo.get_delivery_mode(chat_id)
        stored = self._delivery_cache[chat_id]
        if stored in (DELIVERY_LIBRARY, DELIVERY_CHAT):
            return stored
        return DELIVERY_LIBRARY

    def _is_locked_to_chat(self, chat_id: int, user_id: int | None = None) -> bool:
        """Accounts in TELEGRAM_CHAT_DELIVERY_USERS always get chat delivery.

        The list is the owner's guarantee that those accounts never write to the
        library: /deliver cannot switch them, and a stored value does not count.
        """
        users = self.config.telegram_chat_delivery_users
        return chat_id in users or (user_id is not None and user_id in users)

    def _is_chat_delivery(self, chat_id: int, user_id: int | None = None) -> bool:
        return self._delivery_mode(chat_id, user_id) == DELIVERY_CHAT

    def _set_delivery(self, chat_id: int, mode: str) -> None:
        self._delivery_cache[chat_id] = mode
        self.settings_repo.set_delivery_mode(chat_id, mode, auto_mode=self._is_auto(chat_id))

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to use the bot (fail-closed)."""
        if not self.config.telegram_allowed_users:
            return False
        return user_id in self.config.telegram_allowed_users

    async def _check_auth(self, update: Update) -> bool:
        """Check authorization and send a message if denied."""
        user = update.effective_user
        if user is not None and self._is_authorized(user.id):
            return True
        message = update.effective_message
        if message is not None and user is not None:
            # Include the numeric ID so a self-hoster can copy it straight
            # into TELEGRAM_ALLOWED_USERS instead of hunting for it.
            await message.reply_text(f"You are not authorized to use this bot.\nYour Telegram user ID: {user.id}")
        return False

    # =========================================================================
    # CANCELLATION
    # =========================================================================

    def _cancel_chat_operations(self, chat_id: int) -> bool:
        """Cancel all active operations for a chat.

        Bumps the generation counter (signals running search flows to abort)
        and cancels tracked background tasks (downloads).

        Returns True if something was actually cancelled.
        """
        had_work = bool(
            self.pending.get(chat_id) or self._spotify_candidates.get(chat_id) or self._active_tasks.get(chat_id)
        )

        self._chat_generation[chat_id] = self._chat_generation.get(chat_id, 0) + 1

        for task in self._active_tasks.pop(chat_id, set()):
            task.cancel()

        self.pending.pop(chat_id, None)
        self._import_pending.pop(chat_id, None)
        self._import_auto.pop(chat_id, None)
        self._import_user.pop(chat_id, None)
        self._spotify_candidates.pop(chat_id, None)
        self._spotify_page.pop(chat_id, None)
        self._awaiting_direct_metadata.pop(chat_id, None)

        stale_ids = [k for k, v in self.downloads.items() if v.chat_id == chat_id]
        for dl_id in stale_ids:
            del self.downloads[dl_id]

        return had_work

    def _is_stale(self, chat_id: int, generation: int) -> bool:
        """True when *generation* has been superseded by a newer request."""
        return self._chat_generation.get(chat_id, 0) != generation

    def _track_task(self, chat_id: int, task: asyncio.Task):
        """Register a background task for cancellation tracking."""
        self._active_tasks.setdefault(chat_id, set()).add(task)

        def _on_done(t: asyncio.Task) -> None:
            tasks = self._active_tasks.get(chat_id)
            if tasks is not None:
                tasks.discard(t)
                if not tasks:
                    del self._active_tasks[chat_id]

        task.add_done_callback(_on_done)

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        if not await self._check_auth(update):
            return

        await update.message.reply_text(
            "Send me a song name (e.g., `Nancy Sinatra Bang Bang`) "
            "and I'll find the best copy on Soulseek: lossless first, or the best quality "
            "for its size in chat delivery.\n\n"
            "Commands:\n"
            "/import — Import a Spotify playlist or album\n"
            "/cancel — Cancel the active import or search\n"
            "/auto — Toggle auto-download mode\n"
            "/deliver — Toggle chat delivery (send tracks here instead of saving)\n"
            "/status — Show active downloads\n"
            "/history — Recent downloads\n"
            "/help — Show this message",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        if not await self._check_auth(update):
            return
        await self.cmd_start(update, context)

    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /auto command — toggle auto-download mode."""
        if not await self._check_auth(update):
            return

        current = self._is_auto(update.effective_chat.id)
        mode_str = "ON" if current else "OFF"
        await update.message.reply_text(
            f"Auto-download mode is currently: *{mode_str}*\n\n"
            "When ON, the best match is downloaded and saved to your library "
            "automatically — no picking, no approval step. The setting survives restarts.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_auto_mode_keyboard(current),
        )

    async def cmd_deliver(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /deliver command — switch between library and chat delivery."""
        if not await self._check_auth(update):
            return

        if self._is_locked_to_chat(update.effective_chat.id, update.effective_user.id):
            await update.message.reply_text(
                "Delivery mode for this account: *Chat* (fixed)\n\n"
                "Tracks are sent here and nothing is saved anywhere. Over 50 MB they are converted to Opus to fit. "
                "This account cannot switch to the library.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        current = self._delivery_mode(update.effective_chat.id, update.effective_user.id)
        mode_str = "Chat" if current == DELIVERY_CHAT else "Library"
        await update.message.reply_text(
            f"Delivery mode for this chat: *{mode_str}*\n\n"
            "Library: the track is saved to the music library (after you approve the preview, unless /auto is on).\n"
            "Chat: the track is sent here and nothing is saved anywhere. Over 50 MB it is "
            "converted to Opus to fit. The setting survives restarts.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_delivery_mode_keyboard(current),
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command — show active searches and downloads."""
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        lines = []

        # Only this chat's activity: /status must not leak other users' work.
        chat_searches = [p for cid, p in self.pending.items() if cid == chat_id]
        if chat_searches:
            lines.append("*Active searches:*\n")
            for pending in chat_searches:
                if pending.track:
                    lines.append(f"• {_escape_md(pending.track.artist)} - {_escape_md(pending.track.title)}")
                else:
                    # Search still resolving on Spotify (or awaiting a pick)
                    lines.append(f"• {_escape_md(pending.query)}")

        chat_downloads = [d for d in self.downloads.values() if d.chat_id == chat_id]
        if chat_downloads:
            lines.append("\n*Active downloads:*\n")
            for dl in chat_downloads:
                lines.append(
                    f"• {_escape_md(dl.track.artist)} - {_escape_md(dl.track.title)} ({_escape_md(dl.result.basename)})"
                )

        job_id = self._active_import.get(chat_id)
        if job_id:
            completed, failed, skipped, total = await asyncio.to_thread(self.import_repo.get_job_progress, job_id)
            mode = "auto-save" if self._import_auto.get(chat_id) else "review"
            lines.append(
                f"\n*Import ({mode}):* {completed + failed + skipped}/{total} processed — "
                f"✅ {completed} · ❌ {failed} · ⏭ {skipped}"
            )

        if not lines:
            await update.message.reply_text("No active searches or downloads.")
            return

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history command — show recent downloads."""
        if not await self._check_auth(update):
            return

        records = await asyncio.to_thread(self.history_repo.get_recent, 10)

        if not records:
            await update.message.reply_text("No downloads yet.")
            return

        lines = ["*Recent downloads:*\n"]
        for entry in records:
            icon = {"success": "✅", "delivered": "\U0001f4e8", "rejected": "🚫"}.get(entry.status, "❌")
            lines.append(f"{icon} `{entry.filename}`")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    # =========================================================================
    # TEXT MESSAGE HANDLER (song search)
    # =========================================================================

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle free-text messages — treat as song search queries."""
        if not await self._check_auth(update):
            return

        query = update.message.text.strip()
        if not query:
            return

        chat_id = update.effective_chat.id

        # Check if we're awaiting "Artist - Title" for a direct Soulseek search
        if chat_id in self._awaiting_direct_metadata:
            search_query = self._awaiting_direct_metadata.pop(chat_id)
            generation = self._chat_generation.get(chat_id, 0)

            if " - " in query:
                artist, title = query.split(" - ", 1)
            else:
                artist, title = query, search_query

            display_track = synthetic_track(artist.strip(), title.strip())

            searching_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f50d Searching slskd for: `{search_query}`\n"
                f"Saving as: *{display_track.artist} - {display_track.title}*",
                parse_mode=ParseMode.MARKDOWN,
            )

            await self._do_direct_slskd_search(
                context,
                chat_id,
                search_query,
                searching_msg,
                generation,
                display_track=display_track,
                user_id=update.effective_user.id,
            )
            return

        # Cancel any in-flight search / download for this chat immediately —
        # but first snapshot their status messages so they can be marked as
        # superseded instead of sitting frozen at "Searching…" forever.
        stale_message_ids = []
        old_pending = self.pending.get(chat_id)
        if old_pending and old_pending.message_id:
            stale_message_ids.append(old_pending.message_id)
        stale_message_ids += [
            dl.status_message_id
            for dl in self.downloads.values()
            if dl.chat_id == chat_id and dl.status_message_id and dl.source_path is None
        ]
        self._cancel_chat_operations(chat_id)
        generation = self._chat_generation[chat_id]
        for message_id in stale_message_ids:
            with contextlib.suppress(Exception):
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text="⏹ Superseded by a newer request"
                )

        # Step 0: Check for similar files already in the library (meaningless
        # in chat delivery, which never uses the library)
        similar = (
            None if self._is_chat_delivery(chat_id, update.effective_user.id) else self.pipeline.find_similar(query)
        )
        if similar:
            existing_list = "\n".join(f"• `{f}`" for f in similar[:5])
            await update.message.reply_text(
                f"⚠️ *Similar files already in library:*\n\n{existing_list}\n\nContinue searching anyway?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_duplicate_keyboard(),
            )
            self.pending[chat_id] = PendingSearch(query=query, track=None)
            return

        await self._do_search(update, context, query, generation)

    async def _do_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, generation: int):
        """Resolve metadata via Spotify, then proceed to slskd search."""
        chat_id = update.effective_chat.id

        searching_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Looking up: `{query}`",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            unique_tracks = self.pipeline.resolve(query)
            if self._is_stale(chat_id, generation):
                return

            if not unique_tracks:
                await _safe_edit(
                    searching_msg,
                    f"Could not find `{query}` on Spotify.\nYou can search Soulseek directly instead.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_direct_search_keyboard(),
                )
                # Store query for direct search callback
                self.pending[chat_id] = PendingSearch(query=query, track=None)
                return

            if len(unique_tracks) == 1:
                await self._do_slskd_search(
                    context, chat_id, unique_tracks[0], searching_msg, generation, user_id=update.effective_user.id
                )
                return

            self._spotify_candidates[chat_id] = unique_tracks
            self._spotify_page[chat_id] = 0
            self.pending[chat_id] = PendingSearch(query=query, track=None)
            await _safe_edit(
                searching_msg,
                self._format_spotify_results(unique_tracks, page=0),
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=build_spotify_keyboard(unique_tracks, page=0),
            )

        except Exception:
            logger.exception(f"Unexpected error in _do_search for: {query}")
            self._spotify_candidates.pop(chat_id, None)
            self._spotify_page.pop(chat_id, None)
            await _safe_edit(searching_msg, "Something went wrong. Please try again.")

    async def _do_slskd_search(
        self, context, chat_id: int, track: TrackInfo, searching_msg, generation: int, user_id: int | None = None
    ):
        """Search slskd for a resolved Spotify track."""
        try:
            await _safe_edit(
                searching_msg,
                f"🎵 *{track.artist} - {track.title}*\n"
                f"Album: {track.album} ({track.year})\n"
                f"Duration: {track.duration_display}\n\n"
                f"Searching slskd...",
                parse_mode=ParseMode.MARKDOWN,
            )

            clean_title = clean_search_title(track.title)
            search_query = f"{track.artist} {clean_title}"
            ranked = await self.pipeline.search(search_query, track, self._profile(chat_id, user_id))
            if self._is_stale(chat_id, generation):
                return

            # Fallback 2: title-only search
            if not ranked:
                if self._is_stale(chat_id, generation):
                    return
                logger.info(
                    "No results for '%s', retrying with title-only: '%s'",
                    search_query,
                    clean_title,
                )
                await _safe_edit(
                    searching_msg,
                    f"🎵 *{track.artist} - {track.title}*\n\n"
                    f"No results with full query — retrying with song title only…",
                    parse_mode=ParseMode.MARKDOWN,
                )
                ranked = await self.pipeline.search(clean_title, track, self._profile(chat_id, user_id))
                if self._is_stale(chat_id, generation):
                    return

            # Fallback 3: keyword reduction + album year
            if not ranked and not has_non_latin_script(clean_title):
                reduced_queries = build_reduced_queries(clean_title, track.year)
                if reduced_queries:
                    if self._is_stale(chat_id, generation):
                        return
                    logger.info(
                        "No results for title-only '%s', trying keyword reduction + year",
                        clean_title,
                    )
                    await _safe_edit(
                        searching_msg,
                        f"🎵 *{track.artist} - {track.title}*\n\n"
                        f"Still no results — trying keyword variations with year…",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    for fallback_query in reduced_queries:
                        if self._is_stale(chat_id, generation):
                            return
                        ranked = await self.pipeline.search(fallback_query, track, self._profile(chat_id, user_id))
                        if ranked:
                            logger.info("Keyword-reduction fallback hit: '%s'", fallback_query)
                            break

            # Fallback 4: artist + Latin keywords
            if not ranked:
                if self._is_stale(chat_id, generation):
                    return
                latin_kw = extract_latin_keywords(clean_title)
                if latin_kw:
                    fb4_query = f"{track.artist} {' '.join(latin_kw)}"
                else:
                    fb4_query = track.artist
                logger.info(
                    "Trying artist + Latin keywords fallback: '%s'",
                    fb4_query,
                )
                await _safe_edit(
                    searching_msg,
                    f"🎵 *{track.artist} - {track.title}*\n\nStill no results — trying artist + keyword search…",
                    parse_mode=ParseMode.MARKDOWN,
                )
                ranked = await self.pipeline.search(
                    fb4_query, track, self._profile(chat_id, user_id), max_duration_diff=120, response_limit=150
                )
                if self._is_stale(chat_id, generation):
                    return

                if ranked:
                    logger.info("Artist-keyword fallback hit: '%s'", fb4_query)

            if self._is_stale(chat_id, generation):
                return

            if not ranked:
                # Not a dead end: offer the manual escape hatch. The scorer
                # also filters live/remix/duration mismatches, so "no results"
                # regularly means "nothing survived the filters".
                self.pending[chat_id] = PendingSearch(query=f"{track.artist} {clean_title}", track=None)
                await _safe_edit(
                    searching_msg,
                    f"🎵 *{track.artist} - {track.title}* ({track.duration_display})\n\n"
                    f"No results found on Soulseek matching this track.\n"
                    f"Try a different query, or search Soulseek directly "
                    f"(skips the duration and live/remix filters).",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_direct_search_keyboard(),
                )
                return

            search_id = uuid4().hex[:8]
            self.pending[chat_id] = PendingSearch(
                query=f"{track.artist} {track.title}",
                track=track,
                results=ranked,
                message_id=searching_msg.message_id,
                search_id=search_id,
            )

            results_text = self._format_results(track, ranked, page=0, page_size=self.config.max_results)

            if self._is_auto(chat_id):
                # Auto-mode: no picker, no approval — take the top-ranked match.
                best = ranked[0]
                await _safe_edit(
                    searching_msg,
                    f"{results_text}\n\n\U0001f916 *Auto-mode:* downloading best match #1…",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await self._launch_download(context, chat_id, track, best, 0, search_id, user_id=user_id)
                return

            await _safe_edit(
                searching_msg,
                results_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_results_keyboard(
                    ranked, page=0, page_size=self.config.max_results, search_id=search_id
                ),
            )

        except Exception:
            logger.exception(f"Unexpected error in _do_slskd_search for: {track.artist} - {track.title}")
            self.pending.pop(chat_id, None)
            await _safe_edit(
                searching_msg,
                "Something went wrong during the search. Please try again.",
            )

    # =========================================================================
    # CALLBACK QUERY HANDLER (button presses)
    # =========================================================================

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses."""
        query = update.callback_query
        with contextlib.suppress(BadRequest):
            await query.answer()

        if not self._is_authorized(query.from_user.id):
            return

        chat_id = update.effective_chat.id
        data = query.data

        # Dispatch callbacks by prefix
        prefix = data.split(":", 1)[0]
        handler = {
            "direct": self._handle_direct_search,
            "ic": self._handle_import_callback,
            "ix": self._handle_import_callback,
            "ia": self._handle_import_callback,
            "ir": self._handle_import_callback,
            "is": self._handle_import_callback,
            "retry": self._handle_retry,
            "next": self._handle_next_result,
            "dup": self._handle_duplicate_response,
            "sp_page": self._handle_spotify_page,
            "sp": self._handle_spotify_selection,
            "dl_page": self._handle_results_page,
            "dl": self._handle_download_selection,
            "approve": self._handle_approval,
            "reject": self._handle_approval,
        }.get(prefix)

        if handler:
            await handler(update, context, chat_id, data)
            return

        # Delivery-mode toggle
        if data in (f"deliver:{DELIVERY_LIBRARY}", f"deliver:{DELIVERY_CHAT}"):
            if self._is_locked_to_chat(chat_id, query.from_user.id):
                await query.edit_message_text(
                    "Delivery mode: *Chat* (fixed for this account)",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            mode = data.split(":", 1)[1]
            self._set_delivery(chat_id, mode)
            mode_str = "Chat" if mode == DELIVERY_CHAT else "Library"
            await query.edit_message_text(
                f"Delivery mode: *{mode_str}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Auto-mode toggle (inline, no separate handler needed)
        if data.startswith("auto:"):
            enabled = data == "auto:on"
            self._set_auto(chat_id, enabled)
            mode_str = "ON" if enabled else "OFF"
            await query.edit_message_text(
                f"Auto-download mode: *{mode_str}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    async def _handle_duplicate_response(self, update, context, chat_id: int, data: str):
        """Handle Continue/Cancel response to duplicate detection."""
        query = update.callback_query
        action = data.split(":", 1)[1]

        pending = self.pending.pop(chat_id, None)

        if action == "cancel" or not pending:
            await query.edit_message_text("Cancelled.")
            return

        await query.edit_message_text(
            f"Continuing with search: `{pending.query}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        generation = self._chat_generation.get(chat_id, 0)
        await self._do_search(update, context, pending.query, generation)

    async def _handle_spotify_page(self, update, context, chat_id: int, data: str):
        """Handle Spotify page navigation (◀️ / ▶️)."""
        query = update.callback_query
        candidates = self._spotify_candidates.get(chat_id)
        if not candidates:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            return

        self._spotify_page[chat_id] = page
        await query.edit_message_text(
            self._format_spotify_results(candidates, page=page),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=build_spotify_keyboard(candidates, page=page),
        )

    async def _handle_spotify_selection(self, update, context, chat_id: int, data: str):
        """Handle Spotify track selection from multiple results."""
        query = update.callback_query
        action = data.split(":", 1)[1]

        candidates = self._spotify_candidates.pop(chat_id, None)
        self._spotify_page.pop(chat_id, None)

        if action == "cancel" or not candidates:
            await query.edit_message_text("Cancelled.")
            return

        try:
            index = int(action)
        except ValueError:
            return

        if index >= len(candidates):
            return

        track = candidates[index]
        await query.edit_message_text(
            f"Selected: *{track.artist} - {track.title}* ({track.duration_display})",
            parse_mode=ParseMode.MARKDOWN,
        )

        searching_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 Searching slskd...",
            parse_mode=ParseMode.MARKDOWN,
        )
        generation = self._chat_generation.get(chat_id, 0)
        await self._do_slskd_search(context, chat_id, track, searching_msg, generation, user_id=query.from_user.id)

    @staticmethod
    def _split_search_callback(data: str) -> tuple[str, str]:
        """Split ``dl:<search_id>:<action>`` (or legacy ``dl:<action>``) into (search_id, action)."""
        parts = data.split(":", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
        return "", parts[1]

    async def _handle_results_page(self, update, context, chat_id: int, data: str):
        """Handle slskd results page navigation (◀️ / ▶️)."""
        query = update.callback_query
        pending = self.pending.get(chat_id)
        if not pending or not pending.track:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        search_id, action = self._split_search_callback(data)
        if search_id != pending.search_id:
            await _safe_query_edit(query, "⌛ These results are out of date. Send a new search.")
            return

        try:
            page = int(action)
        except ValueError:
            return

        pending.page = page
        results_text = self._format_results(
            pending.track,
            pending.results,
            page=page,
            page_size=self.config.max_results,
        )
        await query.edit_message_text(
            results_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_results_keyboard(
                pending.results, page=page, page_size=self.config.max_results, search_id=pending.search_id
            ),
        )

    async def _handle_download_selection(self, update, context, chat_id: int, data: str):
        """Handle when user picks a file to download from results."""
        query = update.callback_query
        pending = self.pending.get(chat_id)
        if not pending:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        search_id, action = self._split_search_callback(data)
        if search_id != pending.search_id:
            # Button belongs to an older search; acting on it would download
            # from the CURRENT result list under the old labels.
            await _safe_query_edit(query, "⌛ These results are out of date. Send a new search.")
            return

        if action == "cancel":
            del self.pending[chat_id]
            await query.edit_message_text("Cancelled.")
            return

        if action == "auto":
            index = 0
        else:
            try:
                index = int(action)
            except ValueError:
                return

        if index >= len(pending.results):
            await _safe_query_edit(query, "⌛ These results are out of date. Send a new search.")
            return

        result = pending.results[index]
        track = pending.track
        await self._launch_download(
            context, chat_id, track, result, index, pending.search_id, update=update, user_id=query.from_user.id
        )

    async def _launch_download(
        self,
        context,
        chat_id: int,
        track: TrackInfo,
        result: SearchResult,
        index: int,
        search_id: str,
        update=None,
        user_id: int | None = None,
    ):
        """Send the download status message and start the download task."""
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⬇️ *Downloading #{index + 1}...*\n"
                f"{track.artist} - {track.title}\n"
                f"From: `{result.username}`\n"
                f"File: `{result.basename}`"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )

        task = context.application.create_task(
            self._do_download(context, chat_id, track, result, status_msg, index, search_id, user_id=user_id),
            update=update,
        )
        self._track_task(chat_id, task)

    # =========================================================================
    # DOWNLOAD + PREVIEW + APPROVAL
    # =========================================================================

    def _next_dl_id(self) -> str:
        """Generate a short unique download ID.

        Random rather than sequential: sequential IDs restart at 1 after a
        restart, so a stale approve/reject button embedded in an old Telegram
        message would act on an unrelated new download.
        """
        return uuid4().hex[:8]

    def _has_next_result(self, chat_id: int, current_index: int) -> bool:
        pending = self.pending.get(chat_id)
        return pending is not None and current_index + 1 < len(pending.results)

    async def _do_download(
        self,
        context,
        chat_id: int,
        track: TrackInfo,
        result: SearchResult,
        status_msg,
        result_index: int = 0,
        search_id: str = "",
        user_id: int | None = None,
    ):
        """Download a file, send it to Telegram for preview, and ask for approval."""
        dl_id = self._next_dl_id()
        label = f"#{result_index + 1}"

        try:
            outcome = await self.pipeline.fetch(
                result,
                self._make_progress_reporter(
                    status_msg,
                    f"⬇️ *Downloading {label}...*\n{track.artist} - {track.title}\nFrom: `{result.username}`",
                ),
            )
            if outcome.error == ENQUEUE_FAILED:
                pending_dl = PendingDownload(
                    track=track,
                    result=result,
                    chat_id=chat_id,
                    status_message_id=status_msg.message_id,
                    result_index=result_index,
                    search_id=search_id,
                    user_id=user_id,
                )
                self.downloads[dl_id] = pending_dl
                has_next = self._has_next_result(chat_id, result_index)
                await status_msg.edit_text(
                    f"❌ Failed to enqueue download from `{result.username}`.\nThe user might be offline.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_retry_next_keyboard(dl_id) if has_next else build_retry_keyboard(dl_id),
                )
                return

            if outcome.error == DOWNLOAD_FAILED:
                state = outcome.state
                pending_dl = PendingDownload(
                    track=track,
                    result=result,
                    chat_id=chat_id,
                    status_message_id=status_msg.message_id,
                    result_index=result_index,
                    search_id=search_id,
                    user_id=user_id,
                )
                self.downloads[dl_id] = pending_dl
                has_next = self._has_next_result(chat_id, result_index)
                await status_msg.edit_text(
                    f"❌ Download failed: {state}\nFile: `{result.basename}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_retry_next_keyboard(dl_id) if has_next else build_retry_keyboard(dl_id),
                )
                await self.pipeline.record_history(track, result, "failed")
                return

            if not outcome.ok:
                await status_msg.edit_text(
                    "❌ Downloaded file not found on disk.\nCheck DOWNLOAD_DIR configuration.",
                )
                await self.pipeline.record_history(track, result, "file_not_found")
                return

            source_path = outcome.path
            verdict = outcome.verdict

            pending_dl = PendingDownload(
                track=track,
                result=result,
                chat_id=chat_id,
                source_path=source_path,
                status_message_id=status_msg.message_id,
                result_index=result_index,
                search_id=search_id,
                user_id=user_id,
            )
            self.downloads[dl_id] = pending_dl

            quality_line = f"Quality: {result.quality_display} | {result.duration_display}"
            if verdict:
                quality_line += f"\n{verdict.display}"
            elif result.is_lossless:
                quality_line += f"\n{not_checked_display(result.extension)}"

            if self._is_chat_delivery(chat_id, user_id):
                # Chat delivery decides WHERE the track goes (auto-mode only
                # decided whether to ask first): straight into the chat.
                await _safe_edit(
                    status_msg,
                    f"✅ *{label} Downloaded!* Sending to this chat...\n`{result.basename}`\n{quality_line}",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await self._deliver_download(context, chat_id, dl_id, pending_dl, status_msg, quality_line, label)
                return

            if self._is_auto(chat_id):
                await self._auto_save(chat_id, dl_id, pending_dl, status_msg, quality_line, label)
                return

            await status_msg.edit_text(
                f"✅ *{label} Downloaded!* Sending preview...\n`{result.basename}`\n{quality_line}",
                parse_mode=ParseMode.MARKDOWN,
            )

            file_size = os.path.getsize(source_path) if os.path.isfile(source_path) else 0
            caption = f"{label} {quality_line}\nSave to library?"

            if file_size > TELEGRAM_FILE_LIMIT:
                await self._send_large_file(
                    context,
                    chat_id,
                    track,
                    result,
                    source_path,
                    file_size,
                    quality_line,
                    label,
                    dl_id,
                )
            else:
                target_name = self.pipeline.target_filename(track, result.extension)
                try:
                    with open(source_path, "rb") as f:
                        sent = await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            filename=target_name,
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration_secs,
                            caption=caption,
                            reply_markup=build_approve_keyboard(dl_id),
                        )
                except BadRequest:
                    logger.info("send_audio failed, falling back to send_document for %s", result.basename)
                    with open(source_path, "rb") as f:
                        sent = await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=target_name,
                            caption=caption,
                            reply_markup=build_approve_keyboard(dl_id),
                        )
                if dl_id in self.downloads:
                    self.downloads[dl_id].approval_message_id = sent.message_id

        except asyncio.CancelledError:
            logger.info("Download cancelled for %s", result.basename)
            self.downloads.pop(dl_id, None)
            raise
        except Exception:
            logger.exception(f"Download failed for {result.basename}")
            await status_msg.edit_text(
                f"❌ Error downloading `{result.basename}`. Check logs.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _auto_save(
        self, chat_id: int, dl_id: str, pending_dl: PendingDownload, status_msg, quality_line: str, label: str
    ):
        """Save a downloaded file straight to the library (auto-mode: no preview, no approval)."""
        track = pending_dl.track
        result = pending_dl.result

        target_path = await self.pipeline.save(pending_dl.source_path, track, result)
        # Popped only after saving: the entry keeps the source protected
        # from the orphan sweep until it is gone.
        self.downloads.pop(dl_id, None)
        if target_path:
            target_name = os.path.basename(target_path)
            await _safe_edit(
                status_msg,
                f"✅ *{label} Auto-saved:* `{target_name}`\n{quality_line}",
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info(f"Auto-saved: {target_name}")
        else:
            await _safe_edit(
                status_msg,
                f"❌ {label} Downloaded but failed to save. Check logs.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _deliver_download(
        self, context, chat_id: int, dl_id: str, pending_dl: PendingDownload, status_msg, quality_line: str, label: str
    ) -> bool:
        """Chat delivery: send the downloaded track into the chat, save nothing, delete the source.

        Returns whether the track was sent.
        """
        track = pending_dl.track
        result = pending_dl.result
        source_path = pending_dl.source_path

        outcome, note = await self._send_to_chat(
            context, chat_id, track, result, source_path, f"{label} {quality_line}"
        )
        if outcome == "sent":
            await self.pipeline.discard(source_path)
            # Popped only after cleanup: the entry keeps the source protected
            # from the orphan sweep until it is gone.
            self.downloads.pop(dl_id, None)
            await _safe_edit(
                status_msg,
                f"✅ *{label} Sent:* `{note}`\n{quality_line}",
                parse_mode=ParseMode.MARKDOWN,
            )
            await self.pipeline.record_history(track, result, "delivered", filename=note)
            logger.info(f"Delivered to chat: {note}")
            return True

        # Keep the source for the orphan sweep (it is no longer protected) and
        # keep the entry so Retry / Try next still work.
        pending_dl.source_path = None
        self.downloads[dl_id] = pending_dl
        has_next = self._has_next_result(chat_id, pending_dl.result_index)
        await _safe_edit(
            status_msg,
            f"❌ {label} {note}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_retry_next_keyboard(dl_id) if has_next else build_retry_keyboard(dl_id),
        )
        await self.pipeline.record_history(track, result, outcome)
        return False

    async def _send_to_chat(
        self, context, chat_id: int, track: TrackInfo, result: SearchResult, source_path: str, caption: str
    ) -> tuple[str, str]:
        """Send a finished download into the chat as the deliverable (chat delivery).

        At or under the limit the file goes as-is (with Spotify artwork embedded
        first, best effort). Over it, the file is converted to Opus at the
        highest bitrate whose estimate fits, retrying once one step lower.

        Returns (outcome, note): outcome is "sent" (note = sent filename) or
        "too_large" / "convert_failed" / "send_failed" (note = Markdown-safe reason).
        """
        size = os.path.getsize(source_path) if os.path.isfile(source_path) else 0
        if size <= TELEGRAM_FILE_LIMIT:
            # The source is deleted right after sending, so tagging it is free.
            await self.pipeline.embed_artwork(source_path, track)
            size = os.path.getsize(source_path) if os.path.isfile(source_path) else 0
        if size <= TELEGRAM_FILE_LIMIT:
            target_name = self.pipeline.target_filename(track, result.extension)
            error = await self._send_audio_file(context, chat_id, source_path, target_name, track, caption)
            return ("sent", target_name) if error is None else ("send_failed", error)

        original = f"original {size / (1024 * 1024):.0f} MB {result.extension.upper()}"
        too_large = ("too_large", f"Could not fit this track under 50 MB, even converted to Opus ({original}).")
        for kbps in opus_bitrates_that_fit(track.duration_secs or result.length or 0):
            ogg_path = await self.pipeline.convert_to_opus(source_path, kbps)
            if not ogg_path:
                return "convert_failed", f"Could not convert this track to fit under 50 MB ({original})."
            try:
                if os.path.getsize(ogg_path) > TELEGRAM_FILE_LIMIT:
                    continue
                target_name = self.pipeline.target_filename(track, "ogg")
                converted_caption = f"{caption}\n\U0001f3a7 Converted to Opus {kbps} kbps, {original}"
                error = await self._send_audio_file(context, chat_id, ogg_path, target_name, track, converted_caption)
                return ("sent", target_name) if error is None else ("send_failed", error)
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(ogg_path)
        return too_large

    @staticmethod
    async def _send_audio_file(context, chat_id: int, path: str, filename: str, track: TrackInfo, caption: str):
        """Send *path* as audio (document on BadRequest). Returns None, or a Markdown-safe error."""
        try:
            try:
                with open(path, "rb") as f:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=f,
                        filename=filename,
                        title=track.title,
                        performer=track.artist,
                        duration=track.duration_secs,
                        caption=caption,
                    )
            except BadRequest:
                logger.info("send_audio failed, falling back to send_document for %s", filename)
                with open(path, "rb") as f:
                    await context.bot.send_document(chat_id=chat_id, document=f, filename=filename, caption=caption)
        except Exception as exc:
            logger.exception("Sending %s to chat %s failed", filename, chat_id)
            return f"Could not send the file to Telegram: {_escape_md(str(exc))}"
        return None

    async def _send_large_file(
        self,
        context,
        chat_id: int,
        track: TrackInfo,
        result: SearchResult,
        source_path: str,
        file_size: int,
        quality_line: str,
        label: str,
        dl_id: str,
    ):
        """Convert a >50 MB file to OGG and send.  Trim only as last resort.

        Strategy:
        1. Convert full song to OGG Opus (~128 kbps).
        2. If OGG ≤ 50 MB → send the full song.
        3. If OGG > 50 MB → trim to ~1 min and send that.
        """
        # Step 1: full OGG conversion
        ogg_path = await self.pipeline.convert_to_opus(source_path)

        if ogg_path:
            ogg_size = os.path.getsize(ogg_path)
            if ogg_size <= TELEGRAM_FILE_LIMIT:
                try:
                    target_name = self.pipeline.target_filename(track, "ogg")
                    caption = (
                        f"🎧 {label} Converted to OGG "
                        f"(original: {file_size / (1024 * 1024):.0f}MB {result.extension.upper()})\n"
                        f"{quality_line}\nSave to library?"
                    )
                    with open(ogg_path, "rb") as f:
                        sent = await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            filename=target_name,
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration_secs,
                            caption=caption,
                            reply_markup=build_approve_keyboard(dl_id),
                        )
                    if dl_id in self.downloads:
                        self.downloads[dl_id].approval_message_id = sent.message_id
                    return
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(ogg_path)
            else:
                # Full OGG still too large — clean up, will trim below.
                with contextlib.suppress(OSError):
                    os.unlink(ogg_path)

        # Step 2: trim to ~1 min
        preview_path = await self.pipeline.preview_clip(source_path, duration_secs=60.0)
        if not preview_path:
            logger.error("Preview creation failed for %s, cannot send to Telegram", source_path)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ {label} Could not create preview for "
                    f"{file_size / (1024 * 1024):.0f}MB file.\n"
                    f"{quality_line}\n\nSave to library anyway?"
                ),
                reply_markup=build_approve_keyboard(dl_id),
            )
            if dl_id in self.downloads:
                self.downloads[dl_id].approval_message_id = sent.message_id
            return

        try:
            preview_ext = os.path.splitext(preview_path)[1].lstrip(".")
            target_name = self.pipeline.target_filename(track, preview_ext, title=f"{track.title} (1min preview)")
            preview_caption = (
                f"🎧 {label} ~1 min preview "
                f"(full file: {file_size / (1024 * 1024):.0f}MB)\n"
                f"{quality_line}\n"
                f"Save to library?"
            )
            with open(preview_path, "rb") as f:
                sent = await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    filename=target_name,
                    title=f"{track.title} (1min preview)",
                    performer=track.artist,
                    duration=60,
                    caption=preview_caption,
                    reply_markup=build_approve_keyboard(dl_id),
                )
            if dl_id in self.downloads:
                self.downloads[dl_id].approval_message_id = sent.message_id
        finally:
            with contextlib.suppress(OSError):
                os.unlink(preview_path)

    async def _handle_approval(self, update, context, chat_id: int, data: str):
        """Handle approve/reject of a downloaded file."""
        query = update.callback_query
        action, dl_id = data.split(":", 1)

        pending_dl = self.downloads.pop(dl_id, None)
        if not pending_dl:
            await self._edit_approval_message(query, "⏹ Cancelled")
            return

        if pending_dl.chat_id != chat_id:
            self.downloads[dl_id] = pending_dl
            return

        track = pending_dl.track
        result = pending_dl.result

        if action == "approve" and pending_dl.source_path and self._is_chat_delivery(chat_id, pending_dl.user_id):
            # A library-mode preview approved after switching to chat delivery:
            # deliver it here instead of writing the library.
            self.downloads[dl_id] = pending_dl
            await self._edit_approval_message(query, "\U0001f4e8 Sending to this chat instead of saving...")
            label = f"#{pending_dl.result_index + 1}"
            status_msg = await context.bot.send_message(chat_id=chat_id, text=f"\U0001f4e8 {label} Sending...")
            quality_line = f"Quality: {result.quality_display} | {result.duration_display}"
            sent = await self._deliver_download(context, chat_id, dl_id, pending_dl, status_msg, quality_line, label)
            await self._edit_approval_message(
                query, "\U0001f4e8 Sent to this chat" if sent else "❌ Not sent, see the message below"
            )
            return

        if action == "approve":
            if pending_dl.source_path:
                target_path = await self.pipeline.save(pending_dl.source_path, track, result)
                if target_path:
                    target_name = os.path.basename(target_path)
                    await self._edit_approval_message(query, f"✅ Saved: `{target_name}`")
                    logger.info(f"Approved and saved: {target_name}")

                    # Dismiss every other pending download for this chat.
                    await self._dismiss_other_downloads(context, chat_id)
                else:
                    await self._edit_approval_message(query, "❌ Failed to save file. Check logs.")
            else:
                await self._edit_approval_message(query, "❌ Source file not found.")
                await self.pipeline.record_history(track, result, "file_not_found")

        elif action == "reject":
            self.pipeline.remove_file(pending_dl.source_path)
            await self._edit_approval_message(query, f"🚫 Rejected: {track.artist} - {track.title}")
            await self.pipeline.record_history(track, result, "rejected")
            logger.info(f"Rejected: {track.artist} - {track.title} ({result.basename})")

    async def _dismiss_other_downloads(self, context, chat_id: int):
        """Cancel all remaining pending downloads for a chat after one is approved."""
        # Remove the results keyboard so no more downloads can be started.
        pending = self.pending.pop(chat_id, None)
        if pending and pending.message_id:
            with contextlib.suppress(Exception):
                await context.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=pending.message_id,
                )

        # Dismiss other pending download approval messages and clean up files.
        stale = [(k, v) for k, v in self.downloads.items() if v.chat_id == chat_id]
        for dl_id, dl in stale:
            del self.downloads[dl_id]
            self.pipeline.remove_file(dl.source_path)
            if dl.approval_message_id:
                try:
                    await context.bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=dl.approval_message_id,
                        caption="⏹ Cancelled",
                    )
                except Exception:
                    with contextlib.suppress(Exception):
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=dl.approval_message_id,
                            text="⏹ Cancelled",
                        )

        for task in self._active_tasks.pop(chat_id, set()):
            task.cancel()

    @staticmethod
    def _make_progress_reporter(status_msg: Message, header: str) -> "Callable[[DownloadStatus], Awaitable[None]]":
        """Build a progress callback that live-edits the download status message.

        Skips edits when the rendered line hasn't changed (Telegram rejects
        no-op edits, and queued transfers can sit unchanged for minutes).
        """
        last_line = ""

        async def _report(status: DownloadStatus) -> None:
            nonlocal last_line
            state_lower = (status.state or "").lower()
            if "queue" in state_lower:
                line = "⏳ Queued at the source…"
            else:
                speed = f" · {status.average_speed / (1024 * 1024):.1f} MB/s" if status.average_speed else ""
                line = f"{status.percent_complete:.0f}%{speed}"
            if line == last_line:
                return
            last_line = line
            await _safe_edit(status_msg, f"{header}\n{line}", parse_mode=ParseMode.MARKDOWN)

        return _report

    @staticmethod
    async def _edit_approval_message(query, text: str, reply_markup=None):
        """Edit the approval message — works for both audio captions and text messages.

        Editing without reply_markup strips the buttons; pass it explicitly
        when the message must stay actionable.
        """
        try:
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception:
            with contextlib.suppress(Exception):
                await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)

    # =========================================================================
    # DIRECT SEARCH (skip Spotify)
    # =========================================================================

    async def _handle_direct_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: str):
        """Handle 'Search Soulseek directly' button — asks for Artist - Title, then searches slskd."""
        query = update.callback_query

        pending = self.pending.get(chat_id)
        if not pending:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        search_query = pending.query
        self._awaiting_direct_metadata[chat_id] = search_query

        await query.edit_message_text(
            "\U0001f3b5 How should this track be saved?\n\n"
            "Send the name as: `Artist - Title`\n"
            "(This will be used for the filename and tags)",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _do_direct_slskd_search(
        self,
        context,
        chat_id: int,
        query: str,
        searching_msg,
        generation: int,
        display_track: TrackInfo | None = None,
        user_id: int | None = None,
    ):
        """Search slskd without Spotify metadata. Duration scoring gives flat 15 points."""
        try:
            # Use display_track from Spotify candidates if available, otherwise parse query
            if display_track:
                track = synthetic_track(
                    display_track.artist, display_track.title, album=display_track.album, year=display_track.year
                )
            else:
                artist, title = parse_query_artist_title(query)
                track = synthetic_track(artist, title)

            ranked = await self.pipeline.search(query, track, self._profile(chat_id, user_id))
            if self._is_stale(chat_id, generation):
                return

            if not ranked:
                self.pending[chat_id] = PendingSearch(query=query, track=None)
                await _safe_edit(
                    searching_msg,
                    f"\U0001f50e Direct search: `{query}`\n\nNo results found on Soulseek.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_direct_search_keyboard(),
                )
                return

            search_id = uuid4().hex[:8]
            self.pending[chat_id] = PendingSearch(
                query=query,
                track=track,
                results=ranked,
                message_id=searching_msg.message_id,
                search_id=search_id,
            )

            results_text = self._format_results(track, ranked, page=0, page_size=self.config.max_results)
            await _safe_edit(
                searching_msg,
                results_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_results_keyboard(
                    ranked, page=0, page_size=self.config.max_results, search_id=search_id
                ),
            )

        except Exception:
            logger.exception(f"Direct search failed for: {query}")
            await _safe_edit(searching_msg, "Something went wrong. Please try again.")

    # =========================================================================
    # IMPORT COMMANDS
    # =========================================================================

    async def cmd_import(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /import <spotify_url> — import playlist or album."""
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id
        args = update.message.text.split(maxsplit=1)

        if len(args) < 2:
            await update.message.reply_text(
                "Usage: `/import <spotify_playlist_or_album_url>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        url = args[1].strip()

        if not PlaylistResolver.is_spotify_url(url):
            await update.message.reply_text(
                "Please provide a valid Spotify playlist or album URL.",
            )
            return

        # Check for existing active import
        active = await asyncio.to_thread(self.import_repo.get_active_job, chat_id)
        if active:
            await update.message.reply_text(
                f"You already have an active import: *{_escape_md(active.name)}* ({active.completed_tracks}/{active.total_tracks})\n"
                f"Use /cancel to stop it first.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        status_msg = await update.message.reply_text("\U0001f50d Resolving playlist...")

        playlist_info = await asyncio.to_thread(self.playlist_resolver.resolve, url)
        if not playlist_info:
            await _safe_edit(status_msg, "Failed to resolve playlist. Check the URL and try again.")
            return

        # Create job in DB
        job_id = await asyncio.to_thread(
            self.import_repo.create_job,
            chat_id=chat_id,
            spotify_url=url,
            name=playlist_info.name,
            total_tracks=playlist_info.total_tracks,
        )

        # Add tracks to DB
        track_dicts = [
            {
                "position": i + 1,
                "artist": t.artist,
                "title": t.title,
                "album": t.album,
                "duration_ms": t.duration_ms,
                "spotify_url": t.spotify_url,
                "year": t.year,
            }
            for i, t in enumerate(playlist_info.tracks)
        ]
        await asyncio.to_thread(self.import_repo.add_tracks, job_id, track_dicts)

        # Show summary
        type_label = "album" if playlist_info.is_album else "playlist"
        await _safe_edit(
            status_msg,
            f"\U0001f4cb Found {type_label}: *{_escape_md(playlist_info.name)}*\n"
            f"By: {_escape_md(playlist_info.owner)}\n"
            f"Tracks: {playlist_info.total_tracks}\n\n"
            f"Import all tracks one by one?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_import_confirm_keyboard(job_id),
        )

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel — cancel active import or search."""
        if not await self._check_auth(update):
            return

        chat_id = update.effective_chat.id

        # Cancel import if active. Fall back to the DB when the in-memory map
        # is empty (abandoned confirm screen, or state lost to a restart) —
        # otherwise the pending/active row blocks /import forever while this
        # very command reports "Nothing to cancel."
        job_id = self._active_import.pop(chat_id, None)
        if job_id is None:
            active = await asyncio.to_thread(self.import_repo.get_active_job, chat_id)
            if active is not None:
                job_id = active.id
        if job_id:
            await asyncio.to_thread(self.import_repo.update_job_status, job_id, JobStatus.cancelled)
            self._cancel_chat_operations(chat_id)
            await update.message.reply_text("❌ Import cancelled.")
            return

        # Otherwise cancel regular operations
        had_work = self._cancel_chat_operations(chat_id)
        if had_work:
            await update.message.reply_text("❌ Cancelled.")
        else:
            await update.message.reply_text("Nothing to cancel.")

    async def _handle_import_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: str
    ):
        """Route import-related callbacks (ic/ix/ia/ir/is prefixes)."""
        query = update.callback_query
        prefix, _, payload = data.partition(":")
        parts = payload.split(":")

        try:
            job_id = int(parts[0])
        except (IndexError, ValueError):
            return

        # IDOR: verify job belongs to this chat
        job = await asyncio.to_thread(self.import_repo.get_job_for_chat, job_id, chat_id)
        if not job:
            await _safe_query_edit(query, "⏹ Import not found.")
            return

        if prefix == "ic":
            auto = len(parts) > 1 and parts[1] == "auto"
            self._import_auto[chat_id] = auto
            self._import_user[chat_id] = query.from_user.id
            if self._is_chat_delivery(chat_id, query.from_user.id):
                mode_note = "sending every track to this chat"
            else:
                mode_note = "auto-saving every track" if auto else "you review each track"
            await _safe_query_edit(query, f"✅ Import started — {mode_note}...")
            await asyncio.to_thread(self.import_repo.update_job_status, job_id, JobStatus.active)
            self._active_import[chat_id] = job_id
            generation = self._chat_generation.get(chat_id, 0)
            task = context.application.create_task(
                self._process_next_import_track(context, chat_id, job_id, generation),
                update=update,
            )
            self._track_task(chat_id, task)

        elif prefix == "ix":
            await asyncio.to_thread(self.import_repo.update_job_status, job_id, JobStatus.cancelled)
            self._active_import.pop(chat_id, None)
            self._import_auto.pop(chat_id, None)
            self._import_user.pop(chat_id, None)
            await _safe_query_edit(query, "❌ Import cancelled.")

        elif prefix == "ia":
            track_id = int(parts[1])
            dl_id = parts[2]
            await self._handle_import_approve(update, context, chat_id, job_id, track_id, dl_id)

        elif prefix == "ir":
            track_id = int(parts[1])
            await asyncio.to_thread(
                self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, "Rejected by user"
            )
            await _safe_query_edit(query, "\U0001f6ab Track rejected.")
            generation = self._chat_generation.get(chat_id, 0)
            await self._process_next_import_track(context, chat_id, job_id, generation)

        elif prefix == "is":
            track_id = int(parts[1])
            await asyncio.to_thread(self.import_repo.complete_track, job_id, track_id, TrackStatus.skipped)
            await _safe_query_edit(query, "⏭ Track skipped.")
            generation = self._chat_generation.get(chat_id, 0)
            await self._process_next_import_track(context, chat_id, job_id, generation)

    async def _handle_import_approve(self, update, context, chat_id: int, job_id: int, track_id: int, dl_id: str):
        """Approve a download within an import flow."""
        query = update.callback_query
        pending_dl = self.downloads.pop(dl_id, None)

        if not pending_dl:
            await self._edit_approval_message(query, "⏹ Download expired")
            return

        if not pending_dl.source_path:
            # Keep the buttons alive: stripping them here would freeze the
            # whole import with no way forward once the download finishes.
            await self._edit_approval_message(
                query,
                "❌ Source file not ready. Download may still be in progress — try again in a moment.",
                reply_markup=build_import_track_keyboard(job_id, track_id, dl_id),
            )
            self.downloads[dl_id] = pending_dl
            return

        track = pending_dl.track
        result = pending_dl.result

        if self._is_chat_delivery(chat_id, pending_dl.user_id):
            # Preview sent in library mode, approved after switching to chat
            # delivery: deliver it here instead of writing the library.
            self.downloads[dl_id] = pending_dl
            await self._edit_approval_message(query, "\U0001f4e8 Sending to this chat instead of saving...")
            status_msg = await context.bot.send_message(
                chat_id=chat_id, text=f"\U0001f4cb Import: {track.artist} - {track.title}\n\U0001f4e8 Sending..."
            )
            generation = self._chat_generation.get(chat_id, 0)
            await self._import_deliver(
                context, chat_id, job_id, track_id, dl_id, track, result, pending_dl.source_path, status_msg, generation
            )
            return

        target_path = await self.pipeline.save(pending_dl.source_path, track, result)
        if target_path:
            target_name = os.path.basename(target_path)
            await self._edit_approval_message(query, f"✅ Saved: `{target_name}`")
            await asyncio.to_thread(self.import_repo.complete_track, job_id, track_id, TrackStatus.completed)
        else:
            await self._edit_approval_message(query, "❌ Failed to save file.")
            await asyncio.to_thread(
                self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, "File processing failed"
            )

        # Continue to next track
        generation = self._chat_generation.get(chat_id, 0)
        await self._process_next_import_track(context, chat_id, job_id, generation)

    async def _process_next_import_track(self, context, chat_id: int, job_id: int, generation: int):
        """Process the next pending track in an import job."""
        if self._is_stale(chat_id, generation):
            return

        next_track = await asyncio.to_thread(self.import_repo.get_next_pending_track, job_id)

        if not next_track:
            # All tracks processed
            progress = await asyncio.to_thread(self.import_repo.get_job_progress, job_id)
            completed, failed, skipped, total = progress
            await asyncio.to_thread(self.import_repo.update_job_status, job_id, JobStatus.completed)
            self._active_import.pop(chat_id, None)
            done_word = "Sent" if self._is_chat_delivery(chat_id, self._import_user.get(chat_id)) else "Saved"
            self._import_auto.pop(chat_id, None)
            self._import_user.pop(chat_id, None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f3c1 *Import complete!*\n\n"
                f"✅ {done_word}: {completed}\n"
                f"❌ Failed: {failed}\n"
                f"⏭ Skipped: {skipped}\n"
                f"\U0001f4ca Total: {total}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Build TrackInfo from import track
        track_info = TrackInfo(
            artist=next_track.artist,
            title=next_track.title,
            album=next_track.album,
            duration_ms=next_track.duration_ms,
            spotify_url=next_track.spotify_url,
            year=next_track.year,
        )

        progress = await asyncio.to_thread(self.import_repo.get_job_progress, job_id)
        completed, failed, skipped, total = progress
        position = completed + failed + skipped + 1

        await asyncio.to_thread(self.import_repo.update_track_status, next_track.id, TrackStatus.searching)

        searching_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"\U0001f4cb *Import [{position}/{total}]*\n"
            f"\U0001f50d Searching: *{track_info.artist} - {track_info.title}*\n"
            f"Album: {track_info.album} ({track_info.year})",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Use the existing search logic but with import-aware download handling
        await self._do_import_slskd_search(
            context, chat_id, track_info, searching_msg, generation, job_id, next_track.id
        )

    async def _do_import_slskd_search(
        self, context, chat_id: int, track: TrackInfo, searching_msg, generation: int, job_id: int, track_id: int
    ):
        """Search slskd for an import track. Similar to _do_slskd_search but with import keyboards."""
        try:
            clean_title = clean_search_title(track.title)
            search_query = f"{track.artist} {clean_title}"
            profile = self._profile(chat_id, self._import_user.get(chat_id))
            ranked = await self.pipeline.search(search_query, track, profile)
            if self._is_stale(chat_id, generation):
                return

            # Title-only fallback
            if not ranked:
                if self._is_stale(chat_id, generation):
                    return
                profile = self._profile(chat_id, self._import_user.get(chat_id))
                ranked = await self.pipeline.search(clean_title, track, profile)
                if self._is_stale(chat_id, generation):
                    return

            if self._is_stale(chat_id, generation):
                return

            if not ranked:
                await _safe_edit(
                    searching_msg,
                    f"\U0001f4cb *Import track:* {track.artist} - {track.title}\n\nNo results found on Soulseek.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_import_skip_keyboard(job_id, track_id),
                )
                await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)
                return

            # Auto-pick best result and start download
            best = ranked[0]

            # Store results for potential "next result" retry (separate from regular search)
            search_id = uuid4().hex[:8]
            self._import_pending[chat_id] = PendingSearch(
                query=search_query,
                track=track,
                results=ranked,
                message_id=searching_msg.message_id,
                search_id=search_id,
            )

            dl_id = self._next_dl_id()
            pending_dl = PendingDownload(
                track=track,
                result=best,
                chat_id=chat_id,
                source_path=None,
                status_message_id=searching_msg.message_id,
                search_id=search_id,
                user_id=self._import_user.get(chat_id),
            )
            self.downloads[dl_id] = pending_dl

            await _safe_edit(
                searching_msg,
                f"\U0001f4cb *Import track:* {track.artist} - {track.title}\n"
                f"⬇️ Downloading: `{best.basename}`\n"
                f"From: `{best.username}` | {best.quality_display}",
                parse_mode=ParseMode.MARKDOWN,
            )

            # Start download
            task = context.application.create_task(
                self._do_import_download(
                    context, chat_id, track, best, searching_msg, generation, job_id, track_id, dl_id
                ),
                update=None,
            )
            self._track_task(chat_id, task)

        except Exception:
            logger.exception(f"Import search failed for: {track.artist} - {track.title}")
            await _safe_edit(searching_msg, f"❌ Search failed for {track.artist} - {track.title}")
            await asyncio.to_thread(
                self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, "Search error"
            )
            await self._process_next_import_track(context, chat_id, job_id, generation)

    async def _do_import_download(
        self,
        context,
        chat_id: int,
        track: TrackInfo,
        result: SearchResult,
        status_msg,
        generation: int,
        job_id: int,
        track_id: int,
        dl_id: str,
    ):
        """Download a file within an import flow."""
        try:
            outcome = await self.pipeline.fetch(
                result,
                self._make_progress_reporter(
                    status_msg,
                    f"\U0001f4cb *Import track:* {track.artist} - {track.title}\n"
                    f"⬇️ Downloading: `{result.basename}`\nFrom: `{result.username}`",
                ),
                analyze=False,
            )
            if outcome.error == ENQUEUE_FAILED:
                if self._import_auto.get(chat_id):
                    await self._import_auto_fail(
                        context,
                        chat_id,
                        job_id,
                        track_id,
                        dl_id,
                        status_msg,
                        generation,
                        f"❌ Failed to enqueue from `{result.username}` — continuing.",
                        "Enqueue failed",
                    )
                    return
                # No file exists yet, so never offer a Save button here —
                # tapping it could only strip the keyboard and stall the job.
                await _safe_edit(
                    status_msg,
                    f"❌ Failed to enqueue from `{result.username}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_import_skip_keyboard(job_id, track_id),
                )
                await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)
                return

            if outcome.error == DOWNLOAD_FAILED:
                state = outcome.state
                if self._import_auto.get(chat_id):
                    await self._import_auto_fail(
                        context,
                        chat_id,
                        job_id,
                        track_id,
                        dl_id,
                        status_msg,
                        generation,
                        f"❌ Download failed ({state}): `{result.basename}` — continuing.",
                        state,
                    )
                    return
                await _safe_edit(
                    status_msg,
                    f"❌ Download failed: {state}\n`{result.basename}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_retry_keyboard(dl_id),
                )
                await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)
                return

            if not outcome.ok:
                if self._import_auto.get(chat_id):
                    await self._import_auto_fail(
                        context,
                        chat_id,
                        job_id,
                        track_id,
                        dl_id,
                        status_msg,
                        generation,
                        "❌ Downloaded file not found on disk — continuing.",
                        "File not found on disk",
                    )
                    return
                await _safe_edit(
                    status_msg,
                    "❌ Downloaded file not found on disk.",
                    reply_markup=build_import_skip_keyboard(job_id, track_id),
                )
                await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)
                return

            source_path = outcome.path
            # Update PendingDownload with source path
            if dl_id in self.downloads:
                self.downloads[dl_id].source_path = source_path

            if self._is_chat_delivery(chat_id, self._import_user.get(chat_id)):
                # Nothing to review: the track itself is the deliverable.
                await self._import_deliver(
                    context, chat_id, job_id, track_id, dl_id, track, result, source_path, status_msg, generation
                )
                return

            if self._import_auto.get(chat_id):
                await self._import_auto_save(
                    context, chat_id, job_id, track_id, dl_id, track, result, source_path, status_msg, generation
                )
                return

            # Send file for approval
            await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)

            file_size = os.path.getsize(source_path) if os.path.isfile(source_path) else 0
            quality_line = f"{result.quality_display} | {result.duration_display}"
            caption = f"\U0001f4cb Import: {track.artist} - {track.title}\n{quality_line}"

            if file_size > TELEGRAM_FILE_LIMIT:
                # For large files, just show approve button without sending file
                await _safe_edit(
                    status_msg,
                    f"✅ Downloaded: `{result.basename}` ({file_size / (1024 * 1024):.0f}MB)\n"
                    f"{quality_line}\n\nFile too large to preview. Save to library?",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_import_track_keyboard(job_id, track_id, dl_id),
                )
            else:
                target_name = self.pipeline.target_filename(track, result.extension)
                try:
                    with open(source_path, "rb") as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            filename=target_name,
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration_secs,
                            caption=caption,
                            reply_markup=build_import_track_keyboard(job_id, track_id, dl_id),
                        )
                except BadRequest:
                    with open(source_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=target_name,
                            caption=caption,
                            reply_markup=build_import_track_keyboard(job_id, track_id, dl_id),
                        )

        except asyncio.CancelledError:
            self.downloads.pop(dl_id, None)
            raise
        except Exception:
            logger.exception(f"Import download failed for {result.basename}")
            await _safe_edit(status_msg, f"❌ Error downloading `{result.basename}`", parse_mode=ParseMode.MARKDOWN)
            await asyncio.to_thread(
                self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, "Download error"
            )
            await self._process_next_import_track(context, chat_id, job_id, generation)

    async def _import_auto_fail(
        self,
        context,
        chat_id: int,
        job_id: int,
        track_id: int,
        dl_id: str,
        status_msg,
        generation: int,
        message: str,
        reason: str,
    ):
        """Unattended import: mark the track failed and move on instead of pausing."""
        self.downloads.pop(dl_id, None)
        await _safe_edit(status_msg, message, parse_mode=ParseMode.MARKDOWN)
        await asyncio.to_thread(self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, reason)
        await self._process_next_import_track(context, chat_id, job_id, generation)

    async def _import_deliver(
        self,
        context,
        chat_id: int,
        job_id: int,
        track_id: int,
        dl_id: str,
        track: TrackInfo,
        result: SearchResult,
        source_path: str,
        status_msg,
        generation: int,
    ):
        """Chat delivery inside an import: send the track to the chat, save nothing, move on."""
        heading = f"\U0001f4cb *Import:* {_escape_md(track.artist)} - {_escape_md(track.title)}"
        caption = (
            f"\U0001f4cb Import: {track.artist} - {track.title}\n{result.quality_display} | {result.duration_display}"
        )
        outcome, note = await self._send_to_chat(context, chat_id, track, result, source_path, caption)
        if outcome == "sent":
            await self.pipeline.discard(source_path)
            # Popped only after cleanup (orphan-sweep protection, see _auto_save).
            self.downloads.pop(dl_id, None)
            await _safe_edit(status_msg, f"{heading}\n✅ Sent: `{note}`", parse_mode=ParseMode.MARKDOWN)
            await self.pipeline.record_history(track, result, "delivered", filename=note)
            await asyncio.to_thread(self.import_repo.complete_track, job_id, track_id, TrackStatus.completed)
            await self._process_next_import_track(context, chat_id, job_id, generation)
            return

        await self.pipeline.record_history(track, result, outcome)
        if self._import_auto.get(chat_id):
            # Source kept for the orphan sweep once the entry is gone.
            await self._import_auto_fail(
                context,
                chat_id,
                job_id,
                track_id,
                dl_id,
                status_msg,
                generation,
                f"{heading}\n❌ {note} — continuing.",
                outcome,
            )
            return
        self.downloads.pop(dl_id, None)
        await _safe_edit(
            status_msg,
            f"{heading}\n❌ {note}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_import_skip_keyboard(job_id, track_id),
        )
        await asyncio.to_thread(self.import_repo.update_track_status, track_id, TrackStatus.awaiting_approval)

    async def _import_auto_save(
        self,
        context,
        chat_id: int,
        job_id: int,
        track_id: int,
        dl_id: str,
        track: TrackInfo,
        result: SearchResult,
        source_path: str,
        status_msg,
        generation: int,
    ):
        """Unattended import: save the track straight to the library, no preview upload."""
        self.downloads.pop(dl_id, None)
        target_path = await self.pipeline.save(source_path, track, result)
        if target_path:
            target_name = os.path.basename(target_path)
            await _safe_edit(
                status_msg,
                f"\U0001f4cb *Import:* {_escape_md(track.artist)} - {_escape_md(track.title)}\n"
                f"✅ Auto-saved: `{target_name}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            await asyncio.to_thread(self.import_repo.complete_track, job_id, track_id, TrackStatus.completed)
        else:
            await _safe_edit(
                status_msg,
                f"\U0001f4cb *Import:* {_escape_md(track.artist)} - {_escape_md(track.title)}\n"
                f"❌ Failed to save — continuing.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await asyncio.to_thread(
                self.import_repo.complete_track, job_id, track_id, TrackStatus.failed, "File processing failed"
            )
        await self._process_next_import_track(context, chat_id, job_id, generation)

    # =========================================================================
    # RETRY HANDLERS
    # =========================================================================

    async def _handle_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: str):
        """Retry a failed download."""
        query = update.callback_query
        dl_id = data.split(":", 1)[1]

        pending_dl = self.downloads.pop(dl_id, None)
        if not pending_dl:
            await _safe_query_edit(query, "⏹ Download expired. Send a new search.")
            return

        if pending_dl.chat_id != chat_id:
            self.downloads[dl_id] = pending_dl
            return

        result = pending_dl.result
        track = pending_dl.track
        result_index = pending_dl.result_index

        await _safe_query_edit(
            query,
            f"\U0001f504 Retrying: `{result.basename}`...",
            parse_mode=ParseMode.MARKDOWN,
        )

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⬇️ Re-downloading from `{result.username}`...",
            parse_mode=ParseMode.MARKDOWN,
        )

        task = context.application.create_task(
            self._do_download(
                context,
                chat_id,
                track,
                result,
                status_msg,
                result_index,
                pending_dl.search_id,
                user_id=pending_dl.user_id,
            ),
            update=update,
        )
        self._track_task(chat_id, task)

    async def _handle_next_result(self, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, data: str):
        """Try the next-best search result after a failed download."""
        query = update.callback_query
        dl_id = data.split(":", 1)[1]

        pending = self.pending.get(chat_id) or self._import_pending.get(chat_id)
        pending_dl = self.downloads.pop(dl_id, None)

        if not pending or not pending.results or not pending_dl:
            await _safe_query_edit(query, "⏹ No more results available. Try a new search.")
            return

        if pending_dl.chat_id != chat_id:
            self.downloads[dl_id] = pending_dl
            return

        if pending.search_id != pending_dl.search_id:
            # The failed download belongs to an older search; indexing into the
            # current result list would fetch an unrelated file.
            await _safe_query_edit(query, "⏹ No more results available. Try a new search.")
            return

        next_idx = pending_dl.result_index + 1
        if next_idx >= len(pending.results):
            await _safe_query_edit(query, "⏹ No more results to try.")
            return

        next_result = pending.results[next_idx]
        track = pending_dl.track

        await _safe_query_edit(
            query,
            f"⏭ Trying next result: `{next_result.basename}`",
            parse_mode=ParseMode.MARKDOWN,
        )

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⬇️ Downloading from `{next_result.username}`...",
            parse_mode=ParseMode.MARKDOWN,
        )

        task = context.application.create_task(
            self._do_download(
                context,
                chat_id,
                track,
                next_result,
                status_msg,
                next_idx,
                pending.search_id,
                user_id=pending_dl.user_id,
            ),
            update=update,
        )
        self._track_task(chat_id, task)

    # =========================================================================
    # HELPERS
    # =========================================================================

    @staticmethod
    def _format_spotify_results(tracks: list[TrackInfo], page: int = 0, page_size: int = 5) -> str:
        """Format Spotify track candidates for selection (one page)."""
        total = len(tracks)
        start = page * page_size
        end = min(start + page_size, total)
        total_pages = (total + page_size - 1) // page_size

        header = "🔍 *Multiple matches found on Spotify:*"
        if total_pages > 1:
            header += f" (page {page + 1}/{total_pages})"
        lines = [header + "\n"]

        for i in range(start, end):
            t = tracks[i]
            lines.append(
                f"*#{i + 1} {t.artist} - {t.title}*\n"
                f"    Album: {t.album} ({t.year}) | {t.duration_display}\n"
                f"    [Listen on Spotify]({t.spotify_url})"
            )
        lines.append("\nPick the correct version:")
        return "\n".join(lines)

    def _format_results(
        self,
        track: TrackInfo,
        results: list[SearchResult],
        page: int = 0,
        page_size: int = 10,
    ) -> str:
        """Format search results for display in Telegram (one page)."""
        total = len(results)
        start = page * page_size
        end = min(start + page_size, total)
        total_pages = (total + page_size - 1) // page_size

        lossless = sum(1 for r in results if r.is_lossless)
        lossy = total - lossless
        matches = f"{total} match" if total == 1 else f"{total} matches"
        if lossless and lossy:
            found = f"Found {matches} ({lossless} lossless, {lossy} lossy):\n"
        elif lossless:
            found = f"Found {matches}, all lossless:\n"
        else:
            found = f"Found {matches}, all lossy (no lossless copy found):\n"

        is_direct = track.duration_ms == 0
        if is_direct:
            if track.artist:
                header = [f"🎵 *{track.artist} - {track.title}*\n", found]
            else:
                header = [f"\U0001f50e *Direct search:* `{track.title}`\n", found]
        else:
            header = [
                f"🎵 *{track.artist} - {track.title}*",
                f"Duration: {track.duration_display} | Album: {track.album}\n",
                found,
            ]

        if total_pages > 1:
            header.append(f"📄 Page {page + 1}/{total_pages}\n")

        lines = header
        for i in range(start, end):
            r = results[i]
            slot_icon = "🟢" if r.has_free_slot else "🔴"
            # quality_display falls back to the format name when slskd reports no
            # quality; name the format once in that case ("APE", not "APE [APE]").
            ext_tag = r.extension.upper()
            quality = r.quality_display if r.quality_display == ext_tag else f"{r.quality_display} [{ext_tag}]"
            lines.append(
                f"*#{i + 1}* {slot_icon} `{r.duration_display}` | {quality} | {r.size_mb:.0f}MB\n    `{r.basename}`"
            )

        return "\n".join(lines)

    async def _orphan_sweep_loop(self) -> None:
        """The pipeline's hourly orphan sweep, with this bot's in-flight downloads protected."""
        await self.pipeline.orphan_sweep_loop(
            lambda: {dl.source_path for dl in self.downloads.values() if dl.source_path}
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler: log the crash and tell the user something broke.

        Without this, python-telegram-bot logs one line and the user sees
        nothing at all — a crashed command is indistinguishable from a dead bot.
        """
        logger.error("Unhandled error while processing update", exc_info=context.error)
        message = update.effective_message if isinstance(update, Update) else None
        if message is not None:
            with contextlib.suppress(Exception):
                await message.reply_text("⚠️ Something went wrong. Please try again.")


async def _register_commands(app: Application) -> None:
    """Publish the command menu so Telegram's `/` autocomplete shows every command.

    Without this the biggest feature in the bot (/import) is invisible: nothing
    in the UI ever reveals it exists.
    """
    await app.bot.set_my_commands(
        [
            BotCommand("import", "Import a Spotify playlist or album"),
            BotCommand("cancel", "Cancel the active import or search"),
            BotCommand("auto", "Toggle auto-download mode"),
            BotCommand("deliver", "Toggle chat delivery (send tracks here instead of saving)"),
            BotCommand("status", "Show active searches and downloads"),
            BotCommand("history", "Recent downloads"),
            BotCommand("help", "How to use the bot"),
        ]
    )


def create_bot(config: Config) -> Application:
    """
    Create and configure the Telegram bot application.

    Args:
        config: Application configuration.

    Returns:
        Configured telegram Application ready to run.
    """
    bot = MusicBot(config, Pipeline(config))

    async def _post_init(app: Application) -> None:
        await _register_commands(app)
        if config.download_cleanup_hours > 0:
            # Plain asyncio task, deliberately NOT app.create_task: PTB must
            # never await this infinite loop as part of its own lifecycle.
            bot._sweep_task = asyncio.get_running_loop().create_task(bot._orphan_sweep_loop())
            logger.info(
                f"Orphan sweep enabled: files older than {config.download_cleanup_hours}h "
                f"are removed from the downloads dir hourly"
            )

    async def _post_shutdown(app: Application) -> None:
        task = getattr(bot, "_sweep_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Only react to NEW messages: an edited message arrives with
    # update.message=None and would crash every handler that replies.
    new_messages = filters.UpdateType.MESSAGE

    # Command handlers
    app.add_handler(CommandHandler("start", bot.cmd_start, filters=new_messages))
    app.add_handler(CommandHandler("help", bot.cmd_help, filters=new_messages))
    app.add_handler(CommandHandler("auto", bot.cmd_auto, filters=new_messages))
    app.add_handler(CommandHandler("deliver", bot.cmd_deliver, filters=new_messages))
    app.add_handler(CommandHandler("status", bot.cmd_status, filters=new_messages))
    app.add_handler(CommandHandler("history", bot.cmd_history, filters=new_messages))
    app.add_handler(CommandHandler("import", bot.cmd_import, filters=new_messages))
    app.add_handler(CommandHandler("cancel", bot.cmd_cancel, filters=new_messages))

    # Callback query handler (inline keyboard buttons)
    app.add_handler(CallbackQueryHandler(bot.handle_callback))

    # Text message handler (song search) — must be last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & new_messages, bot.handle_text))

    # Surface crashes to the operator and the user instead of swallowing them
    app.add_error_handler(bot.on_error)

    logger.info("Telegram bot configured")
    return app
