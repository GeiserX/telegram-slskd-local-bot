"""The library index behind the duplicate check: build, refresh, subfolders, off the event loop."""

import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from music_downloader import pipeline as pipeline_mod
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.persistence import library_index as library_index_mod
from music_downloader.persistence.database import Database
from music_downloader.persistence.library_index import CANDIDATE_LIMIT, LibraryIndex
from music_downloader.search.slskd_client import SearchResult
from tests.test_chat_delivery import CHAT, _make_bot, _make_config, _make_context


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    return str(path)


@pytest.fixture
def index(tmp_path):
    (tmp_path / "music").mkdir()
    return LibraryIndex(Database(str(tmp_path / "data" / "importer.db")), str(tmp_path / "music"))


class TestBuild:
    def test_subfolders_are_indexed_with_relative_paths(self, index, tmp_path):
        _touch(tmp_path / "music" / "Pink Floyd" / "Meddle" / "Pink Floyd - Echoes.flac")
        _touch(tmp_path / "music" / "Nancy Sinatra - Bang Bang.mp3")
        _touch(tmp_path / "music" / "Pink Floyd" / "cover.jpg")
        assert index.rebuild() == 2
        assert index.find_similar("Pink Floyd Echoes") == [
            os.path.join("Pink Floyd", "Meddle", "Pink Floyd - Echoes.flac")
        ]

    def test_rebuild_forgets_deleted_files(self, index, tmp_path):
        path = _touch(tmp_path / "music" / "Pink Floyd - Echoes.flac")
        _touch(tmp_path / "music" / "Nancy Sinatra - Bang Bang.mp3")
        index.rebuild()
        os.remove(path)
        index.rebuild()
        assert index.find_similar("Pink Floyd Echoes") == []

    def test_missing_or_empty_root_keeps_the_previous_rows(self, index, tmp_path):
        # An unmounted share must not silence the duplicate check until the next pass.
        _touch(tmp_path / "music" / "Pink Floyd - Echoes.flac")
        assert index.rebuild() == 1
        os.rename(tmp_path / "music", tmp_path / "away")
        assert index.rebuild() == 0
        (tmp_path / "music").mkdir()
        assert index.rebuild() == 0
        assert index.find_similar("Pink Floyd Echoes") == ["Pink Floyd - Echoes.flac"]

    def test_name_starting_with_two_dots_is_indexed(self, index, tmp_path):
        # "..Intro.mp3" is inside the library; only a path that climbs out is refused.
        path = _touch(tmp_path / "music" / "..Intro.mp3")
        assert index.rebuild() == 1
        assert index.find_similar("Intro") == ["..Intro.mp3"]
        assert index.add(path) is True

    def test_accents_do_not_hide_a_match(self, index, tmp_path):
        # The prefilter compares accent-free stems: "beyonce" finds "Beyoncé".
        _touch(tmp_path / "music" / "Beyoncé.flac")
        index.rebuild()
        assert index.find_similar("Beyonce") == ["Beyoncé.flac"]

    def test_add_records_one_saved_file(self, index, tmp_path):
        index.rebuild()
        path = _touch(tmp_path / "music" / "Sub" / "Pink Floyd - Echoes.flac")
        assert index.add(path) is True
        assert index.find_similar("Pink Floyd Echoes") == [os.path.join("Sub", "Pink Floyd - Echoes.flac")]
        assert index.add(_touch(tmp_path / "elsewhere" / "Pink Floyd - Echoes.flac")) is False
        assert index.add(_touch(tmp_path / "music" / "notes.txt")) is False

    def test_fuzzy_match_runs_on_at_most_candidate_limit_rows(self, index, tmp_path):
        for i in range(CANDIDATE_LIMIT + 150):
            _touch(tmp_path / "music" / f"Song {i}.mp3")
        _touch(tmp_path / "music" / "Unrelated.mp3")
        index.rebuild()
        calls = []
        real = library_index_mod.similarity
        with patch.object(library_index_mod, "similarity", side_effect=lambda q, s: calls.append(s) or real(q, s)):
            index.find_similar("Song")
        assert len(calls) == CANDIDATE_LIMIT
        assert "unrelated" not in calls

    def test_prefilter_prefers_rows_matching_more_words(self, index, tmp_path):
        for i in range(CANDIDATE_LIMIT + 50):
            _touch(tmp_path / "music" / f"Pink {i}.mp3")
        _touch(tmp_path / "music" / "Pink Floyd - Echoes.flac")
        index.rebuild()
        assert index.find_similar("Pink Floyd Echoes")[0] == "Pink Floyd - Echoes.flac"


