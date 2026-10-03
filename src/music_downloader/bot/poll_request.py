"""The getUpdates HTTP request, reporting every successful poll to the health state."""

from collections.abc import Callable

from telegram.request import HTTPXRequest


class PollTrackingRequest(HTTPXRequest):
    """HTTPXRequest that calls *on_success* whenever Telegram answers a getUpdates with 200.

    Used only as the Application's get_updates_request, so every request
    through it is a poll.
    """

    def __init__(self, on_success: Callable[[], None], **kwargs) -> None:
        kwargs.setdefault("connection_pool_size", 1)
        super().__init__(**kwargs)
        self._on_success = on_success

    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        code, payload = await super().do_request(*args, **kwargs)
        if code == 200:
            self._on_success()
        return code, payload
