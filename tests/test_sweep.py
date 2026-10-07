"""The library sweep end to end over a fake pipeline: replace, review, the three decisions, purge, resume,
the Telegram report and buttons, the MCP tools, the env vars and the MusicBrainz retry."""

import asyncio
import datetime
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from music_downloader.bot.keyboards import build_sweep_review_keyboard
from music_downloader.mcp.tools import McpTools
from music_downloader.metadata.musicbrainz import MusicBrainzClient
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.database import Database
from music_downloader.persistence.sweep_repo import RUN_RUNNING, SweepRun
from music_downloader.pipeline import sweep as sw
from music_downloader.pipeline import sweep_rules as rules
from music_downloader.pipeline.fetch import FetchOutcome, GatedFetch
from music_downloader.pipeline.resolve import Candidate
from music_downloader.pipeline.search import RankedResults
from music_downloader.pipeline.sweep_rules import Audit
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import CHAT, _make_bot, _make_config

STEM = "Elvis Presley - Always On My Mind"
TRACK = TrackInfo("Elvis Presley", "Always On My Mind", "Separate Ways", 200_000, "u", "1972")
SAME_FP = [i * 2654435761 % 2**32 for i in range(1200)]
OTHER_FP = [i * 40503 % 2**32 for i in range(1200)]


def _audit(ext="flac", cutoff=15.0, length=200.0, depth=16, rate=44100, **tags):
    a = Audit(ext=ext, length=length, depth=depth, rate=rate, strict=True, cutoff=cutoff)
    a.artist = tags.get("artist", "Elvis Presley")
    a.title = tags.get("title", "Always On My Mind")
    a.album = tags.get("album", "Separate Ways")
    a.tier = rules.tier_of(ext, depth, rate, True, cutoff)
    return a


class FakePipeline:
    """What LibrarySweep uses of a Pipeline: Spotify, slskd and the gate canned, SQLite and files real."""

    def __init__(self, tmp_path, copies=1, keep_days=7):
        self.lib = tmp_path / "music"
        self.dl = tmp_path / "downloads"
        self.lib.mkdir()
        self.dl.mkdir()
        self.config = SimpleNamespace(
            output_dir=str(self.lib),
            library_sweep_users={CHAT},
            library_sweep_schedule="",
            library_sweep_pause_secs=0,
            library_sweep_keep_days=keep_days,
        )
        self.db = Database(str(tmp_path / "importer.db"))
        self.album_owner = "bot"
        self.library_index = MagicMock()
        self.resolve_match = AsyncMock(return_value=[Candidate(TRACK, True)])
        self.copies = [
            SearchResult(f"peer{i}", f"\\m\\{STEM} {i}.flac", 30_000_000, bit_depth=16, sample_rate=44100, length=200)
            for i in range(copies)
        ]
        self.search = AsyncMock(side_effect=lambda q, t, p: RankedResults(list(self.copies)))
        self.fetched = []
        self.discarded = []
        self.record_history = AsyncMock()
        self.forget_transfer = MagicMock()

    async def fetch_for_library(self, results, index, track, progress_for):
        result = results[index]
        path = self.dl / f"{STEM} {index}.flac"
        path.write_bytes(b"candidate %d" % index)
        self.fetched.append(str(path))
        return GatedFetch(FetchOutcome(path=str(path), transfer_id=f"t{index}"), result, index)

    async def discard(self, path, username="", transfer_id=""):
        self.discarded.append(path)
        os.remove(path)


def _sweep(tmp_path, candidate: Audit, fp=SAME_FP, original: Audit | None = None, **kw):
    pipe = FakePipeline(tmp_path, **kw)
    (pipe.lib / f"{STEM}.flac").write_bytes(b"original")
    sweep = sw.LibrarySweep(pipe)
    original = original or _audit()
    sweep.audit = lambda p: original if os.path.dirname(p) == str(pipe.lib) else candidate
    sweep.fingerprint = lambda p: SAME_FP if os.path.dirname(p) == str(pipe.lib) else fp
    sweep.sleep = AsyncMock()
    return sweep, pipe


async def _run_once(sweep, force=False):
    run, why = sweep.start("test", force)
    assert run is not None, why
    await sweep._task
    return sweep.repo.latest_run()


