"""
Scoring engine for ranking slskd search results.
Scores results based on duration match, audio quality, source reliability,
and filename analysis to filter out unwanted versions.
"""

import logging
import math
import re

from music_downloader.config import BYTES_PER_MB, DEFAULT_UPLOAD_LIMIT_BYTES
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

logger = logging.getLogger(__name__)

# Scoring weights
DURATION_MAX_POINTS = 40.0
DURATION_CLOSE_POINTS = 25.0
DURATION_FLAT_POINTS = 15.0  # only when the track's own length is unknown (direct search)
QUALITY_HIRES_POINTS = 15.0
QUALITY_CD_POINTS = 10.0
SAMPLE_RATE_HIRES_POINTS = 10.0
SAMPLE_RATE_CD_POINTS = 5.0
SLOT_AVAILABLE_POINTS = 7.5
SPEED_MAX_POINTS = 7.5
QUEUE_MAX_POINTS = 5.0
# A result further than this from the track's length is a different version
# (live, extended, radio edit). It earns no duration points, is kept only when
# a caller passes max_duration_diff, and never jumps the lossless-first order.
SAME_VERSION_MAX_DIFF_SECS = 30

# Ranking profiles. "library" keeps the original hi-res preference; "chat" (chat
# delivery, files sent into Telegram) trades the 25 audio-quality points for
# perceived quality versus size: a small transparent file is the best buy.
PROFILE_LIBRARY = "library"
PROFILE_CHAT = "chat"

# Perceived-quality tiers, used by the chat profile and for lossy files in the
# library profile (bit depth and sample rate say nothing about a lossy file).
PERCEIVED_TOP_POINTS = 25.0  # lossless, or lossy at 256 kbps or more: transparent to most ears
PERCEIVED_TOP_KBPS = 256
PERCEIVED_GOOD_POINTS = 20.0  # 192-255 kbps: a step below transparent
PERCEIVED_GOOD_KBPS = 192
PERCEIVED_FAIR_POINTS = 10.0  # 128-191 kbps: audibly worse on good gear
PERCEIVED_FAIR_KBPS = 128
PERCEIVED_POOR_POINTS = 1.0  # under 128 kbps: near zero
PERCEIVED_UNKNOWN_POINTS = PERCEIVED_FAIR_POINTS  # lossy with no bitrate and no way to estimate one
# Codecs are not equal per kbps. A lossy file's bitrate is first scaled to the
# MP3 bitrate it sounds like, then tiered: Opus 128 counts as MP3 256 (top),
# AAC or Vorbis 128 as MP3 192 (good). An .ogg may hold Opus or Vorbis and the
# extension cannot tell, so it takes the lower Vorbis factor.
CODEC_MP3_EQUIVALENT = {"opus": 2.0, "aac": 1.5, "m4a": 1.5, "ogg": 1.5, "wma": 1.0, "mp3": 1.0}

# One ordered quality scale, lowest first (quality_tier). Lossy copies go by
# their MP3-equivalent bitrate, on the same thresholds as the perceived points
# above; lossless copies by bit depth. The wishlist's "a better copy" means a
# strictly higher tier.
TIER_LOSSY_UNDER_128 = 0
TIER_LOSSY_128 = 1
TIER_LOSSY_192 = 2
TIER_LOSSY_256 = 3
TIER_LOSSLESS_16 = 4
TIER_LOSSLESS_24 = 5
TIER_LABELS = {
    TIER_LOSSY_UNDER_128: "lossy under 128 kbps",
    TIER_LOSSY_128: "lossy 128 kbps",
    TIER_LOSSY_192: "lossy 192 kbps",
    TIER_LOSSY_256: "lossy 256+ kbps",
    TIER_LOSSLESS_16: "lossless 16-bit",
    TIER_LOSSLESS_24: "lossless 24-bit",
}
_LOSSY_TIER_POINTS = {
    TIER_LOSSY_UNDER_128: PERCEIVED_POOR_POINTS,
    TIER_LOSSY_128: PERCEIVED_FAIR_POINTS,
    TIER_LOSSY_192: PERCEIVED_GOOD_POINTS,
    TIER_LOSSY_256: PERCEIVED_TOP_POINTS,
}


def mp3_equivalent_kbps(result: SearchResult) -> float | None:
    """A lossy copy's bitrate scaled to the MP3 bitrate it sounds like, or None when unknown.

    A copy without a reported bitrate gets one estimated from its size and
    length (both known for nearly every Soulseek file).
    """
    kbps = result.bit_rate
    if not kbps and result.size and result.length:
        kbps = result.size * 8 / result.length / 1000
    if not kbps:
        return None
    return kbps * CODEC_MP3_EQUIVALENT.get(result.extension, 1.0)


