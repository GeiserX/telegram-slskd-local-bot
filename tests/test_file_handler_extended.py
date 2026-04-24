"""Extended tests for file_handler - covering find_similar and edge cases."""

import pytest

from music_downloader.processor.file_handler import FileProcessor


class TestFindSimilar:
    @pytest.fixture
    def processor(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        return FileProcessor(str(download_dir), str(output_dir))

    def test_no_output_dir(self, tmp_path):
        p = FileProcessor(str(tmp_path / "dl"), str(tmp_path / "nonexistent"))
        result = p.find_similar("test")
        assert result == []

    def test_finds_similar_files(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Nancy Sinatra - Bang Bang.flac").write_text("data")
        (output_dir / "Nancy Sinatra - Summer Wine.flac").write_text("data")
        result = processor.find_similar("Nancy Sinatra Bang Bang")
        assert len(result) > 0
        assert "Nancy Sinatra - Bang Bang.flac" in result

    def test_no_similar_files(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Completely Different.flac").write_text("data")
        result = processor.find_similar("Nancy Sinatra Bang Bang")
        assert result == []

    def test_ignores_non_audio(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Nancy Sinatra - Bang Bang.txt").write_text("data")
        (output_dir / "Nancy Sinatra - Bang Bang.jpg").write_text("data")
        result = processor.find_similar("Nancy Sinatra Bang Bang")
        assert result == []

    def test_sorted_by_similarity(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Nancy Sinatra - Bang Bang.flac").write_text("data")
        (output_dir / "Nancy Sinatra - Something Else.flac").write_text("data")
        result = processor.find_similar("Nancy Sinatra Bang Bang")
        if len(result) >= 2:
            # Best match should be first
            assert "Bang Bang" in result[0]

    def test_various_audio_extensions(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Song.mp3").write_text("data")
        (output_dir / "Song.ogg").write_text("data")
        (output_dir / "Song.wav").write_text("data")
        result = processor.find_similar("Song")
        assert len(result) == 3

    def test_empty_query(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Song.flac").write_text("data")
        result = processor.find_similar("")
        # Empty query should not match well (low similarity)
        # but should not crash
        assert isinstance(result, list)

    def test_custom_threshold(self, processor, tmp_path):
        output_dir = tmp_path / "output"
        (output_dir / "Nancy Sinatra - Bang Bang.flac").write_text("data")
        # High threshold should be stricter
        result_high = processor.find_similar("Nancy Sinatra Bang Bang", threshold=0.9)
        result_low = processor.find_similar("Nancy Sinatra Bang Bang", threshold=0.3)
        assert len(result_low) >= len(result_high)


class TestFindDownloadedFileFallback:
    def test_fallback_search(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        # File under a different username directory
        other_dir = download_dir / "otheruser" / "subdir"
        other_dir.mkdir(parents=True)
        (other_dir / "song.flac").write_text("data")

        processor = FileProcessor(str(download_dir), str(output_dir))
        # Search with wrong username should still find via fallback
        result = processor.find_downloaded_file("wronguser", "\\Music\\song.flac")
        assert result is not None
        assert result.endswith("song.flac")


class TestProcessFileEdgeCases:
    def test_process_nonexistent_file(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        processor = FileProcessor(str(download_dir), str(output_dir))
        result = processor.process_file("/nonexistent/file.flac", "Artist", "Title")
        assert result is None

    def test_process_file_no_extension(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        source = tmp_path / "sourcefile"
        source.write_text("data")
        processor = FileProcessor(str(download_dir), str(output_dir))
        result = processor.process_file(str(source), "Artist", "Title")
        assert result is not None


class TestCleanupDownload:
    def test_cleanup_removes_empty_parent(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        parent = tmp_path / "downloads" / "user" / "subdir"
        parent.mkdir(parents=True)
        source = parent / "song.flac"
        source.write_text("data")
        processor = FileProcessor(str(download_dir), str(output_dir))
        processor.cleanup_download(str(source))
        assert not source.exists()
        assert not parent.exists()

    def test_cleanup_nonexistent(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        processor = FileProcessor(str(download_dir), str(output_dir))
        result = processor.cleanup_download("/nonexistent/file.flac")
        assert result is False

    def test_build_filename_custom_template(self, tmp_path):
        download_dir = tmp_path / "downloads"
        output_dir = tmp_path / "output"
        download_dir.mkdir()
        output_dir.mkdir()
        processor = FileProcessor(str(download_dir), str(output_dir), filename_template="{title} by {artist}")
        result = processor.build_filename("Artist", "Song", "mp3")
        assert result == "Song by Artist.mp3"
