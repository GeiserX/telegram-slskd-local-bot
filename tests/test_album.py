"""Album delivery: listing a peer's folder, fetching every file, naming them, the cover, the job, a restart."""

import asyncio
import os
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock, patch

import mutagen.easyid3
import pytest
import requests

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence.album_repo import (
    ALBUM_DONE,
    ALBUM_INTERRUPTED,
    ALBUM_RUNNING,
    AlbumJob,
    AlbumRepository,
    FileOutcome,
)
from music_downloader.persistence.database import Database
from music_downloader.pipeline import Pipeline
from music_downloader.pipeline import album as _album
from music_downloader.pipeline.album import (
    REASON_NO_ANSWER,
    REASON_NO_AUDIO,
    REASON_UNREACHABLE,
    AlbumArt,
    FolderListing,
    browse_folder,
    fetch_folder,
    locate,
    parse_filename,
    track_info_for,
)
from music_downloader.pipeline.fetch import DOWNLOAD_FAILED, ENQUEUE_FAILED, FILE_NOT_FOUND
from music_downloader.processor.file_handler import FileProcessor
from music_downloader.search.slskd_client import DownloadStatus, SearchResult, SlskdClient
from tests.test_chat_delivery import _make_config

PEER = "vinylhoarder"
FOLDER = "@@abcd\\Music\\Pink Floyd\\1971 - Meddle"
TRACK = TrackInfo("Pink Floyd", "Echoes", "Meddle", 1_411_000, "https://x/t", "1971")


def _raw_folder(name=FOLDER):
    """slskd's users/directory answer: audio files with attributes in both shapes, plus junk."""
    return [
        {
            "name": name,
            "fileCount": 6,
            "files": [
                {
                    "filename": "02 - Pillow of Winds.flac",
                    "size": 30_000_000,
                    "extension": "flac",
                    "attributes": [
                        {"type": "Length", "value": 310},
                        {"type": "BitDepth", "value": 16},
                        {"type": "SampleRate", "value": 44100},
                    ],
                },
                {
                    "filename": "01 - One of These Days.flac",
                    "size": 40_000_000,
                    "extension": "",
                    "bitDepth": 24,
                    "sampleRate": 96000,
                    "length": 357,
                },
                {"filename": "cover.jpg", "size": 500_000, "extension": "jpg"},
                {"filename": "Meddle.cue", "size": 2_000, "extension": "cue"},
                {"filename": "Meddle.log", "size": 3_000},
                {
                    "filename": "06 - Echoes.mp3",
                    "size": 22_000_000,
                    "extension": "mp3",
                    "attributes": [{"type": 0, "value": 320}, {"type": 1, "value": 1411}],
                },
            ],
        }
    ]


def _slskd(raw=None):
    slskd = MagicMock(spec=SlskdClient)
    slskd.browse_directory.return_value = raw if raw is not None else _raw_folder()
    slskd.enqueue_files.return_value = True
    slskd.enqueue_download.return_value = True
    slskd.get_download_status.return_value = None
    return slskd


# ---------------------------------------------------------------------------
# browse_folder
# ---------------------------------------------------------------------------