class TestOneSong:
    async def test_same_recording_and_better_replaces_and_parks_the_original(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5, length=204.0))
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_UPGRADED: 1}
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"candidate 0"
        parked = pipe.lib / sw.REPLACED_DIR / f"{STEM}.flac.sweep"
        assert parked.read_bytes() == b"original"
        assert not list(pipe.dl.iterdir()), "the download is deleted"
        reps = sweep.repo.replacements_for_run(run.id)
        assert [r.stem for r in reps] == [STEM] and reps[0].parked_path == str(parked)
        pipe.record_history.assert_awaited()

    async def test_another_recording_is_staged_for_the_ear_and_the_library_is_untouched(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5, length=200.5), fp=OTHER_FP)
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_REVIEW: 1}
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"original"
        review = sweep.repo.pending_review_for(STEM)
        assert review.proposal_path == str(pipe.lib / sw.REVIEW_DIR / f"{STEM}.flac.sweep")
        assert Path(review.proposal_path).read_bytes() == b"candidate 0"
        assert "another recording" in review.reason
        assert not list(pipe.dl.iterdir())

    async def test_without_fpcalc_the_length_rule_decides(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5, length=204.0))
        sweep.fingerprint = lambda p: None
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_REVIEW: 1}

    async def test_not_better_copies_are_deleted_and_the_next_tried(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=15.5), copies=5)
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_NONE_QUALIFIED: 1}
        assert len(pipe.fetched) == sw.OTHER_TRIES
        assert sorted(pipe.discarded) == sorted(pipe.fetched)
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"original"

    async def test_no_confident_match_searches_nothing(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5))
        pipe.resolve_match.return_value = [Candidate(TRACK, False)]
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_NO_MATCH: 1}
        pipe.search.assert_not_awaited()

    async def test_a_hires_song_is_not_searched_unless_forced(self, tmp_path):
        hires = _audit(cutoff=40.0, depth=24, rate=96000)
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5), original=hires)
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_HIRES: 1}
        pipe.resolve_match.assert_not_awaited()
        run = await _run_once(sweep, force=True)
        pipe.resolve_match.assert_awaited()

    async def test_a_pending_pair_is_not_downloaded_again(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5), fp=OTHER_FP)
        await _run_once(sweep)
        pipe.fetched.clear()
        run = await _run_once(sweep)
        assert run.counts == {sw.OUTCOME_WAITING: 1}
        assert pipe.fetched == []

    async def test_a_target_of_another_format_is_never_overwritten(self, tmp_path):
        mp3 = Audit(ext="mp3", length=200.0, kbps=320, tier=rules.TIER_LOSSY, artist="Elvis Presley")
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5), original=mp3)
        (pipe.lib / f"{STEM}.flac").rename(pipe.lib / f"{STEM}.mp3")
        (pipe.lib / f"{STEM}.flac").write_bytes(b"someone else's flac")
        sweep.audit = lambda p: mp3 if p.endswith(f"{STEM}.mp3") else _audit(cutoff=21.5)
        await _run_once(sweep)
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"someone else's flac"
        assert (pipe.lib / f"{STEM}.mp3").read_bytes() == b"original"


