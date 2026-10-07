"""The MusicBrainz client over recorded responses (tests/fixtures/musicbrainz); no network."""

import json
from pathlib import Path

import httpx

from music_downloader.metadata.musicbrainz import API_URL, MIN_INTERVAL_SECS, MusicBrainzClient

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Clock:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, secs):
        self.slept.append(secs)
        self.now += secs


def _client(handler, clock=None):
    clock = clock or Clock()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MusicBrainzClient(http=http, clock=clock, sleep=clock.sleep), clock


class TestRequests:
    def test_query_user_agent_and_format(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=_fixture("robbie_williams_a_man_for_all_seasons"))

        client, _ = _client(handler)
        client.search_recordings("Robbie Williams", 'A Man for "All" Seasons')
        [request] = seen
        assert str(request.url).startswith(API_URL)
        assert request.url.params["query"] == 'artist:"Robbie Williams" AND recording:"A Man for \\"All\\" Seasons"'
        assert request.url.params["fmt"] == "json"
        agent = request.headers["user-agent"]
        assert agent.startswith("telegram-slskd-local-bot/")
        assert "https://github.com/GeiserX/telegram-slskd-local-bot" in agent

    def test_one_request_per_second(self):
        clock = Clock()
        client, _ = _client(lambda r: httpx.Response(200, json={"recordings": []}), clock)
        client.search_recordings("A", "B")
        client.search_recordings("A", "C")
        assert clock.slept == [MIN_INTERVAL_SECS]
        clock.now += 5
        client.search_recordings("A", "D")
        assert clock.slept == [MIN_INTERVAL_SECS]

    def test_errors_give_no_candidates(self):
        client, _ = _client(lambda r: httpx.Response(503, text="slow down"))
        assert client.search_recordings("A", "B") == []

        def broken(request):
            raise httpx.ConnectError("down")

        client, _ = _client(broken)
        assert client.search_recordings("A", "B") == []

    def test_an_empty_title_sends_nothing(self):
        calls = []
        client, _ = _client(lambda r: calls.append(r) or httpx.Response(200, json={}))
        assert client.search_recordings("A", "  ") == []
        assert calls == []


class TestParsing:
    def test_recording_becomes_a_namespaced_track(self):
        client, _ = _client(lambda r: httpx.Response(200, json=_fixture("robbie_williams_a_man_for_all_seasons")))
        [track] = client.search_recordings("Robbie Williams", "A Man for All Seasons")
        assert (track.artist, track.title, track.duration_ms) == ("Robbie Williams", "A Man for All Seasons", 239760)
        assert track.album == "Superhits"
        assert track.source == "musicbrainz"
        assert track.source_id == "musicbrainz:recording:e4825193-3c7b-43eb-b660-c9ffa793295d"
        assert track.spotify_url == ""

    def test_live_recordings_are_left_out(self):
        client, _ = _client(lambda r: httpx.Response(200, json=_fixture("graham_central_station_jam")))
        tracks = client.search_recordings("Graham Central Station", "Jam")
        lengths = [t.duration_secs for t in tracks]
        assert 218 in lengths  # the single edit
        assert all("live" not in t.title.lower() for t in tracks)
        assert len(tracks) == 5  # 7 recorded, the 2 live ones dropped

    def test_a_live_title_keeps_live_recordings(self):
        client, _ = _client(lambda r: httpx.Response(200, json=_fixture("graham_central_station_jam")))
        assert len(client.search_recordings("Graham Central Station", "Jam (Live)")) == 7

    def test_credit_joins_every_artist(self):
        data = {
            "recordings": [
                {
                    "id": "x1",
                    "title": "Under Pressure",
                    "length": 248000,
                    "artist-credit": [{"name": "Queen", "joinphrase": " & "}, {"name": "David Bowie"}],
                    "releases": [{"title": "Hot Space", "date": "1982-05-21"}],
                }
            ]
        }
        client, _ = _client(lambda r: httpx.Response(200, json=data))
        [track] = client.search_recordings("Queen", "Under Pressure")
        assert track.artist == "Queen & David Bowie"
        assert track.year == "1982"

    def test_no_recordings(self):
        client, _ = _client(lambda r: httpx.Response(200, json=_fixture("toshiro_masuda_raising_fighting_spirit")))
        assert client.search_recordings("Toshiro Masuda", "The Raising Fighting Spirit") == []
