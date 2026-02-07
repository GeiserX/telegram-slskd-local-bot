"""
Telegram bot handlers for music search and download.
"""

import logging
import os
from dataclasses import dataclass, field

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from music_downloader.bot.keyboards import build_auto_mode_keyboard, build_results_keyboard
from music_downloader.config import Config
from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.scorer import ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

logger = logging.getLogger(__name__)


@dataclass
class PendingSearch:
    """Holds state for an active search/download session."""

    query: str
    track: TrackInfo
    results: list[SearchResult] = field(default_factory=list)
    message_id: int | None = None


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

        # Download history (last N downloads)
        self.history: list[dict] = []

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to use the bot."""
        if not self.config.telegram_allowed_users:
            return True  # No restrictions
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
            "Send me a song name (e.g., `Nancy Sinatra Bang Bang`) and I'll find and download it in FLAC.\n\n"
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
            "When ON, the best match is downloaded automatically without asking you to pick.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_auto_mode_keyboard(self.auto_mode),
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command — show active searches."""
        if not await self._check_auth(update):
            return

        if not self.pending:
            await update.message.reply_text("No active searches or downloads.")
            return

        lines = ["*Active searches:*\n"]
        for _chat_id, pending in self.pending.items():
            lines.append(f"• {pending.track.artist} - {pending.track.title}")
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
            icon = "✅" if status == "success" else "❌"
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

        # Step 1: Resolve metadata via Spotify
        searching_msg = await update.message.reply_text(f"🔍 Looking up: `{query}`", parse_mode=ParseMode.MARKDOWN)

        track = self.spotify.search(query)
        if not track:
            await searching_msg.edit_text(
                f"Could not find `{query}` on Spotify. Try a more specific query.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await searching_msg.edit_text(
            f"🎵 *{track.artist} - {track.title}*\n"
            f"Album: {track.album} ({track.year})\n"
            f"Duration: {track.duration_display}\n\n"
            f"Searching slskd for FLAC...",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Step 2: Search slskd (use resolved metadata + "flac" for better results)
        search_query = f"{track.artist} {track.title} flac"
        raw_responses = await self.slskd.search(search_query, timeout_secs=self.config.search_timeout_secs)
        all_results = self.slskd.parse_results(raw_responses)

        # Step 3: Score and rank
        ranked = self.scorer.score_results(all_results, track)

        if not ranked:
            await searching_msg.edit_text(
                f"🎵 *{track.artist} - {track.title}* ({track.duration_display})\n\n"
                f"No FLAC results found on Soulseek matching this track.\n"
                f"Try a different search query.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Store pending search
        self.pending[chat_id] = PendingSearch(
            query=query,
            track=track,
            results=ranked,
            message_id=searching_msg.message_id,
        )

        # Step 4: Auto-mode or show results
        if self.auto_mode:
            await self._do_download(update, context, chat_id, 0, searching_msg)
        else:
            # Show top results
            results_text = self._format_results(track, ranked[: self.config.max_results])
            await searching_msg.edit_text(
                results_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_results_keyboard(ranked, self.config.max_results),
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

        # Download selection
        if data.startswith("dl:"):
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

            await self._do_download(update, context, chat_id, index, query.message)

    # =========================================================================
    # DOWNLOAD LOGIC
    # =========================================================================

    async def _do_download(self, update, context, chat_id: int, index: int, message):
        """Execute the download for the selected result."""
        pending = self.pending.get(chat_id)
        if not pending or index >= len(pending.results):
            await message.edit_text("Invalid selection.")
            return

        result = pending.results[index]
        track = pending.track

        await message.edit_text(
            f"⬇️ *Downloading...*\n\n"
            f"{track.artist} - {track.title}\n"
            f"From: `{result.username}`\n"
            f"File: `{result.basename}`\n"
            f"Quality: {result.quality_display}",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Enqueue download in slskd
        success = self.slskd.enqueue_download(result)
        if not success:
            await message.edit_text(
                f"❌ Failed to enqueue download from {result.username}.\n"
                f"The user might be offline. Try another result.",
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
            await message.edit_text(f"❌ Download failed: {state}\nTry another result or search again.")
            self._add_history(track, result, "failed")
            return

        # Find the downloaded file on disk
        source_path = self.processor.find_downloaded_file(result.username, result.filename)
        if not source_path:
            await message.edit_text("❌ Downloaded file not found on disk. Check DOWNLOAD_DIR configuration.")
            self._add_history(track, result, "file_not_found")
            return

        # Rename and move to output directory
        target_path = self.processor.process_file(source_path, track.artist, track.title)
        if not target_path:
            await message.edit_text("❌ Failed to process file. Check logs.")
            self._add_history(track, result, "process_failed")
            return

        # Cleanup the original download
        self.processor.cleanup_download(source_path)

        # Remove from pending
        self.pending.pop(chat_id, None)

        # Report success
        target_name = os.path.basename(target_path) if target_path else f"{track.artist} - {track.title}.flac"
        await message.edit_text(
            f"✅ *Downloaded and saved!*\n\n"
            f"`{target_name}`\n"
            f"Quality: {result.quality_display}\n"
            f"Duration: {result.duration_display}",
            parse_mode=ParseMode.MARKDOWN,
        )

        self._add_history(track, result, "success")
        logger.info(f"Successfully processed: {target_name}")

    def _format_results(self, track: TrackInfo, results: list[SearchResult]) -> str:
        """Format search results for display in Telegram."""
        lines = [
            f"🎵 *{track.artist} - {track.title}*",
            f"Duration: {track.duration_display} | Album: {track.album}\n",
            f"Found {len(results)} matches:\n",
        ]

        for i, r in enumerate(results):
            slot_icon = "🟢" if r.has_free_slot else "🔴"
            lines.append(
                f"*#{i + 1}* {slot_icon} `{r.duration_display}` | "
                f"{r.quality_display} | {r.size_mb:.0f}MB\n"
                f"    _{r.basename}_"
            )

        return "\n".join(lines)

    def _add_history(self, track: TrackInfo, result: SearchResult, status: str):
        """Add an entry to download history."""
        self.history.append(
            {
                "artist": track.artist,
                "title": track.title,
                "filename": f"{track.artist} - {track.title}.flac",
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
