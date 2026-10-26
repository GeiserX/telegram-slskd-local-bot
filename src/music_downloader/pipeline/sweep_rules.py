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
import threading
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
# Below this a copy is another performance of the song, not another master: the owner kept their own file
# over 12 of 14 such copies and over 12 of 227 closer ones. The sweep tries the other copies first and
# offers one of these only when nothing closer qualifies.
FAR_SIMILARITY = 0.6

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


def strict_decode_ok(path: str) -> bool | None:
    """True when ffmpeg decodes the whole file with CRC checks and stops at no error, False when it reports one.

    None when the decode could not run or did not finish (no ffmpeg, a
    timeout): a tool failure is not proof of damage. tier_of() then goes by the
    cutoff, and better() never accepts a copy that was not decoded.
    """
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
        return None
    stderr = r.stderr.decode(errors="ignore").lower()
    return r.returncode == 0 and not any(
        h in stderr for h in ("decode_frame() failed", "invalid", "error while decoding")
    )


def probe(path: str, ext: str | None = None) -> Audit:
    """Format, length, depth, rate, bitrate and the artist, title and album tags (no decode).

    *ext* names the format of a file whose name does not end in it (a parked original).
    """
    ext = ext or os.path.splitext(path)[1].lstrip(".").lower()
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


def audit(path: str, ext: str | None = None) -> Audit:
    """Measure *path*: probe, then for lossless files the strict decode and the spectral cutoff. Blocking."""
    result = probe(path, ext)
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


def wanted(tier: str, copy: SearchResult, original_length: float, repair: bool = False) -> bool:
    """Whether a Soulseek copy could beat a song of *tier*, judged from the search result alone.

    Only lossless formats the spectrum check reads are worth a download (the
    rest could never be proven better), of about the original's length. A CD
    song only wants 24-bit copies above 48 kHz, unless it is to be *repaired*
    (it sounds worse than the copy it replaced): then any such copy may do.
    """
    if copy.extension not in CHECKABLE_EXTENSIONS:
        return False
    if original_length and copy.length and abs(copy.length - original_length) > WANTED_MAX_DIFF_SECS:
        return False
    if repair:
        return True
    if tier == TIER_CD:
        return (copy.bit_depth or 0) >= 24 and (copy.sample_rate or 0) > 48000
    return tier != TIER_HIRES


def better(original: Audit, candidate: Audit, repair: bool = False) -> bool:
    """Whether a downloaded copy is a real improvement on the original, judged by its cutoff.

    It must decode cleanly and be lossless. A damaged original needs a copy no
    worse; one to be *repaired* (it sounds worse than the copy it replaced) a
    genuine CD cutoff, or one at most 0.5 kHz below its own when that is lower
    (an unknown cutoff counts as a CD's);
    a CD original genuine content above HIRES_CUTOFF_KHZ; a fake,
    uncertain or lossy one a cutoff of at least CD_CUTOFF_KHZ and MIN_GAIN_KHZ
    above its own.
    """
    if candidate.strict is not True or candidate.ext in LOSSY_EXTENSIONS or candidate.cutoff is None:
        return False
    c, oc = candidate.cutoff, original.cutoff or 0.0
    if repair:
        own = original.cutoff if original.cutoff is not None else CD_CUTOFF_KHZ + 0.5
        return c >= min(own - 0.5, CD_CUTOFF_KHZ)
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
    the same recording (SAME_RECORDING_SIMILARITY or more) is SAME when its
    length is within REVIEW_MAX_DIFF_SECS and its tags name nobody new, REVIEW
    otherwise; another recording is REVIEW, unless its length is off by more
    than REVIEW_MAX_DIFF_SECS. The sweep replaces nothing by itself: SAME and
    REVIEW both go to the owner's ear, SAME marked as the same recording.
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


def missing_tools() -> list[str]:
    """What the sweep cannot measure without: ffmpeg (strict decode) and the analysis extra (cutoff, fingerprint math).

    fpcalc is optional (judge() falls back to length and tags), so it is not listed.
    """
    from music_downloader.processor.lossless_analyzer import HAS_ANALYSIS

    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg")
    if not HAS_ANALYSIS:
        missing.append("numpy, scipy and soundfile (pip install 'telegram-slskd-local-bot[analysis]')")
    return missing


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
# How it sounds: highs, clipping and dynamics, against the owner's own copy
# ---------------------------------------------------------------------------

