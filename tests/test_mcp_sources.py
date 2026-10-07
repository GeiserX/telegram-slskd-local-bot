"""MCP tools on tracks that did not come from Spotify: MusicBrainz and Soulseek candidates end to end."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from music_downloader.metadata.spotify import SOURCE_MUSICBRAINZ, SOURCE_SOULSEEK, TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.wishlist_repo import WishlistRepository
from music_downloader.pipeline.resolve import Candidate
from tests.test_mcp import OWNER, TRACK, _tools

MB_TRACK = TrackInfo(
    "Robbie Williams",
    "A Man for All Seasons",
    "Superhits",
    239_760,
    "",
    "2003",
    source=SOURCE_MUSICBRAINZ,
    source_id="musicbrainz:recording:e4825193-3c7b-43eb-b660-c9ffa793295d",
)
SLSK_TRACK = TrackInfo(
    "Toshiro Masuda",
    "The Raising Fighting Spirit",
    "",
    97_000,
    "",
    "",
    source=SOURCE_SOULSEEK,
    source_id="soulseek:peer1:\\Music\\Naruto\\Toshiro Masuda - The Raising Fighting Spirit.mp3",
)
RESPONSES = [{"username": "peer1", "files": []}]


class TestResolveTrack:
    @pytest.mark.asyncio
    async def test_candidates_say_their_source_and_confidence(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.candidates = [Candidate(MB_TRACK, True), Candidate(TRACK, False)]
        out = await tools.resolve_track(artist="Robbie Williams", title="A Man for All Seasons", duration_secs=239.8)
        assert pipeline.resolve_calls == [("", "Robbie Williams", "A Man for All Seasons", 239.8)]
        assert out["confident"] is True
        first, second = out["candidates"]
        assert (first["source"], first["source_id"], first["confident"]) == (
            "musicbrainz",
            "musicbrainz:recording:e4825193-3c7b-43eb-b660-c9ffa793295d",
            True,
        )
        assert (second["source"], second["confident"]) == ("spotify", False)
        assert first["id"] != second["id"] and first["id"].startswith("t")

    @pytest.mark.asyncio
    async def test_needs_a_query_or_artist_and_title(self, tmp_path):
        tools, _, _ = _tools(tmp_path)
        with pytest.raises(ToolError, match="artist and title"):
            await tools.resolve_track(artist="Robbie Williams")

    @pytest.mark.asyncio
    async def test_nothing_found_is_not_confident(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.candidates = []
        assert await tools.resolve_track("nothing at all") == {"confident": False, "candidates": []}


class TestNonSpotifyTracksThroughTheChain:
    @pytest.mark.asyncio
    async def test_soulseek_candidate_ranks_its_own_responses(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.candidates = [Candidate(SLSK_TRACK, False, RESPONSES)]
        [cand] = (await tools.resolve_track("Toshiro Masuda - The Raising Fighting Spirit"))["candidates"]
        assert (cand["source"], cand["confident"]) == ("soulseek", False)
        found = await tools.search_copies(cand["id"], profile="chat")
        assert pipeline.search_queries == []
        assert pipeline.ranked_responses == [(RESPONSES, SLSK_TRACK, "chat")]
        assert found["total"] == 3
        done = await tools.download(found["copies"][0]["id"])
        assert done["ok"] is True
        assert done["path"].endswith("Toshiro Masuda - The Raising Fighting Spirit.flac")

    @pytest.mark.asyncio
    async def test_musicbrainz_track_searches_downloads_and_lists_its_album(self, tmp_path):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.candidates = [Candidate(MB_TRACK, True)]
        [cand] = (await tools.resolve_track("Robbie Williams - A Man for All Seasons"))["candidates"]
        found = await tools.search_copies(cand["id"])
        assert pipeline.search_queries == [("Robbie Williams A Man for All Seasons", "library")]
        copy_id = found["copies"][0]["id"]
        assert (await tools.download(copy_id))["ok"] is True
        assert (await tools.album_listing(copy_id))["count"] == 2
        album = await tools.album_download(copy_id, deliver="path")
        assert album["landed"] == 2
        assert pipeline.album_calls[0][1] is MB_TRACK


class TestWishlistHoldsAnySource:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("track", [MB_TRACK, SLSK_TRACK])
    async def test_a_non_spotify_track_is_stored_and_read_back(self, tmp_path, track):
        tools, pipeline, _ = _tools(tmp_path)
        pipeline.candidates = [Candidate(track, track.source == SOURCE_MUSICBRAINZ)]
        [cand] = (await tools.resolve_track(f"{track.artist} - {track.title}"))["candidates"]
        added = await tools.wishlist_add(cand["id"])
        assert added["already_waiting"] is False
        assert added["wish"]["track"]["source"] == track.source
        assert added["wish"]["track"]["source_id"] == track.source_id
        [listed] = (await tools.wishlist_list())["wishes"]
        assert listed["track"]["source_id"] == track.source_id
        stored = pipeline.wishlist_repo.get(added["wish"]["id"])
        assert stored.track == track
        assert stored.chat_id == OWNER

    def test_a_row_written_before_sources_existed_reads_as_spotify(self, tmp_path):
        db = Database(str(tmp_path / "old.db"))
        old = {"artist": "A", "title": "B", "album": "", "duration_ms": 1000, "spotify_url": "u", "year": ""}
        db.connection.execute(
            "INSERT INTO wishlist (chat_id, user_id, track, profile, wanted, created_at) VALUES (1, 1, ?, 'library', 'any', 0)",
            (json.dumps(old),),
        )
        db.connection.commit()
        [wish] = WishlistRepository(db).list_all()
        assert (wish.track.source, wish.track.source_id) == ("spotify", "")
