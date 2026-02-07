"""Tests for the file processor."""

import os

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

    def test_sanitize_filename(self):
        """Test filename sanitization."""
        assert FileProcessor._sanitize_filename('Hello: "World"') == "Hello World"
        assert FileProcessor._sanitize_filename("a/b\\c") == "abc"
        assert FileProcessor._sanitize_filename("  spaces  ") == "spaces"
        assert FileProcessor._sanitize_filename("multiple   spaces") == "multiple spaces"
