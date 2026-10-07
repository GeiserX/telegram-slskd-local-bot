"""The lossless gate: a library delivery never keeps a lossless file made from a lossy one.

The fixtures are real FLACs made by ffmpeg and checked by the real spectrum
analyzer: pink noise kept lossless, and the same noise squeezed through a
low-bitrate MP3 first. Chat delivery is not gated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader.persistence.pending_repo import PendingDownload, PendingSearch
from music_downloader.pipeline import fetch as pipeline_fetch
from music_downloader.pipeline.fetch import (
    ALL_REJECTED,
    DOWNLOAD_FAILED,
    FetchOutcome,
    fetch_gated,
    lossy_source,
)
from music_downloader.processor.lossless_analyzer import LosslessVerdict, analyze_lossless
from music_downloader.search.slskd_client import DownloadStatus
from tests.test_chat_delivery import (
    CHAT,
    _edits,
    _make_bot,
    _make_config,
    _make_context,
    _make_result,
    _make_track,
    _status_msg,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

# Two independent pink noise channels: a spectrum that, like music, reaches Nyquist.
_NOISE = (
    "anoisesrc=d=4:c=pink:r={rate}:a=0.5:seed=1[a];anoisesrc=d=4:c=pink:r={rate}:a=0.5:seed=2[b];[a][b]amerge=inputs=2"
)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def flacs(tmp_path_factory):
    """name -> path: real16/real96 are genuine; mp3_64 is a 64 kbps MP3 as 16/44.1 FLAC,
    mp3_128_hires a 128 kbps MP3 upsampled to 24/96, mp3_128 a 128 kbps MP3 as 16/44.1 FLAC."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    d = tmp_path_factory.mktemp("gate")
    out = {name: str(d / f"{name}.flac") for name in ("real16", "real96", "mp3_64", "mp3_128", "mp3_128_hires")}
    _ffmpeg("-filter_complex", _NOISE.format(rate=44100), "-c:a", "flac", "-sample_fmt", "s16", out["real16"])
    _ffmpeg("-filter_complex", _NOISE.format(rate=96000), "-c:a", "flac", "-sample_fmt", "s32", out["real96"])
    for kbps in (64, 128):
        mp3 = str(d / f"{kbps}.mp3")
        _ffmpeg("-filter_complex", _NOISE.format(rate=44100), "-c:a", "libmp3lame", "-b:a", f"{kbps}k", mp3)
        _ffmpeg("-i", mp3, "-c:a", "flac", "-sample_fmt", "s16", out[f"mp3_{kbps}"])
    _ffmpeg(
        "-i", str(d / "128.mp3"), "-af", "aresample=96000", "-c:a", "flac", "-sample_fmt", "s32", out["mp3_128_hires"]
    )
    return out


def _verdict(cutoff_khz: float, sample_rate: int = 44100, bit_depth: int = 16) -> LosslessVerdict:
    return LosslessVerdict("FAKE", cutoff_khz, sample_rate / 2000, sample_rate, bit_depth)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestLossySource:
    @pytest.mark.parametrize(
        "cutoff, rate, depth, reason",
        [
            (14.0, 44100, 16, "transcoded from lossy, cutoff 14.0 kHz"),
            (15.9, 48000, 16, "transcoded from lossy, cutoff 15.9 kHz"),
            (16.0, 44100, 16, None),  # at the line: not clearly lossy
            (19.4, 96000, 24, "upsampled from lossy, cutoff 19.4 kHz"),
            (17.0, 192000, 24, "upsampled from lossy, cutoff 17.0 kHz"),
            (19.5, 96000, 24, None),
            (18.0, 44100, 16, None),  # a 256 kbps-like edge: the gate only takes the clear cases
            (14.0, 32000, 16, None),  # below 44.1 kHz there is nothing to judge by
        ],
    )
    def test_thresholds(self, cutoff, rate, depth, reason):
        assert lossy_source(_verdict(cutoff, rate, depth)) == reason

    def test_no_verdict_passes(self):
        assert lossy_source(None) is None


