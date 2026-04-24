"""Tests for SlskdClient._search_inner and _stop_and_collect."""

from unittest.mock import MagicMock, patch

import pytest

from music_downloader.search.slskd_client import SlskdClient


class TestSearchInner:
    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    @pytest.mark.asyncio
    async def test_search_completes_normally(self, client):
        """Search completes when isComplete=True."""
        client.client.searches.get_all = MagicMock(return_value=[])
        client.client.searches.search_text = MagicMock(return_value={"id": "search-1"})
        call_count = 0

        def state_side_effect(id, includeResponses=False):
            nonlocal call_count
            call_count += 1
            if includeResponses:
                return {"responses": [{"username": "u", "files": []}], "responseCount": 1}
            if call_count >= 2:
                return {"fileCount": 5, "responseCount": 1, "isComplete": True}
            return {"fileCount": 0, "responseCount": 0, "isComplete": False}

        client.client.searches.state = MagicMock(side_effect=state_side_effect)
        client.client.searches.delete = MagicMock()

        results = await client._search_inner("test query", timeout_secs=10, response_limit=100)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_stabilizes(self, client):
        """Search stops when file count stabilizes."""
        client.client.searches.get_all = MagicMock(return_value=[])
        client.client.searches.search_text = MagicMock(return_value={"id": "search-2"})

        call_count = 0

        def state_side_effect(id, includeResponses=False):
            nonlocal call_count
            call_count += 1
            if includeResponses:
                return {"responses": [{"username": "u"}], "responseCount": 1}
            # Always return same count to trigger stabilization
            return {"fileCount": 10, "responseCount": 2, "isComplete": False}

        client.client.searches.state = MagicMock(side_effect=state_side_effect)
        client.client.searches.delete = MagicMock()

        results = await client._search_inner("test", timeout_secs=15, response_limit=100)
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_falls_back_to_search_responses(self, client):
        """When state(includeResponses) is empty, falls back to search_responses."""
        client.client.searches.get_all = MagicMock(return_value=[])
        client.client.searches.search_text = MagicMock(return_value={"id": "search-3"})

        def state_side_effect(id, includeResponses=False):
            if includeResponses:
                return {"responses": [], "responseCount": 5, "fileCount": 10}
            return {"fileCount": 10, "responseCount": 5, "isComplete": True}

        client.client.searches.state = MagicMock(side_effect=state_side_effect)
        client.client.searches.search_responses = MagicMock(return_value=[{"username": "u"}])
        client.client.searches.delete = MagicMock()

        results = await client._search_inner("test", timeout_secs=10, response_limit=100)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_cleanup_stale_searches(self, client):
        """Stale searches are cleaned up before new search."""
        client.client.searches.get_all = MagicMock(return_value=[
            {"id": "old-1"}, {"id": "old-2"},
        ])
        client.client.searches.delete = MagicMock()
        client.client.searches.search_text = MagicMock(return_value={"id": "new-1"})

        def state_side_effect(id, includeResponses=False):
            if includeResponses:
                return {"responses": [], "responseCount": 0}
            return {"fileCount": 0, "responseCount": 0, "isComplete": True}

        client.client.searches.state = MagicMock(side_effect=state_side_effect)

        await client._search_inner("test", timeout_secs=5, response_limit=100)
        # Should have deleted old-1 and old-2 plus the new search
        assert client.client.searches.delete.call_count >= 2

    @pytest.mark.asyncio
    async def test_cleanup_stale_exception_ignored(self, client):
        """Exceptions during cleanup are silently ignored."""
        client.client.searches.get_all = MagicMock(side_effect=Exception("network"))
        client.client.searches.search_text = MagicMock(return_value={"id": "s1"})

        def state_side_effect(id, includeResponses=False):
            if includeResponses:
                return {"responses": [], "responseCount": 0}
            return {"fileCount": 0, "responseCount": 0, "isComplete": True}

        client.client.searches.state = MagicMock(side_effect=state_side_effect)
        client.client.searches.delete = MagicMock()

        results = await client._search_inner("test", timeout_secs=5, response_limit=100)
        assert isinstance(results, list)


class TestStopAndCollect:
    @pytest.fixture
    def client(self):
        with patch("slskd_api.SlskdClient") as mock_cls:
            c = SlskdClient("http://localhost:5030", "test-key")
            c.client = mock_cls.return_value
            return c

    @pytest.mark.asyncio
    async def test_stop_and_collect_with_responses(self, client):
        client.client.searches.stop = MagicMock()
        client.client.searches.state = MagicMock(return_value={
            "responses": [{"username": "u1"}, {"username": "u2"}],
            "responseCount": 2,
        })
        client.client.searches.delete = MagicMock()

        results = await client._stop_and_collect("search-1")
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_stop_and_collect_fallback(self, client):
        client.client.searches.stop = MagicMock()
        client.client.searches.state = MagicMock(return_value={
            "responses": [],
            "responseCount": 3,
        })
        client.client.searches.search_responses = MagicMock(return_value=[{"username": "u"}])
        client.client.searches.delete = MagicMock()

        results = await client._stop_and_collect("search-1")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_stop_and_collect_exception(self, client):
        client.client.searches.stop = MagicMock()
        client.client.searches.state = MagicMock(side_effect=Exception("fail"))
        client.client.searches.delete = MagicMock()

        results = await client._stop_and_collect("search-1")
        assert results == []