class TestDecisions:
    async def _pending(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5, artist="Elvis Presley with the RPO"), fp=OTHER_FP)
        await _run_once(sweep)
        return sweep, pipe, sweep.repo.pending_review_for(STEM)

    async def test_keep_mine_deletes_the_proposal_and_declines_the_song(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        await sweep.decide(review.id, "keep_mine")
        assert not os.path.exists(review.proposal_path)
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"original"
        assert sweep.repo.pending_reviews() == []
        assert sweep.repo.is_declined(STEM)
        # The next sweep downloads again but proposes nothing for the ear.
        (pipe.lib / f"{STEM}.flac").write_bytes(b"original")
        run = await _run_once(sweep, force=True)
        assert run.counts == {sw.OUTCOME_NONE_QUALIFIED: 1}
        assert sweep.repo.pending_reviews() == []

    async def test_take_new_replaces_and_parks(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        _, path = await sweep.decide(review.id, "take_new")
        assert path == str(pipe.lib / f"{STEM}.flac")
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"candidate 0"
        assert (pipe.lib / sw.REPLACED_DIR / f"{STEM}.flac.sweep").read_bytes() == b"original"
        assert not os.path.exists(review.proposal_path)
        assert not sweep.repo.is_declined(STEM)

    async def test_keep_both_adds_the_proposal_as_its_own_song(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        _, path = await sweep.decide(review.id, "keep_both")
        assert os.path.basename(path) == f"{STEM} (with the RPO).flac"
        assert Path(path).read_bytes() == b"candidate 0"
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"original"
        assert sweep.repo.is_declined(STEM)
        pipe.library_index.add.assert_called_with(path)

    async def test_a_second_decision_is_refused(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        await sweep.decide(review.id, "keep_mine")
        with pytest.raises(sw.SweepError, match="already decided"):
            await sweep.decide(review.id, "take_new")

    async def test_a_failed_move_leaves_the_pair_open(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        with patch.object(sw, "replace_in_library", side_effect=OSError("disk full")), pytest.raises(OSError):
            await sweep.decide(review.id, "take_new")
        assert [r.id for r in sweep.repo.pending_reviews()] == [review.id]
        assert os.path.exists(review.proposal_path)

    async def test_unknown_decision(self, tmp_path):
        sweep, pipe, review = await self._pending(tmp_path)
        with pytest.raises(sw.SweepError):
            await sweep.decide(review.id, "toss_a_coin")


class TestFilesAndPurge:
    def test_replace_restores_the_original_when_the_rename_fails(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "A - B.mp3").write_bytes(b"orig")
        src = tmp_path / "new.flac"
        src.write_bytes(b"new")
        real_replace = os.replace

        def flaky(a, b):
            if str(b).endswith("A - B.flac"):
                raise OSError("boom")
            return real_replace(a, b)

        with patch.object(sw.os, "replace", side_effect=flaky), pytest.raises(OSError):
            sw.replace_in_library(str(lib / "A - B.mp3"), str(src), "flac", str(lib / sw.REPLACED_DIR))
        assert (lib / "A - B.mp3").read_bytes() == b"orig"
        assert sorted(p.name for p in lib.iterdir()) == [".sweep-replaced", "A - B.mp3"]

    def test_parked_names_never_collide(self, tmp_path):
        p = tmp_path / "x.flac.sweep"
        p.write_bytes(b"1")
        assert sw.free_path(str(p)) == str(tmp_path / "x.flac.2.sweep")

    def test_purge_after_keep_days_and_only_parked_files(self, tmp_path):
        pipe = FakePipeline(tmp_path, keep_days=7)
        sweep = sw.LibrarySweep(pipe)
        parked_dir = pipe.lib / sw.REPLACED_DIR
        parked_dir.mkdir()
        old, new = parked_dir / "old.flac.sweep", parked_dir / "new.flac.sweep"
        old.write_bytes(b"o")
        new.write_bytes(b"n")
        outside = pipe.lib / "Someone - Song.flac"
        outside.write_bytes(b"user file")
        now = time.time()
        for path, age in ((old, 8), (new, 1), (outside, 30)):
            sweep.repo.add_replacement(
                sw.Replacement("s", "x", str(path), "", "", "auto", replaced_at=now - age * 86400)
            )
        assert sweep.purge_parked(now) == 1
        assert not old.exists() and new.exists()
        assert outside.read_bytes() == b"user file", "a path outside .sweep-replaced is never deleted"

    async def test_keep_days_zero_deletes_at_once(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5), keep_days=0)
        await _run_once(sweep)
        assert not (pipe.lib / sw.REPLACED_DIR / f"{STEM}.flac.sweep").exists()
        assert (pipe.lib / f"{STEM}.flac").read_bytes() == b"candidate 0"

    def test_the_library_index_leaves_the_sweep_folders_out(self, tmp_path):
        from music_downloader.persistence.library_index import LibraryIndex

        lib = tmp_path / "lib"
        (lib / sw.REPLACED_DIR).mkdir(parents=True)
        (lib / "A - B.flac").write_bytes(b"")
        (lib / sw.REPLACED_DIR / "A - B.flac").write_bytes(b"")
        index = LibraryIndex(Database(str(tmp_path / "i.db")), str(lib))
        assert index.rebuild() == 1
        assert index.find_stems("A - B") == ["A - B.flac"]
        index.remove(str(lib / "A - B.flac"))
        assert index.find_stems("A - B") == []

    def test_library_files_skip_hidden_and_non_audio(self, tmp_path):
        (tmp_path / "A - B.flac").write_bytes(b"")
        (tmp_path / "A - B.lrc").write_bytes(b"")
        (tmp_path / ".hidden.flac").write_bytes(b"")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "C - D.flac").write_bytes(b"")
        assert sw.library_files(str(tmp_path)) == ["A - B.flac"]


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@needs_ffmpeg
class TestCarry:
    def test_tags_and_cover_the_candidate_lacks_are_carried(self, tmp_path):
        import mutagen.flac

        def flac(path, **meta):
            args = [a for k, v in meta.items() for a in ("-metadata", f"{k}={v}")]
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", *args, "-c:a", "flac", str(path)],
                check=True,
            )
            return str(path)

        orig = flac(tmp_path / "o.flac", artist="Elvis Presley", album="Separate Ways", date="1972")
        f = mutagen.flac.FLAC(orig)
        pic = mutagen.flac.Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", b"\xff\xd8cover"
        f.add_picture(pic)
        f.save()
        cand = flac(tmp_path / "c.flac", artist="Elvis Presley", album="Their Own Album")
        sw.carry_tags_and_cover(orig, cand)
        c = mutagen.flac.FLAC(cand)
        assert c["album"] == ["Their Own Album"], "the candidate's own tags win"
        assert c["date"] == ["1972"]
        assert c.pictures[0].data == b"\xff\xd8cover"


class TestDueAndResume:
    def _song(self, tier, last):
        return sw.SweepSong("f", "s", 1, 1.0, tier, {}, last_checked_at=last)

    def test_per_tier_cadence(self):
        now = 10_000_000.0
        run = SweepRun(started_at=now)
        day = 86400
        assert sw.is_due(self._song(rules.TIER_FAKE, now - day), run, now)
        assert not sw.is_due(self._song(rules.TIER_CD, now - 29 * day), run, now)
        assert sw.is_due(self._song(rules.TIER_CD, now - 31 * day), run, now)
        assert not sw.is_due(self._song(rules.TIER_HIRES, now - 400 * day), run, now)
        assert sw.is_due(self._song(rules.TIER_HIRES, None), run, now)
        assert sw.is_due(self._song(rules.TIER_HIRES, now - day), SweepRun(started_at=now, force=True), now)
        assert not sw.is_due(self._song(rules.TIER_FAKE, now + 1), run, now), "checked in this run already"

    async def test_an_interrupted_sweep_continues_without_redoing_songs(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=15.0), copies=1)
        (pipe.lib / "Other - Song.flac").write_bytes(b"x")
        run = sweep.repo.add_run(SweepRun(trigger="schedule", started_at=time.time() - 60))
        # The first song was checked in this run before the stop.
        for name in (f"{STEM}.flac",):
            st = os.stat(pipe.lib / name)
            sweep.repo.save_audit(sw.SweepSong(name, STEM, st.st_size, st.st_mtime, "fake", _audit().to_dict()))
            sweep.repo.mark_checked(name, time.time(), sw.OUTCOME_NONE_QUALIFIED)
        assert sweep.resume().id == run.id
        await sweep._task
        done = sweep.repo.latest_run()
        assert done.status == "done"
        assert pipe.resolve_match.await_count == 1
        assert pipe.resolve_match.await_args.args[1:3] == ("Other", "Song")

    async def test_only_one_sweep_at_a_time(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5))
        gate = asyncio.Event()

        async def slow(*a, **k):
            await gate.wait()
            return []

        pipe.resolve_match.side_effect = slow
        run, _ = sweep.start("test")
        assert run is not None
        again, why = sweep.start("test")
        assert again is None and "already running" in why
        gate.set()
        await sweep._task

    def test_a_live_sweep_in_another_process_blocks_a_start(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit())
        sweep.repo.add_run(SweepRun(owner="stdio", status=RUN_RUNNING))
        run, why = sweep.start("test")
        assert run is None and "another process" in why

    def test_off_without_users(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit())
        pipe.config.library_sweep_users = set()
        assert sweep.start("test") == (None, "the library sweep is off: LIBRARY_SWEEP_USERS is empty")


