"""The library sweep: look for a better copy of every library song, replace it when sure, ask the ear when not.

Off unless LIBRARY_SWEEP_USERS names someone. One sweep at a time, one song at
a time, one download at a time, LIBRARY_SWEEP_PAUSE_SECS between songs that
searched Soulseek. Each song is audited (sweep_rules.audit, cached by size and
mtime), and checked when due: fake, lossy, uncertain and damaged songs every
sweep, CD-quality songs once a month, hi-res songs never unless forced. A due
song is resolved (Spotify, MusicBrainz), searched, and up to three copies that
could beat it are downloaded through the lossless gate and measured. A better
copy of the same recording replaces the song: the original's missing tags and
cover are carried over, the original is parked as
<OUTPUT_DIR>/.sweep-replaced/<name>.sweep for LIBRARY_SWEEP_KEEP_DAYS. A
better copy that may be another recording is staged as
<OUTPUT_DIR>/.sweep-review/<name>.sweep for the owner to judge (Keep mine,
Take new, Keep both). The parked and staged files end in ".sweep" so no
player or transcoder watching the library takes them for songs.

The sweep touches only files it downloaded, the one file it replaces (parked
first) and files it staged or parked itself. Every state is in SQLite
(persistence/sweep_repo.py), so a restart continues the sweep where it stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import os
import shutil
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from music_downloader.formats import AUDIO_EXTENSIONS
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.sweep_repo import (
    DECISION_KEEP_BOTH,
    DECISION_KEEP_MINE,
    DECISION_TAKE_NEW,
    DECISIONS,
    RUN_DONE,
    RUN_INTERRUPTED,
    Replacement,
    Review,
    SweepRepository,
    SweepRun,
    SweepSong,
)
from music_downloader.pipeline import sweep_rules as rules
from music_downloader.pipeline.search import clean_search_title
from music_downloader.pipeline.sweep_rules import Audit

logger = logging.getLogger(__name__)

# How a song's check ended (sweep_songs.outcome and the report's counts).
OUTCOME_UPGRADED = "upgraded"
OUTCOME_REVIEW = "review"
OUTCOME_NONE_QUALIFIED = "none_qualified"
OUTCOME_NO_BETTER_COPY = "no_better_copy"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_HIRES = "hires"
OUTCOME_WAITING = "review_waiting"
OUTCOME_SKIPPED = "skipped"
OUTCOME_ERROR = "error"
OUTCOME_LABELS = {
    OUTCOME_UPGRADED: "Upgraded",
    OUTCOME_REVIEW: "For your ear",
    OUTCOME_NONE_QUALIFIED: "Copies tried, none qualified",
    OUTCOME_NO_BETTER_COPY: "No better copy on Soulseek",
    OUTCOME_NO_MATCH: "No confident match",
    OUTCOME_HIRES: "Already hi-res",
    OUTCOME_WAITING: "Waiting for your ear",
    OUTCOME_SKIPPED: "Skipped (not named Artist - Title)",
    OUTCOME_ERROR: "Errors",
}

REPLACED_DIR = ".sweep-replaced"
REVIEW_DIR = ".sweep-review"
# Parked and staged files end in this, so nothing watching the library takes them for songs.
PARKED_SUFFIX = ".sweep"
# A CD-quality song is searched again after this long (hi-res: never; the rest: every sweep).
CD_RECHECK_SECS = 30 * 86400
# Copies downloaded per song at most: hi-res copies are big, so a CD song gets fewer.
CD_TRIES = 2
OTHER_TRIES = 3
# A running sweep row not written for this long belongs to a process that stopped.
STALE_RUN_SECS = 15 * 60
# While a song downloads, the run row is written at least this often (STALE_RUN_SECS reads it).
HEARTBEAT_SECS = 60
# The schedule loop wakes at least this often (purging parked originals).
LOOP_TICK_SECS = 3600


@dataclass
class SongResult:
    outcome: str
    detail: str = ""
    searched: bool = False


@dataclass
class SweepReport:
    """What the owner is told: the run, its automatic replacements, every pair still waiting for the ear.

    *in_progress*: the schedule came round while a sweep was still running.
    """

    run: SweepRun
    replacements: list[Replacement] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    in_progress: bool = False


ReportHook = Callable[[SweepReport], Awaitable[None]]


class SweepError(Exception):
    """A decision that cannot be applied (unknown song, already decided, file gone)."""


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def library_files(root: str) -> list[str]:
    """Audio files at the top level of *root* (hidden ones left out), by name."""
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    names = []
    for name in entries:
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if not name.startswith(".") and ext in AUDIO_EXTENSIONS and os.path.isfile(os.path.join(root, name)):
            names.append(name)
    return sorted(names)


def is_due(song: SweepSong, run: SweepRun, now: float) -> bool:
    """Whether *song* is checked in *run*: not yet in this run, and its tier's recheck time has come."""
    if song.last_checked_at is not None and song.last_checked_at >= run.started_at:
        return False
    if run.force or song.last_checked_at is None:
        return True
    if song.tier == rules.TIER_HIRES:
        return False
    if song.tier == rules.TIER_CD:
        return now - song.last_checked_at >= CD_RECHECK_SECS
    return True


