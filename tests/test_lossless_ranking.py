"""Tests for lossless-first library ranking and the chat "best bang for buck" profile (v0.14.0)."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import mutagen.mp3
import pytest

from music_downloader.bot.handlers import TELEGRAM_FILE_LIMIT, MusicBot, PendingDownload
from music_downloader.formats import LOSSLESS_EXTENSIONS, LOSSY_EXTENSIONS, is_lossless
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.scorer import (
    CHAT_OPUS_CONVERSION_POINTS,
    CHAT_SIZE_PENALTY_MAX_POINTS,
    PROFILE_CHAT,
    ResultScorer,
)
from music_downloader.search.slskd_client import SearchResult, SlskdClient
from music_downloader.tools.embed_artwork import embed_artwork_into_file

CHAT = 67890
MB = 1024 * 1024
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _make_config(td=None, chat_users=None):
    td = td or tempfile.mkdtemp()
    config = MagicMock()
    config.telegram_bot_token = "test-token"
    config.spotify_client_id = "test-id"
    config.spotify_client_secret = "test-secret"
    config.slskd_host = "http://localhost:5030"
    config.slskd_api_key = "test-key"
    config.telegram_allowed_users = {12345, CHAT}
    config.telegram_chat_delivery_users = set(chat_users or ())
    config.auto_mode = False
    config.max_results = 5
    config.duration_tolerance_secs = 5
    config.exclude_keywords = ["live", "remix"]
    config.download_dir = os.path.join(td, "downloads")
    config.output_dir = os.path.join(td, "music")
    config.data_dir = os.path.join(td, "data")
    config.filename_template = "{artist} - {title}"
    config.search_timeout_secs = 30
    config.download_timeout_secs = 600
    config.download_cleanup_hours = 24
    return config


def _make_bot(config=None):
    with (
        patch("music_downloader.bot.handlers.SpotifyResolver"),
        patch("music_downloader.bot.handlers.SlskdClient"),
    ):
        return MusicBot(config or _make_config())


def _make_track(duration_ms=162_000):
    return TrackInfo(
        artist="Nancy Sinatra",
        title="Bang Bang",
        album="X",
        duration_ms=duration_ms,
        spotify_url="u",
        year="1966",
    )


def _result(name, ext, size, bit_rate=None, bit_depth=None, sample_rate=44100, slot=True, speed=1_000_000):
    return SearchResult(
        username=name,
        filename=f"\\Music\\Nancy Sinatra - Bang Bang {name}.{ext}",
        size=size,
        bit_rate=bit_rate,
        bit_depth=bit_depth,
        sample_rate=sample_rate,
        length=162,
        has_free_slot=slot,
        upload_speed=speed,
        queue_length=0,
    )


def _flac(name, size, slot=True, speed=1_000_000):
    return _result(name, "flac", size, bit_depth=16, slot=slot, speed=speed)


def _mp3(name, size, kbps, slot=True, speed=1_000_000):
    return _result(name, "mp3", size, bit_rate=kbps, slot=slot, speed=speed)


def _chat_score(result):
    return ResultScorer().score_results([result], _make_track(), profile=PROFILE_CHAT)[0].score


def _write_fake_mp3(path):
    """20 silent MPEG-1 Layer III frames (128 kbps, 44.1 kHz): enough for mutagen."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    with open(path, "wb") as f:
        f.write(frame * 20)


# ---------------------------------------------------------------------------
# Lossless / lossy classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize("ext", ["flac", "alac", "wav", "aiff", "aif", "ape", "wv", "tta", "tak"])
    def test_lossless_extensions(self, ext):
        assert is_lossless(ext)
        assert is_lossless(f".{ext.upper()}")
        assert _result("u", ext, 1).is_lossless

    @pytest.mark.parametrize("ext", ["mp3", "aac", "m4a", "ogg", "opus", "wma"])
    def test_lossy_extensions(self, ext):
        assert not is_lossless(ext)
        assert not _result("u", ext, 1).is_lossless

    def test_sets_are_disjoint(self):
        assert not LOSSLESS_EXTENSIONS & LOSSY_EXTENSIONS

    def test_parser_accepts_every_lossless_and_lossy_extension(self):
        with patch("slskd_api.SlskdClient"):
            client = SlskdClient("http://localhost:5030", "test-key")
        exts = sorted(LOSSLESS_EXTENSIONS | LOSSY_EXTENSIONS)
        files = [{"filename": f"\\Music\\Song.{e}", "size": 1} for e in exts + ["jpg", "cue"]]
        results = client.parse_results([{"username": "u", "files": files}], flac_only=False)
        assert sorted(r.extension for r in results) == exts


# ---------------------------------------------------------------------------
# Library delivery: lossless first, lossy below
# ---------------------------------------------------------------------------


