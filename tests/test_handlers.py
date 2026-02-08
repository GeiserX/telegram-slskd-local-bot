"""Tests for bot handler helpers."""

from __future__ import annotations

from music_downloader.bot.handlers import _build_reduced_queries, _clean_search_title


class TestBuildReducedQueries:
    """Tests for _build_reduced_queries()."""

    def test_two_word_title(self):
        """Two-word title produces two queries, one per dropped word."""
        result = _build_reduced_queries("Purple Rain", "1984")
        assert result == ["Rain 1984", "Purple 1984"]

    def test_three_word_title(self):
        """Three-word title produces three queries."""
        result = _build_reduced_queries("Somewhere I Belong", "2003")
        assert result == [
            "I Belong 2003",
            "Somewhere Belong 2003",
            "Somewhere I 2003",
        ]

    def test_single_word_title_returns_empty(self):
        """Single-word titles are skipped (user requirement)."""
        assert _build_reduced_queries("Crazy", "1999") == []

    def test_empty_title_returns_empty(self):
        assert _build_reduced_queries("", "2000") == []

    def test_no_year_returns_empty(self):
        """Without a year the fallback is pointless."""
        assert _build_reduced_queries("Purple Rain", "") == []

    def test_none_year_returns_empty(self):
        assert _build_reduced_queries("Purple Rain", None) == []

    def test_four_word_title(self):
        result = _build_reduced_queries("Wish You Were Here", "1975")
        assert len(result) == 4
        assert result[0] == "You Were Here 1975"
        assert result[3] == "Wish You Were 1975"


class TestCleanSearchTitle:
    """Tests for _clean_search_title()."""

    def test_strips_remastered_suffix(self):
        assert _clean_search_title("Purple Rain - Remastered 2009") == "Purple Rain"

    def test_strips_mono_suffix(self):
        assert _clean_search_title("Hey Jude - Mono") == "Hey Jude"

    def test_strips_parenthesised_remaster(self):
        assert _clean_search_title("Come Together (Remastered 2009)") == "Come Together"

    def test_leaves_normal_title_alone(self):
        assert _clean_search_title("Bohemian Rhapsody") == "Bohemian Rhapsody"