class TestSchedule:
    async def test_a_slot_starts_a_sweep_once_and_a_running_sweep_gets_a_progress_report(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit())
        reports = []
        sweep.on_report = AsyncMock(side_effect=reports.append)
        sched = rules.parse_schedule("weekly:sun:04:00")
        loop_started = datetime.datetime(2026, 10, 7, 12, 0).timestamp()
        # Before the first slot after the loop started: nothing.
        await sweep._maybe_start(sched, datetime.datetime(2026, 10, 10, 12, 0), loop_started)
        assert sweep.repo.latest_run() is None
        gate = asyncio.Event()
        pipe.resolve_match.side_effect = lambda *a, **k: gate.wait()
        sunday = datetime.datetime(2026, 10, 11, 4, 0, 5)
        await sweep._maybe_start(sched, sunday, loop_started)
        first = sweep.repo.latest_run()
        assert first is not None and first.trigger == "schedule"
        # The clock here is made up: the run started at the slot.
        sweep.repo._conn.execute("UPDATE sweep_runs SET started_at = ?", (sunday.timestamp(),))
        await sweep._maybe_start(sched, sunday, loop_started)
        assert sweep.repo.latest_run().id == first.id and reports == []
        # A week later the sweep still runs: a progress report, once.
        next_sunday = datetime.datetime(2026, 10, 18, 4, 0, 5)
        await sweep._maybe_start(sched, next_sunday, loop_started)
        await sweep._maybe_start(sched, next_sunday, loop_started)
        assert [r.in_progress for r in reports] == [True]
        gate.set()
        await sweep.stop()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _bot(tmp_path, chat_users=None):
    config = _make_config(str(tmp_path), chat_users=chat_users)
    config.library_sweep_users = {CHAT, 12345}
    config.library_sweep_schedule = ""
    config.library_sweep_keep_days = 7
    config.library_sweep_pause_secs = 0
    bot = _make_bot(config)
    os.makedirs(config.output_dir, exist_ok=True)
    return bot, config