class TestLibraryRanking:
    def _ranked(self, results):
        bot = _make_bot()
        bot.slskd.parse_results = MagicMock(return_value=results)
        return bot._rank_responses([], _make_track(), chat_id=CHAT)

    def test_every_lossless_above_every_lossy_each_group_by_score(self):
        results = [
            _mp3("fast320", 10 * MB, 320, slot=True, speed=10_000_000),  # best source of all
            _flac("slowflac", 30 * MB, slot=False, speed=0),
            _mp3("slow128", 5 * MB, 128, slot=False, speed=0),
            _flac("fastflac", 30 * MB, slot=True, speed=10_000_000),
            _result("ape", "ape", 30 * MB, bit_depth=16, slot=True, speed=500_000),
        ]
        ranked = self._ranked(results)
        kinds = [r.is_lossless for r in ranked]
        assert kinds == [True, True, True, False, False]
        lossless = [r for r in ranked if r.is_lossless]
        lossy = [r for r in ranked if not r.is_lossless]
        assert [r.score for r in lossless] == sorted((r.score for r in lossless), reverse=True)
        assert [r.score for r in lossy] == sorted((r.score for r in lossy), reverse=True)
        assert ranked[0].username == "fastflac"
        # The best-sourced lossy copy still scores above a lossless one; only the partition puts it below.
        assert ranked[3].username == "fast320" and ranked[3].score > ranked[2].score

    def test_lossy_only_is_still_offered(self):
        ranked = self._ranked([_mp3("a", 5 * MB, 128), _mp3("b", 10 * MB, 320)])
        assert [r.username for r in ranked] == ["b", "a"]  # 320 above 128 inside the lossy group


class TestResultsHeader:
    def _text(self, results):
        return _make_bot()._format_results(_make_track(), results)

    def test_mixed_counts(self):
        results = [_flac("a", 30 * MB), _flac("b", 30 * MB), _mp3("c", 10 * MB, 320)]
        text = self._text(results)
        assert "Found 3 matches (2 lossless, 1 lossy):" in text
        assert "[FLAC]" in text and "[MP3]" in text

    def test_all_lossless(self):
        assert "Found 2 matches, all lossless:" in self._text([_flac("a", 30 * MB), _flac("b", 30 * MB)])

    def test_all_lossy(self):
        text = self._text([_mp3("c", 10 * MB, 320)])
        assert "Found 1 matches, all lossy (no lossless copy found):" in text

    def test_direct_search_counts_too(self):
        track = _make_track(duration_ms=0)
        text = _make_bot()._format_results(track, [_flac("a", 30 * MB), _mp3("c", 10 * MB, 320)])
        assert "Found 2 matches (1 lossless, 1 lossy):" in text


# ---------------------------------------------------------------------------
# Chat delivery: perceived quality versus size
# ---------------------------------------------------------------------------


class TestChatProfile:
    def test_small_mp3_320_beats_big_cd_flac(self):
        assert _chat_score(_mp3("m", 10 * MB, 320)) > _chat_score(_flac("f", 35 * MB))

    def test_cd_flac_beats_mp3_128_of_the_same_size(self):
        assert _chat_score(_flac("f", 20 * MB)) > _chat_score(_mp3("m", 20 * MB, 128))

    def test_bitrate_tiers_ordered(self):
        scores = [_chat_score(_mp3(str(k), 8 * MB, k)) for k in (320, 256, 192, 128, 96)]
        assert scores[0] == scores[1]  # 256 and up is the top tier
        assert scores[1] > scores[2] > scores[3] > scores[4]

    def test_lossless_and_256_share_the_top_tier(self):
        assert _chat_score(_flac("f", 8 * MB)) == _chat_score(_mp3("m", 8 * MB, 256))

    def test_size_penalty_monotonic_and_capped(self):
        sizes = [1 * MB, 5 * MB, 10 * MB, 25 * MB, 40 * MB, TELEGRAM_FILE_LIMIT]
        scores = [_chat_score(_flac("f", s)) for s in sizes]
        assert scores[0] == scores[1]  # small files cost nothing
        assert all(a > b for a, b in zip(scores[1:], scores[2:], strict=False))
        assert scores[1] - scores[-1] == pytest.approx(CHAT_SIZE_PENALTY_MAX_POINTS, abs=0.02)

    def test_lossy_bitrate_estimated_from_size_when_missing(self):
        # 10 MB over 162 s is about 518 kbps: top tier, not "unknown".
        unknown = _result("m", "mp3", 10 * MB)
        assert _chat_score(unknown) == _chat_score(_mp3("m", 10 * MB, 320))

    def test_oversize_scores_as_opus_regardless_of_size(self):
        big, huge = _flac("big", 60 * MB), _flac("huge", 200 * MB)
        assert _chat_score(big) == _chat_score(huge)
        fitting_top = _chat_score(_flac("small", 1 * MB))
        assert fitting_top - _chat_score(big) == pytest.approx(CHAT_OPUS_CONVERSION_POINTS)
        # Converted, it still scores below a lossless file right at the limit.
        assert _chat_score(big) < _chat_score(_flac("limit", TELEGRAM_FILE_LIMIT))

    def test_library_profile_unchanged_for_lossless(self):
        # The library still prefers hi-res: 24-bit beats 16-bit whatever the size.
        hires = _result("h", "flac", 45 * MB, bit_depth=24, sample_rate=96000)
        cd = _flac("c", 20 * MB)
        scored = ResultScorer().score_results([hires, cd], _make_track())
        assert [r.username for r in scored] == ["h", "c"]


