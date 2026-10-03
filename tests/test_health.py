"""GET /health is 200 only while Telegram polling and slskd both work."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.request import HTTPXRequest

from music_downloader.__main__ import HealthHandler
from music_downloader.bot.poll_request import PollTrackingRequest
from music_downloader.health import HealthState, slskd_probe_loop
from music_downloader.search.slskd_client import SlskdClient


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _healthy_state(clock: FakeClock) -> HealthState:
    health = HealthState(clock=clock)
    health.telegram_running = lambda: True
    health.mark_poll()
    health.mark_slskd_ok()
    return health


class TestHealthState:
    def test_healthy_when_both_are_fresh(self):
        assert _healthy_state(FakeClock()).report() == (True, {"status": "healthy"})

    def test_telegram_not_running(self):
        health = _healthy_state(FakeClock())
        health.telegram_running = lambda: False
        ok, body = health.report()
        assert not ok and body["failed"] == "telegram"

    def test_no_poll_yet(self):
        health = HealthState(clock=FakeClock())
        health.telegram_running = lambda: True
        health.mark_slskd_ok()
        ok, body = health.report()
        assert not ok and body["failed"] == "telegram"

    def test_stale_poll_fails_telegram(self):
        clock = FakeClock()
        health = _healthy_state(clock)
        clock.now += 121
        health.mark_slskd_ok()  # slskd stays fresh: only the poll is old
        ok, body = health.report()
        assert not ok
        assert body["failed"] == "telegram"
        assert "121 s ago" in body["reason"]

    def test_poll_just_inside_the_window_is_fine(self):
        clock = FakeClock()
        health = _healthy_state(clock)
        clock.now += 119
        health.mark_slskd_ok()
        assert health.report()[0]

    def test_stale_slskd_fails_slskd(self):
        clock = FakeClock()
        health = _healthy_state(clock)
        clock.now += 61
        health.mark_poll()  # Telegram stays fresh: only slskd is old
        ok, body = health.report()
        assert not ok
        assert body["failed"] == "slskd"
        assert "61 s ago" in body["reason"]

    def test_slskd_never_answered(self):
        health = HealthState(clock=FakeClock())
        health.telegram_running = lambda: True
        health.mark_poll()
        ok, body = health.report()
        assert not ok and body["failed"] == "slskd"


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(server, path):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestHttp:
    def test_healthy_is_200(self, server):
        server.health = _healthy_state(FakeClock())
        assert _get(server, "/health") == (200, b'{"status":"healthy"}')

    def test_stale_slskd_is_503_with_a_one_line_reason(self, server):
        clock = FakeClock()
        server.health = _healthy_state(clock)
        clock.now += 61
        server.health.mark_poll()
        code, body = _get(server, "/health")
        assert code == 503
        assert b"\n" not in body
        assert json.loads(body)["failed"] == "slskd"

    def test_stale_poll_is_503(self, server):
        clock = FakeClock()
        server.health = _healthy_state(clock)
        clock.now += 200
        server.health.mark_slskd_ok()
        code, body = _get(server, "/health")
        assert code == 503
        assert json.loads(body)["failed"] == "telegram"

    def test_ready_is_a_constant_200(self, server):
        server.health = HealthState()  # nothing healthy at all
        assert _get(server, "/ready")[0] == 200


class TestPollTracking:
    async def test_successful_poll_is_recorded(self):
        calls = []
        request = PollTrackingRequest(lambda: calls.append(1))
        with patch.object(HTTPXRequest, "do_request", AsyncMock(return_value=(200, b"{}"))):
            await request.do_request("url", "POST")
        assert calls == [1]

    async def test_failed_poll_is_not(self):
        calls = []
        request = PollTrackingRequest(lambda: calls.append(1))
        with patch.object(HTTPXRequest, "do_request", AsyncMock(return_value=(409, b"{}"))):
            await request.do_request("url", "POST")
        assert calls == []

    def test_create_bot_wires_the_poll_request_and_the_running_check(self):
        from music_downloader.bot.handlers import create_bot

        health = HealthState()
        config = MagicMock()
        config.data_dir = __import__("tempfile").mkdtemp()
        config.download_cleanup_hours = 24
        with (
            patch("music_downloader.pipeline.SpotifyResolver"),
            patch("music_downloader.pipeline.SlskdClient"),
            patch("music_downloader.bot.handlers.Application") as app_cls,
        ):
            builder = app_cls.builder.return_value
            for chain in ("token", "post_init", "post_shutdown", "get_updates_request"):
                getattr(builder, chain).return_value = builder
            app = builder.build.return_value
            create_bot(config, health)
        request = builder.get_updates_request.call_args.args[0]
        assert isinstance(request, PollTrackingRequest)
        app.running = True
        app.updater.running = True
        assert health.telegram_running()
        app.updater.running = False
        assert not health.telegram_running()


class TestSlskdProbe:
    async def test_probe_answer_marks_slskd(self):
        health = HealthState(clock=FakeClock())
        with (
            patch("music_downloader.health.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await slskd_probe_loop(lambda: True, health)
        assert health.last_slskd_ok == 1000.0

    async def test_probe_failure_does_not(self):
        health = HealthState(clock=FakeClock())
        with (
            patch("music_downloader.health.asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await slskd_probe_loop(lambda: False, health)
        assert health.last_slskd_ok is None

    def test_is_up_asks_application_state(self):
        with patch("slskd_api.SlskdClient"):
            client = SlskdClient("http://localhost:5030", "k")
        assert client.is_up()
        client.client.application.state.assert_called_once_with()
        client.client.application.state.side_effect = ConnectionError("down")
        assert not client.is_up()