class TestBrowseFolder:
    async def test_lists_audio_files_only_sorted_with_attributes(self):
        slskd = _slskd()
        listing = await browse_folder(slskd, PEER, FOLDER)
        slskd.browse_directory.assert_called_once_with(PEER, FOLDER)
        assert listing.answered and listing.reason == ""
        assert [f.basename for f in listing.files] == [
            "01 - One of These Days.flac",
            "02 - Pillow of Winds.flac",
            "06 - Echoes.mp3",
        ]
        first, second, mp3 = listing.files
        assert first.filename == f"{FOLDER}\\01 - One of These Days.flac"
        assert first.extension == "flac" and (first.bit_depth, first.sample_rate, first.length) == (24, 96000, 357)
        assert (second.bit_depth, second.sample_rate, second.length) == (16, 44100, 310)
        assert (mp3.bit_rate, mp3.length) == (320, 1411)
        assert listing.total_size == 92_000_000
        assert listing.formats == ["flac", "mp3"]

    async def test_a_single_directory_object_and_full_paths_are_accepted(self):
        raw = {"name": FOLDER + "\\", "files": [{"filename": f"{FOLDER}\\03 - Fearless.flac", "size": 5}]}
        listing = await browse_folder(_slskd(raw), PEER, FOLDER)
        assert [f.filename for f in listing.files] == [f"{FOLDER}\\03 - Fearless.flac"]

    async def test_offline_peer_gives_an_empty_listing_with_a_reason(self):
        slskd = _slskd()
        slskd.browse_directory.side_effect = requests.exceptions.HTTPError("404 Client Error: user offline")
        listing = await browse_folder(slskd, PEER, FOLDER)
        assert listing.files == [] and listing.answered is False
        assert listing.reason == REASON_UNREACHABLE and "offline" in listing.detail

    async def test_peer_that_never_answers_times_out(self):
        slskd = _slskd()
        slskd.browse_directory.side_effect = lambda *_: time.sleep(0.5) or _raw_folder()
        listing = await browse_folder(slskd, PEER, FOLDER, timeout_secs=0.05)
        assert listing.files == [] and listing.answered is False and listing.reason == REASON_NO_ANSWER

    async def test_folder_without_audio(self):
        raw = [{"name": FOLDER, "files": [{"filename": "cover.jpg", "size": 1, "extension": "jpg"}]}]
        listing = await browse_folder(_slskd(raw), PEER, FOLDER)
        assert listing.answered is True and listing.files == [] and listing.reason == REASON_NO_AUDIO

    def test_remote_dir(self):
        assert _album.remote_dir(f"{FOLDER}\\06 - Echoes.flac") == FOLDER
        assert _album.remote_dir("bare.flac") == ""

    def test_slskd_client_wraps_users_directory(self):
        with patch("slskd_api.SlskdClient"):
            client = SlskdClient("http://localhost:5030", "k")
        client.client.users.directory.return_value = {"name": FOLDER, "files": []}
        assert client.browse_directory(PEER, FOLDER) == [{"name": FOLDER, "files": []}]
        client.client.users.directory.assert_called_once_with(username=PEER, directory=FOLDER)
        client.client.transfers.enqueue.return_value = True
        assert client.enqueue_files(PEER, [("a\\1.flac", 10), ("a\\2.flac", 20)]) is True
        client.client.transfers.enqueue.assert_called_once_with(
            username=PEER, files=[{"filename": "a\\1.flac", "size": 10}, {"filename": "a\\2.flac", "size": 20}]
        )
        client.client.transfers.enqueue.side_effect = requests.exceptions.HTTPError("500")
        assert client.enqueue_files(PEER, [("a\\1.flac", 10)]) is False


# ---------------------------------------------------------------------------
# fetch_folder
# ---------------------------------------------------------------------------


def _listing(names=("01 - A.flac", "02 - B.flac", "03 - C.flac")):
    files = [_album.FolderFile(f"{FOLDER}\\{n}", 1000 + i, n.rsplit(".", 1)[-1]) for i, n in enumerate(names)]
    return FolderListing(PEER, FOLDER, files)


def _processor(tmp_path):
    return FileProcessor(str(tmp_path / "downloads"), str(tmp_path / "music"))


def _land(tmp_path, remote):
    """Write the file where slskd puts it: <downloads>/<remote folder name>/<file>."""
    leaf, name = remote.split("\\")[-2:]
    path = tmp_path / "downloads" / leaf / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + b"\0" * 64)
    return str(path)


def _waiter(tmp_path, states):
    """wait_for_download that lands the file and answers *states[basename]* (None = timed out)."""

    async def wait(username, filename, timeout_secs, progress_cb):
        name = filename.rsplit("\\", 1)[-1]
        state = states.get(name, "Completed, Succeeded")
        if isinstance(state, Exception):
            raise state
        await progress_cb(DownloadStatus(username, filename, "InProgress", 40.0))
        if state is None:
            return None
        if "Succeeded" in state:
            _land(tmp_path, filename)
        return DownloadStatus(username, filename, state, 100.0, transfer_id=f"tx-{name[:2]}")

    return AsyncMock(side_effect=wait)


