"""The library sweep's judgement: tiers, better, sameness, the fingerprint, keep-both names, the schedule."""

import datetime
import shutil
import subprocess

import pytest

from music_downloader.pipeline import sweep_rules as rules
from music_downloader.pipeline.sweep_rules import (
    REJECT,
    REVIEW,
    SAME,
    Audit,
    better,
    judge,
    keep_both_name,
    next_run,
    parse_schedule,
    previous_run,
    sameness,
    similarity,
    tier_of,
    wanted,
)
from music_downloader.search.slskd_client import SearchResult


def _a(ext="flac", cutoff=21.0, depth=16, rate=44100, strict=True, length=200.0, **tags) -> Audit:
    a = Audit(ext=ext, length=length, depth=depth, rate=rate, strict=strict if ext != "mp3" else None, cutoff=cutoff)
    a.artist, a.title, a.album = tags.get("artist", ""), tags.get("title", ""), tags.get("album", "")
    a.tier = tier_of(ext, depth, rate, a.strict, cutoff)
    return a


class TestTiers:
    @pytest.mark.parametrize(
        ("ext", "depth", "rate", "strict", "cutoff", "tier"),
        [
            ("mp3", None, 44100, None, None, rules.TIER_LOSSY),
            ("m4a", None, 44100, None, 21.0, rules.TIER_LOSSY),
            ("flac", 16, 44100, False, 21.0, rules.TIER_DAMAGED),
            ("flac", 24, 96000, True, 30.0, rules.TIER_HIRES),
            # A 24/96 file with nothing above 24 kHz is a CD master in a big box.
            ("flac", 24, 96000, True, 22.0, rules.TIER_CD),
            # 24-bit at 48 kHz is not hi-res whatever its spectrum.
            ("flac", 24, 48000, True, 23.9, rules.TIER_CD),
            ("flac", 16, 44100, True, 21.5, rules.TIER_CD),
            ("flac", 16, 44100, True, 19.5, rules.TIER_CD),
            ("flac", 16, 44100, True, 19.4, rules.TIER_UNCERTAIN),
            ("flac", 16, 44100, True, 16.0, rules.TIER_UNCERTAIN),
            ("flac", 16, 44100, True, 15.9, rules.TIER_FAKE),
            ("flac", 16, 44100, True, None, rules.TIER_UNCERTAIN),
            ("ape", 16, 44100, True, None, rules.TIER_UNCERTAIN),
        ],
    )
    def test_tier_from_the_cutoff(self, ext, depth, rate, strict, cutoff, tier):
        assert tier_of(ext, depth, rate, strict, cutoff) == tier

    def test_tiers_are_ordered_worst_first(self):
        assert rules.TIERS[0] == rules.TIER_DAMAGED and rules.TIERS[-1] == rules.TIER_HIRES

    def test_audit_round_trips_through_a_dict(self):
        a = _a(cutoff=18.2, artist="X", title="Y")
        assert Audit.from_dict({**a.to_dict(), "unknown": 1}) == a

    def test_describe(self):
        assert rules.describe(_a(cutoff=21.94)) == "FLAC 16/44.1, cutoff 21.9 kHz"
        assert rules.describe(Audit(ext="mp3", kbps=320)) == "MP3 320 kbps"
        assert rules.describe(_a(strict=False, cutoff=None)) == "FLAC 16/44.1, decode errors"


class TestWanted:
    def _copy(self, ext="flac", depth=16, rate=44100, length=200):
        return SearchResult("u", f"\\x\\a.{ext}", 1, bit_depth=depth, sample_rate=rate, length=length)

    def test_only_checkable_lossless_copies(self):
        assert wanted(rules.TIER_LOSSY, self._copy(), 200)
        assert not wanted(rules.TIER_LOSSY, self._copy("mp3"), 200)
        assert not wanted(rules.TIER_LOSSY, self._copy("ape"), 200)

    def test_length_within_three_seconds(self):
        assert wanted(rules.TIER_FAKE, self._copy(length=203), 200)
        assert not wanted(rules.TIER_FAKE, self._copy(length=204), 200)
        assert wanted(rules.TIER_FAKE, self._copy(length=None), 200)

    def test_a_cd_song_wants_hires_only(self):
        assert not wanted(rules.TIER_CD, self._copy(depth=16), 200)
        assert not wanted(rules.TIER_CD, self._copy(depth=24, rate=48000), 200)
        assert wanted(rules.TIER_CD, self._copy(depth=24, rate=96000), 200)
        assert not wanted(rules.TIER_HIRES, self._copy(depth=24, rate=192000), 200)


