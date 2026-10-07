"""Turn a free-text query into track candidates: Spotify, then MusicBrainz, then Soulseek file names.

A candidate is *confident* when its artist and title are the asked ones once
spelling is folded (accents, curly quotes, "&", a leading "The") and neutral
suffixes are dropped (" - 2011 Remaster", "(Single Edit)", "(feat. X)"), and,
when the caller knows the length, when it lasts within MATCH_MAX_DIFF_SECS.
A suffix that names another recording (" - Live", "(Remix)", "Karaoke") is
never dropped, so it keeps the titles apart.
"""

import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from music_downloader.metadata.musicbrainz import MusicBrainzClient
from music_downloader.metadata.spotify import SOURCE_SOULSEEK, SpotifyResolver, TrackInfo
from music_downloader.pipeline.search import RankedResults, clean_search_title

# The same recording on two releases differs by seconds of silence or fade
# (Exitlude: 146 s on Spotify, 152 s in a library file); another edit of the
# song differs by tens of seconds (Come On Eileen: single edit 205 s, album 287 s).
MATCH_MAX_DIFF_SECS = 8
# At most this many MusicBrainz candidates come back (it lists one recording per compilation).
MUSICBRAINZ_MAX_CANDIDATES = 10

_APOSTROPHES = "'‘’ʼ`´"
_ARTICLES = {"the", "a", "an"}
_ARTIST_STOPWORDS = {"the", "and", "feat", "ft", "featuring", "with", "vs"}
# A suffix or bracket that names the same recording, matched against the whole suffix.
_NEUTRAL_RE = re.compile(
    r"(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+version)?(?:\s+\d{4})?"
    r"|(?:single|radio|album|original|lp)\s+(?:edit|version|mix)"
    r"|original|mono|stereo|(?:mono|stereo)\s+version|explicit|clean"
    r"|(?:feat\.?|ft\.?|featuring|with)\s+.+"
    r"|from\s+.+",
    re.IGNORECASE,
)
_DASH_SUFFIX_RE = re.compile(r"\s+[-–—]\s+([^-–—]+)$")
_BRACKET_RE = re.compile(r"\s*[(\[]([^)\]]*)[)\]]")
_TRACK_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*[-.)_]?\s+")


def fold(text: str) -> str:
    """*text* casefolded, without accents or apostrophes, "&" as "and", words joined by one space."""
    text = text.casefold()
    for ch in _APOSTROPHES:
        text = text.replace(ch, "")
    text = text.replace("&", " and ")
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", plain))


def strip_neutral(title: str) -> str:
    """*title* without the suffixes and brackets that name the same recording."""
    title = _BRACKET_RE.sub(lambda m: "" if _NEUTRAL_RE.fullmatch(m.group(1).strip()) else m.group(0), title)
    while (m := _DASH_SUFFIX_RE.search(title)) and _NEUTRAL_RE.fullmatch(m.group(1).strip()):
        title = title[: m.start()]
    return title.strip()


def title_words(title: str) -> list[str]:
    """The words two titles of one recording share: folded, neutral suffixes and articles dropped."""
    words = fold(strip_neutral(title)).split()
    return [w for w in words if w not in _ARTICLES] or words


def _artist_words(artist: str) -> set[str]:
    words = fold(artist).split()
    return {w for w in words if w not in _ARTIST_STOPWORDS} or set(words)


def same_artist(asked: str, found: str) -> bool:
    """True when one artist's words hold the other's ("Dexys Midnight Runners" in "Kevin Rowland & Dexys...")."""
    a, f = _artist_words(asked), _artist_words(found)
    return bool(a) and bool(f) and (a <= f or f <= a)


def is_confident(track: TrackInfo, artist: str, title: str, duration_secs: float | None = None) -> bool:
    """True when *track* is *title* by *artist*, and lasts *duration_secs* give or take MATCH_MAX_DIFF_SECS."""
    if not (artist and title) or not same_artist(artist, track.artist):
        return False
    if title_words(track.title) != title_words(title):
        return False
    if duration_secs:
        return track.duration_ms > 0 and abs(track.duration_ms / 1000 - duration_secs) <= MATCH_MAX_DIFF_SECS
    return True


def split_query(query: str, tracks: list[TrackInfo]) -> tuple[str, str]:
    """(artist, title) of *query*: split at " - ", else where a candidate's artist starts or ends it.

    ("", query) when neither works.
    """
    if " - " in query:
        artist, title = query.split(" - ", 1)
        return artist.strip(), title.strip()
    words = query.split()
    for t in tracks:
        artist = fold(t.artist)
        for k in range(1, len(words)):
            if fold(" ".join(words[:k])) == artist:
                return " ".join(words[:k]), " ".join(words[k:])
            if fold(" ".join(words[k:])) == artist:
                return " ".join(words[k:]), " ".join(words[:k])
    return "", query.strip()