# The cutoff says how high a spectrum reaches, not how much treble is there: a
# 24/96 transfer reaching 24 kHz can still sound duller than a CD. The highs are
# the 4-16 kHz energy against the 1-4 kHz mids, in dB, so a copy that is only
# louder, or a master with more bass, reads the same; only highs that were cut
# read lower. The first measure (5-15 kHz against everything up to 15 kHz)
# called a bass-heavier master duller: it flagged 10 of 229 accepted copies whose
# highs were intact. Among those 229 the copies muffled to the ear sat 3.3 to
# 7.6 dB below the copy they replaced.
HIGHS_BAND_HZ = (4000, 16000)
MIDS_BAND_HZ = (1000, 4000)
# A proposal with highs this much lower than the owner's copy is never offered.
HIGHS_MAX_LOSS_DB = 3.0
# Clipping is counted as flat tops: runs of three or more equal samples above
# -6 dBFS. A clipped file keeps its flat tops when it is turned down later.
# A clean 16-bit CD shows a few dozen from loud bass (43 on the Zombies CD);
# the same file clipped by 4 dB showed 8166.
CLIP_LEVEL = 2**30  # -6 dBFS in 32-bit samples
# A proposal with more flat tops per minute than this, and more than CLIP_FACTOR
# times the owner's, is never offered.
CLIP_MAX_PER_MIN = 100
CLIP_FACTOR = 4
# The dynamics are the crest: the peak against the average level of the parts
# that are not silent (seconds above SILENT_DBFS), in dB. A loudness-war
# remaster is limited until its crest shrinks, often with no flat top at all.
# Of 229 accepted copies, 8 had lost 3.2 to 5.9 dB against the copy they
# replaced, all of them louder remasters; 97 had gained over 3 dB.
SILENT_DBFS = -60.0
# A proposal whose crest is this much lower than the owner's copy is never offered.
CREST_MAX_LOSS_DB = 3.0
SOUND_TIMEOUT_SECS = 900
# A decode that stops before this share of the file's length is not judged.
SOUND_MIN_COVERAGE = 0.9


@dataclass
class Sound:
    """How a file sounds: *highs* in dB (see HIGHS_BAND_HZ), *clips* flat tops per minute, *crest* in dB."""

    highs: float
    clips: float
    crest: float