def quality_tier(result: SearchResult) -> int:
    """Where *result* sits on the quality scale (TIER_*).

    Lossless: 24-bit or more is the top tier, anything else (16-bit, or no
    bit depth reported) the one below. Lossy: by MP3-equivalent bitrate; an
    unknown bitrate counts as 128, like PERCEIVED_UNKNOWN_POINTS.
    """
    if result.is_lossless:
        return TIER_LOSSLESS_24 if (result.bit_depth or 0) >= 24 else TIER_LOSSLESS_16
    kbps = mp3_equivalent_kbps(result)
    if kbps is None:
        return TIER_LOSSY_128
    if kbps >= PERCEIVED_TOP_KBPS:
        return TIER_LOSSY_256
    if kbps >= PERCEIVED_GOOD_KBPS:
        return TIER_LOSSY_192
    if kbps >= PERCEIVED_FAIR_KBPS:
        return TIER_LOSSY_128
    return TIER_LOSSY_UNDER_128


# Chat profile size cost, inside the range that fits Telegram's upload limit
# (ResultScorer's chat_size_limit_bytes, TELEGRAM_MAX_UPLOAD_MB in config).
CHAT_SIZE_FREE_BYTES = 5 * BYTES_PER_MB  # files this small cost nothing
CHAT_SIZE_PENALTY_MAX_POINTS = 5.0  # cost at the end of the curve, linear from the free size
# The curve ends at the upload limit or at 50 MB, whichever is lower. A local
# Bot API server raises the limit to 2000 MB; a fitting file above 50 MB then
# pays the full cost instead of a near-zero slice of a 2000 MB curve.
CHAT_SIZE_PENALTY_FULL_BYTES = 50 * BYTES_PER_MB
# Above 50 MB (only reachable with a local Bot API server) the cost keeps growing with the
# logarithm of the size, by this much more at the cap. Log, not linear: a 900 MB hi-res copy
# must lose to a 57 MB MP3 320 even when its peer has the best slot and speed (up to 15
# source points), and the first doubling above 50 MB must already cost something.
CHAT_SIZE_OVER_FULL_POINTS = 20.0
# A file over the limit is sent as Opus converted from it: scored as its own
# tier minus this, with no size cost (the Opus that goes out fits).
CHAT_OPUS_CONVERSION_POINTS = 8.0