def _review(bot, config, tmp_path, stem=STEM):
    lib = config.output_dir
    current = os.path.join(lib, f"{stem}.flac")
    with open(current, "wb") as f:
        f.write(b"mine")
    staged_dir = os.path.join(lib, sw.REVIEW_DIR)
    os.makedirs(staged_dir, exist_ok=True)
    staged = os.path.join(staged_dir, f"{stem}.flac.sweep")
    with open(staged, "wb") as f:
        f.write(b"theirs")
    return bot.pipeline.sweep.repo.add_review(
        sw.Review(
            stem=stem,
            current_path=current,
            proposal_path=staged,
            proposal_ext="flac",
            both_name=f"{stem} (alternate).flac",
            reason="another recording (fingerprint 0.60), length differs 0.4 s",
            current_desc="FLAC 16/44.1, cutoff 15.0 kHz",
            proposal_desc="FLAC 16/44.1, cutoff 21.5 kHz",
            current_len=200.0,
            proposal_len=200.4,
        )
    )


class TestTelegram:
    def test_chat_delivery_accounts_never_get_the_sweep(self, tmp_path):
        bot, _ = _bot(tmp_path, chat_users={12345})
        assert bot._sweep_users() == {CHAT}

    async def test_report_then_each_pair_with_three_buttons(self, tmp_path, monkeypatch):
        monkeypatch.setattr("music_downloader.bot.handlers.SWEEP_SEND_PAUSE_SECS", 0)
        bot, config = _bot(tmp_path, chat_users={12345})
        review = _review(bot, config, tmp_path)
        run = SweepRun(id=1, trigger="schedule", total=2400, done=2400, counts={"upgraded": 2, "review": 1})
        rep = sw.Replacement(STEM, "x", "y", "MP3 320 kbps", "FLAC 16/44.1, cutoff 21.9 kHz", "auto")
        tg = AsyncMock()
        await bot._send_sweep_report(tg, sw.SweepReport(run, [rep], [review]))
        assert [c.kwargs["chat_id"] for c in tg.send_message.await_args_list] == [CHAT]
        text = tg.send_message.await_args.kwargs["text"]
        assert "Weekly library sweep done" in text and "Upgraded: 2" in text and "For your ear: 1" in text
        assert "MP3 320 kbps → FLAC 16/44.1" in text
        sent = tg.send_audio.await_args_list
        assert [c.kwargs["filename"] for c in sent] == [f"{STEM} (yours).flac", f"{STEM} (proposal).flac"]
        assert sent[0].kwargs["reply_markup"] is None
        assert sent[1].kwargs["reply_markup"] == build_sweep_review_keyboard(review.id)
        assert "Why it needs your ear" in sent[1].kwargs["caption"]
        assert bot.pipeline.sweep.repo.get_review(review.id).offered_at is not None

    async def test_a_file_over_the_cap_goes_as_opus(self, tmp_path, monkeypatch):
        monkeypatch.setattr("music_downloader.bot.handlers.SWEEP_SEND_PAUSE_SECS", 0)
        bot, config = _bot(tmp_path)
        review = _review(bot, config, tmp_path)
        bot.pipeline.upload_limit_bytes = 3
        bot.pipeline.opus_bitrates_that_fit = lambda secs: [96]  # the real estimate never fits 3 bytes
        ogg = tmp_path / "small.ogg"
        ogg.write_bytes(b"o")
        bot.pipeline.convert_to_opus = AsyncMock(side_effect=lambda p, k: shutil.copy(ogg, tmp_path / f"{k}.ogg"))
        tg = AsyncMock()
        await bot._send_sweep_pair(tg, CHAT, review, 1, 1)
        names = [c.kwargs["filename"] for c in tg.send_audio.await_args_list]
        assert names == [f"{STEM} (yours).ogg", f"{STEM} (proposal).ogg"]
        assert "Opus" in tg.send_audio.await_args.kwargs["caption"]

    @pytest.mark.parametrize(
        ("choice", "mine", "proposal_left", "note"),
        [
            ("m", b"mine", False, "Kept yours"),
            ("n", b"theirs", False, "Took the new one"),
            ("b", b"mine", False, "Kept both"),
        ],
    )
    async def test_the_buttons(self, tmp_path, choice, mine, proposal_left, note):
        bot, config = _bot(tmp_path)
        review = _review(bot, config, tmp_path)
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = CHAT
        update.callback_query.message.caption_html = "2. Proposal"
        await bot._handle_sweep_decision(update, MagicMock(), CHAT, f"swp:{review.id}:{choice}")
        with open(review.current_path, "rb") as f:
            assert f.read() == mine
        assert os.path.exists(review.proposal_path) == proposal_left
        kwargs = update.callback_query.edit_message_caption.await_args.kwargs
        assert note in kwargs["caption"] and kwargs["reply_markup"] is None
        if choice == "b":
            assert os.path.exists(os.path.join(config.output_dir, f"{STEM} (alternate).flac"))

    async def test_a_second_tap_says_it_was_decided(self, tmp_path):
        bot, config = _bot(tmp_path)
        review = _review(bot, config, tmp_path)
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = CHAT
        update.callback_query.message.caption_html = ""
        await bot._handle_sweep_decision(update, MagicMock(), CHAT, f"swp:{review.id}:m")
        await bot._handle_sweep_decision(update, MagicMock(), CHAT, f"swp:{review.id}:n")
        assert "already decided" in update.callback_query.edit_message_caption.await_args.kwargs["caption"]
        with open(review.current_path, "rb") as f:
            assert f.read() == b"mine"

    async def test_someone_without_the_sweep_cannot_decide(self, tmp_path):
        bot, config = _bot(tmp_path, chat_users={12345})
        review = _review(bot, config, tmp_path)
        update = MagicMock()
        update.callback_query = AsyncMock()
        update.callback_query.from_user.id = 12345
        await bot._handle_sweep_decision(update, MagicMock(), 12345, f"swp:{review.id}:n")
        assert bot.pipeline.sweep.repo.pending_reviews()

    async def test_sweep_command(self, tmp_path):
        bot, config = _bot(tmp_path, chat_users={12345})
        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 12345
        update.message = AsyncMock()
        await bot.cmd_sweep(update, SimpleNamespace(args=[]))
        assert "not on for this account" in update.message.reply_text.await_args.args[0]
        update.effective_user.id = update.effective_chat.id = CHAT
        await bot.cmd_sweep(update, SimpleNamespace(args=["status"]))
        assert "Library sweep" in update.message.reply_text.await_args.args[0]
        with patch.object(bot.pipeline.sweep, "start", return_value=(SweepRun(id=3), "")) as start:
            await bot.cmd_sweep(update, SimpleNamespace(args=["force"]))
        start.assert_called_once_with("telegram", force=True)


