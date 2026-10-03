"""Soulseek search queries and the ranking of what comes back."""

import re

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.scorer import CHAT_SIZE_LIMIT_BYTES, PROFILE_CHAT, ResultScorer
from music_downloader.search.slskd_client import SearchResult, SlskdClient

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


def rank(
    slskd: SlskdClient,
    scorer: ResultScorer,
    raw_responses,
    track: TrackInfo,
    profile: str,
    max_duration_diff: int | None = None,
) -> list[SearchResult]:
    """Parse raw slskd responses (every audio format) and rank them for *profile*.

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
    ranked = scorer.score_results(results, track, profile=profile, **score_kwargs)
    if profile == PROFILE_CHAT:
        return [r for r in ranked if r.size <= CHAT_SIZE_LIMIT_BYTES] + [
            r for r in ranked if r.size > CHAT_SIZE_LIMIT_BYTES
        ]

    def leads(r: SearchResult) -> bool:
        return r.is_lossless and not scorer.is_other_version(r, track)

    return [r for r in ranked if leads(r)] + [r for r in ranked if not leads(r)]