class TestFetchFolder:
    async def test_one_request_enqueues_the_whole_folder(self, tmp_path):
        slskd = _slskd()
        slskd.wait_for_download = _waiter(tmp_path, {})
        listing = _listing()
        outcomes = await fetch_folder(slskd, _processor(tmp_path), listing, 600, 7200)
        slskd.enqueue_files.assert_called_once_with(PEER, [(f.filename, f.size) for f in listing.files])
        slskd.enqueue_download.assert_not_called()
        assert [o.ok for o in outcomes] == [True, True, True]
        assert outcomes[0].path == str(tmp_path / "downloads" / "1971 - Meddle" / "01 - A.flac")
        assert [o.transfer_id for o in outcomes] == ["tx-01", "tx-02", "tx-03"]

    async def test_list_refused_falls_back_to_one_by_one(self, tmp_path):
        slskd = _slskd()
        slskd.enqueue_files.return_value = False
        # B is refused but slskd lists it (the batch got it in); C is refused and unknown.
        slskd.enqueue_download.side_effect = lambda r: not r.filename.endswith(("B.flac", "C.flac"))
        slskd.get_download_status.side_effect = lambda u, f: (
            DownloadStatus(u, f, "Queued, Remotely") if f.endswith("B.flac") else None
        )
        slskd.wait_for_download = _waiter(tmp_path, {})
        outcomes = await fetch_folder(slskd, _processor(tmp_path), _listing(), 600, 7200)
        assert slskd.enqueue_download.call_count == 3
        assert all(isinstance(c.args[0], SearchResult) for c in slskd.enqueue_download.call_args_list)
        assert [o.error for o in outcomes] == [None, None, ENQUEUE_FAILED]
        assert slskd.wait_for_download.await_count == 2

    async def test_one_failed_file_never_stops_the_others(self, tmp_path):
        slskd = _slskd()
        names = ("01 - A.flac", "02 - B.flac", "03 - C.flac", "04 - D.flac", "05 - E.flac")
        slskd.wait_for_download = _waiter(
            tmp_path,
            {
                "02 - B.flac": "Completed, Errored",
                "03 - C.flac": None,
                "04 - D.flac": RuntimeError("slskd went away"),
                # E finishes, but slskd put it somewhere the bot cannot see.
                "05 - E.flac": "Completed, Succeeded",
            },
        )
        processor = _processor(tmp_path)
        processor.find_downloaded_file = MagicMock(return_value=None)
        real_land = _land

        def land_elsewhere(tmp, remote):
            return None if remote.endswith("E.flac") else real_land(tmp, remote)

        progress, finished = [], []

        async def on_progress(i, total, state, pct):
            progress.append((i, total, state, pct))

        async def on_file(i, outcome):
            finished.append((i, outcome.error))

        with patch(f"{__name__}._land", side_effect=land_elsewhere):
            outcomes = await fetch_folder(slskd, processor, _listing(names), 600, 7200, on_progress, on_file)
        assert [o.error for o in outcomes] == [None, DOWNLOAD_FAILED, DOWNLOAD_FAILED, DOWNLOAD_FAILED, FILE_NOT_FOUND]
        assert [o.state for o in outcomes[1:4]] == ["Completed, Errored", "Timeout", "slskd went away"]
        assert finished == [(i, o.error) for i, o in enumerate(outcomes)]
        assert (0, 5, "Queued", 0.0) in progress and (0, 5, "InProgress", 40.0) in progress
        assert (0, 5, "Completed, Succeeded", 100.0) in progress
        assert {p[1] for p in progress} == {5}

    async def test_album_cap_fails_the_files_left_without_waiting(self, tmp_path):
        slskd = _slskd()
        slskd.wait_for_download = _waiter(tmp_path, {})
        now = [0.0]

        def clock():
            return now[0]

        async def on_file(i, outcome):
            now[0] += 100  # each file "takes" 100 s

        outcomes = await fetch_folder(slskd, _processor(tmp_path), _listing(), 600, 150, None, on_file, clock)
        assert [o.error for o in outcomes] == [None, None, DOWNLOAD_FAILED]
        assert outcomes[2].state == "Timeout"
        assert slskd.wait_for_download.await_count == 2
        # The second file only had what was left of the album cap.
        assert slskd.wait_for_download.await_args_list[1].kwargs["timeout_secs"] == 50

    async def test_a_raising_progress_or_file_callback_is_contained(self, tmp_path):
        slskd = _slskd()
        slskd.wait_for_download = _waiter(tmp_path, {})
        boom = AsyncMock(side_effect=RuntimeError("telegram flood"))
        outcomes = await fetch_folder(slskd, _processor(tmp_path), _listing(), 600, 7200, boom, boom)
        assert [o.ok for o in outcomes] == [True, True, True]

    async def test_empty_listing_does_nothing(self, tmp_path):
        slskd = _slskd()
        assert await fetch_folder(slskd, _processor(tmp_path), FolderListing(PEER, FOLDER), 600, 7200) == []
        slskd.enqueue_files.assert_not_called()