class ResultScorer:
    """Scores and ranks slskd search results against a Spotify track."""

    def __init__(
        self,
        duration_tolerance_secs: int = 5,
        exclude_keywords: list[str] | None = None,
        chat_size_limit_bytes: int = DEFAULT_UPLOAD_LIMIT_BYTES,
    ):
        self.duration_tolerance = duration_tolerance_secs
        self.chat_size_limit = chat_size_limit_bytes
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
        profile: str = PROFILE_LIBRARY,
    ) -> list[SearchResult]:
        """
        Score and rank search results against the reference track.
        Filters out unwanted results and sorts by score (highest first).

        Args:
            results: Audio search results from slskd.
            track: Reference track info from Spotify.
            max_duration_diff: Override the hard duration cutoff (seconds).
                When set, results beyond the normal 30 s tolerance but
                within this limit receive 0 duration points instead of
                being excluded.  Useful for fallback searches where a
                different version of the same song is acceptable.
            profile: PROFILE_LIBRARY (hi-res preferred) or PROFILE_CHAT (perceived
                quality versus size, for files sent into Telegram).

        Returns:
            Filtered and sorted list of SearchResult with scores assigned.
        """
        scored = []

        for result in results:
            score = self._calculate_score(result, track, max_duration_diff, profile)
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
        self,
        result: SearchResult,
        track: TrackInfo,
        max_duration_diff: int | None = None,
        profile: str = PROFILE_LIBRARY,
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
        # A copy whose length is unknown even after estimating it earns
        # nothing here: it may be any track (see length_unknown).
        target_secs = track.duration_secs
        length = self.effective_length(result)
        if target_secs == 0:
            score += DURATION_FLAT_POINTS
        elif length is not None:
            diff = abs(length - target_secs)

            if diff <= self.duration_tolerance:
                score += DURATION_MAX_POINTS - (diff * 2)
            elif diff <= 10:
                score += DURATION_CLOSE_POINTS - (diff - self.duration_tolerance) * 3
            elif diff <= SAME_VERSION_MAX_DIFF_SECS:
                score += max(0.0, 10.0 - (diff - 10) * 0.5)
            elif max_duration_diff is not None and diff <= max_duration_diff:
                pass  # 0 duration points — different version, still acceptable
            else:
                logger.debug(f"Excluded (duration {length}s vs {target_secs}s): {result.basename}")
                return None

        # ===== AUDIO QUALITY (0-25 points) =====
        if profile == PROFILE_CHAT:
            score += self._chat_quality_points(result)
        elif not result.is_lossless:
            score += self._perceived_points(result)
        # Library, lossless: prefer hi-res, higher bit depth and sample rate score better
        elif result.bit_depth:
            if result.bit_depth >= 24:
                score += QUALITY_HIRES_POINTS  # Hi-res — preferred
            elif result.bit_depth == 16:
                score += QUALITY_CD_POINTS  # CD quality — good
            else:
                score += SAMPLE_RATE_CD_POINTS

        if profile != PROFILE_CHAT and result.is_lossless and result.sample_rate:
            if result.sample_rate >= 88200:
                score += SAMPLE_RATE_HIRES_POINTS  # 88.2kHz / 96kHz+ — preferred
            elif result.sample_rate == 48000:
                score += 7.0
            elif result.sample_rate == 44100:
                score += 6.0  # CD standard — still good
            else:
                score += 3.0

        # ===== SOURCE RELIABILITY (0-20 points) =====
        if result.has_free_slot:
            score += SLOT_AVAILABLE_POINTS

        if result.upload_speed > 0:
            # Normalize speed (cap at 10MB/s for scoring)
            speed_score = min(result.upload_speed / 1_000_000, 10) * (SPEED_MAX_POINTS / 10)
            score += speed_score

        if result.queue_length == 0:
            score += QUEUE_MAX_POINTS
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

        score += artist_match * SPEED_MAX_POINTS
        score += title_match * SPEED_MAX_POINTS

        return round(score, 2)

    @staticmethod
    def _perceived_points(result: SearchResult) -> float:
        """Perceived-quality points: lossless is top tier, lossy goes by its quality_tier.

        A lossy result whose bitrate is neither reported nor estimable gets
        PERCEIVED_UNKNOWN_POINTS.
        """
        if result.is_lossless:
            return PERCEIVED_TOP_POINTS
        if mp3_equivalent_kbps(result) is None:
            return PERCEIVED_UNKNOWN_POINTS
        return _LOSSY_TIER_POINTS[quality_tier(result)]

    def _chat_quality_points(self, result: SearchResult) -> float:
        """Chat profile: perceived quality minus a size cost ("best bang for buck").

        A fitting file pays up to CHAT_SIZE_PENALTY_MAX_POINTS, growing linearly
        from CHAT_SIZE_FREE_BYTES to the limit or CHAT_SIZE_PENALTY_FULL_BYTES,
        whichever is lower. When the limit is above that (a local Bot API
        server), the cost keeps growing with the logarithm of the size, by
        CHAT_SIZE_OVER_FULL_POINTS more at the limit. A file over the limit will be converted to Opus,
        so it scores as its tier minus the conversion.
        """
        points = self._perceived_points(result)
        if result.size > self.chat_size_limit:
            return max(0.0, points - CHAT_OPUS_CONVERSION_POINTS)
        curve_end = min(self.chat_size_limit, CHAT_SIZE_PENALTY_FULL_BYTES)
        over_free = max(0, min(result.size, curve_end) - CHAT_SIZE_FREE_BYTES)
        cost = CHAT_SIZE_PENALTY_MAX_POINTS * over_free / max(1, curve_end - CHAT_SIZE_FREE_BYTES)
        if self.chat_size_limit > curve_end and result.size > curve_end:
            ratio = math.log(min(result.size, self.chat_size_limit) / curve_end) / math.log(
                self.chat_size_limit / curve_end
            )
            cost += CHAT_SIZE_OVER_FULL_POINTS * ratio
        return max(0.0, points - cost)

    @staticmethod
    def effective_length(result: SearchResult) -> int | None:
        """The copy's length in seconds: reported, else estimated, else None.

        Peers often share files without a length. For a lossy file with a
        known bitrate the length follows from size and bitrate
        (size * 8 / (kbps * 1000)), close enough to compare with the track.
        A lossless file's size depends on how well it compressed, so it gets
        no estimate.
        """
        if result.length:
            return result.length
        if not result.is_lossless and result.bit_rate and result.size:
            return round(result.size * 8 / (result.bit_rate * 1000))
        return None

    @classmethod
    def length_unknown(cls, result: SearchResult, track: TrackInfo) -> bool:
        """True when the track has a length and the copy's cannot be known or estimated.

        Such a copy earns no duration points and may not lead the list: an
        unrelated file with no length used to outrank the right one.
        """
        return bool(track.duration_secs) and cls.effective_length(result) is None

    @classmethod
    def is_other_version(cls, result: SearchResult, track: TrackInfo) -> bool:
        """True when both lengths are known (or estimated) and differ by more than SAME_VERSION_MAX_DIFF_SECS."""
        length = cls.effective_length(result)
        if not track.duration_secs or length is None:
            return False
        return abs(length - track.duration_secs) > SAME_VERSION_MAX_DIFF_SECS
