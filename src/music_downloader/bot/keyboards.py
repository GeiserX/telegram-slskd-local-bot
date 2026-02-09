"""
Inline keyboard builders for the Telegram bot.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult


def build_results_keyboard(
    results: list[SearchResult],
    page: int = 0,
    page_size: int = 10,
) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with search results for the user to pick from.

    Each button shows: duration | quality | size
    Callback data format: dl:<index>
    """
    start = page * page_size
    end = min(start + page_size, len(results))
    page_results = results[start:end]

    buttons = []
    for i, result in enumerate(page_results):
        absolute_idx = start + i
        label = f"#{absolute_idx + 1} {result.duration_display} | {result.quality_display} | {result.size_mb:.0f}MB"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dl:{absolute_idx}")])

    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"dl_page:{page - 1}"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"dl_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    # Action row
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


def build_spotify_keyboard(
    tracks: list[TrackInfo],
    page: int = 0,
    page_size: int = 5,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for selecting from multiple Spotify results."""
    start = page * page_size
    end = min(start + page_size, len(tracks))
    page_tracks = tracks[start:end]

    buttons = []
    for i, t in enumerate(page_tracks):
        absolute_idx = start + i
        label = f"#{absolute_idx + 1} {t.artist} - {t.title} ({t.duration_display})"
        # Truncate to fit Telegram's button text limit
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"sp:{absolute_idx}")])

    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"sp_page:{page - 1}"))
    if end < len(tracks):
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"sp_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton("Cancel", callback_data="sp:cancel")])
    return InlineKeyboardMarkup(buttons)


def build_auto_mode_keyboard(current_mode: bool) -> InlineKeyboardMarkup:
    """Build keyboard to toggle auto mode."""
    if current_mode:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Disable auto-mode", callback_data="auto:off")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("Enable auto-mode", callback_data="auto:on")]])