class TestBetter:
    def test_cd_needs_genuine_content_above_24_khz(self):
        cd = _a(cutoff=21.5)
        assert not better(cd, _a(cutoff=22.05))
        assert not better(cd, _a(depth=24, rate=96000, cutoff=22.0))  # a CD master upsampled
        assert better(cd, _a(depth=24, rate=96000, cutoff=30.0))

    def test_fake_uncertain_and_lossy_need_19_5_and_one_khz_more(self):
        assert not better(_a(cutoff=15.0), _a(cutoff=19.4))
        assert better(_a(cutoff=15.0), _a(cutoff=19.5))
        assert not better(_a(cutoff=19.2), _a(cutoff=20.1))  # uncertain at 19.2 needs 20.2
        assert better(_a(cutoff=19.2), _a(cutoff=20.2))
        assert better(Audit(ext="mp3", tier=rules.TIER_LOSSY), _a(cutoff=19.5))
        assert not better(Audit(ext="mp3", tier=rules.TIER_LOSSY), _a(cutoff=19.0))

    def test_damaged_needs_not_worse(self):
        damaged = _a(strict=False, cutoff=21.0)
        assert better(damaged, _a(cutoff=19.0))
        assert not better(damaged, _a(cutoff=18.9))

    def test_a_candidate_must_decode_cleanly_and_be_lossless(self):
        fake = _a(cutoff=14.0)
        assert not better(fake, _a(strict=False, cutoff=22.0))
        assert not better(fake, Audit(ext="mp3", cutoff=22.0, strict=None, tier=rules.TIER_LOSSY))
        assert not better(fake, _a(cutoff=None))

    def test_nothing_beats_hires(self):
        assert not better(_a(depth=24, rate=96000, cutoff=40.0), _a(depth=24, rate=192000, cutoff=90.0))


STEM = "Elvis Presley - Always On My Mind"


def _orig(**kw):
    return _a(cutoff=15.0, artist="Elvis Presley", title="Always On My Mind", album="Separate Ways", **kw)


def _cand(length=200.0, **tags):
    tags.setdefault("artist", "Elvis Presley")
    tags.setdefault("title", "Always On My Mind")
    tags.setdefault("album", "Separate Ways")
    return _a(cutoff=21.5, length=length, **tags)


class TestSameness:
    def test_same_length_same_names_is_the_same(self):
        assert sameness(_orig(), _cand(201.4), STEM)[0] == SAME

    def test_a_few_seconds_off_is_for_the_ear(self):
        kind, why = sameness(_orig(), _cand(204.0), STEM)
        assert kind == REVIEW and "length differs 4.0 s" in why

    def test_more_than_ten_seconds_is_another_edit(self):
        assert sameness(_orig(), _cand(211.0), STEM)[0] == REJECT

    def test_another_artist_is_rejected(self):
        kind, why = sameness(_orig(), _cand(artist="The King's Singers"), STEM)
        assert kind == REJECT and "artist" in why

    @pytest.mark.parametrize(
        "tags",
        [
            {"album": "Karaoke Hits Vol. 3"},
            {"album": "A Tribute to the King"},
            {"title": "Always On My Mind (In the Style of Elvis Presley)"},
        ],
    )
    def test_karaoke_tribute_and_covers_are_rejected(self, tags):
        assert sameness(_orig(), _cand(**tags), STEM)[0] == REJECT

    @pytest.mark.parametrize("word", ["Live", "Unplugged", "Acoustic", "Remix"])
    def test_another_recording_is_rejected(self, word):
        assert sameness(_orig(), _cand(title=f"Always On My Mind ({word})"), STEM)[0] == REJECT

    def test_a_live_original_accepts_a_live_copy(self):
        stem = "Elvis Presley - Always On My Mind (Live)"
        orig = _a(cutoff=15.0, artist="Elvis Presley", title="Always On My Mind (Live)")
        assert sameness(orig, _cand(title="Always On My Mind (Live)"), stem)[0] == SAME

    def test_remaster_years_are_not_performers(self):
        assert sameness(_orig(), _cand(title="Always On My Mind - 2002 Remaster"), STEM)[0] == SAME

    def test_an_extra_performer_is_for_the_ear(self):
        kind, why = sameness(_orig(), _cand(artist="Elvis Presley with the Royal Philharmonic Orchestra"), STEM)
        assert kind == REVIEW and "philharmonic" in why

    def test_another_title_is_rejected(self):
        assert sameness(_orig(), _cand(title="Suspicious Minds"), STEM)[0] == REJECT


