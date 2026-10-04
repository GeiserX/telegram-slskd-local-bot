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
    *,
    search_id: str,
    stop_wish_id: int | None = None,
) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with search results for the user to pick from.

    Each button shows: duration | quality | size
    Callback data format: dl:<search_id>:<index> — the search_id binds the
    button to the search that produced it, so a stale keyboard from an
    earlier search can never act on the current one.

    Below the results: "Wait for a better copy" (wish:better:<search_id>),
    or "Stop waiting" (wish:stop:<id>) when the list comes from wish *stop_wish_id*.
    """
    start = page * page_size
    end = min(start + page_size, len(results))
    page_results = results[start:end]

    buttons = []
    for i, result in enumerate(page_results):
        absolute_idx = start + i
        label = f"#{absolute_idx + 1} {result.duration_display} | {result.quality_display} | {result.size_mb:.0f}MB"
        buttons.append([InlineKeyboardButton(label, callback_data=f"dl:{search_id}:{absolute_idx}")])

    if stop_wish_id is not None:
        buttons.append([InlineKeyboardButton("\U0001f515 Stop waiting", callback_data=f"wish:stop:{stop_wish_id}")])
    elif results:
        buttons.append([build_wait_better_button(search_id)])

    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"dl_page:{search_id}:{page - 1}"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"dl_page:{search_id}:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    # Action row
    action_row = []
    if results:
        action_row.append(InlineKeyboardButton("Auto-pick best", callback_data=f"dl:{search_id}:auto"))
    action_row.append(InlineKeyboardButton("Cancel", callback_data=f"dl:{search_id}:cancel"))
    buttons.append(action_row)

    return InlineKeyboardMarkup(buttons)


def build_wait_better_button(search_id: str) -> InlineKeyboardButton:
    """Wishlist: search this track again later for a copy better than result #1."""
    return InlineKeyboardButton("⏳ Wait for a better copy", callback_data=f"wish:better:{search_id}")


def build_nothing_found_keyboard(search_id: str) -> InlineKeyboardMarkup:
    """Nothing found: search Soulseek directly, or put the track on the wishlist."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001f50e Search Soulseek directly", callback_data="direct:search")],
            [InlineKeyboardButton("\U0001f514 Tell me when it appears", callback_data=f"wish:any:{search_id}")],
        ]
    )


def build_wishlist_keyboard(wish_ids: list[int]) -> InlineKeyboardMarkup:
    """/wishlist: one Remove button per wish, numbered as in the list."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"Remove #{n}", callback_data=f"wish:rm:{wid}")] for n, wid in enumerate(wish_ids, 1)]
    )


def without_wish_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """*markup* minus its wishlist buttons (callback data wish:...), or None when nothing is left."""
    if markup is None:
        return None
    rows = [[b for b in row if not str(b.callback_data or "").startswith("wish:")] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(rows) if rows else None


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

    buttons.append([InlineKeyboardButton("\U0001f50e Search Soulseek directly", callback_data="direct:search")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="sp:cancel")])
    return InlineKeyboardMarkup(buttons)


def build_auto_mode_keyboard(current_mode: bool) -> InlineKeyboardMarkup:
    """Build keyboard to toggle auto mode."""
    if current_mode:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Disable auto-mode", callback_data="auto:off")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("Enable auto-mode", callback_data="auto:on")]])


def build_delivery_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    """Build keyboard to switch between library and chat delivery."""
    if current_mode == "chat":
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("\U0001f4da Save to library instead", callback_data="deliver:library")]]
        )
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("\U0001f4e8 Send in chat instead", callback_data="deliver:chat")]]
    )