# ---------------------------------------------------------------------------
# MCP, config, MusicBrainz
# ---------------------------------------------------------------------------


class TestMcp:
    async def test_off_is_a_tool_error(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit())
        pipe.config.library_sweep_users = set()
        pipe.sweep = sweep
        with pytest.raises(ToolError, match="off"):
            await McpTools(pipe).library_sweep_status()

    async def test_reviews_and_decide(self, tmp_path):
        sweep, pipe = _sweep(tmp_path, _audit(cutoff=21.5), fp=OTHER_FP)
        pipe.sweep = sweep
        tools = McpTools(pipe)
        started = await tools.library_sweep_run()
        assert started["started"] is True
        await sweep._task
        reviews = (await tools.library_sweep_reviews())["reviews"]
        assert [r["stem"] for r in reviews] == [STEM]
        assert reviews[0]["proposal"]["cutoff_khz"] == 21.5
        with pytest.raises(ToolError, match="No pair waits"):
            await tools.library_sweep_decide("Nobody - Nothing", "keep_mine")
        with pytest.raises(ToolError, match="decision must be"):
            await tools.library_sweep_decide(STEM, "maybe")
        out = await tools.library_sweep_decide(STEM, "take_new")
        assert out == {"stem": STEM, "decision": "take_new", "path": str(pipe.lib / f"{STEM}.flac")}
        status = await tools.library_sweep_status()
        assert status["reviews_waiting"] == 0 and status["run"]["counts"] == {"review": 1}


