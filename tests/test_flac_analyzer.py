"""Tests for FLAC authenticity analyzer."""

import os
import tempfile

import numpy as np
import soundfile as sf

from music_downloader.processor.flac_analyzer import FlacVerdict, analyze_flac, create_preview_clip


class TestFlacVerdict:
    """Test FlacVerdict display properties."""

    def test_authentic_display(self):
        v = FlacVerdict(verdict="AUTHENTIC", cutoff_khz=22.05, nyquist_khz=22.05, sample_rate=44100, bit_depth=16)
        assert "Lossless OK" in v.display
        assert "22.1kHz" in v.display
        assert v.emoji == "\u2705"

    def test_warning_display(self):
        v = FlacVerdict(verdict="WARNING", cutoff_khz=19.5, nyquist_khz=22.05, sample_rate=44100, bit_depth=16)
        assert "Possible transcode" in v.display
        assert "19.5kHz" in v.display
        assert v.emoji == "\u26a0\ufe0f"

    def test_suspicious_display(self):
        v = FlacVerdict(verdict="SUSPICIOUS", cutoff_khz=18.0, nyquist_khz=22.05, sample_rate=44100, bit_depth=16)
        assert "Likely transcode" in v.display
        assert "18.0kHz" in v.display

    def test_fake_display(self):
        v = FlacVerdict(verdict="FAKE", cutoff_khz=16.0, nyquist_khz=22.05, sample_rate=44100, bit_depth=16)
        assert "Fake lossless" in v.display
        assert "16.0kHz" in v.display
        assert v.emoji == "\u274c"


class TestAnalyzeFlac:
    """Test analyze_flac with synthetic FLAC files."""

    @staticmethod
    def _create_test_flac(
        filepath: str,
        sample_rate: int = 44100,
        duration: float = 5.0,
        cutoff_hz: float | None = None,
    ):
        """Create a synthetic FLAC file.

        If cutoff_hz is None, generate broadband white noise (authentic).
        If cutoff_hz is set, low-pass filter the noise to simulate a transcode.
        """
        from scipy.signal import butter, sosfilt

        n_samples = int(sample_rate * duration)
        rng = np.random.default_rng(42)
        data = rng.standard_normal(n_samples).astype(np.float32)

        if cutoff_hz is not None:
            nyquist = sample_rate / 2
            # Butterworth low-pass to create a sharp spectral cutoff
            sos = butter(10, cutoff_hz / nyquist, btype="low", output="sos")
            data = sosfilt(sos, data).astype(np.float32)

        # Normalize
        data = data / np.max(np.abs(data)) * 0.8

        sf.write(filepath, data, sample_rate, subtype="PCM_16")

    def test_authentic_flac(self):
        """Broadband white noise FLAC should be AUTHENTIC."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path)  # no cutoff = broadband
            result = analyze_flac(path, sample_duration=5.0)
            assert result is not None
            assert result.verdict == "AUTHENTIC"
            assert result.sample_rate == 44100
            assert result.bit_depth == 16
        finally:
            os.unlink(path)

    def test_fake_flac_low_cutoff(self):
        """FLAC with sharp low-pass at 15kHz should be detected as FAKE."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path, cutoff_hz=15000.0)
            result = analyze_flac(path, sample_duration=5.0)
            assert result is not None
            assert result.verdict in ("FAKE", "SUSPICIOUS")
        finally:
            os.unlink(path)

    def test_silent_file_is_authentic(self):
        """A near-silent file should default to AUTHENTIC (can't analyze)."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            n_samples = 44100 * 3
            data = np.zeros(n_samples, dtype=np.float32)
            sf.write(path, data, 44100, subtype="PCM_16")
            result = analyze_flac(path, sample_duration=3.0)
            assert result is not None
            assert result.verdict == "AUTHENTIC"
        finally:
            os.unlink(path)

    def test_nonexistent_file_returns_none(self):
        """Analyzing a non-existent file should return None."""
        result = analyze_flac("/tmp/nonexistent_test_file.flac")
        assert result is None

    def test_hi_res_authentic(self):
        """96kHz broadband file should be AUTHENTIC with 48kHz Nyquist."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path, sample_rate=96000)
            result = analyze_flac(path, sample_duration=5.0)
            assert result is not None
            assert result.verdict == "AUTHENTIC"
            assert result.sample_rate == 96000
            assert result.nyquist_khz == 48.0
        finally:
            os.unlink(path)


class TestCreatePreviewClip:
    """Test create_preview_clip function."""

    @staticmethod
    def _create_test_flac(filepath: str, sample_rate: int = 44100, duration: float = 60.0):
        """Create a test FLAC file of the given duration."""
        rng = np.random.default_rng(42)
        n_samples = int(sample_rate * duration)
        data = (rng.standard_normal(n_samples) * 0.3).astype(np.float32)
        sf.write(filepath, data, sample_rate, subtype="PCM_16")

    def test_creates_preview_clip(self):
        """Preview clip should be shorter than the original."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path, duration=120.0)
            original_size = os.path.getsize(path)

            preview_path = create_preview_clip(path, duration_secs=30.0)
            assert preview_path is not None
            assert os.path.isfile(preview_path)

            # Preview should be significantly smaller than the 120s original
            preview_size = os.path.getsize(preview_path)
            assert preview_size < original_size * 0.5

            # Verify the preview file is valid audio
            info = sf.info(preview_path)
            assert info.samplerate == 44100
            # Duration should be ~30s (allow small tolerance)
            preview_duration = info.frames / info.samplerate
            assert 29.0 <= preview_duration <= 31.0

            os.unlink(preview_path)
        finally:
            os.unlink(path)

    def test_short_file_returns_full(self):
        """If the file is shorter than the preview duration, return the whole thing."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path, duration=10.0)
            preview_path = create_preview_clip(path, duration_secs=30.0)
            assert preview_path is not None

            info = sf.info(preview_path)
            preview_duration = info.frames / info.samplerate
            # Should contain the full ~10s (starting at 20% = ~2s, so ~8s remaining)
            assert preview_duration > 5.0

            os.unlink(preview_path)
        finally:
            os.unlink(path)

    def test_nonexistent_file_returns_none(self):
        """Non-existent file should return None."""
        result = create_preview_clip("/tmp/nonexistent_preview_test.flac")
        assert result is None

    def test_preview_preserves_sample_rate(self):
        """Preview should keep the original sample rate."""
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as f:
            path = f.name
        try:
            self._create_test_flac(path, sample_rate=96000, duration=60.0)
            preview_path = create_preview_clip(path, duration_secs=30.0)
            assert preview_path is not None

            info = sf.info(preview_path)
            assert info.samplerate == 96000

            os.unlink(preview_path)
        finally:
            os.unlink(path)
