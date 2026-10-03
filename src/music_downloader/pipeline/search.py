"""Soulseek search queries and the ranking of what comes back."""

import logging
import re
import unicodedata

from music_downloader.config import DEFAULT_UPLOAD_LIMIT_BYTES
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.scorer import PROFILE_CHAT, ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

logger = logging.getLogger(__name__)

# Noise keywords that Spotify appends to track titles but Soulseek users never use.
# Named remixes (e.g. "Butch Vig Remix") are intentionally excluded — they
# represent distinct versions the user specifically selected.
_NOISE_PATTERN = (
    r"Mono|Stereo|Remaster(?:ed)?(?:\s+\d{4})?"
    r"|Deluxe(?:\s+Edition)?"
    r"|Ultimate\s+Mix|Single\s+Version|Album\s+Version"
    r"|Radio\s+Edit|Bonus\s+Track|Anniversary(?:\s+Edition)?"
    r"|Super\s+Deluxe|Special\s+Edition"
    r"|\d{4}\s+(?:Mix|Remix|Remaster(?:ed)?|Version)"
    r"|(?:German|French|Spanish|Italian|Japanese|Portuguese|English)\s+Version"
    r"|Remix"
)

# Matches trailing " - Remastered 2009", " - German Version 1989 Remix; ...", etc.
# Once a noise keyword is detected after a dash, everything to EOL is stripped.
_VERSION_SUFFIX_RE = re.compile(
    r"\s*[-–]\s*(?:" + _NOISE_PATTERN + r").*$",
    re.IGNORECASE,
)

# Same patterns inside parentheses: "(Remastered 2009)", "(German Version)", etc.
_VERSION_PAREN_RE = re.compile(
    r"\s*\((?:" + _NOISE_PATTERN + r")[^)]*\)",
    re.IGNORECASE,
)


_QUOTE_CHARS = "'\"‘’“”"


def clean_search_title(title: str) -> str:
    """Strip Spotify version suffixes that add noise to Soulseek keyword search."""
    title = _VERSION_SUFFIX_RE.sub("", title)
    title = _VERSION_PAREN_RE.sub("", title)
    title = title.strip()
    if len(title) >= 2 and title[0] in _QUOTE_CHARS and title[-1] in _QUOTE_CHARS:
        title = title[1:-1].strip()
    return title


def build_reduced_queries(title: str, year: str) -> list[str]:
    """Build fallback search queries by dropping one word at a time and appending the year.

    Soulseek users sometimes block entire phrases (e.g. "Purple Rain").
    Removing one keyword at a time while adding the album year often
    bypasses server-side filters while still narrowing results enough
    to find the right track.

    Args:
        title: The (cleaned) song title, e.g. "Purple Rain".
        year: Album release year, e.g. "1984".

    Returns:
        List of fallback query strings.  Empty if the title has fewer
        than 2 words or no year is available.
    """
    if not year:
        return []
    words = title.split()
    if len(words) < 2:
        return []
    queries: list[str] = []
    for i in range(len(words)):
        reduced = " ".join(words[:i] + words[i + 1 :])
        queries.append(f"{reduced} {year}")
    return queries


def has_non_latin_script(text: str) -> bool:
    """True when *text* contains characters from non-Latin scripts (CJK, Cyrillic, etc.)."""
    return any(c.isalpha() and ord(c) > 0x024F for c in text)


_NOISE_WORDS = frozenset(
    {
        "single",
        "version",
        "long",
        "short",
        "full",
        "edit",
        "mix",
        "remastered",
        "remaster",
        "deluxe",
        "edition",
        "bonus",
        "track",
        "album",
        "mono",
        "stereo",
        "original",
        "extended",
        "feat",
        "featuring",
        "ft",
        "the",
        "an",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
    }
)


def extract_latin_keywords(title: str) -> list[str]:
    """Extract meaningful Latin keywords from a potentially mixed-script title.

    Strips common noise words so only distinctive keywords remain,
    e.g. ``["KURENAI"]`` from ``"紅 - KURENAI - シングル… - Single Long Version"``.
    """
    words = re.findall(r"[a-zA-Z]{2,}", title)
    return [w for w in words if w.lower() not in _NOISE_WORDS]


