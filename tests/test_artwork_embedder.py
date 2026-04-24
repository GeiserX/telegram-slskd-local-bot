"""Tests for batch artwork embedder module."""

import os
import struct
from unittest.mock import MagicMock, patch

import mutagen.flac
import pytest

from music_downloader.tools.artwork_embedder import (
    _download_image,
    _embed_flac,
    _embed_m4a,
    _fetch_spotify_artwork_url,
    _flac_has_art,
    _m4a_has_art,
    _parse_artist_title,
    _sibling_m4a_path,
    run,
)


def _create_test_flac(path: str, tags: dict | None = None, with_art: bool = False) -> None:
    """Create a minimal valid FLAC file."""
    streaminfo = bytes(
        [
            0x10, 0x00, 0x10, 0x00,
            0x00, 0x00, 0x00,
            0x00, 0x00, 0x00,
            0x0A, 0xC4, 0x42, 0xF0,
            0x00, 0x00, 0x00, 0x00,
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

    audio = mutagen.flac.FLAC(path)
    if tags:
        for k, v in tags.items():
            audio[k] = v
    if with_art:
        pic = mutagen.flac.Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        audio.add_picture(pic)
    audio.save()


class TestParseArtistTitle:
    def test_standard_format(self):
        result = _parse_artist_title("Nancy Sinatra - Bang Bang.flac")
        assert result == ("Nancy Sinatra", "Bang Bang")

    def test_with_path(self):
        result = _parse_artist_title("/music/Nancy Sinatra - Bang Bang.flac")
        assert result == ("Nancy Sinatra", "Bang Bang")

    def test_no_separator(self):
        result = _parse_artist_title("JustAFilename.flac")
        assert result is None

    def test_multiple_dashes(self):
        result = _parse_artist_title("Artist - Title - Extra.flac")
        assert result is not None
        assert result[0] == "Artist"


class TestFlacHasArt:
    def test_has_art(self, tmp_path):
        path = str(tmp_path / "art.flac")
        _create_test_flac(path, with_art=True)
        assert _flac_has_art(path) is True

    def test_no_art(self, tmp_path):
        path = str(tmp_path / "noart.flac")
        _create_test_flac(path, with_art=False)
        assert _flac_has_art(path) is False

    def test_invalid_file(self, tmp_path):
        path = str(tmp_path / "bad.flac")
        with open(path, "w") as f:
            f.write("not a flac")
        assert _flac_has_art(path) is False


class TestM4aHasArt:
    def test_invalid_file(self, tmp_path):
        path = str(tmp_path / "bad.m4a")
        with open(path, "w") as f:
            f.write("not m4a")
        assert _m4a_has_art(path) is False

    def test_with_art(self):
        with patch("music_downloader.tools.artwork_embedder.mutagen.mp4.MP4") as mock:
            mock_file = MagicMock()
            tags_mock = MagicMock()
            tags_mock.get.return_value = [b"data"]
            mock_file.tags = tags_mock
            mock.return_value = mock_file
            assert _m4a_has_art("test.m4a") is True

    def test_no_art(self):
        with patch("music_downloader.tools.artwork_embedder.mutagen.mp4.MP4") as mock:
            mock_file = MagicMock()
            tags_mock = MagicMock()
            tags_mock.get.return_value = []
            mock_file.tags = tags_mock
            mock.return_value = mock_file
            assert _m4a_has_art("test.m4a") is False


class TestFetchSpotifyArtworkUrl:
    def test_returns_url(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {
            "tracks": {
                "items": [
                    {"album": {"images": [{"url": "https://i.scdn.co/image/abc"}]}}
                ]
            }
        }
        result = _fetch_spotify_artwork_url(mock_sp, "Artist", "Title")
        assert result == "https://i.scdn.co/image/abc"

    def test_no_tracks(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {"tracks": {"items": []}}
        assert _fetch_spotify_artwork_url(mock_sp, "A", "T") is None

    def test_no_images(self):
        mock_sp = MagicMock()
        mock_sp.search.return_value = {
            "tracks": {"items": [{"album": {"images": []}}]}
        }
        assert _fetch_spotify_artwork_url(mock_sp, "A", "T") is None

    def test_exception(self):
        mock_sp = MagicMock()
        mock_sp.search.side_effect = Exception("API fail")
        assert _fetch_spotify_artwork_url(mock_sp, "A", "T") is None


class TestDownloadImage:
    def test_success(self):
        with patch("music_downloader.tools.artwork_embedder.httpx") as mock_httpx:
            mock_resp = MagicMock()
            mock_resp.content = b"\xff\xd8\xff\xe0"
            mock_resp.raise_for_status = MagicMock()
            mock_httpx.get.return_value = mock_resp
            result = _download_image("https://example.com/art.jpg")
            assert result == b"\xff\xd8\xff\xe0"

    def test_failure(self):
        with patch("music_downloader.tools.artwork_embedder.httpx") as mock_httpx:
            mock_httpx.get.side_effect = Exception("timeout")
            result = _download_image("https://example.com/art.jpg")
            assert result is None


class TestEmbedFlac:
    def test_success(self, tmp_path):
        path = str(tmp_path / "test.flac")
        _create_test_flac(path)
        result = _embed_flac(path, b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        assert result is True
        audio = mutagen.flac.FLAC(path)
        assert len(audio.pictures) == 1

    def test_invalid_file(self, tmp_path):
        path = str(tmp_path / "bad.flac")
        with open(path, "w") as f:
            f.write("not flac")
        result = _embed_flac(path, b"\x00")
        assert result is False


class TestEmbedM4a:
    def test_success(self):
        with patch("music_downloader.tools.artwork_embedder.mutagen.mp4.MP4") as mock:
            mock_file = MagicMock()
            mock_file.tags = {}
            mock.return_value = mock_file
            result = _embed_m4a("test.m4a", b"\xff\xd8\xff\xe0")
            assert result is True

    def test_failure(self):
        with patch("music_downloader.tools.artwork_embedder.mutagen.mp4.MP4") as mock:
            mock.side_effect = Exception("fail")
            result = _embed_m4a("test.m4a", b"\xff\xd8\xff\xe0")
            assert result is False


class TestSiblingM4aPath:
    def test_sibling_exists(self, tmp_path):
        m4a = tmp_path / "Song.m4a"
        m4a.write_text("data")
        result = _sibling_m4a_path(str(tmp_path / "Song.flac"), str(tmp_path))
        assert result is not None
        assert result.endswith("Song.m4a")

    def test_sibling_missing(self, tmp_path):
        result = _sibling_m4a_path(str(tmp_path / "Song.flac"), str(tmp_path))
        assert result is None


class TestRun:
    def test_run_dry_run(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "Artist - Title.flac")
        _create_test_flac(flac_path)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                stats = run(str(music_dir), dry_run=True)
                assert stats["total"] == 1
                assert stats["missing"] == 1
                assert len(stats["skipped"]) == 1

    def test_run_no_credentials_exits(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "", "SPOTIFY_CLIENT_SECRET": ""}, clear=False):
            with pytest.raises(SystemExit):
                run(str(music_dir))

    def test_run_embeds_artwork(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "Artist - Title.flac")
        _create_test_flac(flac_path)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                with patch("music_downloader.tools.artwork_embedder._fetch_spotify_artwork_url") as mock_url:
                    mock_url.return_value = "https://example.com/art.jpg"
                    with patch("music_downloader.tools.artwork_embedder._download_image") as mock_dl:
                        mock_dl.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100
                        with patch("music_downloader.tools.artwork_embedder._embed_flac") as mock_embed:
                            mock_embed.return_value = True
                            with patch("music_downloader.tools.artwork_embedder.time.sleep"):
                                stats = run(str(music_dir))
                                assert stats["embedded"] == 1

    def test_run_unparseable_filename(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "JustAFilename.flac")
        _create_test_flac(flac_path)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                stats = run(str(music_dir))
                assert len(stats["failed"]) == 1

    def test_run_no_spotify_artwork(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "Artist - Title.flac")
        _create_test_flac(flac_path)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                with patch("music_downloader.tools.artwork_embedder._fetch_spotify_artwork_url") as mock_url:
                    mock_url.return_value = None
                    with patch("music_downloader.tools.artwork_embedder.time.sleep"):
                        stats = run(str(music_dir))
                        assert len(stats["failed"]) == 1

    def test_run_download_image_fails(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "Artist - Title.flac")
        _create_test_flac(flac_path)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                with patch("music_downloader.tools.artwork_embedder._fetch_spotify_artwork_url") as mock_url:
                    mock_url.return_value = "https://example.com/art.jpg"
                    with patch("music_downloader.tools.artwork_embedder._download_image") as mock_dl:
                        mock_dl.return_value = None
                        stats = run(str(music_dir))
                        assert len(stats["failed"]) == 1

    def test_run_writes_report(self, tmp_path):
        music_dir = tmp_path / "music"
        music_dir.mkdir()
        flac_path = str(music_dir / "NoSeparator.flac")
        _create_test_flac(flac_path)
        report_path = str(tmp_path / "report.txt")

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                run(str(music_dir), report_path=report_path)
                assert os.path.isfile(report_path)

    def test_run_with_aac_and_alac_dirs(self, tmp_path):
        music_dir = tmp_path / "music"
        aac_dir = tmp_path / "aac"
        alac_dir = tmp_path / "alac"
        music_dir.mkdir()
        aac_dir.mkdir()
        alac_dir.mkdir()

        flac_path = str(music_dir / "Artist - Title.flac")
        _create_test_flac(flac_path)
        # Create sibling M4A in AAC dir
        (aac_dir / "Artist - Title.m4a").write_bytes(b"\x00" * 100)

        with patch.dict(os.environ, {"SPOTIFY_CLIENT_ID": "id", "SPOTIFY_CLIENT_SECRET": "secret"}):
            with patch("music_downloader.tools.artwork_embedder.spotipy.Spotify"):
                with patch("music_downloader.tools.artwork_embedder._fetch_spotify_artwork_url") as mock_url:
                    mock_url.return_value = "https://example.com/art.jpg"
                    with patch("music_downloader.tools.artwork_embedder._download_image") as mock_dl:
                        mock_dl.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100
                        with patch("music_downloader.tools.artwork_embedder._embed_flac") as mock_ef:
                            mock_ef.return_value = True
                            with patch("music_downloader.tools.artwork_embedder._m4a_has_art") as mock_has:
                                mock_has.return_value = False
                                with patch("music_downloader.tools.artwork_embedder._embed_m4a") as mock_em:
                                    mock_em.return_value = True
                                    with patch("music_downloader.tools.artwork_embedder.time.sleep"):
                                        stats = run(
                                            str(music_dir),
                                            aac_dir=str(aac_dir),
                                            alac_dir=str(alac_dir),
                                        )
                                        assert stats["embedded"] == 1
                                        mock_em.assert_called_once()
