"""
Scoring engine for ranking slskd search results.
Scores results based on duration match, audio quality, source reliability,
and filename analysis to filter out unwanted versions.
"""

import logging
import re

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

logger = logging.getLogger(__name__)


class ResultScorer:
    """Scores and ranks slskd search results against a Spotify track."""

    def __init__(
        self,
        duration_tolerance_secs: int = 5,
        exclude_keywords: list[str] | None = None,
    ):
        self.duration_tolerance = duration_tolerance_secs
        self.exclude_keywords = exclude_keywords or [
            "live",
            "remix",
            "acoustic",
            "karaoke",
            "instrumental",
            "cover",
            "demo",
            "radio edit",
            "tribute",
        ]

    def score_results(
        self,
        results: list[SearchResult],
        track: TrackInfo,
        max_duration_diff: int | None = None,
    ) -> list[SearchResult]:
        """
        Score and rank search results against the reference track.
        Filters out unwanted results and sorts by score (highest first).

        Args:
            results: FLAC search results from slskd.
            track: Reference track info from Spotify.
            max_duration_diff: Override the hard duration cutoff (seconds).
                When set, results beyond the normal 30 s tolerance but
                within this limit receive 0 duration points instead of
                being excluded.  Useful for fallback searches where a
                different version of the same song is acceptable.

        Returns:
            Filtered and sorted list of SearchResult with scores assigned.
        """
        scored = []

        for result in results:
            score = self._calculate_score(result, track, max_duration_diff)
            if score is not None:
                result.score = score
                scored.append(result)

        # Sort by score descending
        scored.sort(key=lambda r: r.score, reverse=True)

        # Deduplicate by basename (keep highest score)
        seen_basenames = set()
        deduplicated = []
        for result in scored:
            basename_key = result.basename.lower()
            if basename_key not in seen_basenames:
                seen_basenames.add(basename_key)
                deduplicated.append(result)

        logger.info(f"Scored {len(scored)} results, {len(deduplicated)} after dedup (from {len(results)} total)")
        return deduplicated

    def _calculate_score(
        self, result: SearchResult, track: TrackInfo, max_duration_diff: int | None = None
    ) -> float | None:
        """
        Calculate a score for a single result.

        Returns:
            Score (0-100), or None if the result should be excluded.
        """
        score = 0.0

        # ===== EXCLUDE FILTER =====
        filename_lower = result.filename.lower()
        basename_lower = result.basename.lower()

        for keyword in self.exclude_keywords:
            if keyword in basename_lower:
                if keyword.lower() not in track.title.lower():
                    logger.debug(f"Excluded (keyword '{keyword}'): {result.basename}")
                    return None

        # ===== DURATION MATCH (0-40 points) =====
        if result.length is not None and result.length > 0:
            target_secs = track.duration_secs
            diff = abs(result.length - target_secs)

            if diff <= self.duration_tolerance:
                score += 40.0 - (diff * 2)
            elif diff <= 10:
                score += 25.0 - (diff - self.duration_tolerance) * 3
            elif diff <= 30:
                score += max(0.0, 10.0 - (diff - 10) * 0.5)
            elif max_duration_diff is not None and diff <= max_duration_diff:
                pass  # 0 duration points — different version, still acceptable
            else:
                logger.debug(f"Excluded (duration {result.length}s vs {target_secs}s): {result.basename}")
                return None
        else:
            score += 15.0

        # ===== AUDIO QUALITY (0-25 points) =====
        # Prefer 16-bit/44.1kHz (CD quality) for consistency
        if result.bit_depth:
            if result.bit_depth == 16:
                score += 15.0  # Standard CD quality — preferred
            elif result.bit_depth == 24:
                score += 12.0  # Hi-res — good but larger files
            else:
                score += 5.0

        if result.sample_rate:
            if result.sample_rate == 44100:
                score += 10.0
            elif result.sample_rate in (48000, 96000, 88200):
                score += 7.0
            else:
                score += 3.0

        # ===== SOURCE RELIABILITY (0-20 points) =====
        if result.has_free_slot:
            score += 10.0  # Free slot means faster download

        if result.upload_speed > 0:
            # Normalize speed (cap at 10MB/s for scoring)
            speed_score = min(result.upload_speed / 1_000_000, 10) * 1.0
            score += speed_score

        if result.queue_length == 0:
            score += 5.0
        elif result.queue_length < 5:
            score += 2.0

        # ===== FILENAME RELEVANCE (0-15 points) =====
        # Boost results that contain the artist and title in the filename
        artist_lower = track.artist.lower()
        title_lower = track.title.lower()

        # Simple word matching
        artist_words = set(re.findall(r"\w+", artist_lower))
        title_words = set(re.findall(r"\w+", title_lower))
        filename_words = set(re.findall(r"\w+", filename_lower))

        artist_match = len(artist_words & filename_words) / max(len(artist_words), 1)
        title_match = len(title_words & filename_words) / max(len(title_words), 1)

        score += artist_match * 7.5
        score += title_match * 7.5

        return round(score, 2)
