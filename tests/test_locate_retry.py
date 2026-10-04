"""The lookup of a finished download keeps looking while slskd moves the file into place (tsb-507)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from music_downloader.pipeline import fetch as _fetch
from music_downloader.pipeline.fetch import FILE_NOT_FOUND, locate_landed


@pytest.fixture
def retry_on(monkeypatch):
    monkeypatch.setattr(_fetch, "LOCATE_RETRY_SECS", 2.0)
    monkeypatch.setattr(_fetch, "LOCATE_RETRY_STEP_SECS", 0.5)


@pytest.mark.asyncio
async def test_file_that_lands_a_moment_later_is_found(retry_on):
    find = MagicMock(side_effect=[None, None, "/downloads/u/song.mp3"])
    sleep = AsyncMock()
    assert await locate_landed(find, sleep=sleep) == "/downloads/u/song.mp3"
    assert find.call_count == 3
    assert [c.args[0] for c in sleep.await_args_list] == [0.5, 0.5]


@pytest.mark.asyncio
async def test_gives_up_after_the_budget(retry_on):
    find = MagicMock(return_value=None)
    sleep = AsyncMock()
    assert await locate_landed(find, sleep=sleep) is None
    assert find.call_count == 5  # at 0, 0.5, 1.0, 1.5 and 2.0 s
    assert sleep.await_count == 4


@pytest.mark.asyncio
async def test_fetch_reports_file_not_found_only_after_retrying(retry_on, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(_fetch.asyncio, "sleep", sleep)
    slskd = MagicMock()
    slskd.enqueue_download.return_value = True
    status = MagicMock(is_failed=False, state="Completed, Succeeded", transfer_id="t1")
    slskd.wait_for_download = AsyncMock(return_value=status)
    processor = MagicMock()
    processor.find_downloaded_file.return_value = None
    result = MagicMock(username="u", filename="\\x\\song.mp3", extension="mp3")
    outcome = await _fetch.fetch(slskd, processor, result, timeout_secs=5, progress_cb=None, analyze=None)
    assert outcome.error == FILE_NOT_FOUND
    assert processor.find_downloaded_file.call_count == 5
