"""Turn a free-text query into Spotify track candidates, or into a synthetic track."""

from music_downloader.metadata.spotify import SpotifyResolver, TrackInfo


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
        query_artist = query.split(" - ", 1)[0].strip().lower()

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
        if query_artist and query_artist not in artist_lower:
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