def sound(path: str) -> Sound | None:
    """Measure the highs, the clipping and the dynamics of *path* in one streamed decode.

    None when it cannot be decoded, when the decode covers less than
    SOUND_MIN_COVERAGE of the file's length (a partial measure could refuse a
    good copy), or when it takes over SOUND_TIMEOUT_SECS: a timer kills ffmpeg
    then, so a stalled decode cannot block the read.
    """
    import json

    import numpy as np

    try:
        info = json.loads(
            subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=sample_rate,channels:format=duration",
                    "-of",
                    "json",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            ).stdout
            or "{}"
        )
        stream = (info.get("streams") or [{}])[0]
        rate, channels = int(stream.get("sample_rate") or 0), int(stream.get("channels") or 0)
        duration = float((info.get("format") or {}).get("duration") or 0)
        if rate <= 0 or channels <= 0:
            return None
        proc = subprocess.Popen(
            _nice(["ffmpeg", "-v", "error", "-i", path, "-f", "s32le", "-"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        logger.warning("Could not measure the sound of %s", path, exc_info=True)
        return None
    timer = threading.Timer(SOUND_TIMEOUT_SECS, proc.kill)
    timer.start()
    window = np.hanning(rate)
    spectrum = np.zeros(rate // 2 + 1)
    flat_tops = 0
    samples = 0
    peak = 0.0
    seconds: list[float] = []  # mean square of each whole second
    frame = channels * 4
    try:
        while True:
            chunk = proc.stdout.read(rate * frame)
            if len(chunk) < frame:
                break
            x = np.frombuffer(chunk[: len(chunk) // frame * frame], dtype="<i4").reshape(-1, channels)
            samples += len(x)
            for c in range(channels):
                v = x[:, c].astype(np.int64)
                loud = np.abs(v) >= CLIP_LEVEL
                same = np.concatenate(([0], ((v[1:] == v[:-1]) & loud[1:] & loud[:-1]).astype(np.int8), [0]))
                edges = np.diff(same)
                lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
                flat_tops += int((lengths >= 2).sum())
            f = x.astype(np.float64) / 2**31
            peak = max(peak, float(np.abs(f).max()))
            if len(x) == rate:
                seconds.append(float((f**2).mean()))
                spectrum += np.abs(np.fft.rfft(f.mean(axis=1) * window)) ** 2
    finally:
        timed_out = not timer.is_alive()
        timer.cancel()
        proc.kill()
        proc.wait()
    if timed_out:
        logger.warning("Measuring the sound of %s took over %d s", path, SOUND_TIMEOUT_SECS)
        return None
    if duration and samples / rate < SOUND_MIN_COVERAGE * duration:
        logger.warning("Only %.0f of %.0f s of %s decoded; its sound is not judged", samples / rate, duration, path)
        return None
    freqs = np.fft.rfftfreq(rate, 1 / rate)
    mids = spectrum[(freqs >= MIDS_BAND_HZ[0]) & (freqs < MIDS_BAND_HZ[1])].sum()
    highs = spectrum[(freqs >= HIGHS_BAND_HZ[0]) & (freqs < HIGHS_BAND_HZ[1])].sum()
    heard = [m for m in seconds if m > 10 ** (SILENT_DBFS / 10)]
    if samples < rate or mids <= 0 or highs <= 0 or not heard or peak <= 0:
        return None
    level = 10 * float(np.log10(sum(heard) / len(heard)))
    return Sound(
        highs=round(10 * float(np.log10(highs / mids)), 1),
        clips=round(flat_tops / (samples / rate / 60), 1),
        crest=round(20 * float(np.log10(peak)) - level, 1),
    )


def sounds_worse(
    original: Sound | None, candidate: Sound | None, original_lossy: bool = False, mine: str = "yours"
) -> str | None:
    """Why the candidate sounds worse than *original*, the owner's copy (*mine* names it), or None.

    Worse is duller (highs cut), clipped, or squashed (dynamics lost). Nothing
    is judged when either file could not be measured. A lossy original's flat
    tops are not compared: its decoder never keeps them.
    """
    if original is None or candidate is None:
        return None
    if original.highs - candidate.highs > HIGHS_MAX_LOSS_DB:
        return f"duller than {mine} (highs {candidate.highs - original.highs:+.1f} dB)"
    if candidate.clips > CLIP_MAX_PER_MIN and (original_lossy or candidate.clips > CLIP_FACTOR * original.clips):
        return f"clipped ({candidate.clips:.0f} flat tops a minute, {mine} {original.clips:.0f})"
    if original.crest - candidate.crest > CREST_MAX_LOSS_DB:
        return f"more squashed than {mine} (dynamics {candidate.crest - original.crest:+.1f} dB)"
    return None


def clipping_words(clips: float) -> str:
    """Flat tops a minute in words: none, slight or heavy."""
    if clips < CLIP_MAX_PER_MIN / 4:
        return "none"
    return "slight" if clips <= CLIP_MAX_PER_MIN else "heavy"


def _gap(value: float, against: float | None, more: str, less: str) -> str:
    if against is None:
        return ""
    gap = value - against
    return f" ({abs(gap):.1f} dB {more if gap > 0 else less} than yours)" if abs(gap) >= 0.5 else " (same as yours)"


def describe_sound(s: Sound | None, mine: Sound | None = None) -> str:
    """One line for a caption: "highs -9.1 dB, clipping none (12/min), dynamics 14.0 dB", with the gaps to *mine*."""
    if s is None:
        return ""
    line = f"highs {s.highs:.1f} dB" + _gap(s.highs, mine and mine.highs, "brighter", "duller")
    line += f", clipping {clipping_words(s.clips)} ({s.clips:.0f}/min)"
    line += f", dynamics {s.crest:.1f} dB" + _gap(s.crest, mine and mine.crest, "more", "less")
    return line


# ---------------------------------------------------------------------------
# Keep both: the proposal as a song of its own
# ---------------------------------------------------------------------------

_CONNECTOR_RE = re.compile(r"^(?:with|feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
_JOINER_RE = re.compile(r"^(?:[,&+/;:-]|and\b|x\b|vs\.?)\s*", re.IGNORECASE)
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# Longest suffix kept in a keep-both name: file names stop at 255 bytes.
_SUFFIX_MAX = 80


def _clean_suffix(text: str) -> str:
    text = re.sub(r"\s+", " ", _FORBIDDEN.sub("", text)).strip(" .-")
    return text if len(text) <= _SUFFIX_MAX else text[:_SUFFIX_MAX].rsplit(" ", 1)[0].strip(" .-")


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
