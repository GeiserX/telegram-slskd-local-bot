"""Naming follow-ups found reviewing 0.17.0: WAV/AIFF tags, format-aware duplicates, {track} cleanup, parsing."""

import pytest

from music_downloader.persistence.database import Database
from music_downloader.persistence.library_index import LibraryIndex
from music_downloader.pipeline.album import parse_filename, read_tags
from music_downloader.processor.file_handler import FileProcessor


@pytest.mark.parametrize("ext,fmt", [("wav", "WAV"), ("aiff", "AIFF")])
def test_wav_and_aiff_id3_tags_are_read(tmp_path, ext, fmt):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    import mutagen.aiff
    import mutagen.id3
    import mutagen.wave

    path = tmp_path / f"Track06.{ext}"
    sf.write(str(path), np.zeros(8000, dtype="float32"), 8000, format=fmt, subtype="PCM_16")
    f = mutagen.wave.WAVE(str(path)) if ext == "wav" else mutagen.aiff.AIFF(str(path))
    f.add_tags()
    f.tags.add(mutagen.id3.TPE1(encoding=3, text=["Pink Floyd"]))
    f.tags.add(mutagen.id3.TIT2(encoding=3, text=["Echoes"]))
    f.tags.add(mutagen.id3.TALB(encoding=3, text=["Meddle"]))
    f.tags.add(mutagen.id3.TRCK(encoding=3, text=["6/6"]))
    f.save()
    tags, length_ms = read_tags(str(path))
    assert tags["artist"] == "Pink Floyd" and tags["title"] == "Echoes"
    assert tags["album"] == "Meddle" and tags["tracknumber"] == "6/6"
    assert length_ms == 1000


def test_find_stem_can_insist_on_a_format(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "Pink Floyd - Echoes.mp3").write_bytes(b"x")
    idx = LibraryIndex(Database(str(tmp_path / "db" / "importer.db")), str(root))
    idx.rebuild()
    assert idx.find_stem("Pink Floyd - Echoes") == "Pink Floyd - Echoes.mp3"  # name only, as before
    assert idx.find_stem("Pink Floyd - Echoes", "flac") is None
    assert idx.find_stem("Pink Floyd - Echoes", "mp3") == "Pink Floyd - Echoes.mp3"


@pytest.mark.asyncio
async def test_library_mp3_does_not_make_the_album_flac_a_duplicate(tmp_path):
    from tests.test_album import TRACK, _pipeline

    pipeline = _pipeline(tmp_path)
    music = tmp_path / "music"
    music.mkdir(parents=True, exist_ok=True)
    (music / "Pink Floyd - Echoes.mp3").write_bytes(b"lossy")
    pipeline.library_index.rebuild()
    # The FLAC is new to the library: the lossy copy must not count.
    assert await pipeline.library_copy("/downloads/x/06 - Echoes.flac", TRACK) is None
    # The reverse still counts: a library FLAC makes the album MP3 a duplicate...
    (music / "Pink Floyd - Echoes.mp3").unlink()
    (music / "Pink Floyd - Echoes.flac").write_bytes(b"lossless")
    pipeline.library_index.rebuild()
    assert (await pipeline.library_copy("/downloads/x/06 - Echoes.mp3", TRACK)) == str(
        music / "Pink Floyd - Echoes.flac"
    )
    # ...and so does the same format.
    assert (await pipeline.library_copy("/downloads/x/06 - Echoes.flac", TRACK)) == str(
        music / "Pink Floyd - Echoes.flac"
    )


@pytest.mark.parametrize(
    "template,artist,title,expected",
    [
        ("{track} {artist} - {title}", "-M-", "Machistador", "-M- - Machistador.flac"),
        ("{artist} - {title} ({track})", "A", "T", "A - T.flac"),
        ("{artist} - {track}. {title}", "A", "T", "A - T.flac"),
        ("{artist} - {title} - {track}", "A", "T", "A - T.flac"),
        ("{track} - {artist} - {title}", "A", "T", "A - T.flac"),
    ],
)
def test_missing_track_number_never_touches_artist_or_title(tmp_path, template, artist, title, expected):
    proc = FileProcessor(str(tmp_path / "d"), str(tmp_path / "m"), filename_template=template)
    assert proc.build_filename(artist, title, "flac") == expected
    assert "03" in proc.build_filename(artist, title, "flac", 3)


def test_artist_album_number_title_parses():
    assert parse_filename("Artist - Album - 03 - Title.mp3") == ("Artist - Album", "Title", 3)
    assert parse_filename("Artist - 03 - Title.mp3") == ("Artist", "Title", 3)
    assert parse_filename("Artist - Title.mp3") == ("Artist", "Title", None)
