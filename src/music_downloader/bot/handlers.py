"""
Telegram bot handlers for music search and download.
"""

import contextlib
import logging
import os
import re
from dataclasses import dataclass, field

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
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
    build_duplicate_keyboard,
    build_results_keyboard,
    build_spotify_keyboard,
)
from music_downloader.config import Config
from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.processor.flac_analyzer import FlacVerdict, analyze_flac, create_preview_clip
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

logger = logging.getLogger(__name__)

# Telegram bot API file size limit: 50 MB
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024


def _escape_md(text: str) -> str:
    """Escape Markdown V1 special characters for safe display."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def _safe_edit(msg: Message, text: str, **kwargs) -> bool:
    """Edit a Telegram message, swallowing common failures.

    Returns True on success, False if the edit failed (logged as warning).
    """
    try:
        await msg.edit_text(text, **kwargs)
        return True
    except BadRequest as exc:
        logger.warning(f"Telegram edit failed (BadRequest): {exc}")
        return False
    except TimedOut:
        logger.warning("Telegram edit timed out")
        return False
    except NetworkError as exc:
        logger.warning(f"Telegram edit network error: {exc}")
        return False


# Regex to strip Spotify version suffixes that pollute Soulseek keyword search.
# Matches trailing " - Remastered 2009", " - Mono", " - Deluxe", etc.
_VERSION_SUFFIX_RE = re.compile(
    r"\s*[-–]\s*("
    r"Mono|Stereo|Remaster(?:ed)?(?:\s+\d{4})?"
    r"|Deluxe(?:\s+Edition)?"
    r"|Ultimate\s+Mix|Single\s+Version|Album\s+Version"
    r"|Radio\s+Edit|Bonus\s+Track|Anniversary(?:\s+Edition)?"
    r"|Super\s+Deluxe|Special\s+Edition|\d{4}\s+Mix"
    r").*$",
    re.IGNORECASE,
)

# Same patterns but inside parentheses: "(Remastered 2009)", "(Mono)", etc.
_VERSION_PAREN_RE = re.compile(
    r"\s*\("
    r"(?:Mono|Stereo|Remaster(?:ed)?(?:\s+\d{4})?"
    r"|Deluxe(?:\s+Edition)?"
    r"|Ultimate\s+Mix|Single\s+Version|Album\s+Version"
    r"|Radio\s+Edit|Bonus\s+Track|Anniversary(?:\s+Edition)?"
    r"|Super\s+Deluxe|Special\s+Edition|\d{4}\s+Mix)"
    r"\)",
    re.IGNORECASE,
)


def _clean_search_title(title: str) -> str:
    """Strip Spotify version suffixes that add noise to Soulseek keyword search."""
    title = _VERSION_SUFFIX_RE.sub("", title)
    title = _VERSION_PAREN_RE.sub("", title)
    return title.strip()


def _build_reduced_queries(title: str, year: str) -> list[str]:
    """Build fallback search queries by dropping one word at a time and appending the year.

    Soulseek users sometimes block entire phrases (e.g. "Purple Rain").
    Removing one keyword at a time while adding the album year often
    bypasses server-side filters while still narrowing results enough
    to find the right track.

    Args:
        title: The (cleaned) song title, e.g. "Purple Rain".
        year: Album release year, e.g. "1984".

    Returns:
        List of fallback query strings.  Empty if the title has fewer
        than 2 words or no year is available.
    """
    if not year:
        return []
    words = title.split()
    if len(words) < 2:
        return []
    queries: list[str] = []
    for i in range(len(words)):
        reduced = " ".join(words[:i] + words[i + 1 :])
        queries.append(f"{reduced} {year}")
    return queries


_NOISE_WORDS = frozenset({
    "single", "version", "long", "short", "full", "edit", "mix",
    "remastered", "remaster", "deluxe", "edition", "bonus", "track",
    "album", "mono", "stereo", "original", "extended",
    "feat", "featuring", "ft", "the", "an", "and", "or", "of",
    "in", "on", "at", "to", "for", "with", "from", "by",
})


def _extract_latin_keywords(title: str) -> list[str]:
    """Extract meaningful Latin keywords from a potentially mixed-script title.

    Strips common noise words so only distinctive keywords remain,
    e.g. ``["KURENAI"]`` from ``"紅 - KURENAI - シングル… - Single Long Version"``.
    """
    words = re.findall(r"[a-zA-Z]{2,}", title)
    return [w for w in words if w.lower() not in _NOISE_WORDS]


@dataclass
class PendingSearch:
    """Holds state for an active search session."""

    query: str
    track: TrackInfo | None = None
    results: list[SearchResult] = field(default_factory=list)
    message_id: int | None = None
    is_fallback: bool = False
    page: int = 0


@dataclass
class PendingDownload:
    """Tracks a single file download waiting for approval."""

    track: TrackInfo
    result: SearchResult
    source_path: str | None = None  # Path in /downloads
    status_message_id: int | None = None


class MusicBot:
    """Telegram bot for music discovery and download."""

    def __init__(self, config: Config):
        self.config = config
        self.spotify = SpotifyResolver(config.spotify_client_id, config.spotify_client_secret)
        self.slskd = SlskdClient(config.slskd_host, config.slskd_api_key)
        self.scorer = ResultScorer(
            duration_tolerance_secs=config.duration_tolerance_secs,
            exclude_keywords=config.exclude_keywords,
        )
        self.processor = FileProcessor(
            download_dir=config.download_dir,
            output_dir=config.output_dir,
            filename_template=config.filename_template,
        )
        self.auto_mode = config.auto_mode

        # Per-user pending searches (chat_id -> PendingSearch)
        self.pending: dict[int, PendingSearch] = {}

        # Active downloads keyed by short numeric ID
        # download_id -> PendingDownload
        self.downloads: dict[str, PendingDownload] = {}
        self._dl_counter = 0

        # Per-chat Spotify candidates when multiple tracks match (chat_id -> list[TrackInfo])
        self._spotify_candidates: dict[int, list[TrackInfo]] = {}
        # Current page for Spotify browsing (chat_id -> page)
        self._spotify_page: dict[int, int] = {}

        # Download history (last N downloads)
        self.history: list[dict] = []

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to use the bot."""
        if not self.config.telegram_allowed_users:
            return True
        return user_id in self.config.telegram_allowed_users

    async def _check_auth(self, update: Update) -> bool:
        """Check authorization and send a message if denied."""
        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text("You are not authorized to use this bot.")
            return False
        return True

    # =========================================================================
    # COMMAND HANDLERS
    # =========================================================================

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        if not await self._check_auth(update):
            return

        await update.message.reply_text(
            "Send me a song name (e.g., `Nancy Sinatra Bang Bang`) "
            "and I'll find and download it in FLAC.\n\n"
            "Commands:\n"
            "/auto — Toggle auto-download mode\n"
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

        mode_str = "ON" if self.auto_mode else "OFF"
        await update.message.reply_text(
            f"Auto-download mode is currently: *{mode_str}*\n\n"
            "When ON, the best FLAC match is downloaded automatically without asking you to pick.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_auto_mode_keyboard(self.auto_mode),
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command — show active searches and downloads."""
        if not await self._check_auth(update):
            return

        lines = []

        if self.pending:
            lines.append("*Active searches:*\n")
            for _chat_id, pending in self.pending.items():
                lines.append(f"• {pending.track.artist} - {pending.track.title}")

        if self.downloads:
            lines.append("\n*Active downloads:*\n")
            for _dl_id, dl in self.downloads.items():
                lines.append(f"• {dl.track.artist} - {dl.track.title} ({dl.result.basename})")

        if not lines:
            await update.message.reply_text("No active searches or downloads.")
            return

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /history command — show recent downloads."""
        if not await self._check_auth(update):
            return

        if not self.history:
            await update.message.reply_text("No downloads yet.")
            return

        lines = ["*Recent downloads:*\n"]
        for entry in self.history[-10:]:
            status = entry.get("status", "unknown")
            icon = {"success": "✅", "rejected": "🚫"}.get(status, "❌")
            lines.append(f"{icon} {entry.get('filename', 'unknown')}")

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

        # Step 0: Check for similar files already in the library
        similar = self.processor.find_similar(query)
        if similar:
            existing_list = "\n".join(f"• `{f}`" for f in similar[:5])
            await update.message.reply_text(
                f"⚠️ *Similar files already in library:*\n\n{existing_list}\n\nContinue searching anyway?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_duplicate_keyboard(),
            )
            # Store the query so we can resume if user clicks "Continue"
            self.pending[chat_id] = PendingSearch(query=query, track=None)
            return

        await self._do_search(update, context, query)

    async def _do_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
        """Resolve metadata via Spotify, then proceed to slskd search."""
        chat_id = update.effective_chat.id

        searching_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔍 Looking up: `{query}`",
            parse_mode=ParseMode.MARKDOWN,
        )

        try:
            # Get multiple Spotify results (fetch many, paginate in UI)
            tracks = self.spotify.search_multiple(query, limit=20)
            if not tracks:
                await _safe_edit(
                    searching_msg,
                    f"Could not find `{query}` on Spotify. Try a more specific query.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            # If the query has "artist - title" form, use the artist portion
            # to filter out irrelevant Spotify results (Spotify search is loose).
            query_artist = ""
            if " - " in query:
                query_artist = query.split(" - ", 1)[0].strip().lower()

            # Deduplicate by artist + title + album (preserves remastered,
            # live, deluxe editions while collapsing true duplicates).
            seen = set()
            unique_tracks = []
            for t in tracks:
                # Skip results whose artist doesn't match the queried one
                if query_artist and query_artist not in t.artist.lower():
                    continue
                key = (t.artist.lower(), t.title.lower(), t.album.lower())
                if key not in seen:
                    seen.add(key)
                    unique_tracks.append(t)

            # If artist filter removed everything, fall back to unfiltered dedup
            if not unique_tracks:
                seen = set()
                for t in tracks:
                    key = (t.artist.lower(), t.title.lower(), t.album.lower())
                    if key not in seen:
                        seen.add(key)
                        unique_tracks.append(t)

            # If only 1 unique track, auto-select and go straight to slskd
            if len(unique_tracks) == 1:
                await self._do_slskd_search(context, chat_id, unique_tracks[0], searching_msg)
                return

            # Multiple distinct tracks — store them and let user pick (page 0)
            self._spotify_candidates[chat_id] = unique_tracks
            self._spotify_page[chat_id] = 0
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

    async def _do_slskd_search(self, context, chat_id: int, track: TrackInfo, searching_msg):
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

            # Single search — filter by format locally instead of adding
            # "flac" to the query (Soulseek keyword matching is unreliable
            # for extensions embedded in file paths).
            # Also strip Spotify version suffixes like "- Remastered 2009",
            # "- Mono" etc. that add useless keywords and kill results.
            clean_title = _clean_search_title(track.title)
            search_query = f"{track.artist} {clean_title}"
            raw_responses = await self.slskd.search(search_query, timeout_secs=self.config.search_timeout_secs)

            # Try FLAC first from the same result set
            flac_results = self.slskd.parse_results(raw_responses, flac_only=True)
            ranked = self.scorer.score_results(flac_results, track)
            is_fallback = False

            # Fallback: no FLAC survived scoring — try all audio formats
            if not ranked:
                all_audio = self.slskd.parse_results(raw_responses, flac_only=False)
                ranked = self.scorer.score_results(all_audio, track)
                is_fallback = bool(ranked)

            # Fallback 2: try song-name-only search (bypasses server-side
            # artist-name filters that block e.g. Prince, Linkin Park, Beatles).
            if not ranked:
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
                raw_responses = await self.slskd.search(clean_title, timeout_secs=self.config.search_timeout_secs)

                flac_results = self.slskd.parse_results(raw_responses, flac_only=True)
                ranked = self.scorer.score_results(flac_results, track)
                if not ranked:
                    all_audio = self.slskd.parse_results(raw_responses, flac_only=False)
                    ranked = self.scorer.score_results(all_audio, track)
                    is_fallback = bool(ranked)

            # Fallback 3: keyword reduction + album year.
            # Some phrases are blocked entirely (e.g. "Purple Rain").
            # Dropping one word at a time and appending the year often
            # bypasses filters while keeping results relevant.
            if not ranked:
                reduced_queries = _build_reduced_queries(clean_title, track.year)
                if reduced_queries:
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
                        raw_responses = await self.slskd.search(
                            fallback_query, timeout_secs=self.config.search_timeout_secs
                        )
                        flac_results = self.slskd.parse_results(raw_responses, flac_only=True)
                        ranked = self.scorer.score_results(flac_results, track)
                        if ranked:
                            logger.info("Keyword-reduction fallback hit: '%s'", fallback_query)
                            break
                        all_audio = self.slskd.parse_results(raw_responses, flac_only=False)
                        ranked = self.scorer.score_results(all_audio, track)
                        if ranked:
                            is_fallback = True
                            logger.info("Keyword-reduction fallback hit (non-FLAC): '%s'", fallback_query)
                            break

            # Fallback 4: artist-only search with intelligent local filtering.
            # When titles contain non-Latin characters (CJK, Cyrillic, etc.),
            # Soulseek's all-keywords-must-match logic fails because those
            # characters rarely appear in shared file paths.  Searching for
            # just the artist returns their full catalog; we then filter
            # locally by meaningful Latin keywords extracted from the title
            # (e.g. "KURENAI" from "紅 - KURENAI - シングル…").
            if not ranked:
                latin_kw = _extract_latin_keywords(clean_title)
                logger.info(
                    "All fallbacks exhausted for '%s'. Trying artist-only "
                    "search ('%s'), filter keywords: %s",
                    search_query,
                    track.artist,
                    latin_kw or "(none — duration matching only)",
                )
                await _safe_edit(
                    searching_msg,
                    f"🎵 *{track.artist} - {track.title}*\n\n"
                    f"Still no results — browsing artist catalog…",
                    parse_mode=ParseMode.MARKDOWN,
                )
                raw_responses = await self.slskd.search(
                    track.artist, timeout_secs=self.config.search_timeout_secs
                )
                kw_lower = [k.lower() for k in latin_kw]

                for flac_only in (True, False):
                    parsed = self.slskd.parse_results(raw_responses, flac_only=flac_only)

                    if kw_lower:
                        filtered = [
                            r for r in parsed
                            if any(k in r.filename.lower() for k in kw_lower)
                        ]
                        ranked = self.scorer.score_results(filtered, track)

                    if not ranked:
                        ranked = self.scorer.score_results(parsed, track)

                    if ranked:
                        if not flac_only:
                            is_fallback = True
                        logger.info(
                            "Artist-only fallback hit (flac_only=%s, keyword_filtered=%s)",
                            flac_only,
                            bool(kw_lower),
                        )
                        break

            if not ranked:
                await _safe_edit(
                    searching_msg,
                    f"🎵 *{track.artist} - {track.title}* ({track.duration_display})\n\n"
                    f"No results found on Soulseek matching this track.\n"
                    f"Try a different search query.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            # Store pending search
            self.pending[chat_id] = PendingSearch(
                query=f"{track.artist} {track.title}",
                track=track,
                results=ranked,
                message_id=searching_msg.message_id,
                is_fallback=is_fallback,
            )

            # Show results with selection keyboard (page 0)
            results_text = self._format_results(track, ranked, is_fallback, page=0, page_size=self.config.max_results)
            await _safe_edit(
                searching_msg,
                results_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_results_keyboard(ranked, page=0, page_size=self.config.max_results),
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
        await query.answer()

        if not self._is_authorized(query.from_user.id):
            return

        chat_id = update.effective_chat.id
        data = query.data

        # Auto-mode toggle
        if data.startswith("auto:"):
            self.auto_mode = data == "auto:on"
            mode_str = "ON" if self.auto_mode else "OFF"
            await query.edit_message_text(
                f"Auto-download mode: *{mode_str}*",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Duplicate check response
        if data.startswith("dup:"):
            await self._handle_duplicate_response(update, context, chat_id, data)
            return

        # Spotify page navigation
        if data.startswith("sp_page:"):
            await self._handle_spotify_page(update, context, chat_id, data)
            return

        # Spotify track selection
        if data.startswith("sp:"):
            await self._handle_spotify_selection(update, context, chat_id, data)
            return

        # slskd results page navigation
        if data.startswith("dl_page:"):
            await self._handle_results_page(update, context, chat_id, data)
            return

        # Download selection from results
        if data.startswith("dl:"):
            await self._handle_download_selection(update, context, chat_id, data)
            return

        # Approve/reject downloaded file
        if data.startswith("approve:") or data.startswith("reject:"):
            await self._handle_approval(update, context, chat_id, data)
            return

    async def _handle_duplicate_response(self, update, context, chat_id: int, data: str):
        """Handle Continue/Cancel response to duplicate detection."""
        query = update.callback_query
        action = data.split(":", 1)[1]

        pending = self.pending.pop(chat_id, None)

        if action == "cancel" or not pending:
            await query.edit_message_text("Cancelled.")
            return

        # User chose to continue — proceed with the search
        await query.edit_message_text(
            f"Continuing with search: `{pending.query}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await self._do_search(update, context, pending.query)

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

        # Send a new message for the slskd search progress
        searching_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔍 Searching slskd for FLAC...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await self._do_slskd_search(context, chat_id, track, searching_msg)

    async def _handle_results_page(self, update, context, chat_id: int, data: str):
        """Handle slskd results page navigation (◀️ / ▶️)."""
        query = update.callback_query
        pending = self.pending.get(chat_id)
        if not pending or not pending.track:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            return

        pending.page = page
        results_text = self._format_results(
            pending.track,
            pending.results,
            pending.is_fallback,
            page=page,
            page_size=self.config.max_results,
        )
        await query.edit_message_text(
            results_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_results_keyboard(pending.results, page=page, page_size=self.config.max_results),
        )

    async def _handle_download_selection(self, update, context, chat_id: int, data: str):
        """Handle when user picks a file to download from results."""
        query = update.callback_query
        pending = self.pending.get(chat_id)
        if not pending:
            await query.edit_message_text("Search expired. Send a new query.")
            return

        action = data.split(":", 1)[1]

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
            return

        result = pending.results[index]
        track = pending.track

        # DON'T edit the results message — send a NEW message for this download
        # The results keyboard stays active so user can pick more files
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

        # Run download in background so user can select more files
        context.application.create_task(
            self._do_download(context, chat_id, track, result, status_msg, index),
            update=update,
        )

    # =========================================================================
    # DOWNLOAD + PREVIEW + APPROVAL
    # =========================================================================

    def _next_dl_id(self) -> str:
        """Generate a short unique download ID."""
        self._dl_counter += 1
        return str(self._dl_counter)

    async def _do_download(
        self, context, chat_id: int, track: TrackInfo, result: SearchResult, status_msg, result_index: int = 0
    ):
        """Download a file, send it to Telegram for preview, and ask for approval."""
        dl_id = self._next_dl_id()
        label = f"#{result_index + 1}"

        try:
            # Enqueue download in slskd
            success = self.slskd.enqueue_download(result)
            if not success:
                await status_msg.edit_text(
                    f"❌ Failed to enqueue download from `{result.username}`.\nThe user might be offline.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            # Wait for download to complete
            status = await self.slskd.wait_for_download(
                username=result.username,
                filename=result.filename,
                timeout_secs=self.config.download_timeout_secs,
            )

            if status is None or status.is_failed:
                state = status.state if status else "Timeout"
                await status_msg.edit_text(
                    f"❌ Download failed: {state}\nFile: `{result.basename}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                self._add_history(track, result, "failed")
                return

            # Find the downloaded file on disk
            source_path = self.processor.find_downloaded_file(result.username, result.filename)
            if not source_path:
                await status_msg.edit_text(
                    "❌ Downloaded file not found on disk.\nCheck DOWNLOAD_DIR configuration.",
                )
                self._add_history(track, result, "file_not_found")
                return

            # Run FLAC authenticity analysis (spectral cutoff detection)
            flac_verdict = await self._analyze_flac(source_path) if result.extension == "flac" else None

            # Store as pending download for approval
            pending_dl = PendingDownload(
                track=track,
                result=result,
                source_path=source_path,
                status_message_id=status_msg.message_id,
            )
            self.downloads[dl_id] = pending_dl

            # Build quality + analysis line
            quality_line = f"Quality: {result.quality_display} | {result.duration_display}"
            if flac_verdict:
                quality_line += f"\n{flac_verdict.display}"

            # Update status message
            await status_msg.edit_text(
                f"✅ *{label} Downloaded!* Sending preview...\n`{result.basename}`\n{quality_line}",
                parse_mode=ParseMode.MARKDOWN,
            )

            # Send the file to Telegram for preview
            file_size = os.path.getsize(source_path) if os.path.isfile(source_path) else 0
            caption = f"{label} {quality_line}\nSave to library?"

            if file_size > TELEGRAM_FILE_LIMIT:
                # File too large to send directly — create a 30s preview clip
                preview_path = await self._create_preview(source_path)
                if preview_path:
                    try:
                        target_name = self.processor.build_filename(
                            track.artist, f"{track.title} (30s preview)", result.extension
                        )
                        preview_caption = (
                            f"🎧 {label} 30s preview "
                            f"(full file: {file_size / (1024 * 1024):.0f}MB)\n"
                            f"{quality_line}\n"
                            f"Save to library?"
                        )
                        with open(preview_path, "rb") as f:
                            await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=f,
                                filename=target_name,
                                title=f"{track.title} (30s preview)",
                                performer=track.artist,
                                duration=30,
                                caption=preview_caption,
                                reply_markup=build_approve_keyboard(dl_id),
                            )
                    finally:
                        # Clean up preview file
                        with contextlib.suppress(OSError):
                            os.unlink(preview_path)
                else:
                    # Preview creation failed — fall back to text-only message
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"📁 {label} File too large for Telegram "
                            f"({file_size / (1024 * 1024):.0f}MB > 50MB limit).\n"
                            f"{quality_line}\n\n"
                            f"Save to library?"
                        ),
                        reply_markup=build_approve_keyboard(dl_id),
                    )
            else:
                target_name = self.processor.build_filename(track.artist, track.title, result.extension)
                try:
                    # Try sending as playable audio first
                    with open(source_path, "rb") as f:
                        await context.bot.send_audio(
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
                    # Fallback: send as document (works for some edge cases)
                    logger.info("send_audio failed, falling back to send_document for %s", result.basename)
                    with open(source_path, "rb") as f:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            filename=target_name,
                            caption=caption,
                            reply_markup=build_approve_keyboard(dl_id),
                        )

        except Exception:
            logger.exception(f"Download failed for {result.basename}")
            await status_msg.edit_text(
                f"❌ Error downloading `{result.basename}`. Check logs.",
                parse_mode=ParseMode.MARKDOWN,
            )

    async def _handle_approval(self, update, context, chat_id: int, data: str):
        """Handle approve/reject of a downloaded file."""
        query = update.callback_query
        action, dl_id = data.split(":", 1)

        pending_dl = self.downloads.pop(dl_id, None)
        if not pending_dl:
            await query.edit_message_reply_markup(reply_markup=None)
            return

        track = pending_dl.track
        result = pending_dl.result

        if action == "approve":
            # Copy to output directory with proper naming
            if pending_dl.source_path:
                target_path = self.processor.process_file(pending_dl.source_path, track.artist, track.title)
                if target_path:
                    target_name = os.path.basename(target_path)
                    await self._edit_approval_message(query, f"✅ Saved: `{target_name}`")
                    self._add_history(track, result, "success")
                    logger.info(f"Approved and saved: {target_name}")
                else:
                    await self._edit_approval_message(query, "❌ Failed to save file. Check logs.")
                    self._add_history(track, result, "process_failed")
            else:
                await self._edit_approval_message(query, "❌ Source file not found.")
                self._add_history(track, result, "file_not_found")

        elif action == "reject":
            await self._edit_approval_message(query, f"🚫 Rejected: {track.artist} - {track.title}")
            self._add_history(track, result, "rejected")
            logger.info(f"Rejected: {track.artist} - {track.title} ({result.basename})")

    @staticmethod
    async def _edit_approval_message(query, text: str):
        """Edit the approval message — works for both audio captions and text messages."""
        try:
            # Try editing as audio caption first
            await query.edit_message_caption(caption=text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Fall back to editing as text message (for files > 50MB)
            await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN)

    # =========================================================================
    # FLAC ANALYSIS
    # =========================================================================

    @staticmethod
    async def _analyze_flac(filepath: str) -> FlacVerdict | None:
        """Run spectral analysis on a FLAC file in a thread to avoid blocking."""
        import asyncio

        try:
            verdict = await asyncio.to_thread(analyze_flac, filepath)
            if verdict:
                logger.info("FLAC analysis for %s: %s (cutoff=%.1fkHz)", filepath, verdict.verdict, verdict.cutoff_khz)
            return verdict
        except Exception:
            logger.exception("FLAC analysis failed for %s", filepath)
            return None

    @staticmethod
    async def _create_preview(filepath: str, duration_secs: float = 30.0) -> str | None:
        """Create a short audio preview clip in a thread to avoid blocking."""
        import asyncio

        try:
            return await asyncio.to_thread(create_preview_clip, filepath, duration_secs)
        except Exception:
            logger.exception("Preview clip creation failed for %s", filepath)
            return None

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
        is_fallback: bool = False,
        page: int = 0,
        page_size: int = 10,
    ) -> str:
        """Format search results for display in Telegram (one page)."""
        total = len(results)
        start = page * page_size
        end = min(start + page_size, total)
        total_pages = (total + page_size - 1) // page_size

        if is_fallback:
            header = [
                f"🎵 *{track.artist} - {track.title}*",
                f"Duration: {track.duration_display} | Album: {track.album}\n",
                f"⚠️ No FLAC found — showing all formats ({total} matches):\n",
            ]
        else:
            header = [
                f"🎵 *{track.artist} - {track.title}*",
                f"Duration: {track.duration_display} | Album: {track.album}\n",
                f"Found {total} FLAC matches:\n",
            ]

        if total_pages > 1:
            header.append(f"📄 Page {page + 1}/{total_pages}\n")

        lines = header
        for i in range(start, end):
            r = results[i]
            slot_icon = "🟢" if r.has_free_slot else "🔴"
            fmt = r.extension.upper()
            format_tag = f" [{fmt}]" if is_fallback else ""
            safe_name = _escape_md(r.basename)
            lines.append(
                f"*#{i + 1}* {slot_icon} `{r.duration_display}` | "
                f"{r.quality_display}{format_tag} | {r.size_mb:.0f}MB\n"
                f"    *{safe_name}*"
            )

        return "\n".join(lines)

    def _add_history(self, track: TrackInfo, result: SearchResult, status: str):
        """Add an entry to download history."""
        self.history.append(
            {
                "artist": track.artist,
                "title": track.title,
                "filename": f"{track.artist} - {track.title}.{result.extension}",
                "source_user": result.username,
                "status": status,
            }
        )
        # Keep last 50 entries
        if len(self.history) > 50:
            self.history = self.history[-50:]


def create_bot(config: Config) -> Application:
    """
    Create and configure the Telegram bot application.

    Args:
        config: Application configuration.

    Returns:
        Configured telegram Application ready to run.
    """
    bot = MusicBot(config)

    app = Application.builder().token(config.telegram_bot_token).build()

    # Command handlers
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("auto", bot.cmd_auto))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("history", bot.cmd_history))

    # Callback query handler (inline keyboard buttons)
    app.add_handler(CallbackQueryHandler(bot.handle_callback))

    # Text message handler (song search) — must be last
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_text))

    logger.info("Telegram bot configured")
    return app