class TestConfig:
    ENV = {
        "TELEGRAM_BOT_TOKEN": "t",
        "SPOTIFY_CLIENT_ID": "i",
        "SPOTIFY_CLIENT_SECRET": "s",
        "SLSKD_HOST": "http://x",
        "SLSKD_API_KEY": "k",
    }

    def test_defaults_are_off(self):
        from music_downloader.config import Config

        with patch.dict(os.environ, self.ENV, clear=True):
            config = Config()
        assert config.library_sweep_users == set()
        assert config.library_sweep_schedule == ""
        assert (config.library_sweep_pause_secs, config.library_sweep_keep_days) == (20, 7)

    def test_values_and_a_bad_schedule(self):
        from music_downloader.config import Config

        env = {
            **self.ENV,
            "LIBRARY_SWEEP_USERS": "1, 2",
            "LIBRARY_SWEEP_SCHEDULE": "daily:03:30",
            "LIBRARY_SWEEP_PAUSE_SECS": "5",
            "LIBRARY_SWEEP_KEEP_DAYS": "0",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config()
        assert config.library_sweep_users == {1, 2}
        assert (config.library_sweep_pause_secs, config.library_sweep_keep_days) == (5, 0)
        with (
            patch.dict(os.environ, {**env, "LIBRARY_SWEEP_SCHEDULE": "fortnightly"}, clear=True),
            pytest.raises(ValueError),
        ):
            Config()


class TestMusicBrainzRetry:
    def _client(self, statuses):
        calls = []

        def handler(request):
            calls.append(request)
            status = statuses[min(len(calls) - 1, len(statuses) - 1)]
            return httpx.Response(status, json={"recordings": [{"id": "r1", "title": "Jam"}]})

        slept = []
        client = MusicBrainzClient(
            http=httpx.Client(transport=httpx.MockTransport(handler)), clock=lambda: 0.0, sleep=slept.append
        )
        return client, calls, slept

    def test_a_503_is_asked_once_more_after_two_seconds(self):
        client, calls, slept = self._client([503, 200])
        assert [t.title for t in client.search_recordings("Graham Central Station", "Jam")] == ["Jam"]
        assert len(calls) == 2 and 2.0 in slept

    def test_two_503_give_up(self):
        client, calls, slept = self._client([503, 503, 200])
        assert client.search_recordings("Graham Central Station", "Jam") == []
        assert len(calls) == 2


async def test_a_long_download_keeps_the_run_row_fresh(tmp_path, monkeypatch):
    sweep, pipe = _sweep(tmp_path, _audit(cutoff=15.5))
    beats = []
    real = sweep.repo.save_run
    monkeypatch.setattr(sweep.repo, "save_run", lambda run: (beats.append(time.time()), real(run)))

    async def fetch(results, index, track, progress_for):
        before = len(beats)
        sweep._beat_at = 0.0
        await progress_for(results[index], index)(None)
        assert len(beats) == before + 1, "a progress update writes the row"
        return await FakePipeline.fetch_for_library(pipe, results, index, track, progress_for)

    pipe.fetch_for_library = fetch
    await _run_once(sweep)