class TestLocate:
    def test_prefers_the_file_in_the_folder_named_like_the_remote_one(self, tmp_path):
        processor = _processor(tmp_path)
        # Another album's file of the same name, where the plain lookup looks first (the peer's folder).
        other = tmp_path / "downloads" / PEER / "Animals" / "01 - Intro.flac"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"x")
        mine = _land(tmp_path, f"{FOLDER}\\01 - Intro.flac")
        assert locate(processor, PEER, f"{FOLDER}\\01 - Intro.flac") == mine

    def test_user_subfolder_and_fallback(self, tmp_path):
        processor = _processor(tmp_path)
        path = tmp_path / "downloads" / PEER / "1971 - Meddle" / "02 - B.flac"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        assert locate(processor, PEER, f"{FOLDER}\\02 - B.flac") == str(path)
        flat = tmp_path / "downloads" / "elsewhere" / "03 - C.flac"
        flat.parent.mkdir(parents=True)
        flat.write_bytes(b"x")
        assert locate(processor, PEER, f"{FOLDER}\\03 - C.flac") == str(flat)

    def test_never_leaves_the_downloads_dir(self, tmp_path):
        processor = _processor(tmp_path)
        os.makedirs(processor.download_dir)
        (tmp_path / "secret.flac").write_bytes(b"x")
        assert locate(processor, PEER, "x\\..\\secret.flac") is None


# ---------------------------------------------------------------------------
# Names and tags
# ---------------------------------------------------------------------------


