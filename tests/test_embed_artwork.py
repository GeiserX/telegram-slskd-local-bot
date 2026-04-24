"""Tests for embed_artwork module."""

import struct
from unittest.mock import MagicMock, patch

import mutagen.flac
import mutagen.mp4

from music_downloader.tools.embed_artwork import embed_artwork_into_file, fetch_spotify_artwork


def _create_test_flac(path: str, with_art: bool = False) -> None:
    """Create a minimal valid FLAC file."""
    streaminfo = bytes(
        [
            0x10,
            0x00,
            0x10,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x0A,
            0xC4,
            0x42,
            0xF0,
            0x00,
            0x00,
            0x00,
            0x00,
        ]
        + [0x00] * 16
    )
    streaminfo_header = struct.pack(">I", (0x00 << 24) | 34)
    padding_header = struct.pack(">I", (0x81 << 24) | 0)
    with open(path, "wb") as f:
        f.write(b"fLaC")
        f.write(streaminfo_header)
        f.write(streaminfo)
        f.write(padding_header)

    if with_art:
        audio = mutagen.flac.FLAC(path)
        pic = mutagen.flac.Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        audio.add_picture(pic)
        audio.save()


class TestFetchSpotifyArtwork:
    def test_returns_bytes_on_success(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {
            "tracks": {"items": [{"album": {"images": [{"url": "https://example.com/art.jpg"}]}}]}
        }
        with patch("music_downloader.tools.embed_artwork.httpx") as mock_httpx:
            mock_resp = MagicMock()
            mock_resp.content = b"\xff\xd8\xff\xe0JFIF"
            mock_resp.raise_for_status = MagicMock()
            mock_httpx.get.return_value = mock_resp
            result = fetch_spotify_artwork(mock_sp, "Artist", "Title")
            assert result == b"\xff\xd8\xff\xe0JFIF"

    def test_returns_none_no_tracks(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {"tracks": {"items": []}}
        result = fetch_spotify_artwork(mock_sp, "Artist", "Title")
        assert result is None

    def test_returns_none_no_images(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {"tracks": {"items": [{"album": {"images": []}}]}}
        result = fetch_spotify_artwork(mock_sp, "Artist", "Title")
        assert result is None

    def test_returns_none_on_exception(self):
        mock_sp = MagicMock()
        mock_sp.search.side_effect = Exception("API error")
        result = fetch_spotify_artwork(mock_sp, "Artist", "Title")
        assert result is None


class TestEmbedArtworkIntoFile:
    def test_embed_into_flac(self, tmp_path):
        flac_path = str(tmp_path / "test.flac")
        _create_test_flac(flac_path)
        image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        result = embed_artwork_into_file(flac_path, image_data)
        assert result is True
        audio = mutagen.flac.FLAC(flac_path)
        assert len(audio.pictures) == 1

    def test_skip_flac_with_existing_art(self, tmp_path):
        flac_path = str(tmp_path / "test.flac")
        _create_test_flac(flac_path, with_art=True)
        image_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        result = embed_artwork_into_file(flac_path, image_data)
        assert result is False

    def test_unsupported_format(self, tmp_path):
        txt_path = str(tmp_path / "test.txt")
        with open(txt_path, "w") as f:
            f.write("not audio")
        result = embed_artwork_into_file(txt_path, b"\x00")
        assert result is False

    def test_embed_into_m4a(self, tmp_path):
        """Test embedding into M4A (mocked since creating valid M4A is complex)."""
        m4a_path = str(tmp_path / "test.m4a")
        with open(m4a_path, "wb") as f:
            f.write(b"\x00" * 100)

        with patch("music_downloader.tools.embed_artwork.mutagen.mp4.MP4") as mock_mp4:
            mock_file = MagicMock()
            mock_file.tags = {}
            mock_mp4.return_value = mock_file
            result = embed_artwork_into_file(m4a_path, b"\xff\xd8\xff\xe0")
            assert result is True

    def test_embed_m4a_with_existing_art(self, tmp_path):
        m4a_path = str(tmp_path / "test.m4a")
        with open(m4a_path, "wb") as f:
            f.write(b"\x00" * 100)

        with patch("music_downloader.tools.embed_artwork.mutagen.mp4.MP4") as mock_mp4:
            mock_file = MagicMock()
            tags_mock = MagicMock()
            tags_mock.get.return_value = [b"existing"]
            tags_mock.__bool__ = lambda self: True
            mock_file.tags = tags_mock
            mock_mp4.return_value = mock_file
            result = embed_artwork_into_file(m4a_path, b"\xff\xd8\xff\xe0")
            assert result is False

    def test_exception_returns_false(self, tmp_path):
        result = embed_artwork_into_file("/nonexistent/path.flac", b"\x00")
        assert result is False

    def test_no_extension(self, tmp_path):
        path = str(tmp_path / "noext")
        with open(path, "w") as f:
            f.write("data")
        result = embed_artwork_into_file(path, b"\x00")
        assert result is False
