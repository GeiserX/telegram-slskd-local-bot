"""The resolver's confidence rule, its MusicBrainz and Soulseek fallbacks, on songs a library sweep missed.

The Spotify answers below are the first candidates Spotify returned for each
query on 2026-10-07; the MusicBrainz ones are recorded responses in
tests/fixtures/musicbrainz. The lengths are those of the library files.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from music_downloader.metadata.musicbrainz import recording_to_track
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.pipeline import Pipeline
from music_downloader.pipeline import resolve as _resolve
from music_downloader.pipeline.resolve import (
    fold,
    is_confident,
    match,
    soulseek_candidate,
    split_query,
    title_words,
    track_from_file,
)
from music_downloader.pipeline.search import RankedResults
from music_downloader.search.slskd_client import SearchResult, SlskdUnavailableError

FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"


def _t(artist, title, album, secs, track_id=""):
    return TrackInfo(artist, title, album, secs * 1000, "", "", source_id=f"spotify:track:{track_id}")


SPOTIFY = {
    "Dexys Midnight Runners": [
        _t("Dexys Midnight Runners", "Come On Eileen", "Too Rye Ay", 287),
        _t("Dexys Midnight Runners", "Come On Eileen", "Classic 80's", 248),
        _t("Dexys Midnight Runners", "Come On Eileen - Single Edit", "Too-Rye-Ay (As It Should Have Sounded)", 205),
    ],
    "Desireless": [
        _t("Desireless", "Voyage voyage", "François", 266),
        _t("Desireless", "Voyage voyage - Euro Remix", "Voyage voyage (Euro Remix)", 374),
        _t("Desireless", "Voyage voyage - Maxi", "François", 403),
        _t("Desireless", "Voyage, voyage", "More Love & Good Vibrations", 265),
    ],
    "The Killers": [
        _t("The Killers", "Exitlude", "Sam's Town", 146),
        _t("The Killers", "Mr. Brightside", "Hot Fuss", 222),
    ],
    "Graham Central Station": [
        _t("Graham Central Station", "The Jam", "Ain't No 'Bout-A-Doubt It", 492),
        _t("Graham Central Station", "The Jam - Live", "Let Yourself Go (Live San Francisco '75)", 538),
    ],
    "Aliotta Haynes Jeremiah": [
        _t("Aliotta Haynes Jeremiah", "Lake Shore Drive", "Lake Shore Drive", 235),
        _t("Aliotta Haynes Jeremiah", "Lake Shore Drive (Live)", "Cornerstones of Rock", 255),
    ],
    "Smash Mouth": [
        _t("Smash Mouth", "Walkin' On The Sun - Live", "The Fonz", 202),
        _t("Smash Mouth", "Walkin' On The Sun", "Fush Yu Mang (20th Anniversary Edition)", 207),
    ],
    "Toshiro Masuda": [
        _t("Lucas Konrath", "The Raising Fighting Spirit", "The Raising Fighting Spirit", 94),
        _t("Tobin Palmer", "Rising Flood", "Rising Flood", 155),
    ],
    "Robbie Williams": [
        _t("Robbie Williams", "She's The One", "I've Been Expecting You", 258),
        _t("Robbie Williams", "Angels", "Angels", 265),
        _t(
            "Poptastic Karaoke",
            "Man For All Seasons (Robbie Williams Karaoke Tribute) - Karaoke Mix",
            "Poptastic Karaoke Presents",
            256,
        ),
    ],
}

MUSICBRAINZ = {
    ("Graham Central Station", "Jam"): "graham_central_station_jam",
    ("Aliotta Haynes Jeremiah", "Lake Shore Drive"): "aliotta_haynes_jeremiah_lake_shore_drive",
    ("Robbie Williams", "A Man for All Seasons"): "robbie_williams_a_man_for_all_seasons",
    ("Toshiro Masuda", "The Raising Fighting Spirit"): "toshiro_masuda_raising_fighting_spirit",
}


class FakeSpotify:
    def __init__(self):
        self.queries = []

    def search_multiple(self, query, limit=5):
        self.queries.append(query)
        return next((list(v) for k, v in SPOTIFY.items() if fold(query).startswith(fold(k))), [])


class FakeMusicBrainz:
    """Answers from the recorded fixtures, through the real JSON-to-track conversion."""

    def __init__(self):
        self.calls = []

    def search_recordings(self, artist, title, limit=25):
        self.calls.append((artist, title))
        name = MUSICBRAINZ.get((artist, title))
        if name is None:
            return []
        data = json.loads((FIXTURES / f"{name}.json").read_text())
        return [recording_to_track(r) for r in data["recordings"]]


# (file stem, file length in seconds, expected source of the first candidate, its expected length)
SWEEP_MISSES = [
    ("Dexys Midnight Runners - Come On Eileen", 205.9, "spotify", 205),
    ("Desireless - Voyage Voyage", 258.0, "spotify", 265),
    ("The Killers - Exitlude", 151.7, "spotify", 146),
    ("Graham Central Station - Jam", 219.9, "musicbrainz", 218),
    ("Aliotta Haynes Jeremiah - Lake Shore Drive", 229.1, "spotify", 235),
    ("Smash Mouth - Walkin’ on the Sun", 206.4, "spotify", 207),
    ("Robbie Williams - A Man for All Seasons", 239.8, "musicbrainz", 239),
]


class TestFolding:
    def test_curly_apostrophe_accents_and_ampersand_fold_away(self):
        assert fold("Walkin’ on the Sun") == fold("Walkin' On The Sun") == "walkin on the sun"
        # A file name often drops the apostrophe altogether.
        assert fold("Dont Stop Me Now") == fold("Don’t Stop Me Now") == fold("Don't Stop Me Now")
        assert fold("Beyoncé & Jay-Z") == "beyonce and jay z"
        assert fold("Voyage, voyage") == fold("Voyage Voyage")

    @pytest.mark.parametrize(
        "spotify_title",
        [
            "Come On Eileen - Single Edit",
            "Come On Eileen - 2002 Remaster",
            "Come On Eileen (Remastered 2009)",
            "Come On Eileen - Remastered Version",
            "Come On Eileen (feat. Kevin Rowland)",
            'Come On Eileen - From "The Film"',
            "Come On Eileen - Mono",
        ],
    )
    def test_neutral_suffixes_name_the_same_recording(self, spotify_title):
        assert title_words(spotify_title) == title_words("Come On Eileen")

    @pytest.mark.parametrize(
        "other",
        [
            "Come On Eileen - Live",
            "Come On Eileen (Live at Wembley)",
            "Come On Eileen - Euro Remix",
            "Come On Eileen - Maxi",
            "Come On Eileen (Karaoke Version)",
            "Come On Eileen - Acoustic",
            "Come On Eileen - PWL - Britmix",
        ],
    )
    def test_another_recording_keeps_its_suffix(self, other):
        assert title_words(other) != title_words("Come On Eileen")

    def test_a_leading_article_does_not_split_titles(self):
        assert title_words("The Jam") == title_words("Jam")
        assert title_words("The The") == ["the", "the"]


class TestConfidence:
    @pytest.mark.parametrize(("stem", "length", "source", "secs"), SWEEP_MISSES)
    def test_songs_the_sweep_missed_resolve_confidently(self, stem, length, source, secs):
        spotify, musicbrainz = FakeSpotify(), FakeMusicBrainz()
        candidates, _, _ = match(spotify, musicbrainz, stem, duration_secs=length)
        first = candidates[0]
        assert first.confident
        assert first.track.source == source
        assert first.track.duration_secs == secs
        assert bool(musicbrainz.calls) == (source == "musicbrainz")

    @pytest.mark.parametrize(("stem", "length", "source", "secs"), SWEEP_MISSES)
    def test_the_sweeps_free_text_form_resolves_the_same(self, stem, length, source, secs):
        candidates, artist, title = match(
            FakeSpotify(), FakeMusicBrainz(), stem.replace(" - ", " "), duration_secs=length
        )
        assert (artist, title) == tuple(stem.split(" - ", 1))
        assert candidates[0].confident and candidates[0].track.source == source

    def test_a_song_on_neither_source_is_not_confident(self):
        musicbrainz = FakeMusicBrainz()
        candidates, artist, title = match(
            FakeSpotify(), musicbrainz, "Toshiro Masuda - The Raising Fighting Spirit", duration_secs=96.6
        )
        assert not any(c.confident for c in candidates)
        assert musicbrainz.calls == [("Toshiro Masuda", "The Raising Fighting Spirit")]
        assert (artist, title) == ("Toshiro Masuda", "The Raising Fighting Spirit")

    def test_without_a_length_the_album_version_is_confident(self):
        candidates, _, _ = match(FakeSpotify(), FakeMusicBrainz(), "Dexys Midnight Runners - Come On Eileen")
        assert candidates[0].confident and candidates[0].track.duration_secs == 287

    @pytest.mark.parametrize(
        ("track", "artist", "title", "length"),
        [
            # Another song by the same artist.
            (_t("Robbie Williams", "Angels", "", 240), "Robbie Williams", "A Man for All Seasons", 239.8),
            # A karaoke tribute of the right song.
            (
                _t("Poptastic Karaoke", "Man For All Seasons (Robbie Williams Karaoke Tribute)", "", 240),
                "Robbie Williams",
                "A Man for All Seasons",
                239.8,
            ),
            # The right title by someone else.
            (
                _t("Lucas Konrath", "The Raising Fighting Spirit", "", 96),
                "Toshiro Masuda",
                "The Raising Fighting Spirit",
                96.6,
            ),
            # A live recording of the right song, at the right length.
            (_t("Smash Mouth", "Walkin' On The Sun - Live", "", 206), "Smash Mouth", "Walkin’ on the Sun", 206.4),
            # The right song, another edit: 81 s longer than the file.
            (
                _t("Dexys Midnight Runners", "Come On Eileen", "", 287),
                "Dexys Midnight Runners",
                "Come On Eileen",
                205.9,
            ),
            # A title that is only part of the asked one.
            (_t("Robbie Williams", "Seasons", "", 240), "Robbie Williams", "A Man for All Seasons", 239.8),
        ],
    )
    def test_wrong_songs_stay_unconfident(self, track, artist, title, length):
        assert not is_confident(track, artist, title, length)

    def test_unknown_length_is_not_confident_when_the_file_length_is_known(self):
        track = _t("The Killers", "Exitlude", "", 0)
        assert not is_confident(track, "The Killers", "Exitlude", 151.7)
        assert is_confident(track, "The Killers", "Exitlude")

    def test_a_band_credited_with_its_singer_is_the_same_artist(self):
        track = _t("Dexys Midnight Runners", "Come On Eileen", "", 205)
        assert is_confident(track, "Kevin Rowland & Dexys Midnight Runners", "Come On Eileen", 205.9)

    def test_no_artist_no_confidence_and_no_fallback(self):
        musicbrainz = FakeMusicBrainz()
        candidates, artist, _ = match(FakeSpotify(), musicbrainz, "voyage voyage")
        assert artist == "" and candidates == [] and musicbrainz.calls == []


class TestSplitQuery:
    def test_dash_splits(self):
        assert split_query("A - B - C", []) == ("A", "B - C")

    def test_candidate_artist_at_either_end(self):
        tracks = [_t("The Killers", "Exitlude", "", 146)]
        assert split_query("The Killers Exitlude", tracks) == ("The Killers", "Exitlude")
        assert split_query("Exitlude the killers", tracks) == ("the killers", "Exitlude")

    def test_no_split_without_a_matching_artist(self):
        assert split_query("Toshiro Masuda The Raising Fighting Spirit", SPOTIFY["Toshiro Masuda"]) == (
            "",
            "Toshiro Masuda The Raising Fighting Spirit",
        )


def _copy(name, length=97, user="peer1"):
    return SearchResult(
        username=user, filename=f"\\Music\\Naruto OST\\{name}", size=4_000_000, bit_rate=320, length=length
    )


class TestSoulseekFallback:
    def test_file_names_give_artist_and_title(self):
        track = track_from_file(_copy("07 - Toshiro Masuda - The Raising Fighting Spirit.mp3"), "x", "y")
        assert (track.artist, track.title, track.duration_secs) == ("Toshiro Masuda", "The Raising Fighting Spirit", 97)
        assert track.source == "soulseek"
        assert (
            track.source_id
            == "soulseek:peer1:\\Music\\Naruto OST\\07 - Toshiro Masuda - The Raising Fighting Spirit.mp3"
        )

    def test_a_name_without_artist_keeps_the_asked_artist(self):
        track = track_from_file(_copy("12. The Raising Fighting Spirit.flac", length=None), "Toshiro Masuda", "x")
        assert (track.artist, track.title, track.duration_ms) == ("Toshiro Masuda", "The Raising Fighting Spirit", 0)

    @pytest.mark.asyncio
    async def test_best_copy_becomes_the_candidate_and_keeps_the_responses(self):
        best = _copy("Toshiro Masuda - The Raising Fighting Spirit.flac")
        searched, ranked_for = [], []

        async def search(text):
            searched.append(text)
            return [{"username": "peer1"}]

        def rank(responses, track):
            ranked_for.append(track)
            return RankedResults([best], 0)

        found = await soulseek_candidate(search, rank, "Toshiro Masuda", "The Raising Fighting Spirit", 96.6)
        assert searched == ["Toshiro Masuda The Raising Fighting Spirit"]
        assert ranked_for[0].duration_secs == 96
        assert found.track.source == "soulseek" and not found.confident
        assert found.responses == [{"username": "peer1"}]

    @pytest.mark.asyncio
    async def test_nothing_on_soulseek_gives_no_candidate(self):
        async def search(text):
            return []

        assert await soulseek_candidate(search, lambda r, t: RankedResults([], 0), "A", "B") is None


def _pipeline(responses):
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.spotify = FakeSpotify()
    pipeline.musicbrainz = FakeMusicBrainz()
    pipeline.slskd = SimpleNamespace(search=AsyncMock(return_value=responses))
    pipeline.config = SimpleNamespace(search_timeout_secs=5)
    pipeline.rank = lambda raw, track, profile: RankedResults(
        [_copy("Toshiro Masuda - The Raising Fighting Spirit.mp3")] if raw else [], 0
    )
    return pipeline


class TestPipelineResolveMatch:
    @pytest.mark.asyncio
    async def test_soulseek_runs_only_when_both_sources_fail(self):
        pipeline = _pipeline([{"username": "peer1"}])
        candidates = await pipeline.resolve_match("Toshiro Masuda - The Raising Fighting Spirit", duration_secs=96.6)
        assert candidates[0].track.source == "soulseek"
        assert [c.track.artist for c in candidates[1:]] == ["Lucas Konrath", "Tobin Palmer"]
        pipeline.slskd.search.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_confident_match_never_searches_soulseek(self):
        pipeline = _pipeline([{"username": "peer1"}])
        candidates = await pipeline.resolve_match("", "Graham Central Station", "Jam", 219.9)
        assert candidates[0].track.source == "musicbrainz" and candidates[0].confident
        pipeline.slskd.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_copies_leaves_the_spotify_candidates(self):
        pipeline = _pipeline([])
        candidates = await pipeline.resolve_match("Toshiro Masuda - The Raising Fighting Spirit")
        assert [c.track.source for c in candidates] == ["spotify", "spotify"]

    @pytest.mark.asyncio
    async def test_slskd_down_leaves_the_metadata_candidates(self):
        pipeline = _pipeline([])
        pipeline.slskd.search.side_effect = SlskdUnavailableError("down")
        candidates = await pipeline.resolve_match("Toshiro Masuda - The Raising Fighting Spirit")
        assert [c.track.source for c in candidates] == ["spotify", "spotify"]

    @pytest.mark.asyncio
    async def test_a_query_without_artist_never_falls_back(self):
        pipeline = _pipeline([{"username": "peer1"}])
        await pipeline.resolve_match("unknown words here")
        assert pipeline.musicbrainz.calls == []
        pipeline.slskd.search.assert_not_awaited()


def test_resolve_module_keeps_the_old_helpers():
    assert _resolve.synthetic_track("A", "B").duration_ms == 0
    assert _resolve.parse_query_artist_title("a - b") == ("A", "B")