class TestJudgeWithFingerprint:
    def test_no_fingerprint_falls_back_to_the_length_rule(self):
        assert judge(_orig(), _cand(204.0), STEM, None) == sameness(_orig(), _cand(204.0), STEM)

    def test_same_recording_is_replaced_although_the_length_differs(self):
        kind, why = judge(_orig(), _cand(205.0), STEM, 0.92)
        assert kind == SAME and "same recording" in why

    def test_another_recording_is_never_replaced_automatically(self):
        kind, why = judge(_orig(), _cand(200.2), STEM, 0.60)
        assert kind == REVIEW and "another recording" in why

    def test_the_threshold(self):
        assert judge(_orig(), _cand(200.0), STEM, rules.SAME_RECORDING_SIMILARITY)[0] == SAME
        assert judge(_orig(), _cand(200.0), STEM, rules.SAME_RECORDING_SIMILARITY - 0.01)[0] == REVIEW

    def test_same_recording_with_new_names_goes_to_the_ear(self):
        cand = _cand(artist="Elvis Presley with the Royal Philharmonic Orchestra")
        assert judge(_orig(), cand, STEM, 0.95)[0] == REVIEW

    def test_another_recording_far_off_in_length_is_rejected(self):
        assert judge(_orig(), _cand(215.0), STEM, 0.55)[0] == REJECT

    def test_the_tags_still_reject_whatever_the_fingerprint(self):
        assert judge(_orig(), _cand(album="Karaoke Hits"), STEM, 0.99)[0] == REJECT


def _tone(path, expr, rate=44100, secs=60, delay=0):
    """A tone pattern made by ffmpeg (aevalsrc), optionally with *delay* seconds of silence first."""
    src = f"aevalsrc={expr}:s={rate}:d={secs}"
    filters = ["-af", f"adelay={delay * 1000}|{delay * 1000}"] if delay else []
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *filters, "-c:a", "flac", str(path)], check=True
    )
    return str(path)


# Two notes stepping through a scale, at different speeds: a "melody" Chromaprint can follow.
PATTERN_A = "0.5*sin(2*PI*t*440*pow(2\\,floor(mod(t*3\\,7))/12))+0.3*sin(2*PI*t*1100*pow(2\\,floor(mod(t*5\\,5))/12))"
PATTERN_B = "0.5*sin(2*PI*t*523*pow(2\\,floor(mod(t*2\\,11))/12))+0.3*sin(2*PI*t*700*pow(2\\,floor(mod(t*7\\,3))/12))"

needs_fpcalc = pytest.mark.skipif(
    not (shutil.which("fpcalc") and shutil.which("ffmpeg")), reason="needs fpcalc (chromaprint) and ffmpeg"
)