def free_path(path: str) -> str:
    """*path*, or "<base>.<n><suffix>" with the first n that is not taken (suffix = PARKED_SUFFIX or extension)."""
    if not os.path.exists(path):
        return path
    base, suffix = (
        (path[: -len(PARKED_SUFFIX)], PARKED_SUFFIX) if path.endswith(PARKED_SUFFIX) else os.path.splitext(path)
    )
    n = 2
    while os.path.exists(f"{base}.{n}{suffix}"):
        n += 1
    return f"{base}.{n}{suffix}"


def match_owner(path: str, like: str | None) -> None:
    """Give *path* the mode, owner and group of the file *like* (owner and group only when allowed)."""
    if not like or not os.path.isfile(like):
        return
    try:
        st = os.stat(like)
    except OSError:
        return
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IMODE(st.st_mode))
    with contextlib.suppress(OSError):
        os.chown(path, st.st_uid, st.st_gid)


def place(source: str, target: str, like: str | None, move: bool = False) -> str:
    """Put *source* at *target* atomically: copy (or move) to "<target>.importing", match *like*, rename.

    *move* is for a source on the library's own file system (a staged
    proposal); a download is copied. Raises FileExistsError when *target* is taken.
    """
    if os.path.exists(target):
        raise FileExistsError(target)
    tmp = target + ".importing"
    try:
        if move:
            os.replace(source, tmp)
        else:
            shutil.copyfile(source, tmp)
        match_owner(tmp, like)
        os.replace(tmp, target)
    except BaseException:
        if move and os.path.exists(tmp) and not os.path.exists(source):
            with contextlib.suppress(OSError):
                os.replace(tmp, source)
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    return target


def replace_in_library(original: str, source: str, ext: str, replaced_dir: str, move: bool = False) -> tuple[str, str]:
    """Replace the library file *original* with *source* (format *ext*); returns (new path, parked original).

    The new file is "<original's stem>.<ext>" next to it, written atomically
    with the original's mode and owner. The original is parked as
    <replaced_dir>/<its name>.sweep (a free name). When the new file cannot be
    put in place, the original goes back where it was. A *target* of another
    format that already exists is never overwritten (FileExistsError).
    """
    folder = os.path.dirname(original)
    stem = os.path.splitext(os.path.basename(original))[0]
    target = os.path.join(folder, f"{stem}.{ext}")
    if os.path.normpath(target) != os.path.normpath(original) and os.path.exists(target):
        raise FileExistsError(target)
    tmp = target + ".importing"
    try:
        if move:
            os.replace(source, tmp)
        else:
            shutil.copyfile(source, tmp)
        match_owner(tmp, original)
        os.makedirs(replaced_dir, exist_ok=True)
        parked = free_path(os.path.join(replaced_dir, os.path.basename(original) + PARKED_SUFFIX))
        os.replace(original, parked)
        try:
            os.replace(tmp, target)
        except BaseException:
            os.replace(parked, original)
            raise
    except BaseException:
        if move and os.path.exists(tmp) and not os.path.exists(source):
            with contextlib.suppress(OSError):
                os.replace(tmp, source)
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    return target, parked