def build_send_format_keyboard(current_format: str, labels: dict[str, str]) -> InlineKeyboardMarkup:
    """One button per send format (*labels*: key -> label), the current one ticked."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"\u2705 {label}" if key == current_format else label, callback_data=f"format:{key}"
                )
                for key, label in labels.items()
            ]
        ]
    )


def build_direct_search_keyboard() -> InlineKeyboardMarkup:
    """Button to search Soulseek directly without Spotify resolution."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("\U0001f50e Search Soulseek directly", callback_data="direct:search")]]
    )


def build_import_confirm_keyboard(job_id: int) -> InlineKeyboardMarkup:
    """Confirm/cancel keyboard for playlist import, with a review-vs-auto choice."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Review each track", callback_data=f"ic:{job_id}")],
            [InlineKeyboardButton("\U0001f916 Auto-save all", callback_data=f"ic:{job_id}:auto")],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"ix:{job_id}")],
        ]
    )


def build_import_track_keyboard(job_id: int, track_id: int, dl_id: str) -> InlineKeyboardMarkup:
    """Approve/reject/skip keyboard for individual import track downloads."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Save", callback_data=f"ia:{job_id}:{track_id}:{dl_id}"),
                InlineKeyboardButton("\U0001f6ab Reject", callback_data=f"ir:{job_id}:{track_id}"),
            ],
            [
                InlineKeyboardButton("⏭ Skip track", callback_data=f"is:{job_id}:{track_id}"),
            ],
        ]
    )


def build_import_skip_keyboard(job_id: int, track_id: int) -> InlineKeyboardMarkup:
    """Reject/skip keyboard for import tracks with no download available."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\U0001f6ab Mark failed", callback_data=f"ir:{job_id}:{track_id}"),
                InlineKeyboardButton("⏭ Skip track", callback_data=f"is:{job_id}:{track_id}"),
            ],
        ]
    )


def build_import_retry_keyboard(job_id: int, track_id: int, dl_id: str, has_next: bool) -> InlineKeyboardMarkup:
    """Retry (+ Try next result) and Mark failed / Skip, for a failed track in a review-mode import."""
    retry_row = [InlineKeyboardButton("\U0001f504 Retry", callback_data=f"retry:{dl_id}")]
    if has_next:
        retry_row.append(InlineKeyboardButton("⏭ Try next result", callback_data=f"next:{dl_id}"))
    return InlineKeyboardMarkup(
        [
            retry_row,
            [
                InlineKeyboardButton("\U0001f6ab Mark failed", callback_data=f"ir:{job_id}:{track_id}"),
                InlineKeyboardButton("⏭ Skip track", callback_data=f"is:{job_id}:{track_id}"),
            ],
        ]
    )


def build_retry_keyboard(dl_id: str) -> InlineKeyboardMarkup:
    """Retry button shown on download failure."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("\U0001f504 Retry", callback_data=f"retry:{dl_id}")]])


def build_retry_next_keyboard(dl_id: str) -> InlineKeyboardMarkup:
    """Retry + next result buttons shown after repeated failure."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\U0001f504 Retry", callback_data=f"retry:{dl_id}"),
                InlineKeyboardButton("⏭ Try next result", callback_data=f"next:{dl_id}"),
            ]
        ]
    )


def build_album_offer_keyboard(dl_id: str) -> InlineKeyboardMarkup:
    """Shown once a track is saved or sent: take the whole folder it came from (alb:<dl_id>)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("\U0001f4bf Whole album from this source", callback_data=f"alb:{dl_id}")]]
    )


def build_album_confirm_keyboard(dl_id: str, count: int) -> InlineKeyboardMarkup:
    """Under the folder listing: fetch every file (albgo) or drop the offer (albno)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(f"⬇️ Get all {count}", callback_data=f"albgo:{dl_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"albno:{dl_id}"),
            ]
        ]
    )


def build_album_retry_keyboard(dl_id: str) -> InlineKeyboardMarkup:
    """The peer did not answer the listing: ask again (alb) or drop the offer (albno)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\U0001f504 Retry", callback_data=f"alb:{dl_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"albno:{dl_id}"),
            ]
        ]
    )
