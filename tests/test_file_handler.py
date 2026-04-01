"""Tests for the file processor."""

import os

import mutagen.flac
import pytest

from music_downloader.processor.file_handler import FileProcessor


class TestFileProcessor:
    """Tests for FileProcessor."""

    @pytest.fixture
    def processor(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        return FileProcessor(str(download_dir), str(output_dir))

    def test_build_filename(self, processor):
        """Test standard filename building."""
        result = processor.build_filename("Nancy Sinatra", "Bang Bang (My Baby Shot Me Down)")
        assert result == "Nancy Sinatra - Bang Bang (My Baby Shot Me Down).flac"

    def test_build_filename_sanitizes(self, processor):
        """Test that invalid characters are removed."""
        result = processor.build_filename('Artist: "Test"', "Song?Name*Here")
        assert ":" not in result
        assert '"' not in result
        assert "?" not in result
        assert "*" not in result

    def test_find_downloaded_file(self, processor, tmp_path):
        """Test finding a downloaded file by username and remote path."""
        # Create a fake downloaded file
        user_dir = tmp_path / "downloads" / "someuser"
        user_dir.mkdir()
        fake_file = user_dir / "song.flac"
        fake_file.write_text("fake flac data")

        result = processor.find_downloaded_file("someuser", "\\Music\\Artist\\song.flac")
        assert result is not None
        assert result.endswith("song.flac")

    def test_find_downloaded_file_not_found(self, processor):
        """Test that None is returned when file doesn't exist."""
        result = processor.find_downloaded_file("nobody", "\\Music\\nonexistent.flac")
        assert result is None

    def test_process_file(self, processor, tmp_path):
        """Test renaming and moving a file."""
        # Create a source file
        source = tmp_path / "source.flac"
        source.write_text("fake flac data")

        result = processor.process_file(str(source), "Nancy Sinatra", "Bang Bang")
        assert result is not None
        assert os.path.exists(result)
        assert "Nancy Sinatra - Bang Bang.flac" in result

    def test_process_file_deduplicates_flac_atomically(self, processor, tmp_path):
        """Test that process_file deduplicates tags before the file is visible in output."""
        source = tmp_path / "downloads" / "dup.flac"
        _create_test_flac(str(source), {
            "artist": ["Le Tigre", "Le Tigre"],
            "title": ["Deceptacon", "Deceptacon"],
            "genre": ["Punk", "Electronic"],
        })

        result = processor.process_file(str(source), "Le Tigre", "Deceptacon")
        assert result is not None
        assert os.path.exists(result)
        # No temp file left behind
        assert not os.path.exists(result + ".importing")

        audio = mutagen.flac.FLAC(result)
        assert audio["artist"] == ["Le Tigre"]
        assert audio["title"] == ["Deceptacon"]
        # Legitimate multi-value preserved
        assert audio["genre"] == ["Punk", "Electronic"]

    def test_process_file_avoids_overwrite(self, processor, tmp_path):
        """Test that existing files are not overwritten."""
        source1 = tmp_path / "source1.flac"
        source2 = tmp_path / "source2.flac"
        source1.write_text("data1")
        source2.write_text("data2")

        result1 = processor.process_file(str(source1), "Artist", "Song")
        result2 = processor.process_file(str(source2), "Artist", "Song")

        assert result1 != result2
        assert os.path.exists(result1)
        assert os.path.exists(result2)

    def test_cleanup_download(self, processor, tmp_path):
        """Test cleaning up downloaded files."""
        source = tmp_path / "cleanup_test.flac"
        source.write_text("data")

        assert os.path.exists(str(source))
        processor.cleanup_download(str(source))
        assert not os.path.exists(str(source))

    def test_dedup_flac_tags_removes_exact_duplicates(self, tmp_path):
        """Test that exact duplicate tag values are removed."""
        flac_path = str(tmp_path / "test.flac")
        _create_test_flac(flac_path, {"artist": ["Le Tigre", "Le Tigre"], "title": ["Deceptacon", "Deceptacon"]})

        FileProcessor._dedup_flac_tags(flac_path)

        audio = mutagen.flac.FLAC(flac_path)
        assert audio["artist"] == ["Le Tigre"]
        assert audio["title"] == ["Deceptacon"]

    def test_dedup_flac_tags_preserves_legitimate_multi_values(self, tmp_path):
        """Test that different multi-value tags are kept."""
        flac_path = str(tmp_path / "test.flac")
        _create_test_flac(flac_path, {"artist": ["Bad Computer", "Danyka Nadeau"], "genre": ["EDM", "Drum and Bass"]})

        FileProcessor._dedup_flac_tags(flac_path)

        audio = mutagen.flac.FLAC(flac_path)
        assert audio["artist"] == ["Bad Computer", "Danyka Nadeau"]
        assert audio["genre"] == ["EDM", "Drum and Bass"]

    def test_dedup_flac_tags_mixed(self, tmp_path):
        """Test mix of duplicates and legitimate multi-values."""
        flac_path = str(tmp_path / "test.flac")
        _create_test_flac(flac_path, {
            "artist": ["Coldplay", "Coldplay"],
            "genre": ["Rock", "Alternative", "Rock"],
        })

        FileProcessor._dedup_flac_tags(flac_path)

        audio = mutagen.flac.FLAC(flac_path)
        assert audio["artist"] == ["Coldplay"]
        assert audio["genre"] == ["Rock", "Alternative"]

    def test_dedup_flac_tags_skips_non_flac(self, tmp_path):
        """Test that non-FLAC files are silently skipped."""
        mp3_path = str(tmp_path / "test.mp3")
        with open(mp3_path, "wb") as f:
            f.write(b"\x00" * 100)
        # Should not raise (mutagen will fail to parse, caught by try/except)
        FileProcessor._dedup_flac_tags(mp3_path)

    def test_sanitize_filename(self):
        """Test filename sanitization."""
        assert FileProcessor._sanitize_filename('Hello: "World"') == "Hello World"
        assert FileProcessor._sanitize_filename("a/b\\c") == "abc"
        assert FileProcessor._sanitize_filename("  spaces  ") == "spaces"
        assert FileProcessor._sanitize_filename("multiple   spaces") == "multiple spaces"


def _create_test_flac(path: str, tags: dict[str, list[str]]) -> None:
    """Create a minimal valid FLAC file with the given Vorbis comment tags."""
    import struct

    # Valid STREAMINFO: 44100 Hz, stereo, 16-bit, 0 samples
    streaminfo = bytes([
        0x10, 0x00, 0x10, 0x00,        # min/max blocksize = 4096
        0x00, 0x00, 0x00,              # min framesize
        0x00, 0x00, 0x00,              # max framesize
        0x0A, 0xC4, 0x42, 0xF0,        # sample_rate=44100, channels=2, bps=16
        0x00, 0x00, 0x00, 0x00,        # total samples
    ] + [0x00] * 16)                    # MD5

    streaminfo_header = struct.pack(">I", (0x00 << 24) | 34)
    padding_header = struct.pack(">I", (0x81 << 24) | 0)

    with open(path, "wb") as f:
        f.write(b"fLaC")
        f.write(streaminfo_header)
        f.write(streaminfo)
        f.write(padding_header)

    audio = mutagen.flac.FLAC(path)
    for key, values in tags.items():
        audio[key] = values
    audio.save()