_CARRIED_TAGS = ("artist", "title", "album", "date", "tracknumber", "genre", "albumartist")


def carry_tags_and_cover(original: str, candidate: str) -> None:
    """Copy into *candidate* the original's tags it lacks, and the original's cover when it has none. Best effort."""
    import mutagen

    from music_downloader.tools.embed_artwork import embed_artwork_into_file

    try:
        o = mutagen.File(original, easy=True)
        c = mutagen.File(candidate, easy=True)
        if o is not None and c is not None and o.tags is not None:
            if c.tags is None:
                c.add_tags()
            changed = False
            for key in _CARRIED_TAGS:
                value = o.tags.get(key)
                if value and not c.tags.get(key):
                    c.tags[key] = value
                    changed = True
            if changed:
                c.save()
    except Exception:
        logger.warning("Sweep: tags of %s not carried over", original, exc_info=True)
    try:
        cover = _cover_of(original)
        if cover:
            embed_artwork_into_file(candidate, cover)
    except Exception:
        logger.warning("Sweep: cover of %s not carried over", original, exc_info=True)


def _cover_of(path: str) -> bytes | None:
    import base64

    import mutagen
    import mutagen.flac

    f = mutagen.File(path)
    if f is None:
        return None
    if getattr(f, "pictures", None):
        return f.pictures[0].data
    tags = f.tags
    if tags is None:
        return None
    for key in list(tags.keys()):
        if key.startswith("APIC"):
            return tags[key].data
    if "covr" in tags and tags["covr"]:
        return bytes(tags["covr"][0])
    blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None
    if blocks:
        return mutagen.flac.Picture(base64.b64decode(blocks[0])).data
    return None


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def _no_progress(_status) -> None:
    return None