@needs_ffmpeg
class TestTheAnalyzerOnRealFiles:
    @pytest.mark.parametrize(
        "name, reason",
        [
            ("real16", None),
            ("real96", None),
            ("mp3_64", "transcoded from lossy, cutoff 14.0 kHz"),
            ("mp3_128_hires", "upsampled from lossy"),
            ("mp3_128", None),  # cutoff ~16.7 kHz: suspicious, not clearly lossy, kept
        ],
    )
    def test_verdicts(self, flacs, name, reason):
        got = lossy_source(analyze_lossless(flacs[name]))
        assert (got is None) if reason is None else (got is not None and got.startswith(reason))


# ---------------------------------------------------------------------------
# fetch_gated: the loop over the ranked list
# ---------------------------------------------------------------------------


def _fetcher(verdicts):
    """fetch_one returning *verdicts* in order (an Exception-free FetchOutcome or a FetchOutcome itself)."""
    calls = []

    async def fetch_one(result, index):
        calls.append(index)
        v = verdicts[len(calls) - 1]
        return v if isinstance(v, FetchOutcome) else FetchOutcome(path=f"/dl/{index}.flac", verdict=v)

    return fetch_one, calls


class TestFetchGated:
    async def test_a_lossy_sourced_copy_is_discarded_and_the_next_kept(self):
        fetch_one, calls = _fetcher([_verdict(14.0), _verdict(22.05)])
        discard, on_reject = AsyncMock(), AsyncMock()
        results = [_make_result(i) for i in range(3)]
        gated = await fetch_gated(fetch_one, discard, results, 0, 3, on_reject)
        assert gated.outcome.ok and gated.index == 1 and gated.result is results[1]
        assert calls == [0, 1] and gated.kept_lossy is None
        assert [(r.index, r.reason) for r in gated.rejected] == [(0, "transcoded from lossy, cutoff 14.0 kHz")]
        assert discard.await_args.args[1].path == "/dl/0.flac"
        on_reject.assert_awaited_once()
        assert on_reject.await_args.args[1:] == (results[1], 1)

    async def test_a_spent_list_ends_with_all_rejected(self):
        fetch_one, _ = _fetcher([_verdict(14.0), _verdict(15.0)])
        discard = AsyncMock()
        gated = await fetch_gated(fetch_one, discard, [_make_result(i) for i in range(2)], 0, 3)
        assert gated.outcome.error == ALL_REJECTED and len(gated.rejected) == 2
        assert discard.await_count == 2

    async def test_after_max_rejections_the_next_copy_is_kept_and_flagged(self):
        fetch_one, calls = _fetcher([_verdict(14.0)] * 5)
        discard = AsyncMock()
        gated = await fetch_gated(fetch_one, discard, [_make_result(i) for i in range(5)], 0, 3)
        assert calls == [0, 1, 2, 3] and gated.index == 3 and gated.outcome.ok
        assert gated.kept_lossy == "transcoded from lossy, cutoff 14.0 kHz"
        assert discard.await_count == 3

    async def test_a_failed_download_ends_the_run_with_its_outcome(self):
        failed = FetchOutcome(error=DOWNLOAD_FAILED, state="Timeout")
        fetch_one, _ = _fetcher([_verdict(14.0), failed])
        gated = await fetch_gated(fetch_one, AsyncMock(), [_make_result(i) for i in range(3)], 0, 3)
        assert gated.outcome is failed and gated.index == 1 and len(gated.rejected) == 1

    async def test_starts_at_the_chosen_copy(self):
        fetch_one, calls = _fetcher([_verdict(22.05)])
        gated = await fetch_gated(fetch_one, AsyncMock(), [_make_result(i) for i in range(3)], 2, 3)
        assert calls == [2] and gated.index == 2 and not gated.rejected


# ---------------------------------------------------------------------------
# The bot: a single track, end to end with real files and the real analyzer
# ---------------------------------------------------------------------------