@needs_fpcalc
class TestFingerprint:
    def test_the_same_tone_at_another_rate_scores_high(self, tmp_path):
        a = rules.fingerprint(_tone(tmp_path / "a.flac", PATTERN_A))
        b = rules.fingerprint(_tone(tmp_path / "b.flac", PATTERN_A, rate=48000))
        assert a and b
        assert similarity(a, b) >= rules.SAME_RECORDING_SIMILARITY

    def test_a_different_pattern_scores_low(self, tmp_path):
        a = rules.fingerprint(_tone(tmp_path / "a.flac", PATTERN_A))
        b = rules.fingerprint(_tone(tmp_path / "b.flac", PATTERN_B))
        assert similarity(a, b) < 0.7

    def test_silence_at_the_start_is_found_by_the_lag_search(self, tmp_path):
        a = rules.fingerprint(_tone(tmp_path / "a.flac", PATTERN_A))
        b = rules.fingerprint(_tone(tmp_path / "b.flac", PATTERN_A, delay=5))  # the pattern repeats every 7 s
        assert similarity(a, b) >= rules.SAME_RECORDING_SIMILARITY
        # Without the lag search the shifted copy would not be recognised.
        assert similarity(a, b, max_lag_secs=0) < rules.SAME_RECORDING_SIMILARITY

    def test_no_fpcalc_means_no_fingerprint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rules.shutil, "which", lambda name: None)
        assert rules.fingerprint(_tone(tmp_path / "a.flac", PATTERN_A)) is None


class TestSimilarityMath:
    def test_identical_is_one_and_inverted_is_zero(self):
        fp = [i * 2654435761 % 2**32 for i in range(400)]
        assert similarity(fp, fp) == 1.0
        assert similarity(fp, [x ^ 0xFFFFFFFF for x in fp], max_lag_secs=0) == 0.0

    def test_empty_is_zero(self):
        assert similarity([], [1, 2, 3]) == 0.0


class TestKeepBothName:
    def test_extra_performer_from_the_artist_tag(self):
        cand = _cand(artist="Elvis Presley with the Royal Philharmonic Orchestra", album="If I Can Dream")
        name = keep_both_name(STEM, "flac", _orig(), cand, lambda n: False)
        assert name == "Elvis Presley - Always On My Mind (with the Royal Philharmonic Orchestra).flac"

    def test_an_ampersand_becomes_with(self):
        cand = _cand(artist="Elvis Presley & The Royal Philharmonic Orchestra")
        assert keep_both_name(STEM, "flac", _orig(), cand, lambda n: False).endswith(
            "(with The Royal Philharmonic Orchestra).flac"
        )

    def test_a_bracket_of_the_title_next(self):
        cand = _cand(title="Always On My Mind (1997 Version)", album="")
        assert keep_both_name(STEM, "flac", _orig(), cand, lambda n: False) == f"{STEM} (1997 Version).flac"

    def test_the_album_then(self):
        cand = _cand(album="The Essential Elvis")
        assert keep_both_name(STEM, "wav", _orig(), cand, lambda n: False) == f"{STEM} (The Essential Elvis).wav"

    def test_alternate_when_nothing_tells_them_apart(self):
        cand = _cand(album="")
        assert keep_both_name(STEM, "flac", _orig(), cand, lambda n: False) == f"{STEM} (alternate).flac"

    def test_never_a_name_that_is_taken(self):
        taken = {f"{STEM} (alternate).flac", f"{STEM} (alternate) 2.flac"}
        cand = _cand(album="")
        assert keep_both_name(STEM, "flac", _orig(), cand, taken.__contains__) == f"{STEM} (alternate) 3.flac"

    def test_characters_a_file_name_cannot_hold_are_dropped(self):
        cand = _cand(album='Live / "Vegas" 1970?')
        assert keep_both_name(STEM, "flac", _orig(), cand, lambda n: False) == f"{STEM} (Live Vegas 1970).flac"


