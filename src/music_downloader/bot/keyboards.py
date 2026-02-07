"""
Inline keyboard builders for the Telegram bot.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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


def build_confirm_keyboard() -> InlineKeyboardMarkup:
    """Build a simple confirm/cancel keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data="confirm:yes"),
                InlineKeyboardButton("Cancel", callback_data="confirm:no"),
            ]
        ]
    )


def build_auto_mode_keyboard(current_mode: bool) -> InlineKeyboardMarkup:
    """Build keyboard to toggle auto mode."""
    if current_mode:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Disable auto-mode", callback_data="auto:off")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("Enable auto-mode", callback_data="auto:on")]])