class LibrarySweep:
    """The sweep of one library (OUTPUT_DIR), over a Pipeline. Front ends set *on_report*."""

    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.config = pipeline.config
        self.repo = SweepRepository(pipeline.db)
        self.on_report: ReportHook | None = None
        # Replaceable in tests: the measuring and the fingerprint are subprocesses.
        self.audit: Callable[[str], Audit] = rules.audit
        self.fingerprint: Callable[[str], list[int] | None] = rules.fingerprint
        self.sleep = asyncio.sleep
        self._task: asyncio.Task | None = None
        self._run: SweepRun | None = None
        self._decide_lock = asyncio.Lock()
        self._slot_reported: float | None = None

    # ------------------------------------------------------------ properties

    @property
    def users(self) -> set[int]:
        return set(self.config.library_sweep_users or ())

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    @property
    def root(self) -> str:
        return self.config.output_dir

    @property
    def replaced_dir(self) -> str:
        return os.path.join(self.root, REPLACED_DIR)

    @property
    def review_dir(self) -> str:
        return os.path.join(self.root, REVIEW_DIR)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_run(self) -> SweepRun | None:
        return self._run if self.running else None

    # ------------------------------------------------------------- start/stop

    def _live_run_elsewhere(self, now: float) -> SweepRun | None:
        """A sweep another process is running right now (its row written in the last STALE_RUN_SECS)."""
        for run in self.repo.running_runs():
            if self._run is not None and run.id == self._run.id:
                continue
            if now - run.updated_at < STALE_RUN_SECS:
                return run
        return None

    def start(self, trigger: str = "manual", force: bool = False) -> tuple[SweepRun | None, str]:
        """Start a sweep in the background; (the run, "") or (None, why not). Needs a running event loop."""
        if not self.enabled:
            return None, "the library sweep is off: LIBRARY_SWEEP_USERS is empty"
        missing = rules.missing_tools()
        if missing:
            return None, f"the sweep cannot measure files without {' and '.join(missing)}"
        if self.running:
            return None, "a sweep is already running"
        other = self._live_run_elsewhere(time.time())
        if other is not None:
            return None, f"another process is running sweep {other.id}"
        for stale in self.repo.running_runs():
            stale.status = RUN_INTERRUPTED
            self.repo.save_run(stale)
        run = self.repo.add_run(SweepRun(trigger=trigger, force=force, owner=self.pipeline.album_owner))
        self._launch(run)
        return run, ""

    def _launch(self, run: SweepRun) -> None:
        self._run = run
        self._task = asyncio.get_running_loop().create_task(self._sweep(run), name=f"library-sweep-{run.id}")

    def resume(self) -> SweepRun | None:
        """At startup: continue this owner's sweep that a stop interrupted (or any stale one). Returns it."""
        if not self.enabled or self.running:
            return None
        missing = rules.missing_tools()
        if missing:
            logger.error("Library sweep not continued: %s missing", " and ".join(missing))
            return None
        now = time.time()
        for run in self.repo.running_runs():
            if run.owner == self.pipeline.album_owner or now - run.updated_at >= STALE_RUN_SECS:
                logger.info("Library sweep %d continues where it stopped", run.id)
                self._launch(run)
                return run
        return None

    async def stop(self) -> None:
        """Cancel the running sweep (shutdown): its row stays running, the next start continues it."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ a run

    async def _sweep(self, run: SweepRun) -> None:
        keepalive = asyncio.get_running_loop().create_task(self._keepalive(run))
        try:
            if not os.path.isdir(self.root):
                logger.error("Library sweep %d: %s is not a folder (unmounted?); nothing checked", run.id, self.root)
                run.counts[OUTCOME_ERROR] = run.counts.get(OUTCOME_ERROR, 0) + 1
                await self._finish(run)
                return
            names = await asyncio.to_thread(library_files, self.root)
            if names:
                await asyncio.to_thread(self.repo.forget_missing, set(names))
            order = self._order(names)
            run.total, run.done = len(order), 0
            self.repo.save_run(run)
            logger.info(
                "Library sweep %d (%s%s): %d files", run.id, run.trigger, ", forced" if run.force else "", run.total
            )
            pause = False
            for name in order:
                result = await self._check_logged(name, run, pause)
                run.done += 1
                if result is None:
                    continue
                pause = result.searched
                run.counts[result.outcome] = run.counts.get(result.outcome, 0) + 1
                run.current = ""
                self.repo.save_run(run)
            await asyncio.to_thread(self.purge_parked)
            await self._finish(run)
        except asyncio.CancelledError:
            logger.info(
                "Library sweep %d stopped at %d of %d; the next start continues it", run.id, run.done, run.total
            )
            raise
        except Exception:
            logger.exception("Library sweep %d failed", run.id)
            run.status = RUN_INTERRUPTED
            self.repo.save_run(run)
        finally:
            keepalive.cancel()

    def _order(self, names: list[str]) -> list[str]:
        """Worst tier first (files never audited first of all), then by name."""
        tiers = {s.file: s.tier for s in self.repo.songs()}

        def key(name: str):
            tier = tiers.get(name)
            return (rules.TIERS.index(tier) + 1 if tier in rules.TIERS else 0, name)

        return sorted(names, key=key)

    async def _finish(self, run: SweepRun) -> None:
        run.status, run.finished_at, run.current = RUN_DONE, time.time(), ""
        self.repo.save_run(run)
        summary = ", ".join(f"{OUTCOME_LABELS.get(k, k).lower()} {v}" for k, v in sorted(run.counts.items()))
        logger.info("Library sweep %d done: %s", run.id, summary or "nothing was due")
        await self._notify(self.report(run))

    def report(self, run: SweepRun, in_progress: bool = False) -> SweepReport:
        return SweepReport(
            run=run,
            replacements=self.repo.replacements_for_run(run.id) if run.id is not None else [],
            reviews=[] if in_progress else self.repo.pending_reviews(),
            in_progress=in_progress,
        )

    async def _notify(self, report: SweepReport) -> None:
        if self.on_report is None:
            return
        try:
            await self.on_report(report)
        except Exception:
            logger.exception("Sending the library sweep report failed")

    async def _keepalive(self, run: SweepRun) -> None:
        """Write the run row every HEARTBEAT_SECS for the sweep's whole life, so no other process takes it
        for dead while a song downloads, decodes or is fingerprinted."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECS)
            try:
                self.repo.save_run(run)
            except Exception:
                logger.warning("Sweep %s: could not write its row", run.id, exc_info=True)

    async def _check_logged(self, name: str, run: SweepRun, pause: bool) -> SongResult | None:
        """check_song with its log line and its row written; None when the song was not due."""
        path = os.path.join(self.root, name)
        try:
            due = await self._audited_and_due(name, run)
        except Exception:
            logger.exception("Sweep: could not audit %s", name)
            due = None
        if due is None:
            return None
        song, original = due
        if pause:
            await self.sleep(self.config.library_sweep_pause_secs)
        run.current = name
        self.repo.save_run(run)
        try:
            result = await self.check_song(path, song, original, run)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Sweep: checking %s failed", name, exc_info=True)
            result = SongResult(OUTCOME_ERROR, f"{type(exc).__name__}: {exc}"[:300], searched=True)
        self.repo.mark_checked(name, time.time(), result.outcome, result.detail)
        logger.info(
            "Sweep %d/%d %s (%s): %s%s",
            run.done + 1,
            run.total,
            name,
            original.tier,
            OUTCOME_LABELS.get(result.outcome, result.outcome),
            f": {result.detail}" if result.detail else "",
        )
        return result

    async def _audited_and_due(self, name: str, run: SweepRun) -> tuple[SweepSong, Audit] | None:
        """The song's row (audited again when the file changed) and audit, when it is due in *run*."""
        path = os.path.join(self.root, name)
        try:
            st = os.stat(path)
        except OSError:
            return None
        song = self.repo.get_song(name)
        if song is None or song.size != st.st_size or song.mtime != st.st_mtime:
            measured = await asyncio.to_thread(self.audit, path)
            song = SweepSong(
                file=name,
                stem=os.path.splitext(name)[0],
                size=st.st_size,
                mtime=st.st_mtime,
                tier=measured.tier,
                audit=measured.to_dict(),
                declined_at=song.declined_at if song is not None else None,
            )
            self.repo.save_audit(song)
        if not is_due(song, run, time.time()):
            return None
        return song, Audit.from_dict(song.audit)

    async def check_song(self, path: str, song: SweepSong, original: Audit, run: SweepRun) -> SongResult:
        """Look for a better copy of one library song and act on what turns up."""
        stem = song.stem
        if self.repo.pending_review_for(stem) is not None:
            return SongResult(OUTCOME_WAITING, "a pair for this song waits for your ear")
        if original.tier == rules.TIER_HIRES and not run.force:
            return SongResult(OUTCOME_HIRES)
        artist, title = rules.split_stem(stem)
        if not artist:
            return SongResult(OUTCOME_SKIPPED, "name is not Artist - Title")

        candidates = await self.pipeline.resolve_match("", artist, title, original.length or None)
        track: TrackInfo | None = next((c.track for c in candidates if c.confident), None)
        if track is None:
            return SongResult(OUTCOME_NO_MATCH, searched=True)
        clean = clean_search_title(track.title)
        ranked = await self.pipeline.search(f"{track.artist} {clean}", track, "library")
        if not ranked:
            ranked = await self.pipeline.search(clean, track, "library")
        copies = [r for r in ranked if rules.wanted(original.tier, r, original.length)]
        if not copies:
            return SongResult(OUTCOME_NO_BETTER_COPY, searched=True)

        notes: list[str] = []
        tries = CD_TRIES if original.tier == rules.TIER_CD else OTHER_TRIES
        index = 0
        while index < len(copies) and tries > 0:
            tries -= 1

            gated = await self.pipeline.fetch_for_library(copies, index, track, lambda _r, _i: _no_progress)
            index = gated.index + 1
            outcome, result = gated.outcome, gated.result
            notes.extend(f"{r.result.basename}: {r.reason}" for r in gated.rejected)
            if not outcome.ok or not outcome.path:
                notes.append(f"{result.basename}: {outcome.error}")
                continue
            kept, detail = await self._consider(
                path, stem, original, outcome.path, result, outcome.transfer_id, track, run
            )
            if kept is not None:
                return SongResult(kept, detail, searched=True)
            notes.append(f"{result.basename}: {detail}")
        return SongResult(OUTCOME_NONE_QUALIFIED, "; ".join(notes)[:500], searched=True)

    async def _consider(
        self, path, stem, original: Audit, cand_path, result, transfer_id, track, run
    ) -> tuple[str | None, str]:
        """Judge one downloaded copy; replace, stage, or discard it.

        Returns (OUTCOME_UPGRADED or OUTCOME_REVIEW, detail), or (None, why it was discarded).
        The download is deleted from DOWNLOAD_DIR whatever happens.
        """
        try:
            cand = await asyncio.to_thread(self.audit, cand_path)
            if not rules.better(original, cand):
                return None, f"not better ({rules.describe(cand)})"
            similarity = await self._similarity(path, cand_path)
            kind, why = rules.judge(original, cand, stem, similarity)
            if kind == rules.SAME:
                await self._replace(run, stem, path, original, cand_path, cand, track, result, transfer_id)
                return OUTCOME_UPGRADED, f"{rules.describe(original)} -> {rules.describe(cand)} ({why})"
            if kind == rules.REVIEW:
                if self.repo.is_declined(stem):
                    return None, f"you kept your file over another proposal before ({why})"
                await self._stage(run, stem, path, original, cand_path, cand, why, similarity)
                return OUTCOME_REVIEW, why
            return None, f"rejected: {why}"
        finally:
            if os.path.exists(cand_path):
                await self.pipeline.discard(cand_path, result.username, transfer_id)
            else:
                self.pipeline.forget_transfer(result.username, transfer_id)

    async def _similarity(self, original: str, candidate: str) -> float | None:
        a = await asyncio.to_thread(self.fingerprint, original)
        b = await asyncio.to_thread(self.fingerprint, candidate) if a else None
        if not a or not b:
            return None
        return await asyncio.to_thread(rules.similarity, a, b)

    async def _replace(self, run, stem, path, original: Audit, cand_path, cand: Audit, track, result, transfer_id):
        await asyncio.to_thread(carry_tags_and_cover, path, cand_path)
        target, parked = await asyncio.to_thread(replace_in_library, path, cand_path, cand.ext, self.replaced_dir)
        self._index_moved(path, target)
        self.repo.add_replacement(
            Replacement(
                stem=stem,
                new_path=target,
                parked_path=parked,
                from_desc=rules.describe(original),
                to_desc=rules.describe(cand),
                how="auto",
                run_id=run.id,
            )
        )
        with contextlib.suppress(Exception):
            await self.pipeline.record_history(
                track, result, "success", filename=os.path.basename(target), note="sweep"
            )
        if self.config.library_sweep_keep_days == 0:
            await asyncio.to_thread(self.purge_parked)

    async def _stage(self, run, stem, path, original: Audit, cand_path, cand: Audit, why, similarity):
        await asyncio.to_thread(carry_tags_and_cover, path, cand_path)

        def stage() -> str:
            os.makedirs(self.review_dir, exist_ok=True)
            staged = free_path(os.path.join(self.review_dir, f"{stem}.{cand.ext}{PARKED_SUFFIX}"))
            shutil.copyfile(cand_path, staged)
            match_owner(staged, path)
            return staged

        staged = await asyncio.to_thread(stage)
        both = rules.keep_both_name(
            stem, cand.ext, original, cand, lambda n: os.path.exists(os.path.join(self.root, n))
        )
        self.repo.add_review(
            Review(
                stem=stem,
                current_path=path,
                proposal_path=staged,
                proposal_ext=cand.ext,
                both_name=both,
                reason=why,
                current_desc=rules.describe(original),
                proposal_desc=rules.describe(cand),
                current_len=original.length,
                proposal_len=cand.length,
                current_cutoff=original.cutoff,
                proposal_cutoff=cand.cutoff,
                similarity=similarity,
                run_id=run.id,
            )
        )

    def _index_moved(self, old: str, new: str) -> None:
        index = self.pipeline.library_index
        try:
            if os.path.normpath(old) != os.path.normpath(new):
                index.remove(old)
            index.add(new)
        except Exception:
            logger.warning("Sweep: library index not updated for %s", new, exc_info=True)

    # -------------------------------------------------------------- decisions

    async def decide(self, review_id: int, decision: str) -> tuple[Review, str]:
        """Apply the owner's decision on a pair; returns the review and the library path it left.

        keep_mine deletes the proposal; take_new replaces the song with it (the
        song parked like an automatic replacement); keep_both adds it as a song
        of its own under the review's both_name. Either keep marks the song
        declined: no other recording is proposed for it again. Raises
        SweepError when the pair is unknown or already decided.
        """
        if decision not in DECISIONS:
            raise SweepError(f"decision must be one of {', '.join(DECISIONS)}, not {decision!r}")
        async with self._decide_lock:
            review = self.repo.get_review(review_id)
            if review is None:
                raise SweepError(f"no pair {review_id}")
            if review.decision is not None:
                raise SweepError(f"{review.stem}: already decided ({review.decision})")
            if decision != DECISION_KEEP_MINE and not os.path.isfile(review.proposal_path):
                raise SweepError(f"{review.stem}: the proposal file is gone")
            now = time.time()
            self.repo.decide(review.id, decision, now)
            try:
                path = await asyncio.to_thread(self._apply, review, decision)
            except BaseException:
                self.repo.undecide(review.id)
                raise
            review.decision, review.decided_at = decision, now
            if decision in (DECISION_KEEP_MINE, DECISION_KEEP_BOTH):
                self.repo.mark_declined(review.stem, now)
            logger.info("Sweep pair %s (%s): %s -> %s", review.id, review.stem, decision, path)
            return review, path

    def find_pending(self, stem: str) -> Review | None:
        return self.repo.pending_review_for(stem)

    def _apply(self, review: Review, decision: str) -> str:
        if decision == DECISION_KEEP_MINE:
            with contextlib.suppress(FileNotFoundError):
                os.remove(review.proposal_path)
            return review.current_path
        if decision == DECISION_TAKE_NEW:
            if os.path.isfile(review.current_path):
                target, parked = replace_in_library(
                    review.current_path, review.proposal_path, review.proposal_ext, self.replaced_dir, move=True
                )
                self.repo.add_replacement(
                    Replacement(
                        stem=review.stem,
                        new_path=target,
                        parked_path=parked,
                        from_desc=review.current_desc,
                        to_desc=review.proposal_desc,
                        how=DECISION_TAKE_NEW,
                        run_id=review.run_id,
                    )
                )
            else:
                target = os.path.join(self.root, f"{review.stem}.{review.proposal_ext}")
                target = place(review.proposal_path, free_path(target), None, move=True)
            self._index_moved(review.current_path, target)
            if self.config.library_sweep_keep_days == 0:
                self.purge_parked()
            return target
        # DECISION_KEEP_BOTH
        target = free_path(os.path.join(self.root, review.both_name))
        place(review.proposal_path, target, review.current_path, move=True)
        self._index_moved(target, target)
        return target

    # ----------------------------------------------------------------- purge

    def purge_parked(self, now: float | None = None) -> int:
        """Delete parked originals older than LIBRARY_SWEEP_KEEP_DAYS (only files this sweep parked). Blocking."""
        now = time.time() if now is None else now
        cutoff = now - self.config.library_sweep_keep_days * 86400
        purged = 0
        root = os.path.realpath(self.replaced_dir)
        for rep in self.repo.unpurged_before(cutoff):
            real = os.path.realpath(rep.parked_path)
            if not real.startswith(root + os.sep) or not real.endswith(PARKED_SUFFIX):
                logger.warning("Sweep: not purging %s, it is not a parked file", rep.parked_path)
                self.repo.mark_purged(rep.id, now)
                continue
            try:
                os.remove(real)
                purged += 1
                logger.info("Sweep: purged the parked original %s", rep.parked_path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Sweep: could not purge %s", rep.parked_path, exc_info=True)
                continue
            self.repo.mark_purged(rep.id, now)
        return purged

    # -------------------------------------------------------------- schedule

    async def schedule_loop(self, now: Callable[[], datetime.datetime] | None = None) -> None:
        """Continue an interrupted sweep, then start one at every LIBRARY_SWEEP_SCHEDULE slot; purge hourly.

        A slot missed while the bot was down is caught up at start. When a
        slot comes while a sweep still runs, the owner gets a progress report
        instead. The first slot after the feature is turned on starts the first
        sweep (or /sweep, at once).
        """
        now = now or (lambda: datetime.datetime.now().astimezone())
        schedule = rules.parse_schedule(self.config.library_sweep_schedule)
        loop_started = now().timestamp()
        self.resume()
        while True:
            try:
                await asyncio.to_thread(self.purge_parked)
                if schedule is not None:
                    await self._maybe_start(schedule, now(), loop_started)
            except Exception:
                logger.exception("Library sweep schedule tick failed")
            current = now()
            wait = float(LOOP_TICK_SECS)
            if schedule is not None:
                wait = min(wait, max(1.0, (rules.next_run(schedule, current) - current).total_seconds()))
            await self.sleep(wait)

    async def _maybe_start(self, schedule: rules.Schedule, current: datetime.datetime, loop_started: float) -> None:
        slot = rules.previous_run(schedule, current).timestamp()
        last = self.repo.last_started_at()
        due = (last is not None and last < slot) or (last is None and slot >= loop_started)
        if not due:
            return
        if self.running:
            if self._slot_reported != slot and self._run is not None:
                self._slot_reported = slot
                await self._notify(self.report(self._run, in_progress=True))
            return
        run, why = self.start("schedule")
        if run is None:
            logger.info("Scheduled library sweep not started: %s", why)

    def status(self) -> dict:
        """The sweep's state for /sweep status and the MCP: the current or last run, tiers, pairs waiting."""
        run = self.current_run or self.repo.latest_run()
        try:
            schedule = rules.parse_schedule(self.config.library_sweep_schedule)
        except ValueError:
            schedule = None
        next_at = rules.next_run(schedule, datetime.datetime.now().astimezone()) if schedule else None
        return {
            "enabled": self.enabled,
            "running": self.running,
            "schedule": str(schedule) if schedule else "off",
            "next_scheduled": next_at.isoformat(timespec="minutes") if next_at else None,
            "run": None
            if run is None
            else {
                "id": run.id,
                "trigger": run.trigger,
                "forced": run.force,
                "status": run.status,
                "position": run.done,
                "files": run.total,
                "current": run.current or None,
                "counts": dict(run.counts),
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
            },
            "tiers": self.repo.tier_counts(),
            "reviews_waiting": len(self.repo.pending_reviews()),
            "pause_secs": self.config.library_sweep_pause_secs,
            "keep_days": self.config.library_sweep_keep_days,
            "fingerprint": rules.fpcalc_available(),
            "missing_tools": rules.missing_tools(),
        }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