class TestPipeline:
    @pytest.mark.asyncio
    async def test_query_runs_off_the_event_loop(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        seen = []
        real = bot.pipeline.library_index.find_similar
        bot.pipeline.library_index.find_similar = lambda q: seen.append(threading.get_ident()) or real(q)
        await bot.pipeline.find_similar("Echoes")
        assert seen and seen[0] != threading.get_ident()

    @pytest.mark.asyncio
    async def test_save_refreshes_the_index(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        bot.pipeline.library_index.rebuild()
        source = _touch(tmp_path / "downloads" / "u" / "x.flac")
        bot.pipeline.embed_artwork = AsyncMock()
        track = TrackInfo(
            artist="Pink Floyd", title="Echoes", album="Meddle", duration_ms=1, spotify_url="u", year="1971"
        )
        result = SearchResult(username="u", filename="\\x\\x.flac", size=1)
        target = await bot.pipeline.save(source, track, result)
        assert target
        assert await bot.pipeline.find_similar("Pink Floyd Echoes") == ["Pink Floyd - Echoes.flac"]

    @pytest.mark.asyncio
    async def test_index_loop_builds_now_then_hourly_in_a_thread(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        _touch(tmp_path / "music" / "Deep" / "Pink Floyd - Echoes.flac")
        threads = []
        real = bot.pipeline.library_index.rebuild
        bot.pipeline.library_index.rebuild = lambda: threads.append(threading.get_ident()) or real()
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch.object(pipeline_mod.asyncio, "sleep", sleep), pytest.raises(asyncio.CancelledError):
            await bot.pipeline.library_index_loop()
        assert len(threads) == 2
        assert threading.get_ident() not in threads
        sleep.assert_awaited_with(pipeline_mod.LIBRARY_RESCAN_SECS)
        assert pipeline_mod.LIBRARY_RESCAN_SECS == 3600
        assert await bot.pipeline.find_similar("Pink Floyd Echoes") == [
            os.path.join("Deep", "Pink Floyd - Echoes.flac")
        ]

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_does_not_stop_the_loop(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        bot.pipeline.library_index.rebuild = MagicMock(side_effect=[OSError("disk"), 0])
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch.object(pipeline_mod.asyncio, "sleep", sleep), pytest.raises(asyncio.CancelledError):
            await bot.pipeline.library_index_loop()
        assert bot.pipeline.library_index.rebuild.call_count == 2

    @pytest.mark.asyncio
    async def test_duplicate_warning_lists_a_file_in_a_subfolder(self, tmp_path):
        bot = _make_bot(_make_config(str(tmp_path)))
        _touch(tmp_path / "music" / "Pink Floyd" / "Pink Floyd - Echoes.flac")
        bot.pipeline.library_index.rebuild()
        update = MagicMock()
        update.effective_user.id = CHAT
        update.effective_chat.id = CHAT
        update.message.text = "Pink Floyd Echoes"
        update.message.reply_text = AsyncMock()
        await bot.handle_text(update, _make_context())
        text = update.message.reply_text.call_args.args[0]
        assert "Similar files already in library" in text
        assert os.path.join("Pink Floyd", "Pink Floyd - Echoes.flac") in text
