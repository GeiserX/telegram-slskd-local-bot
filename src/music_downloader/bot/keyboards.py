"""
Inline keyboard builders for the Telegram bot.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult


def build_results_keyboard(results: list[SearchResult], max_results: int = 5) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with search results for the user to pick from.

    Each button shows: duration | quality | size
    Callback data format: dl:<index>
    """
    buttons = []
    for i, result in enumerate(results[:max_results]):
        label = f"{result.duration_display} | {result.quality_display} | {result.size_mb:.0f}MB"
        # Prefix with position number
        label = f"#{i + 1} {label}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dl:{i}")])

    # Add auto-pick and cancel buttons
    action_row = []
    if results:
        action_row.append(InlineKeyboardButton("Auto-pick best", callback_data="dl:auto"))
    action_row.append(InlineKeyboardButton("Cancel", callback_data="dl:cancel"))
    buttons.append(action_row)

    return InlineKeyboardMarkup(buttons)


def build_approve_keyboard(download_id: str) -> InlineKeyboardMarkup:
    """Build approve/reject keyboard for a downloaded file."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Save to library", callback_data=f"approve:{download_id}"),
                InlineKeyboardButton("🚫 Reject", callback_data=f"reject:{download_id}"),
            ]
        ]
    )


def build_duplicate_keyboard() -> InlineKeyboardMarkup:
    """Build Continue/Cancel keyboard for duplicate detection."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Continue anyway", callback_data="dup:continue"),
                InlineKeyboardButton("Cancel", callback_data="dup:cancel"),
            ]
        ]
    )


def build_spotify_keyboard(tracks: list[TrackInfo]) -> InlineKeyboardMarkup:
    """Build inline keyboard for selecting from multiple Spotify results."""
    buttons = []
    for i, t in enumerate(tracks):
        label = f"#{i + 1} {t.artist} - {t.title} ({t.duration_display})"
        # Truncate to fit Telegram's button text limit
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"sp:{i}")])

    buttons.append([InlineKeyboardButton("Cancel", callback_data="sp:cancel")])
    return InlineKeyboardMarkup(buttons)


def build_auto_mode_keyboard(current_mode: bool) -> InlineKeyboardMarkup:
    """Build keyboard to toggle auto mode."""
    if current_mode:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Disable auto-mode", callback_data="auto:off")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("Enable auto-mode", callback_data="auto:on")]])
