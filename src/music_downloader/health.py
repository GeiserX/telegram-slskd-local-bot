"""What GET /health reports: is the bot really polling Telegram, and does slskd answer.

Telegram-free: the bot records each successful getUpdates poll with
mark_poll(), and slskd_probe_loop() asks slskd for application/state in the
background, so a health request only reads two timestamps.
"""

import asyncio
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# A getUpdates long poll returns at least every 10 s, so two minutes without a
# successful one means polling is stuck or Telegram is unreachable.
TELEGRAM_MAX_AGE_SECS = 120
# The probe runs every SLSKD_PROBE_INTERVAL_SECS; three misses in a row fail.
SLSKD_MAX_AGE_SECS = 60
SLSKD_PROBE_INTERVAL_SECS = 20


class HealthState:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.last_poll: float | None = None
        self.last_slskd_ok: float | None = None
        # Set by the bot once the Telegram application exists.
        self.telegram_running: Callable[[], bool] = lambda: False

    def mark_poll(self) -> None:
        self.last_poll = self._clock()

    def mark_slskd_ok(self) -> None:
        self.last_slskd_ok = self._clock()

    def report(self) -> tuple[bool, dict]:
        """(healthy, body): the body names the first check that failed."""
        now = self._clock()
        if not self.telegram_running():
            return False, {"status": "unhealthy", "failed": "telegram", "reason": "application not running"}
        if self.last_poll is None:
            return False, {"status": "unhealthy", "failed": "telegram", "reason": "no successful getUpdates yet"}
        age = now - self.last_poll
        if age > TELEGRAM_MAX_AGE_SECS:
            return False, {
                "status": "unhealthy",
                "failed": "telegram",
                "reason": f"last successful getUpdates {age:.0f} s ago",
            }
        if self.last_slskd_ok is None:
            return False, {"status": "unhealthy", "failed": "slskd", "reason": "application/state never answered"}
        age = now - self.last_slskd_ok
        if age > SLSKD_MAX_AGE_SECS:
            return False, {
                "status": "unhealthy",
                "failed": "slskd",
                "reason": f"application/state last answered {age:.0f} s ago",
            }
        return True, {"status": "healthy"}


async def slskd_probe_loop(
    probe: Callable[[], bool], health: HealthState, interval: float = SLSKD_PROBE_INTERVAL_SECS
) -> None:
    """Ask slskd for application/state every *interval* seconds (in a thread) and record each answer."""
    while True:
        try:
            if await asyncio.to_thread(probe):
                health.mark_slskd_ok()
        except Exception:
            logger.exception("slskd health probe crashed")
        await asyncio.sleep(interval)