class TestSchedule:
    def test_default_is_sunday_four(self):
        assert parse_schedule("") == parse_schedule(None) == rules.Schedule(4, 0, 6)
        assert str(parse_schedule("")) == "weekly:sun:04:00"

    def test_weekly_daily_and_off(self):
        assert parse_schedule("weekly:wed:23:30") == rules.Schedule(23, 30, 2)
        assert parse_schedule("DAILY:3:05") == rules.Schedule(3, 5, None)
        assert parse_schedule("off") is None

    @pytest.mark.parametrize("text", ["weekly", "weekly:sunday:04:00", "daily:24:00", "daily:4:60", "monthly:1:04:00"])
    def test_anything_else_is_refused(self, text):
        with pytest.raises(ValueError):
            parse_schedule(text)

    def test_next_and_previous_weekly(self):
        sched = parse_schedule("weekly:sun:04:00")
        wed = datetime.datetime(2026, 10, 7, 12, 0)  # a Wednesday
        assert next_run(sched, wed) == datetime.datetime(2026, 10, 11, 4, 0)
        assert previous_run(sched, wed) == datetime.datetime(2026, 10, 4, 4, 0)
        sunday_early = datetime.datetime(2026, 10, 11, 3, 59)
        assert previous_run(sched, sunday_early) == datetime.datetime(2026, 10, 4, 4, 0)
        sunday_on_time = datetime.datetime(2026, 10, 11, 4, 0)
        assert previous_run(sched, sunday_on_time) == sunday_on_time
        assert next_run(sched, sunday_on_time) == datetime.datetime(2026, 10, 18, 4, 0)

    def test_next_and_previous_daily(self):
        sched = parse_schedule("daily:04:00")
        assert next_run(sched, datetime.datetime(2026, 10, 7, 3, 0)) == datetime.datetime(2026, 10, 7, 4, 0)
        assert previous_run(sched, datetime.datetime(2026, 10, 7, 3, 0)) == datetime.datetime(2026, 10, 6, 4, 0)


needs_analysis = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@needs_analysis
class TestAuditRealFiles:
    def _noise(self, tmp_path):
        path = tmp_path / "noise.flac"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anoisesrc=d=40:c=white:a=0.3",
                "-ar",
                "44100",
                "-sample_fmt",
                "s16",
                "-c:a",
                "flac",
                str(path),
            ],
            check=True,
        )
        return path

    def test_a_genuine_flac_is_cd(self, tmp_path):
        pytest.importorskip("soundfile")
        a = rules.audit(str(self._noise(tmp_path)))
        assert a.strict is True and a.tier == rules.TIER_CD and a.cutoff >= rules.CD_CUTOFF_KHZ

    def test_a_flac_made_from_an_mp3_is_fake(self, tmp_path):
        pytest.importorskip("soundfile")
        noise = self._noise(tmp_path)
        mp3, fake = tmp_path / "n.mp3", tmp_path / "fake.flac"
        # The 15 kHz lowpass of a low-bitrate MP3 (lame keeps about 20 kHz on synthetic noise by itself).
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(noise), "-b:a", "128k", "-cutoff", "15000", str(mp3)], check=True
        )
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-c:a", "flac", str(fake)], check=True)
        a = rules.audit(str(fake))
        assert a.tier == rules.TIER_FAKE, a

    def test_a_corrupted_flac_is_damaged(self, tmp_path):
        noise = self._noise(tmp_path)
        data = bytearray(noise.read_bytes())
        for i in range(len(data) // 2, len(data) // 2 + 4000, 7):
            data[i] ^= 0x5A
        noise.write_bytes(bytes(data))
        assert rules.strict_decode_ok(str(noise)) is False
        assert rules.audit(str(noise)).tier == rules.TIER_DAMAGED


def test_a_long_album_is_cut_at_a_word():
    cand = _cand(album="The Complete Recordings " + "Volume " * 30)
    name = keep_both_name(STEM, "flac", _orig(), cand, lambda n: False)
    assert len(name.encode()) < 200 and name.endswith("Volume).flac")


class TestToolFailures:
    def test_a_decode_that_cannot_run_is_unknown_not_damaged(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("ffmpeg")

        monkeypatch.setattr(rules.subprocess, "run", boom)
        assert rules.strict_decode_ok("/nowhere.flac") is None

    def test_a_timeout_is_unknown(self, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("ffmpeg", 900)

        monkeypatch.setattr(rules.subprocess, "run", slow)
        assert rules.strict_decode_ok("/nowhere.flac") is None

    def test_unknown_goes_by_the_cutoff_and_never_wins(self):
        assert tier_of("flac", 16, 44100, None, 21.0) == rules.TIER_CD
        assert not better(_a(cutoff=14.0), _a(strict=None, cutoff=22.0))

    def test_missing_tools(self, monkeypatch):
        monkeypatch.setattr(rules.shutil, "which", lambda name: None)
        assert rules.missing_tools()[0] == "ffmpeg"
