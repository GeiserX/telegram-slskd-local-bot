"""The wishlist checker: search wished tracks again and hand over what turned up.

A wish is "any" (nothing was found: any copy will do) or "better" (only a
copy above the wish's baseline tier counts, search.scorer.quality_tier). The
checker wakes every WISHLIST_TICK_SECS and searches each wish whose period
(WISHLIST_CHECK_HOURS) has passed, one at a time with a pause between
searches so Soulseek peers are not flooded. What to do with a hit is the
front end's call, through the *deliver* callback.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.wishlist_repo import WANTED_ANY, WANTED_BETTER, Wish, WishlistRepository
from music_downloader.pipeline.search import RankedResults, clean_search_title
from music_downloader.search.scorer import quality_tier

logger = logging.getLogger(__name__)

__all__ = ["WANTED_ANY", "WANTED_BETTER", "Wish", "WishDelivery", "check_due", "is_due", "satisfying", "wish_query"]

# How often the checker wakes to look for due wishes.
WISHLIST_TICK_SECS = 3600

# deliver(wish, matches) -> True when the wish is fulfilled (fetched and
# handed over: the wish is removed), False when the owner was only shown the
# copies (the wish stays and is not shown again until its next period).
WishDelivery = Callable[[Wish, RankedResults], Awaitable[bool]]
SearchFn = Callable[[str, TrackInfo, str], Awaitable[RankedResults]]


def wish_query(track: TrackInfo) -> str:
    """The Soulseek query for a wished track: artist plus the title without version noise."""
    return f"{track.artist} {clean_search_title(track.title)}".strip()


def is_due(wish: Wish, now: float, period_secs: float) -> bool:
    """True when *period_secs* have passed since the wish was last checked, shown or made."""
    last = max(wish.last_checked_at or 0.0, wish.notified_at or 0.0) or wish.created_at
    return now - last >= period_secs


def satisfying(wish: Wish, ranked: RankedResults) -> RankedResults:
    """The copies in *ranked* that satisfy *wish*, in rank order.

    "any": all of them. "better": those above the wish's baseline tier.
    """
    if wish.wanted == WANTED_BETTER:
        baseline = wish.baseline_tier if wish.baseline_tier is not None else -1
        return RankedResults([r for r in ranked if quality_tier(r) > baseline], getattr(ranked, "hidden", 0))
    return ranked


async def check_due(
    repo: WishlistRepository,
    search: SearchFn,
    deliver: WishDelivery,
    period_secs: float,
    pause_secs: float,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    """Search every due wish once, sequentially; returns how many searches ran.

    A wish with no satisfying copy only gets its last_checked_at and checks
    updated. A hit goes to *deliver*: fulfilled wishes are removed, shown ones
    get notified_at. A search or delivery that raises counts as a check.
    """
    due = [w for w in repo.list_all() if is_due(w, now(), period_secs)]
    searched = 0
    for wish in due:
        if repo.get(wish.id) is None:  # removed while earlier wishes were searched
            continue
        if searched:
            await sleep(pause_secs)
        searched += 1
        try:
            ranked = await search(wish_query(wish.track), wish.track, wish.profile)
        except Exception:
            logger.warning("Wishlist search failed for wish %s", wish.id, exc_info=True)
            repo.mark_checked(wish.id, now())
            continue
        matches = satisfying(wish, ranked)
        if not matches:
            repo.mark_checked(wish.id, now())
            continue
        try:
            fulfilled = await deliver(wish, matches)
        except Exception:
            logger.exception("Wishlist delivery failed for wish %s", wish.id)
            repo.mark_checked(wish.id, now())
            continue
        if fulfilled:
            repo.remove(wish.chat_id, wish.id)
            logger.info("Wish %s fulfilled: %s - %s", wish.id, wish.track.artist, wish.track.title)
        else:
            repo.mark_notified(wish.id, now())
    return searched