class TestChatRanking:
    def _ranked(self, results):
        bot = _make_bot(_make_config(chat_users={CHAT}))
        bot.slskd.parse_results = MagicMock(return_value=results)
        return [r.username for r in bot._rank_responses([], _make_track(), chat_id=CHAT)]

    def test_no_lossless_partition_in_chat(self):
        assert self._ranked([_flac("flac35", 35 * MB), _mp3("mp3320", 10 * MB, 320)]) == ["mp3320", "flac35"]

    def test_chat_scores_with_the_chat_profile(self):
        # The library profile would put this 45 MB hi-res FLAC first (25 quality points
        # either way, faster source); the chat profile charges it for its size.
        hires = _result("hires45", "flac", 45 * MB, bit_depth=24, sample_rate=96000, speed=2_000_000)
        assert self._ranked([hires, _mp3("mp3320", 8 * MB, 320)]) == ["mp3320", "hires45"]

    def test_oversize_after_every_fitting_result_and_ranked_among_themselves(self):
        results = [
            _flac("big_fast", 80 * MB, slot=True, speed=10_000_000),
            _mp3("fit128", 5 * MB, 128, slot=False, speed=0),
            _flac("big_slow", 60 * MB, slot=False, speed=0),
            _flac("fit_flac", 30 * MB),
        ]
        # Smaller oversize file gets no edge: as Opus both are the same size; the source decides.
        assert self._ranked(results) == ["fit_flac", "fit128", "big_fast", "big_slow"]


# ---------------------------------------------------------------------------
# Saving a lossy pick end to end
# ---------------------------------------------------------------------------


class TestLossySave:
    def test_process_file_keeps_mp3_extension_and_artwork_embeds_once(self, tmp_path):
        downloads, music = tmp_path / "downloads", tmp_path / "music"
        (downloads / "user").mkdir(parents=True)
        source = downloads / "user" / "01 bang.mp3"
        _write_fake_mp3(source)
        processor = FileProcessor(str(downloads), str(music))
        target = processor.process_file(str(source), "Nancy Sinatra", "Bang Bang")
        assert target == str(music / "Nancy Sinatra - Bang Bang.mp3")
        assert embed_artwork_into_file(target, JPEG) is True
        assert len(mutagen.mp3.MP3(target).tags.getall("APIC")) == 1
        assert embed_artwork_into_file(target, JPEG) is False  # already has artwork: skipped

    def test_artwork_skipped_silently_for_unsupported_format(self, tmp_path):
        wma = tmp_path / "Nancy Sinatra - Bang Bang.wma"
        wma.write_bytes(b"not really wma")
        assert embed_artwork_into_file(str(wma), JPEG) is False
        assert wma.read_bytes() == b"not really wma"

    def test_artwork_embeds_into_ogg_vorbis(self, tmp_path):
        np = pytest.importorskip("numpy")
        sf = pytest.importorskip("soundfile")
        ogg = tmp_path / "a.ogg"
        sf.write(str(ogg), np.zeros(44100, dtype="float32"), 44100, format="OGG", subtype="VORBIS")
        assert embed_artwork_into_file(str(ogg), JPEG) is True
        assert embed_artwork_into_file(str(ogg), JPEG) is False

    @pytest.mark.asyncio
    async def test_auto_save_of_an_mp3_lands_in_the_library_with_artwork(self, tmp_path):
        config = _make_config(str(tmp_path))
        bot = _make_bot(config)
        os.makedirs(os.path.join(config.download_dir, "user"), exist_ok=True)
        source = os.path.join(config.download_dir, "user", "01 bang.mp3")
        _write_fake_mp3(source)
        result = _mp3("user", os.path.getsize(source), 320)
        pending = PendingDownload(track=_make_track(), result=result, chat_id=CHAT, source_path=source)
        bot.downloads["abc"] = pending
        status_msg = AsyncMock()
        with patch("music_downloader.bot.handlers.fetch_spotify_artwork", return_value=JPEG):
            await bot._auto_save(CHAT, "abc", pending, status_msg, "quality", "#1")
        target = os.path.join(config.output_dir, "Nancy Sinatra - Bang Bang.mp3")
        assert os.path.isfile(target) and not os.path.exists(source)
        assert mutagen.mp3.MP3(target).tags.getall("APIC")
        assert "Nancy Sinatra - Bang Bang.mp3" in status_msg.edit_text.call_args[0][0]