def lookup(spotify: SpotifyResolver, query: str, limit: int = 50) -> list[TrackInfo]:
    """Spotify candidates for *query*, deduplicated, artist matches first.

    Empty when Spotify finds nothing. A query written as "Artist - Title"
    keeps only tracks by that artist.
    """
    tracks = spotify.search_multiple(query, limit=limit)
    if not tracks:
        return []

    query_lower = query.lower()
    query_words = set(query_lower.split())
    query_artist = ""
    if " - " in query:
        query_artist = fold(query.split(" - ", 1)[0])

    seen = set()
    unique_tracks = []
    artist_match_tracks = []
    other_tracks = []
    for t in tracks:
        key = (t.artist.lower(), t.title.lower(), t.album.lower())
        if key in seen:
            continue
        seen.add(key)
        artist_lower = t.artist.lower()
        if query_artist and query_artist not in fold(t.artist):
            continue
        artist_words = set(artist_lower.split())
        if (len(artist_words) >= 2 and artist_lower in query_lower) or artist_words.issubset(query_words):
            artist_match_tracks.append(t)
        else:
            other_tracks.append(t)

    artist_match_tracks.sort(key=lambda t: len(t.artist), reverse=True)
    unique_tracks = artist_match_tracks + other_tracks

    if not unique_tracks:
        seen = set()
        for t in tracks:
            key = (t.artist.lower(), t.title.lower(), t.album.lower())
            if key not in seen:
                seen.add(key)
                unique_tracks.append(t)

    return unique_tracks


@dataclass
class Candidate:
    """One answer to a query. *responses*: the raw slskd responses a Soulseek candidate was built from."""

    track: TrackInfo
    confident: bool
    responses: list | None = field(default=None, repr=False)


def _closest_first(candidates: list[Candidate], duration_secs: float | None) -> list[Candidate]:
    """Confident candidates first, each group by closeness to *duration_secs* (stable without one)."""

    def key(c: Candidate):
        diff = abs(c.track.duration_ms / 1000 - duration_secs) if duration_secs and c.track.duration_ms else 0
        return (not c.confident, diff)

    return sorted(candidates, key=key)


def match(
    spotify: SpotifyResolver,
    musicbrainz: MusicBrainzClient,
    query: str,
    artist: str = "",
    title: str = "",
    duration_secs: float | None = None,
) -> tuple[list[Candidate], str, str]:
    """Spotify candidates for the query, or MusicBrainz ones when no Spotify candidate is confident.

    *artist* and *title*, when given, say what the query asks for; otherwise
    they come from the query (split_query). Returns the candidates
    (confident first; the Spotify ones always follow) and the artist and title used.
    """
    query = query.strip() or f"{artist} - {title}"
    tracks = lookup(spotify, query)
    if not (artist and title):
        artist, title = split_query(query, tracks)
    found = [Candidate(t, is_confident(t, artist, title, duration_secs)) for t in tracks]
    if any(c.confident for c in found) or not artist:
        return _closest_first(found, duration_secs), artist, title

    seen = set()
    from_mb = []
    for t in musicbrainz.search_recordings(artist, title):
        key = (fold(t.artist), fold(t.title), t.duration_secs)
        if key not in seen and is_confident(t, artist, title, duration_secs):
            seen.add(key)
            from_mb.append(Candidate(t, True))
    from_mb = _closest_first(from_mb, duration_secs)[:MUSICBRAINZ_MAX_CANDIDATES]
    return from_mb + found, artist, title


def track_from_file(result, artist: str, title: str) -> TrackInfo:
    """A Soulseek copy as a track: artist and title from its file name ("01 - Artist - Title.flac").

    A name without " - " gives the title only; the artist is then the asked one.
    """
    stem = result.basename.rsplit(".", 1)[0]
    stem = _TRACK_NUMBER_RE.sub("", stem).strip()
    file_artist, file_title = stem.split(" - ", 1) if " - " in stem else (artist, stem)
    return TrackInfo(
        artist=file_artist.strip() or artist,
        title=file_title.strip() or title,
        album="",
        duration_ms=int(result.length or 0) * 1000,
        spotify_url="",
        year="",
        source=SOURCE_SOULSEEK,
        source_id=f"soulseek:{result.username}:{result.filename}",
    )


async def soulseek_candidate(
    search: Callable[[str], Awaitable[list]],
    rank: Callable[[list, TrackInfo], RankedResults],
    artist: str,
    title: str,
    duration_secs: float | None = None,
) -> Candidate | None:
    """The best Soulseek copy of *title* by *artist* as a candidate, or None when Soulseek has none.

    *search* runs one slskd search and returns its raw responses; *rank* ranks them for a track.
    The candidate keeps the responses, so the copies need no second search.
    """
    responses = await search(f"{artist} {clean_search_title(title)}")
    if not responses:
        return None
    asked = synthetic_track(artist, title)
    if duration_secs:
        asked.duration_ms = int(duration_secs * 1000)
    ranked = rank(responses, asked)
    if not ranked:
        return None
    return Candidate(track_from_file(ranked[0], artist, title), False, responses)


def parse_query_artist_title(query: str) -> tuple[str, str]:
    """Parse a free-text query into (artist, title) with title-casing.

    Tries " - " separator first. Otherwise assumes last word is title,
    rest is artist (e.g. "david bowie helden" → "David Bowie", "Helden").
    """
    if " - " in query:
        parts = query.split(" - ", 1)
        return parts[0].strip().title(), parts[1].strip().title()
    words = query.strip().split()
    if len(words) >= 3:
        return " ".join(words[:-1]).title(), words[-1].title()
    if len(words) == 2:
        return words[0].title(), words[1].title()
    return "", query.title()


def synthetic_track(artist: str, title: str, album: str = "", year: str = "") -> TrackInfo:
    """A track with no Spotify data (direct Soulseek search): duration 0 means "unknown"."""
    return TrackInfo(
        artist=artist,
        title=title,
        album=album,
        duration_ms=0,
        spotify_url="",
        year=year,
    )
