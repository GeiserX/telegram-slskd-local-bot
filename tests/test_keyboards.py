"""Tests for inline keyboard builders."""

from music_downloader.bot.keyboards import (
    build_approve_keyboard,
    build_auto_mode_keyboard,
    build_duplicate_keyboard,
    build_results_keyboard,
    build_spotify_keyboard,
)
from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult


def _make_result(idx: int = 0) -> SearchResult:
    return SearchResult(
        username=f"user{idx}",
        filename=f"\\Music\\file{idx}.flac",
        size=30_000_000,
        bit_rate=900,
        bit_depth=16,
        sample_rate=44100,
        length=180,
        has_free_slot=True,
        upload_speed=1_000_000,
        queue_length=0,
    )


def _make_track(idx: int = 0) -> TrackInfo:
    return TrackInfo(
        artist=f"Artist{idx}",
        title=f"Title{idx}",
        album=f"Album{idx}",
        duration_ms=180_000,
        spotify_url=f"https://open.spotify.com/track/{idx}",
        year="2024",
    )


class TestBuildResultsKeyboard:
    def test_single_result(self):
        results = [_make_result()]
        kb = build_results_keyboard(results)
        rows = kb.inline_keyboard
        # 1 result button + action row (auto-pick + cancel)
        assert len(rows) == 2
        assert "dl:0" in rows[0][0].callback_data

    def test_multiple_results(self):
        results = [_make_result(i) for i in range(3)]
        kb = build_results_keyboard(results)
        rows = kb.inline_keyboard
        # 3 result rows + action row
        assert len(rows) == 4

    def test_pagination_first_page(self):
        results = [_make_result(i) for i in range(15)]
        kb = build_results_keyboard(results, page=0, page_size=10)
        rows = kb.inline_keyboard
        # 10 result rows + nav row + action row
        nav_row = rows[-2]
        assert any("Next" in btn.text for btn in nav_row)
        assert not any("Prev" in btn.text for btn in nav_row)

    def test_pagination_second_page(self):
        results = [_make_result(i) for i in range(15)]
        kb = build_results_keyboard(results, page=1, page_size=10)
        rows = kb.inline_keyboard
        nav_row = rows[-2]
        assert any("Prev" in btn.text for btn in nav_row)
        assert not any("Next" in btn.text for btn in nav_row)

    def test_pagination_middle_page(self):
        results = [_make_result(i) for i in range(30)]
        kb = build_results_keyboard(results, page=1, page_size=10)
        rows = kb.inline_keyboard
        nav_row = rows[-2]
        assert any("Prev" in btn.text for btn in nav_row)
        assert any("Next" in btn.text for btn in nav_row)

    def test_action_row_has_auto_and_cancel(self):
        results = [_make_result()]
        kb = build_results_keyboard(results)
        action_row = kb.inline_keyboard[-1]
        texts = [btn.text for btn in action_row]
        assert any("Auto" in t for t in texts)
        assert any("Cancel" in t for t in texts)

    def test_empty_results_has_cancel_only(self):
        kb = build_results_keyboard([])
        action_row = kb.inline_keyboard[-1]
        assert len(action_row) == 1
        assert "Cancel" in action_row[0].text


class TestBuildApproveKeyboard:
    def test_has_approve_and_reject(self):
        kb = build_approve_keyboard("42")
        buttons = kb.inline_keyboard[0]
        assert len(buttons) == 2
        assert buttons[0].callback_data == "approve:42"
        assert buttons[1].callback_data == "reject:42"


class TestBuildDuplicateKeyboard:
    def test_has_continue_and_cancel(self):
        kb = build_duplicate_keyboard()
        buttons = kb.inline_keyboard[0]
        assert len(buttons) == 2
        assert buttons[0].callback_data == "dup:continue"
        assert buttons[1].callback_data == "dup:cancel"


class TestBuildSpotifyKeyboard:
    def test_single_page(self):
        tracks = [_make_track(i) for i in range(3)]
        kb = build_spotify_keyboard(tracks)
        rows = kb.inline_keyboard
        # 3 track buttons + cancel row
        assert len(rows) == 4
        assert rows[0][0].callback_data == "sp:0"
        assert rows[-1][0].callback_data == "sp:cancel"

    def test_pagination_multiple_pages(self):
        tracks = [_make_track(i) for i in range(12)]
        kb = build_spotify_keyboard(tracks, page=0, page_size=5)
        rows = kb.inline_keyboard
        # 5 track rows + nav row + cancel row
        nav_row = rows[-2]
        assert any("Next" in btn.text for btn in nav_row)

    def test_pagination_last_page(self):
        tracks = [_make_track(i) for i in range(12)]
        kb = build_spotify_keyboard(tracks, page=2, page_size=5)
        rows = kb.inline_keyboard
        # 2 track rows + nav row + cancel row
        nav_row = rows[-2]
        assert any("Prev" in btn.text for btn in nav_row)
        assert not any("Next" in btn.text for btn in nav_row)

    def test_truncates_long_labels(self):
        track = TrackInfo(
            artist="A Very Long Artist Name That Goes On",
            title="A Very Long Song Title That Is Excessively Verbose And Long",
            album="Album",
            duration_ms=180_000,
            spotify_url="",
            year="2024",
        )
        kb = build_spotify_keyboard([track])
        label = kb.inline_keyboard[0][0].text
        assert len(label) <= 64


class TestBuildAutoModeKeyboard:
    def test_currently_on(self):
        kb = build_auto_mode_keyboard(True)
        btn = kb.inline_keyboard[0][0]
        assert "Disable" in btn.text
        assert btn.callback_data == "auto:off"

    def test_currently_off(self):
        kb = build_auto_mode_keyboard(False)
        btn = kb.inline_keyboard[0][0]
        assert "Enable" in btn.text
        assert btn.callback_data == "auto:on"
