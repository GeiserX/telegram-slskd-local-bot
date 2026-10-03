"""Long tracks: estimated lengths, unknown lengths never lead, the title guard."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.pending_repo import PendingRepository, PendingSearch
from music_downloader.pipeline.search import RankedResults, rank, title_guard, title_tokens
from music_downloader.search.scorer import DURATION_MAX_POINTS, PROFILE_CHAT, PROFILE_LIBRARY, ResultScorer
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import CHAT, _make_bot

ECHOES_SECS = 23 * 60 + 31  # 1411 s


def _echoes(title="Echoes", duration_secs=ECHOES_SECS):
    return TrackInfo(
        artist="Pink Floyd", title=title, album="Meddle", duration_ms=duration_secs * 1000, spotify_url="u", year="1971"
    )


def _r(user, path, size=40_000_000, length=None, bit_rate=None, bit_depth=None):
    return SearchResult(
        username=user,
        filename=path,
        size=size,
        length=length,
        bit_rate=bit_rate,
        bit_depth=bit_depth,
        sample_rate=44100 if bit_depth else None,
        has_free_slot=True,
        upload_speed=1_000_000,
    )


def _rank(results, track, profile, limit=50_000_000):
    slskd = MagicMock()
    slskd.parse_results = MagicMock(return_value=results)
    return rank(slskd, ResultScorer(), [], track, profile, upload_limit_bytes=limit)


# ---------------------------------------------------------------------------
# Length estimate
# ---------------------------------------------------------------------------


class TestLengthEstimate:
    def test_lossy_length_estimated_from_size_and_bitrate(self):
        # 320 kbps for 1411 s is 56,440,000 bytes.
        r = _r("u", "\\Meddle\\Echoes.mp3", size=56_440_000, bit_rate=320)
        assert ResultScorer.effective_length(r) == ECHOES_SECS

    def test_estimated_length_scores_like_a_known_one(self):
        estimated = _r("u", "\\Meddle\\Echoes.mp3", size=56_440_000, bit_rate=320)
        known = _r("u", "\\Meddle\\Echoes.mp3", size=56_440_000, bit_rate=320, length=ECHOES_SECS)
        scorer = ResultScorer()
        assert scorer._calculate_score(estimated, _echoes()) == scorer._calculate_score(known, _echoes())

    def test_estimate_far_from_the_track_is_excluded(self):
        # A 4-minute MP3 is not a 23-minute track.
        four_minutes = _r("u", "\\Meddle\\Echoes.mp3", size=9_600_000, bit_rate=320)
        assert ResultScorer()._calculate_score(four_minutes, _echoes()) is None

    def test_reported_length_wins_over_the_estimate(self):
        r = _r("u", "\\x\\Echoes.mp3", size=56_440_000, bit_rate=320, length=100)
        assert ResultScorer.effective_length(r) == 100

    def test_lossless_gets_no_estimate(self):
        # A FLAC's size depends on how well it compressed.
        r = _r("u", "\\x\\Echoes.flac", size=150_000_000, bit_rate=900, bit_depth=16)
        assert ResultScorer.effective_length(r) is None


# ---------------------------------------------------------------------------
# Unknown length
# ---------------------------------------------------------------------------


class TestUnknownLength:
    def test_unknown_length_earns_no_duration_points(self):
        unknown = _r("u", "\\Meddle\\Echoes.flac", bit_depth=16)
        exact = _r("u", "\\Meddle\\Echoes.flac", bit_depth=16, length=ECHOES_SECS)
        scorer = ResultScorer()
        diff = scorer._calculate_score(exact, _echoes()) - scorer._calculate_score(unknown, _echoes())
        assert diff == pytest.approx(DURATION_MAX_POINTS)

    def test_track_without_length_keeps_flat_points(self):
        # Direct search: nothing to compare, so no copy is penalised.
        track = _echoes(duration_secs=0)
        assert not ResultScorer.length_unknown(_r("u", "\\x\\Echoes.flac", bit_depth=16), track)

    def test_library_unknown_length_lossless_cannot_lead(self):
        unknown = _r("unknown", "\\Meddle\\Echoes.flac", size=150_000_000, bit_depth=24)
        lossy = _r("lossy", "\\Meddle\\Echoes.mp3", size=56_440_000, bit_rate=320)
        ranked = _rank([unknown, lossy], _echoes(), PROFILE_LIBRARY)
        assert [r.username for r in ranked] == ["lossy", "unknown"]

    def test_chat_unknown_length_sorts_after_every_known_length(self):
        # The unknown-length copy is small and lossless (best chat quality),
        # the known one is over the cap; known length still comes first.
        unknown = _r("unknown", "\\Meddle\\06 Echoes.flac", size=4_000_000, bit_depth=16)
        oversize = _r("oversize", "\\Meddle\\Echoes (24bit).flac", size=150_000_000, bit_depth=16, length=ECHOES_SECS)
        fits = _r("fits", "\\Meddle\\Echoes.mp3", size=56_440_000, bit_rate=320)
        ranked = _rank([unknown, oversize, fits], _echoes(), PROFILE_CHAT, limit=60_000_000)
        assert [r.username for r in ranked] == ["fits", "oversize", "unknown"]


# ---------------------------------------------------------------------------
# Title guard
# ---------------------------------------------------------------------------


class TestTitleTokens:
    def test_brackets_version_noise_and_stopwords_dropped(self):
        assert title_tokens("The Dark Side of the Moon (2011 Remaster) [Live]") == {"dark", "side", "moon"}
        assert title_tokens("Echoes - 2011 Remastered Version") == {"echoes"}

    def test_accents_and_punctuation(self):
        assert title_tokens("Café, Olé!") == {"cafe", "ole"}

    def test_one_token_title_keeps_it(self):
        assert title_tokens("Echoes") == {"echoes"}
        assert title_tokens("A") == {"a"}

    def test_stopword_only_title_keeps_its_words(self):
        assert title_tokens("The The") == {"the"}

    def test_bracket_only_title_keeps_bracket_words(self):
        assert title_tokens("(Untitled)") == {"untitled"}


class TestTitleGuard:
    def test_unrelated_album_tracks_hidden(self):
        echoes = _r("a", "\\Pink Floyd\\Meddle\\06 Echoes.flac", bit_depth=16, length=ECHOES_SECS)
        one = _r("b", "\\Pink Floyd\\Meddle\\01 One of These Days.flac", bit_depth=16)
        pillow = _r("c", "\\Pink Floyd\\Meddle\\05 Seamus.flac", bit_depth=16)
        kept, hidden = title_guard([echoes, one, pillow], _echoes())
        assert kept == [echoes]
        assert hidden == 2

    def test_parent_folder_name_counts(self):
        in_folder = _r("a", "\\Music\\Pink Floyd - Echoes\\track01.flac", bit_depth=16)
        kept, hidden = title_guard([in_folder, _r("b", "\\x\\Seamus.flac")], _echoes())
        assert kept == [in_folder]
        assert hidden == 1

    def test_accented_file_name_matches_plain_title(self):
        r = _r("a", "\\x\\Beyoncé - Café.mp3")
        kept, _ = title_guard([r, _r("b", "\\x\\Halo.mp3")], _echoes(title="Cafe"))
        assert kept == [r]

    def test_underscores_separate_words(self):
        r = _r("a", "\\x\\pink_floyd_echoes.mp3")
        kept, hidden = title_guard([r, _r("b", "\\x\\seamus.mp3")], _echoes())
        assert kept == [r] and hidden == 1

    def test_never_empties_the_list(self):
        results = [_r("a", "\\x\\track01.flac"), _r("b", "\\x\\track02.flac")]
        kept, hidden = title_guard(results, _echoes())
        assert kept == results
        assert hidden == 0

    def test_non_latin_only_title_keeps_romanised_copies(self):
        # "紅" shares no word with "KURENAI", yet both are the song.
        romanised = _r("a", "\\X Japan\\KURENAI.flac", bit_depth=16)
        native = _r("b", "\\X Japan\\紅.flac", bit_depth=16)
        kept, hidden = title_guard([romanised, native], _echoes(title="紅"))
        assert kept == [romanised, native]
        assert hidden == 0

    def test_rank_reports_hidden_count(self):
        echoes = _r("a", "\\Meddle\\Echoes.flac", bit_depth=16, length=ECHOES_SECS)
        seamus = _r("b", "\\Meddle\\Seamus.flac", bit_depth=16, length=ECHOES_SECS)
        ranked = _rank([echoes, seamus], _echoes(), PROFILE_LIBRARY)
        assert isinstance(ranked, RankedResults)
        assert [r.username for r in ranked] == ["a"]
        assert ranked.hidden == 1

    def test_direct_search_skips_the_guard(self):
        # A direct search (track without a length) skips the filters on purpose.
        seamus = _r("b", "\\Meddle\\Seamus.flac", bit_depth=16)
        ranked = _rank(
            [_r("a", "\\Meddle\\Echoes.flac", bit_depth=16), seamus], _echoes(duration_secs=0), PROFILE_LIBRARY
        )
        assert {r.username for r in ranked} == {"a", "b"}
        assert ranked.hidden == 0

    def test_header_shows_hidden_count(self):
        bot = _make_bot()
        r = _r("a", "\\Meddle\\Echoes.flac", bit_depth=16, length=ECHOES_SECS)
        text = bot._format_results(_echoes(), [r], hidden=3)
        assert "Found 1 match, all lossless (3 unrelated hidden):" in text
        assert "unrelated" not in bot._format_results(_echoes(), [r])

    def test_hidden_count_survives_a_restart(self, tmp_path):
        repo = PendingRepository(Database(str(tmp_path / "importer.db")))
        repo.save_search(7, PendingSearch(query="q", track=_echoes(), hidden=4))
        assert repo.load_searches()[7].hidden == 4

    @pytest.mark.asyncio
    async def test_search_flow_shows_and_keeps_hidden_count(self):
        bot = _make_bot()
        r = _r("a", "\\Meddle\\Echoes.flac", bit_depth=16, length=ECHOES_SECS)
        bot.pipeline.search = AsyncMock(return_value=RankedResults([r], hidden=2))
        msg = AsyncMock()
        msg.message_id = 9
        await bot._do_slskd_search(MagicMock(), CHAT, _echoes(), msg, generation=0)
        assert "(2 unrelated hidden)" in msg.edit_text.call_args.args[0]
        assert bot.pending[CHAT].hidden == 2