def _gate_bot(tmp_path, flacs, names, **config):
    """A library bot whose search s1 lists one copy per *names* entry, each landing as that fixture."""
    cfg = _make_config(str(tmp_path))
    for key, value in config.items():
        setattr(cfg, key, value)
    bot = _make_bot(cfg)
    bot.pipeline.forget_transfer = MagicMock()
    results = [_make_result(i) for i in range(len(names))]
    for i, name in enumerate(names):
        os.makedirs(os.path.join(cfg.download_dir, f"user{i}"), exist_ok=True)
        shutil.copy(flacs[name], os.path.join(cfg.download_dir, f"user{i}", f"track{i}.flac"))
    bot.slskd = MagicMock()
    bot.slskd.enqueue_download.return_value = True
    bot.slskd.wait_for_download = AsyncMock(
        side_effect=lambda username, filename, **kw: DownloadStatus(username, filename, "Completed, Succeeded")
    )
    bot.pending[CHAT] = PendingSearch(query="q", track=_make_track(), results=results, search_id="s1")
    return bot, results


def _landed(bot, i):
    return os.path.exists(os.path.join(bot.config.download_dir, f"user{i}", f"track{i}.flac"))


def _buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@needs_ffmpeg
class TestBotSingleTrack:
    async def test_a_fake_copy_is_rejected_and_the_next_one_previewed(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "real16"])
        context, status = _make_context(), _status_msg()
        await bot._do_download(context, CHAT, _make_track(), results[0], status, 0, "s1", user_id=CHAT)

        assert not _landed(bot, 0) and _landed(bot, 1)
        caption = context.bot.send_audio.call_args.kwargs["caption"]
        assert "#2" in caption and "\U0001f6ab #1 rejected: transcoded from lossy, cutoff 14.0 kHz" in caption
        [entry] = [d for d in bot.downloads.values() if d.source_path]
        assert (entry.result, entry.result_index) == (results[1], 1)
        assert [r.status for r in bot.history_repo.get_recent(5)] == ["lossy_source"]
        # Nothing was kept lossy, so no wishlist button.
        assert "wish:better:s1" not in _buttons(status.edit_text.call_args.kwargs.get("reply_markup"))

    async def test_every_copy_rejected_keeps_nothing_and_offers_the_wishlist(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "mp3_128_hires"])
        context, status = _make_context(), _status_msg()
        await bot._do_download(context, CHAT, _make_track(), results[0], status, 0, "s1", user_id=CHAT)

        assert not _landed(bot, 0) and not _landed(bot, 1)
        context.bot.send_audio.assert_not_called()
        last = status.edit_text.call_args
        assert "No copy kept" in last.args[0] and "#2 rejected: upsampled from lossy" in last.args[0]
        assert _buttons(last.kwargs["reply_markup"]) == ["wish:better:s1"]

    async def test_after_the_limit_the_next_copy_is_kept_and_the_user_told(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "mp3_64"], lossless_gate_max_rejections=1)
        context, status = _make_context(), _status_msg()
        await bot._do_download(context, CHAT, _make_track(), results[0], status, 0, "s1", user_id=CHAT)

        assert not _landed(bot, 0) and _landed(bot, 1)
        caption = context.bot.send_audio.call_args.kwargs["caption"]
        assert "⚠️ Kept anyway after 1 rejected copies: transcoded from lossy, cutoff 14.0 kHz" in caption
        assert "wish:better:s1" in _buttons(status.edit_text.call_args.kwargs["reply_markup"])

    async def test_auto_mode_saves_the_genuine_copy(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "real16"], auto_mode=True)
        bot.pipeline.embed_artwork = AsyncMock()
        context, status = _make_context(), _status_msg()
        await bot._do_download(context, CHAT, _make_track(), results[0], status, 0, "s1", user_id=CHAT)

        assert os.listdir(bot.config.output_dir) == ["Nancy Sinatra - Bang Bang.flac"]
        last = _edits(status)[-1]
        assert "#2 Auto-saved" in last and "#1 rejected: transcoded from lossy" in last

    async def test_with_the_gate_off_the_fake_copy_is_kept(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "real16"], lossless_gate=False)
        context, status = _make_context(), _status_msg()
        await bot._do_download(context, CHAT, _make_track(), results[0], status, 0, "s1", user_id=CHAT)

        assert _landed(bot, 0)
        caption = context.bot.send_audio.call_args.kwargs["caption"]
        assert "#1" in caption and "rejected" not in caption and "Fake lossless" in caption

    async def test_chat_delivery_is_not_gated(self, tmp_path):
        cfg = _make_config(str(tmp_path), chat_users={CHAT})
        bot = _make_bot(cfg)
        bot.pipeline.fetch = AsyncMock(return_value=FetchOutcome(error=pipeline_fetch.ENQUEUE_FAILED))
        bot.pipeline.fetch_for_library = AsyncMock()
        await bot._do_download(
            _make_context(), CHAT, _make_track(), _make_result(), _status_msg(), 0, "s1", user_id=CHAT
        )
        bot.pipeline.fetch.assert_awaited_once()
        bot.pipeline.fetch_for_library.assert_not_awaited()


