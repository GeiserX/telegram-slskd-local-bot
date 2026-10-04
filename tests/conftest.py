"""Shared test setup."""

import pytest

from music_downloader.pipeline import fetch as _fetch


@pytest.fixture(autouse=True)
def no_locate_retry(monkeypatch):
    """The disk lookup after a finished download retries for 10 s in production; tests take one look.

    tests/test_locate_retry.py re-enables it explicitly.
    """
    monkeypatch.setattr(_fetch, "LOCATE_RETRY_SECS", 0.0)
