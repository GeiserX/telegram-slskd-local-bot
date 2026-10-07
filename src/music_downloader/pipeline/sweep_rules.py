"""What the library sweep decides about one song, as plain functions.

audit() measures a file: format, length, bit depth, rate, a strict decode and
the spectral cutoff, and turns them into a tier. wanted() says whether a
Soulseek copy could be an upgrade before it is downloaded; better() whether a
downloaded copy really is one; judge() whether it is the same recording (a
Chromaprint fingerprint when fpcalc is there, the length and the tags
otherwise). keep_both_name() names a proposal kept next to the original, and
parse_schedule() reads LIBRARY_SWEEP_SCHEDULE.

The thresholds were tuned on a real library; the reasons
sit next to each constant.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from music_downloader.formats import LOSSY_EXTENSIONS
from music_downloader.processor.lossless_analyzer import CHECKABLE_EXTENSIONS, analyze_lossless
from music_downloader.search.slskd_client import SearchResult

logger = logging.getLogger(__name__)

# Tiers, worst first: the order the sweep works through the library in.
TIER_DAMAGED = "damaged"  # lossless, but the strict decode found errors
TIER_FAKE = "fake"  # lossless file whose spectrum stops below FAKE_CUTOFF_KHZ: made from a lossy one
TIER_LOSSY = "lossy"  # MP3, AAC, Ogg...
TIER_UNCERTAIN = "uncertain"  # cutoff between FAKE_CUTOFF_KHZ and CD_CUTOFF_KHZ: a good MP3 or an old master
TIER_CD = "cd"  # genuine CD quality
TIER_HIRES = "hires"  # 24-bit above 48 kHz with real content above HIRES_CUTOFF_KHZ
TIERS = (TIER_DAMAGED, TIER_FAKE, TIER_LOSSY, TIER_UNCERTAIN, TIER_CD, TIER_HIRES)

# The tier comes from the measured cutoff, not from the analyzer's verdict: the
# verdict compares the cutoff with the file's Nyquist, so a genuine 24/96 master
# with nothing above 24 kHz reads as suspect, while the cutoff alone separates a
# lossless master from an MP3 inside a FLAC.
CD_CUTOFF_KHZ = 19.5
FAKE_CUTOFF_KHZ = 16.0
HIRES_CUTOFF_KHZ = 24.0
# A copy of a fake, uncertain or lossy song must reach this much further than the original.
MIN_GAIN_KHZ = 1.0
# A copy whose listed length is further than this from the original's is not downloaded.
WANTED_MAX_DIFF_SECS = 3
# Same recording by length: within SAME_MAX_DIFF_SECS; up to REVIEW_MAX_DIFF_SECS the ear decides.
SAME_MAX_DIFF_SECS = 1.5
REVIEW_MAX_DIFF_SECS = 10.0

# Chromaprint. fpcalc -raw gives one 32-bit item per 4096/3 samples at 11025 Hz.
FP_ITEM_SECS = 4096 / 3 / 11025
FP_LENGTH_SECS = 150
# How far one file may be shifted against the other (silence or an intro at the start).
FP_MAX_LAG_SECS = 50
# Least overlap a lag needs to count, so a few seconds of luck cannot score high.
FP_MIN_OVERLAP_SECS = 20
# Bit similarity of the same recording: one recording under another master scored 0.92,
# another performance of the song 0.60, unrelated audio sits near 0.5.
SAME_RECORDING_SIMILARITY = 0.85

STRICT_DECODE_TIMEOUT_SECS = 900
FPCALC_TIMEOUT_SECS = 120

_STOP = {"the", "and", "feat", "ft", "featuring", "with", "vs", "x", "y", "de", "la", "el", "los", "las", "a", "of"}
_STOP |= {"dj", "mc"}
# Words that say a copy is not the artist's own recording.
NOT_ORIGINAL = (
    "karaoke",
    "tribute",
    "made famous",
    "in the style of",
    "as made famous",
    "cover version",
    "originally performed",
    "re recorded",
    "rerecorded",
    "re record",
    "our version",
    "taylor s version",
    "new version",
)
# Words that name another recording of the song, when the original's name and tags lack them.
OTHER_RECORDING = (
    "live",
    "unplugged",
    "acoustic",
    "remix",
    "remixes",
    "remixed",
    "session",
    "sessions",
    "demo",
    "orchestral",
    "symphonic",
    "mtv",
)
# Version words in a copy's title that the original's title lacks: the ear decides.
VERSION_WORDS = (
    "live",
    "remix",
    "edit",
    "version",
    "mix",
    "acoustic",
    "demo",
    "karaoke",
    "instrumental",
    "cover",
    "cast",
    "mono",
    "stereo",
)
# Words that may appear in a copy's tags without naming a new performer.
_NEUTRAL_EXTRA = {"version", "remaster", "remastered", "mono", "stereo", "single", "radio", "album", "original"}


# ---------------------------------------------------------------------------
# Measuring a file
# ---------------------------------------------------------------------------


@dataclass
class Audit:
    """One file measured. *strict* is None for lossy files (not decoded); *cutoff* in kHz, None when unknown."""

    ext: str
    length: float = 0.0
    depth: int | None = None
    rate: int | None = None
    kbps: int | None = None
    artist: str = ""
    title: str = ""
    album: str = ""
    strict: bool | None = None
    cutoff: float | None = None
    tier: str = TIER_UNCERTAIN

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Audit:
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def tier_of(ext: str, depth: int | None, rate: int | None, strict: bool | None, cutoff: float | None) -> str:
    """The tier of a file from its format, depth, rate, strict decode and cutoff (kHz)."""
    if ext in LOSSY_EXTENSIONS:
        return TIER_LOSSY
    if strict is False:
        return TIER_DAMAGED
    if (depth or 0) >= 24 and (rate or 0) > 48000 and (cutoff or 0) > HIRES_CUTOFF_KHZ:
        return TIER_HIRES
    if cutoff is None:
        return TIER_UNCERTAIN
    if cutoff >= CD_CUTOFF_KHZ:
        return TIER_CD
    if cutoff >= FAKE_CUTOFF_KHZ:
        return TIER_UNCERTAIN
    return TIER_FAKE


def _nice(cmd: list[str]) -> list[str]:
    """*cmd* under `nice -n 19` when nice exists: a sweep must never slow the box down."""
    nice = shutil.which("nice")
    return [nice, "-n", "19", *cmd] if nice else cmd


def strict_decode_ok(path: str) -> bool:
    """True when ffmpeg decodes the whole file with CRC checks and stops at no error."""
    try:
        r = subprocess.run(
            _nice(
                ["ffmpeg", "-v", "error", "-xerror", "-err_detect", "crccheck+explode", "-i", path, "-f", "null", "-"]
            ),
            capture_output=True,
            timeout=STRICT_DECODE_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("Strict decode of %s did not finish", path, exc_info=True)
        return False
    stderr = r.stderr.decode(errors="ignore").lower()
    return r.returncode == 0 and not any(
        h in stderr for h in ("decode_frame() failed", "invalid", "error while decoding")
    )


def probe(path: str) -> Audit:
    """Format, length, depth, rate, bitrate and the artist, title and album tags (no decode)."""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    audit = Audit(ext=ext)
    try:
        import mutagen

        f = mutagen.File(path)
        if f is not None and f.info is not None:
            audit.length = round(float(getattr(f.info, "length", 0) or 0), 1)
            audit.depth = getattr(f.info, "bits_per_sample", None) or None
            audit.rate = getattr(f.info, "sample_rate", None) or None
            audit.kbps = round((getattr(f.info, "bitrate", 0) or 0) / 1000) or None
        easy = mutagen.File(path, easy=True)
        tags = (easy.tags if easy is not None else None) or {}
        audit.artist = (tags.get("artist") or [""])[0]
        audit.title = (tags.get("title") or [""])[0]
        audit.album = (tags.get("album") or [""])[0]
    except Exception:
        logger.debug("Could not read %s with mutagen", path, exc_info=True)
    return audit


def audit(path: str) -> Audit:
    """Measure *path*: probe, then for lossless files the strict decode and the spectral cutoff. Blocking."""
    result = probe(path)
    if result.ext not in LOSSY_EXTENSIONS:
        result.strict = strict_decode_ok(path)
        verdict = analyze_lossless(path) if result.ext in CHECKABLE_EXTENSIONS else None
        result.cutoff = verdict.cutoff_khz if verdict else None
        if verdict:
            result.depth = result.depth or verdict.bit_depth or None
            result.rate = result.rate or verdict.sample_rate or None
    result.tier = tier_of(result.ext, result.depth, result.rate, result.strict, result.cutoff)
    return result


def describe(a: Audit) -> str:
    """One line for a message: "FLAC 16/44.1, cutoff 21.9 kHz" or "MP3 320 kbps"."""
    fmt = a.ext.upper()
    if a.ext in LOSSY_EXTENSIONS:
        return f"{fmt} {a.kbps} kbps" if a.kbps else fmt
    quality = f" {a.depth}/{a.rate / 1000:g}" if a.depth and a.rate else ""
    cutoff = f", cutoff {a.cutoff:.1f} kHz" if a.cutoff is not None else ""
    damaged = ", decode errors" if a.strict is False else ""
    return f"{fmt}{quality}{cutoff}{damaged}"


# ---------------------------------------------------------------------------
# Is a copy worth it?
# ---------------------------------------------------------------------------


def wanted(tier: str, copy: SearchResult, original_length: float) -> bool:
    """Whether a Soulseek copy could beat a song of *tier*, judged from the search result alone.

    Only lossless formats the spectrum check reads are worth a download (the
    rest could never be proven better), of about the original's length. A CD
    song only wants 24-bit copies above 48 kHz.
    """
    if copy.extension not in CHECKABLE_EXTENSIONS:
        return False
    if original_length and copy.length and abs(copy.length - original_length) > WANTED_MAX_DIFF_SECS:
        return False
    if tier == TIER_CD:
        return (copy.bit_depth or 0) >= 24 and (copy.sample_rate or 0) > 48000
    return tier != TIER_HIRES


def better(original: Audit, candidate: Audit) -> bool:
    """Whether a downloaded copy is a real improvement on the original, judged by its cutoff.

    It must decode cleanly and be lossless. A CD original needs genuine content
    above HIRES_CUTOFF_KHZ; a damaged one needs a copy no worse; a fake,
    uncertain or lossy one a cutoff of at least CD_CUTOFF_KHZ and MIN_GAIN_KHZ
    above its own.
    """
    if candidate.strict is not True or candidate.ext in LOSSY_EXTENSIONS or candidate.cutoff is None:
        return False
    c, oc = candidate.cutoff, original.cutoff or 0.0
    if original.tier == TIER_HIRES:
        return False
    if original.tier == TIER_CD:
        return (candidate.depth or 0) >= 24 and (candidate.rate or 0) > 48000 and c > HIRES_CUTOFF_KHZ
    if original.tier == TIER_DAMAGED:
        return c >= min(oc, CD_CUTOFF_KHZ) - 0.5
    return c >= max(CD_CUTOFF_KHZ, oc + MIN_GAIN_KHZ)


# ---------------------------------------------------------------------------
# Is it the same recording?
# ---------------------------------------------------------------------------


def norm(text: str) -> str:
    """Lowercase ASCII words: accents dropped, "&" as "and", everything else a space."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def base_title(title: str) -> str:
    """The title without brackets and without a " - Remastered"/" - Live" style suffix, normalised."""
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "")
    title = re.sub(
        r"\s+-\s+(\d{4}\s+)?(remaster(ed)?|live|remix|radio edit|single version|album version|mono|stereo).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return norm(title)


def _sig(text: str) -> set[str]:
    """The meaningful words of *text*: normalised, longer than one letter, stop words dropped."""
    return {w for w in norm(text).split() if len(w) > 1 and w not in _STOP}


def split_stem(stem: str) -> tuple[str, str]:
    """("Artist", "Title") of a library name "Artist - Title"; ("", stem) without the separator."""
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", stem.strip()


SAME = "same"
REVIEW = "review"
REJECT = "reject"


def sameness(original: Audit, candidate: Audit, stem: str) -> tuple[str, str]:
    """(SAME | REVIEW | REJECT, why) for a better copy, from its tags and length only.

    Rejected: another artist (no meaningful word shared), a karaoke, tribute or
    cover, a live/unplugged/acoustic/remix recording the original is not, a
    different title, or a length more than REVIEW_MAX_DIFF_SECS away. Same:
    within SAME_MAX_DIFF_SECS with no version word or performer the original
    lacks. Anything in between is for the ear. Remaster years and track
    numbers are not new performers.
    """
    artist, title = split_stem(stem)
    hard = _hard_reject(original, candidate, stem)
    if hard:
        return REJECT, hard
    diff = abs((candidate.length or 0) - (original.length or 0))
    extra, added = _extras(original, candidate, stem)
    if diff > REVIEW_MAX_DIFF_SECS:
        return REJECT, f"length differs {diff:.0f} s"
    if diff <= SAME_MAX_DIFF_SECS and not added and not extra:
        return SAME, f"length within {diff:.1f} s"
    return REVIEW, _review_why(diff, added, extra)


def _hard_reject(original: Audit, candidate: Audit, stem: str) -> str:
    """Why the copy cannot be this song at all, or "" when it may be."""
    artist, title = split_stem(stem)
    ct, ca, cal = candidate.title, candidate.artist, candidate.album
    orig_text = norm(" ".join([stem, original.artist, original.title, original.album]))
    if ca and artist and not (_sig(artist) & _sig(ca)):
        return f"artist tag '{ca}'"
    cand_text = norm(" ".join([ca, ct, cal]))
    if any(w in cand_text for w in NOT_ORIGINAL) and not any(w in orig_text for w in NOT_ORIGINAL):
        return f"not the original recording ('{ca} - {ct}', album '{cal}')"
    other = [w for w in OTHER_RECORDING if w in norm(ct + " " + cal).split() and w not in orig_text.split()]
    if other:
        return f"another recording: {', '.join(other)} in '{ct}' / '{cal}'"
    wanted_title = base_title(title)
    if ct and wanted_title and wanted_title not in norm(ct) and norm(ct) not in wanted_title:
        return f"title tag '{ct}'"
    return ""


def _extras(original: Audit, candidate: Audit, stem: str) -> tuple[list[str], list[str]]:
    """(names in the copy's artist/title tags the original lacks, version words its title adds)."""
    _, title = split_stem(stem)
    orig_text = " ".join([stem, original.artist, original.title, original.album])
    extra = _sig(candidate.artist + " " + candidate.title) - _sig(orig_text) - _NEUTRAL_EXTRA
    extra = sorted(w for w in extra if not w.isdigit())
    added = [w for w in VERSION_WORDS if w in norm(candidate.title).split() and w not in norm(title).split()]
    return extra, added


def _review_why(diff: float, added: list[str], extra: list[str]) -> str:
    why = f"length differs {diff:.1f} s"
    if added:
        why += f", title adds {', '.join(added)}"
    if extra:
        why += f", names not in your file: {', '.join(extra)}"
    return why


def judge(original: Audit, candidate: Audit, stem: str, similarity: float | None) -> tuple[str, str]:
    """(SAME | REVIEW | REJECT, why) for a better copy, with the fingerprint *similarity* when there is one.

    Without a fingerprint (None) this is sameness(). With one, the tags still
    reject a copy that cannot be this song; then the fingerprint decides:
    the same recording (SAME_RECORDING_SIMILARITY or more) is replaced
    automatically when its length is within REVIEW_MAX_DIFF_SECS and its tags
    name nobody new, and goes to the ear otherwise; another recording is never
    replaced automatically: the ear decides, unless its length is off by more
    than REVIEW_MAX_DIFF_SECS.
    """
    if similarity is None:
        return sameness(original, candidate, stem)
    hard = _hard_reject(original, candidate, stem)
    if hard:
        return REJECT, hard
    diff = abs((candidate.length or 0) - (original.length or 0))
    extra, added = _extras(original, candidate, stem)
    if similarity >= SAME_RECORDING_SIMILARITY:
        if diff <= REVIEW_MAX_DIFF_SECS and not extra and not added:
            return SAME, f"same recording (fingerprint {similarity:.2f}), length within {diff:.1f} s"
        return REVIEW, f"same recording (fingerprint {similarity:.2f}), " + _review_why(diff, added, extra)
    if diff > REVIEW_MAX_DIFF_SECS:
        return REJECT, f"another recording (fingerprint {similarity:.2f}), length differs {diff:.0f} s"
    return REVIEW, f"another recording (fingerprint {similarity:.2f}), " + _review_why(diff, added, extra)


# ---------------------------------------------------------------------------
# Chromaprint
# ---------------------------------------------------------------------------


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


def fingerprint(path: str, length_secs: int = FP_LENGTH_SECS) -> list[int] | None:
    """The raw Chromaprint fingerprint of the first *length_secs* of *path*; None without fpcalc or on failure."""
    if not fpcalc_available():
        return None
    try:
        r = subprocess.run(
            _nice(["fpcalc", "-raw", "-length", str(length_secs), path]),
            capture_output=True,
            text=True,
            timeout=FPCALC_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("fpcalc did not finish on %s", path, exc_info=True)
        return None
    for line in r.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            items = [int(x) for x in line.split("=", 1)[1].split(",") if x.strip()]
            return items or None
    logger.warning("fpcalc gave no fingerprint for %s: %s", path, r.stderr.strip()[:200])
    return None


def similarity(a: list[int], b: list[int], max_lag_secs: float = FP_MAX_LAG_SECS) -> float:
    """Best share of equal bits between two raw fingerprints, one shifted against the other by up to *max_lag_secs*.

    1.0 is the same audio; unrelated audio sits near 0.5. A shift counts only
    with FP_MIN_OVERLAP_SECS of overlap (or half the shorter fingerprint,
    for very short files).
    """
    import numpy as np

    if not a or not b:
        return 0.0
    x = np.asarray(a, dtype=np.int64).astype(np.uint32)
    y = np.asarray(b, dtype=np.int64).astype(np.uint32)
    max_lag = int(max_lag_secs / FP_ITEM_SECS)
    min_overlap = max(1, min(int(FP_MIN_OVERLAP_SECS / FP_ITEM_SECS), min(len(x), len(y)) // 2))
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    best = 0.0
    for lag in range(-max_lag, max_lag + 1):
        # x[i] against y[i + lag]
        start_x = max(0, -lag)
        start_y = max(0, lag)
        n = min(len(x) - start_x, len(y) - start_y)
        if n < min_overlap:
            continue
        diff = np.bitwise_xor(x[start_x : start_x + n], y[start_y : start_y + n])
        errors = int(table[diff.view(np.uint8)].sum())
        score = 1.0 - errors / (32.0 * n)
        best = max(best, score)
    return best


# ---------------------------------------------------------------------------
# Keep both: the proposal as a song of its own
# ---------------------------------------------------------------------------

_CONNECTOR_RE = re.compile(r"^(?:with|feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
_JOINER_RE = re.compile(r"^(?:[,&+/;:-]|and\b|x\b|vs\.?)\s*", re.IGNORECASE)
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_suffix(text: str) -> str:
    return re.sub(r"\s+", " ", _FORBIDDEN.sub("", text)).strip(" .-")


def keep_both_suffix(stem: str, original: Audit, candidate: Audit) -> str:
    """What sets the proposal apart, for its name: "with <performers>", a bracket of its title, or its album.

    From the artist tag when it holds the original artist plus someone else
    ("Elvis Presley with the Royal Philharmonic Orchestra" gives "with the
    Royal Philharmonic Orchestra"); else a bracket in its title the original's
    title lacks ("(1997 Version)"); else its album; else "alternate".
    """
    artist, title = split_stem(stem)
    ca = candidate.artist.strip()
    if ca and artist and norm(artist) in norm(ca) and norm(artist) != norm(ca):
        start = ca.casefold().find(artist.casefold())
        rest = (ca[:start] + ca[start + len(artist) :]).strip() if start >= 0 else ""
        rest = _JOINER_RE.sub("", rest).strip()
        if rest:
            rest = rest if _CONNECTOR_RE.match(rest) else f"with {rest}"
            return _clean_suffix(rest)
    for bracket in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", candidate.title or ""):
        if norm(bracket) and norm(bracket) not in norm(title):
            return _clean_suffix(bracket)
    if candidate.album.strip() and norm(candidate.album) != norm(title):
        return _clean_suffix(candidate.album)
    return "alternate"


def keep_both_name(stem: str, ext: str, original: Audit, candidate: Audit, exists: Callable[[str], bool]) -> str:
    """The file name the proposal gets when both are kept: "Artist - Title (<suffix>).<ext>", never one taken."""
    suffix = keep_both_suffix(stem, original, candidate) or "alternate"
    name = f"{stem} ({suffix}).{ext}"
    n = 2
    while exists(name):
        name = f"{stem} ({suffix}) {n}.{ext}"
        n += 1
    return name


# ---------------------------------------------------------------------------
# LIBRARY_SWEEP_SCHEDULE
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE = "weekly:sun:04:00"
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Schedule:
    """When scheduled sweeps start, in local time. *weekday* 0 = Monday; None = every day."""

    hour: int
    minute: int
    weekday: int | None = None

    def __str__(self) -> str:
        at = f"{self.hour:02d}:{self.minute:02d}"
        return f"daily:{at}" if self.weekday is None else f"weekly:{_DAYS[self.weekday]}:{at}"


def parse_schedule(text: str | None) -> Schedule | None:
    """LIBRARY_SWEEP_SCHEDULE: "weekly:<mon..sun>:HH:MM", "daily:HH:MM", or "off" (None). Empty = DEFAULT_SCHEDULE.

    Raises ValueError for anything else.
    """
    text = (text or "").strip().lower() or DEFAULT_SCHEDULE
    if text == "off":
        return None
    m = re.fullmatch(r"weekly:([a-z]{3}):(\d{1,2}):(\d{2})", text) or re.fullmatch(r"daily:(\d{1,2}):(\d{2})", text)
    if not m:
        raise ValueError(f"LIBRARY_SWEEP_SCHEDULE must be weekly:<mon..sun>:HH:MM, daily:HH:MM or off, not {text!r}")
    groups = m.groups()
    weekday = None
    if len(groups) == 3:
        if groups[0] not in _DAYS:
            raise ValueError(f"LIBRARY_SWEEP_SCHEDULE: unknown day {groups[0]!r} (mon, tue, wed, thu, fri, sat, sun)")
        weekday = _DAYS.index(groups[0])
        groups = groups[1:]
    hour, minute = int(groups[0]), int(groups[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"LIBRARY_SWEEP_SCHEDULE: {hour:02d}:{minute:02d} is not a time of day")
    return Schedule(hour, minute, weekday)


def previous_run(schedule: Schedule, now: datetime.datetime) -> datetime.datetime:
    """The latest scheduled start at or before *now* (same tzinfo as *now*)."""
    at = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
    if schedule.weekday is None:
        return at if at <= now else at - datetime.timedelta(days=1)
    at -= datetime.timedelta(days=(now.weekday() - schedule.weekday) % 7)
    return at if at <= now else at - datetime.timedelta(days=7)


def next_run(schedule: Schedule, now: datetime.datetime) -> datetime.datetime:
    """The first scheduled start strictly after *now*."""
    step = datetime.timedelta(days=1 if schedule.weekday is None else 7)
    return previous_run(schedule, now) + step