@needs_ffmpeg
class TestBotImport:
    async def test_a_library_import_moves_on_to_the_next_copy(self, tmp_path, flacs):
        bot, results = _gate_bot(tmp_path, flacs, ["mp3_64", "real16"])
        bot.import_repo = MagicMock()
        bot._import_pending[CHAT] = bot.pending.pop(CHAT)
        bot.downloads["dl1"] = PendingDownload(
            track=_make_track(), result=results[0], chat_id=CHAT, search_id="s1", job_id=1, track_id=2
        )
        context, status = _make_context(), _status_msg()
        with patch.object(bot, "_process_next_import_track", new_callable=AsyncMock):
            await bot._do_import_download(
                context, CHAT, _make_track(), results[0], status, generation=0, job_id=1, track_id=2, dl_id="dl1"
            )
        assert not _landed(bot, 0) and _landed(bot, 1)
        entry = bot.downloads["dl1"]
        assert (entry.result, entry.result_index) == (results[1], 1)
        assert "#1 rejected: transcoded from lossy" in context.bot.send_audio.call_args.kwargs["caption"]


# ---------------------------------------------------------------------------
# The MCP download tool
# ---------------------------------------------------------------------------


class TestMcpDownload:
    async def _tools(self, tmp_path, verdicts):
        from music_downloader.mcp.tools import McpTools
        from tests.test_mcp import FakePipeline

        pipeline = FakePipeline(tmp_path)
        seen = []

        async def fetch(result, progress_cb, analyze=True):
            seen.append(result.basename)
            verdict = verdicts[min(len(seen), len(verdicts)) - 1]
            return FetchOutcome(path=f"/downloads/{result.basename}", verdict=verdict, transfer_id="tx")

        pipeline.fetch = fetch
        tools = McpTools(pipeline)
        track_id = (await tools.resolve_track("Nancy Sinatra - Bang Bang"))["candidates"][0]["id"]
        copies = (await tools.search_copies(track_id))["copies"]
        return tools, pipeline, copies, seen

    async def test_rejected_copies_are_listed_and_the_next_one_saved(self, tmp_path):
        tools, pipeline, copies, seen = await self._tools(tmp_path, [_verdict(14.0), _verdict(22.05)])
        done = await tools.download(copies[0]["id"])
        assert done["ok"] is True and len(seen) == 2
        assert done["copy"]["filename"] == seen[1]
        assert done["rejected"] == [
            {"filename": seen[0], "source": copies[0]["source"], "reason": "transcoded from lossy, cutoff 14.0 kHz"}
        ]
        assert pipeline.discarded == [f"/downloads/{seen[0]}"]
        assert "kept_lossy" not in done

    async def test_a_genuine_copy_adds_nothing_to_the_result(self, tmp_path):
        tools, _, copies, _ = await self._tools(tmp_path, [_verdict(22.05)])
        done = await tools.download(copies[0]["id"])
        assert done["ok"] is True and "rejected" not in done and "copy" not in done

    async def test_every_copy_rejected_is_an_error_with_a_wishlist_hint(self, tmp_path):
        tools, _, copies, seen = await self._tools(tmp_path, [_verdict(14.0)])
        done = await tools.download(copies[0]["id"])
        assert done["ok"] is False and done["error"] == ALL_REJECTED
        assert len(done["rejected"]) == len(seen) and "wishlist_add" in done["wishlist_hint"]

    async def test_path_delivery_is_not_gated(self, tmp_path):
        tools, pipeline, copies, seen = await self._tools(tmp_path, [_verdict(14.0)])
        done = await tools.download(copies[0]["id"], deliver="path")
        assert done["ok"] is True and len(seen) == 1 and pipeline.discarded == []