class RankedResults(list):
    """A ranked result list that also says how many copies the title guard hid."""

    def __init__(self, results=(), hidden: int = 0):
        super().__init__(results)
        self.hidden = hidden


# Words too common to tell one title from another (title guard only).
_GUARD_STOPWORDS = frozenset({"a", "the", "of", "and", "feat", "ft"})
_BRACKETS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _words(text: str) -> list[str]:
    """Casefolded, accent-free words of *text*; punctuation and underscores separate words."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.findall(r"[^\W_]+", plain)


def title_tokens(title: str) -> set[str]:
    """The words of *title* a copy of it must show somewhere in its name.

    Version noise (" - Remastered 2009"), anything in parentheses or
    brackets, and stopwords are dropped. A title made only of stopwords
    ("The The") keeps them; a title made only of brackets keeps their words.
    """
    words = _words(_BRACKETS_RE.sub(" ", clean_search_title(title)))
    if not words:
        words = _words(title)
    meaningful = [w for w in words if w not in _GUARD_STOPWORDS]
    return set(meaningful or words)


def _name_words(result: SearchResult) -> set[str]:
    """Words of the copy's file name and of the folder it sits in."""
    parts = result.filename.replace("/", "\\").split("\\")
    parent = parts[-2] if len(parts) > 1 else ""
    return set(_words(result.basename)) | set(_words(parent))


def title_guard(results: list[SearchResult], track: TrackInfo) -> tuple[list[SearchResult], int]:
    """Drop copies whose file and folder name share no word with the track title.

    Returns the kept results and how many were hidden. Never empties the
    list: when every copy would go, all are kept and the guard is skipped.
    """
    tokens = title_tokens(track.title)
    if not tokens or not results:
        return results, 0
    kept = [r for r in results if tokens & _name_words(r)]
    hidden = len(results) - len(kept)
    if not kept:
        logger.info("Title guard skipped for %r: it would hide all %d results", track.title, len(results))
        return results, 0
    if hidden:
        logger.info("Title guard hid %d unrelated result(s) of %d for %r", hidden, len(results), track.title)
    return kept, hidden


def rank(
    slskd: SlskdClient,
    scorer: ResultScorer,
    raw_responses,
    track: TrackInfo,
    profile: str,
    max_duration_diff: int | None = None,
    upload_limit_bytes: int = DEFAULT_UPLOAD_LIMIT_BYTES,
) -> RankedResults:
    """Parse raw slskd responses (every audio format) and rank them for *profile*.

    First the title guard drops copies named after another track (skipped for
    a direct search, whose track has no length: that path skips the filters).
    A copy whose length is unknown even after estimating it never leads: it
    ranks after every copy of known length (chat) or among the lossy results
    (library), whatever its score.

    Library delivery: every lossless result before every lossy one, each
    group by score, so a lossy copy is offered only below the lossless ones.
    A lossless copy of a different version (length off by more than
    SAME_VERSION_MAX_DIFF_SECS) does not lead: it ranks among the lossy
    results by score, so an exact-length lossy copy can beat it.
    Chat delivery: one list scored by the chat profile (perceived quality
    versus size); results over the upload limit sort after every result
    that fits (stable), since those would have to be converted.
    """
    score_kwargs = {"max_duration_diff": max_duration_diff} if max_duration_diff else {}
    results = slskd.parse_results(raw_responses)
    hidden = 0
    if track.duration_secs:
        results, hidden = title_guard(results, track)
    ranked = scorer.score_results(results, track, profile=profile, **score_kwargs)
    if profile == PROFILE_CHAT:
        ordered = sorted(ranked, key=lambda r: (ResultScorer.length_unknown(r, track), r.size > upload_limit_bytes))
        return RankedResults(ordered, hidden)

    def leads(r: SearchResult) -> bool:
        return r.is_lossless and not scorer.is_other_version(r, track) and not ResultScorer.length_unknown(r, track)

    return RankedResults([r for r in ranked if leads(r)] + [r for r in ranked if not leads(r)], hidden)