def _mp3(path, **tags):
    """A tiny real MP3 (silent frames) with ID3 tags."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * 20)
    if tags:
        id3 = mutagen.easyid3.EasyID3()
        for key, value in tags.items():
            id3[key] = value
        id3.save(str(path))
    return str(path)


class TestNames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("01 - One of These Days.flac", (None, "One of These Days", 1)),
            ("01. Mr. Brightside.mp3", (None, "Mr. Brightside", 1)),
            ("03 Fearless.flac", (None, "Fearless", 3)),
            ("07_San Tropez.ogg", (None, "San Tropez", 7)),
            ("2-01 Speak to Me.flac", (None, "Speak to Me", 1)),
            ("Pink Floyd - Echoes.flac", ("Pink Floyd", "Echoes", None)),
            ("06 - Pink Floyd - Echoes.flac", ("Pink Floyd", "Echoes", 6)),
            ("Pink Floyd - 06 - Echoes.flac", ("Pink Floyd", "Echoes", 6)),
            ("1979.flac", (None, "1979", None)),
            ("Echoes.flac", (None, "Echoes", None)),
            ("\\Music\\Meddle\\04 - San Tropez.flac", (None, "San Tropez", 4)),
            ("00 - Hidden.flac", (None, "Hidden", None)),
        ],
    )
    def test_parse_filename(self, name, expected):
        assert parse_filename(name) == expected

    def test_tags_win_over_the_file_name(self, tmp_path):
        path = _mp3(
            tmp_path / "Wrong Artist - 99 - wrong name.mp3",
            artist="Pink Floyd",
            title="Echoes",
            album="Meddle",
            tracknumber="6/6",
            date="1971-11-05",
        )
        info = track_info_for(path, "Fallback", "Hint")
        assert (info.artist, info.title, info.album, info.track_number, info.year) == (
            "Pink Floyd",
            "Echoes",
            "Meddle",
            6,
            "1971",
        )
        assert 0 < info.duration_ms < 2000

    def test_missing_tags_fall_back_to_file_name_then_chosen_track(self, tmp_path):
        path = _mp3(tmp_path / "03 - Fearless.mp3", album="Meddle")
        info = track_info_for(path, "Pink Floyd", "Hint")
        assert (info.artist, info.title, info.album, info.track_number) == ("Pink Floyd", "Fearless", "Meddle", 3)

    def test_album_artist_tag_beats_the_fallback(self, tmp_path):
        path = _mp3(tmp_path / "Seamus.mp3", albumartist="Pink Floyd")
        info = track_info_for(path, "Someone", "Meddle")
        assert (info.artist, info.title, info.album, info.track_number) == ("Pink Floyd", "Seamus", "Meddle", None)

    def test_unreadable_file_uses_the_name_only(self, tmp_path):
        path = tmp_path / "Roger Waters - 05 - Seamus.flac"
        path.write_bytes(b"not audio at all")
        info = track_info_for(str(path), "Pink Floyd", "Meddle")
        assert (info.artist, info.title, info.album, info.track_number, info.duration_ms) == (
            "Roger Waters",
            "Seamus",
            "Meddle",
            5,
            0,
        )


class TestTrackTemplate:
    def _proc(self, tmp_path, template):
        return FileProcessor(str(tmp_path / "d"), str(tmp_path / "m"), filename_template=template)

    def test_default_template_keeps_the_number_out(self, tmp_path):
        assert self._proc(tmp_path, "{artist} - {title}").build_filename("A", "T", "flac", 3) == "A - T.flac"

    @pytest.mark.parametrize(
        ("template", "with_number", "without"),
        [
            ("{track} - {artist} - {title}", "03 - A - T.flac", "A - T.flac"),
            ("{artist} - {track} - {title}", "A - 03 - T.flac", "A - T.flac"),
            ("{track}. {title}", "03. T.flac", "T.flac"),
            ("{artist} - {title} {track}", "A - T 03.flac", "A - T.flac"),
        ],
    )
    def test_track_placeholder(self, tmp_path, template, with_number, without):
        proc = self._proc(tmp_path, template)
        assert proc.build_filename("A", "T", "flac", 3) == with_number
        assert proc.build_filename("A", "T", "flac") == without

    def test_process_file_passes_the_number_and_keeps_the_extension(self, tmp_path):
        proc = self._proc(tmp_path, "{track} - {title}")
        src = tmp_path / "x.MP3"
        src.write_bytes(b"x")
        assert proc.process_file(str(src), "A", "T", 12) == str(tmp_path / "m" / "12 - T.mp3")


# ---------------------------------------------------------------------------
# The cover
# ---------------------------------------------------------------------------


class TestAlbumArt:
    async def test_looked_up_once_for_every_file(self):
        with patch.object(_album, "fetch_spotify_album_artwork", return_value=b"JPEG") as fetch:
            art = AlbumArt("sp", "Pink Floyd", "Meddle", "Echoes")
            results = await asyncio.gather(*(art.get() for _ in range(5)))
        assert results == [b"JPEG"] * 5
        fetch.assert_called_once_with("sp", "Pink Floyd", "Meddle", "Echoes")

    async def test_a_missing_cover_is_not_looked_up_again(self):
        with patch.object(_album, "fetch_spotify_album_artwork", return_value=None) as fetch:
            art = AlbumArt("sp", "A", "B")
            assert await art.get() is None and await art.get() is None
        fetch.assert_called_once()

    def test_album_search_then_track_fallback(self):
        sp = MagicMock()
        sp.search.return_value = {"albums": {"items": [{"images": [{"url": "https://i/cover.jpg"}]}]}}
        response = MagicMock(content=b"IMG")
        with patch.object(_album.httpx, "get", return_value=response) as get:
            assert _album.fetch_spotify_album_artwork(sp, "Pink Floyd", "Meddle", "Echoes") == b"IMG"
        sp.search.assert_called_once_with(q="album:Meddle artist:Pink Floyd", type="album", limit=1)
        get.assert_called_once()
        sp.search.return_value = {"albums": {"items": []}}
        with patch.object(_album, "fetch_spotify_artwork", return_value=b"TRACK") as by_track:
            assert _album.fetch_spotify_album_artwork(sp, "Pink Floyd", "Meddle", "Echoes") == b"TRACK"
        by_track.assert_called_once_with(sp, "Pink Floyd", "Echoes")


# ---------------------------------------------------------------------------
# Pipeline.album and the job
# ---------------------------------------------------------------------------


def _pipeline(tmp_path, template="{artist} - {title}"):
    config = _make_config(str(tmp_path))
    config.filename_template = template
    config.album_timeout_secs = 7200
    with patch("music_downloader.pipeline.SpotifyResolver"), patch("music_downloader.pipeline.SlskdClient"):
        pipeline = Pipeline(config)
    pipeline.slskd = _slskd()
    return pipeline


def _chosen():
    return SearchResult(PEER, f"{FOLDER}\\06 - Echoes.mp3", 22_000_000, bit_rate=320, length=1411)


class TestPipelineAlbum:
    async def test_library_delivery_saves_names_cover_history_and_job(self, tmp_path):
        pipeline = _pipeline(tmp_path)
        pipeline.slskd.wait_for_download = _waiter(tmp_path, {"02 - Pillow of Winds.flac": "Completed, Errored"})
        embedded = []
        with (
            patch.object(_album, "fetch_spotify_album_artwork", return_value=b"COVER") as fetch,
            patch(
                "music_downloader.pipeline.library.embed_artwork_into_file",
                side_effect=lambda p, a: embedded.append((os.path.basename(p), a)) or True,
            ),
        ):
            job, listing = await pipeline.album(_chosen(), TRACK)
        music = tmp_path / "music"
        assert sorted(os.listdir(music)) == ["Pink Floyd - Echoes.mp3", "Pink Floyd - One of These Days.flac"]
        fetch.assert_called_once_with(pipeline.spotify.sp, "Pink Floyd", "Meddle", "Echoes")
        assert sorted(embedded) == [
            ("Pink Floyd - Echoes.mp3", b"COVER"),
            ("Pink Floyd - One of These Days.flac", b"COVER"),
        ]
        assert [o.ok for o in job.outcomes] == [True, False, True]
        assert job.outcomes[0].path == str(music / "Pink Floyd - One of These Days.flac")
        assert job.outcomes[1].state == "Completed, Errored"
        # Sources are gone from the downloads dir once saved.
        assert not os.path.exists(tmp_path / "downloads" / "1971 - Meddle" / "01 - One of These Days.flac")

        rows = pipeline.history_repo.get_recent(10)
        assert {(r.title, r.status, r.note, r.filename) for r in rows} == {
            ("One of These Days", "success", "album", "Pink Floyd - One of These Days.flac"),
            ("Echoes", "success", "album", "Pink Floyd - Echoes.mp3"),
        }
        stored = pipeline.album_repo.get(job.id)
        assert stored.status == ALBUM_DONE and stored.username == PEER and stored.remote_dir == FOLDER
        assert [o.ok if o else None for o in stored.outcomes] == [True, False, True]
        assert stored.track == TRACK and listing.formats == ["flac", "mp3"]

    async def test_track_template_puts_the_number_in(self, tmp_path):
        pipeline = _pipeline(tmp_path, "{track} - {title}")
        pipeline.slskd.wait_for_download = _waiter(tmp_path, {})
        with patch.object(_album, "fetch_spotify_album_artwork", return_value=None):
            await pipeline.album(_chosen(), TRACK)
        names = ["01 - One of These Days.flac", "02 - Pillow of Winds.flac", "06 - Echoes.mp3"]
        assert sorted(os.listdir(tmp_path / "music")) == names
        # The history names each file as saved.
        assert sorted(r.filename for r in pipeline.history_repo.get_recent(10)) == names

    async def test_path_delivery_leaves_the_files_and_notes_history(self, tmp_path):
        pipeline = _pipeline(tmp_path)
        pipeline.slskd.wait_for_download = _waiter(tmp_path, {})
        job, _ = await pipeline.album(_chosen(), TRACK, deliver="path")
        assert not os.path.exists(tmp_path / "music") or os.listdir(tmp_path / "music") == []
        assert all(o.ok and o.path.startswith(str(tmp_path / "downloads")) for o in job.outcomes)
        assert {(r.status, r.note) for r in pipeline.history_repo.get_recent(10)} == {("delivered", "album")}

    async def test_offline_peer_creates_no_job(self, tmp_path):
        pipeline = _pipeline(tmp_path)
        pipeline.slskd.browse_directory.side_effect = requests.exceptions.ConnectionError("offline")
        job, listing = await pipeline.album(_chosen(), TRACK)
        assert listing.answered is False and job.id is None and job.files == []
        pipeline.slskd.enqueue_files.assert_not_called()

    async def test_bad_deliver_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            await _pipeline(tmp_path).album(_chosen(), TRACK, deliver="chat")

    async def test_restart_marks_interrupted_and_processes_what_landed(self, tmp_path):
        pipeline = _pipeline(tmp_path)
        listing = _album.parse_directory(_raw_folder(), FOLDER)
        job = AlbumJob(PEER, FOLDER, listing, TRACK, chat_id=42)
        job.outcomes[0] = FileOutcome(filename=listing[0].filename, path="/music/already.flac")
        pipeline.album_repo.add(job)
        done = AlbumJob(PEER, FOLDER, listing, TRACK, status=ALBUM_DONE)
        pipeline.album_repo.add(done)
        # File 2 finished while the bot was down; file 3 never did.
        _land(tmp_path, listing[1].filename)

        restarted = _pipeline(tmp_path)
        with patch.object(_album, "fetch_spotify_album_artwork", return_value=None):
            jobs = await restarted.album_recover()
        assert [j.id for j in jobs] == [job.id]
        [recovered] = jobs
        assert recovered.status == ALBUM_INTERRUPTED and recovered.chat_id == 42
        assert recovered.unfinished == [2]
        assert recovered.outcomes[1].path == str(tmp_path / "music" / "Pink Floyd - Pillow of Winds.flac")
        assert os.path.isfile(recovered.outcomes[1].path)
        stored = restarted.album_repo.get(job.id)
        assert stored.status == ALBUM_INTERRUPTED and stored.outcomes[2] is None and stored.outcomes[1].ok
        assert restarted.album_repo.list_by_status(ALBUM_RUNNING) == []
        assert restarted.album_repo.get(done.id).status == ALBUM_DONE
        assert [(r.title, r.note) for r in restarted.history_repo.get_recent(5)] == [("Pillow of Winds", "album")]
        # A second start finds nothing left running.
        assert await restarted.album_recover() == []


class TestAlbumRepository:
    def test_round_trip_and_unreadable_rows(self, tmp_path):
        db = Database(str(tmp_path / "a.db"))
        repo = AlbumRepository(db)
        files = _album.parse_directory(_raw_folder(), FOLDER)
        job = repo.add(AlbumJob(PEER, FOLDER, files, TRACK, deliver="path"))
        loaded = repo.get(job.id)
        assert loaded.files == files and loaded.outcomes == [None, None, None] and loaded.deliver == "path"
        db.connection.execute("UPDATE album_jobs SET files = 'nope' WHERE id = ?", (job.id,))
        assert repo.get(job.id) is None and repo.list_by_status(ALBUM_RUNNING) == []
        assert repo.get(999) is None


def test_old_history_table_gets_the_note_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE download_history (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT NOT NULL,
        title TEXT NOT NULL, album TEXT DEFAULT '', filename TEXT NOT NULL, source_user TEXT NOT NULL,
        remote_path TEXT DEFAULT '', status TEXT NOT NULL, duration_secs INTEGER DEFAULT 0,
        file_size INTEGER DEFAULT 0, created_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    )
    conn.execute(
        "INSERT INTO download_history (artist, title, filename, source_user, status) VALUES ('a','t','f','u','success')"
    )
    conn.commit()
    conn.close()
    from music_downloader.persistence.history_repo import HistoryRepository

    repo = HistoryRepository(Database(path))
    repo.add("a", "t2", "f2", "u", "success", note="album")
    assert sorted(r.note for r in repo.get_recent()) == ["", "album"]


def test_album_timeout_setting(monkeypatch):
    from music_downloader.config import Config

    for key in ("TELEGRAM_BOT_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SLSKD_HOST", "SLSKD_API_KEY"):
        monkeypatch.setenv(key, "x")
    monkeypatch.delenv("ALBUM_TIMEOUT_SECS", raising=False)
    assert Config().album_timeout_secs == 7200
    monkeypatch.setenv("ALBUM_TIMEOUT_SECS", "900")
    assert Config().album_timeout_secs == 900
    monkeypatch.setenv("ALBUM_TIMEOUT_SECS", "0")
    assert Config().album_timeout_secs == 1
