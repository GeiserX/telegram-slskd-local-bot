"""Audio formats the bot accepts, split into lossless and lossy.

This is the one place that says which extension is which; the search parser,
the ranker and the library duplicate check all derive from these sets.
"""

LOSSLESS_EXTENSIONS = frozenset({"flac", "alac", "wav", "aiff", "aif", "ape", "wv", "tta", "tak"})

# m4a counts as lossy: the extension alone cannot tell ALAC from AAC, and AAC
# is by far the common case on Soulseek.
LOSSY_EXTENSIONS = frozenset({"mp3", "aac", "m4a", "ogg", "opus", "wma"})

AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | LOSSY_EXTENSIONS


def is_lossless(extension: str) -> bool:
    """True when *extension* (with or without the dot, any case) is a lossless format."""
    return extension.lower().lstrip(".") in LOSSLESS_EXTENSIONS
