"""Tests for murmur.server.audio — T-2, AC-5, AC-11, E-4, E-19."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from murmur.server.audio import AudioDecodeError, decode_to_pcm


def _make_run_ok(pcm_float32: np.ndarray) -> MagicMock:
    """Return a mock subprocess.run that succeeds and yields *pcm_float32* bytes."""
    raw = pcm_float32.astype(np.float32).tobytes()
    result = MagicMock()
    result.returncode = 0
    result.stdout = raw
    result.stderr = b""
    return MagicMock(return_value=result)


def test_decode_returns_float32_16k(tmp_path: Path) -> None:  # [AC-5]
    """decode_to_pcm returns a float32 array when ffmpeg succeeds."""
    pcm = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    run = _make_run_ok(pcm)

    audio_path = tmp_path / "test.m4a"
    audio_path.touch()

    result = decode_to_pcm(audio_path, run=run)

    assert result.dtype == np.float32
    np.testing.assert_array_almost_equal(result, pcm)


def test_decode_passes_correct_ffmpeg_args(tmp_path: Path) -> None:  # [AC-5]
    """ffmpeg is called with -ac 1 -ar 16000 -f f32le."""
    pcm = np.zeros(16000, dtype=np.float32)
    run = _make_run_ok(pcm)

    audio_path = tmp_path / "test.wav"
    audio_path.touch()
    decode_to_pcm(audio_path, run=run)

    call_args = run.call_args[0][0]
    assert "ffmpeg" in call_args[0]
    assert "-ac" in call_args
    assert "1" in call_args
    assert "-ar" in call_args
    assert "16000" in call_args
    assert "-f" in call_args
    assert "f32le" in call_args
    assert str(audio_path) in call_args


def test_ffmpeg_nonzero_raises(tmp_path: Path) -> None:  # [AC-11]
    """Non-zero ffmpeg exit code raises AudioDecodeError."""
    result = MagicMock()
    result.returncode = 1
    result.stdout = b""
    result.stderr = b"Invalid data found"
    run = MagicMock(return_value=result)

    audio_path = tmp_path / "corrupt.m4a"
    audio_path.touch()

    with pytest.raises(AudioDecodeError, match="ffmpeg exited with code 1"):
        decode_to_pcm(audio_path, run=run)


def test_ffmpeg_timeout_raises(tmp_path: Path) -> None:  # [AC-11]
    """TimeoutExpired from ffmpeg raises AudioDecodeError."""
    run = MagicMock(side_effect=subprocess.TimeoutExpired("ffmpeg", 120))

    audio_path = tmp_path / "slow.m4a"
    audio_path.touch()

    with pytest.raises(AudioDecodeError, match="timed out"):
        decode_to_pcm(audio_path, run=run)


def test_ffmpeg_missing_binary_raises(tmp_path: Path) -> None:  # [E-19]
    """FileNotFoundError (ffmpeg not installed) raises AudioDecodeError."""
    run = MagicMock(side_effect=FileNotFoundError("No such file or directory: 'ffmpeg'"))

    audio_path = tmp_path / "audio.m4a"
    audio_path.touch()

    with pytest.raises(AudioDecodeError, match="ffmpeg binary not found"):
        decode_to_pcm(audio_path, run=run)


def test_ffmpeg_empty_output_raises(tmp_path: Path) -> None:  # [E-4]
    """ffmpeg succeeds but returns zero bytes → AudioDecodeError."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = b""
    result.stderr = b""
    run = MagicMock(return_value=result)

    audio_path = tmp_path / "silent.m4a"
    audio_path.touch()

    with pytest.raises(AudioDecodeError, match="no output"):
        decode_to_pcm(audio_path, run=run)


def test_decode_large_array(tmp_path: Path) -> None:
    """decode_to_pcm handles a 1-second 16 kHz float32 array (16000 samples)."""
    pcm = np.sin(np.linspace(0, np.pi * 2, 16000)).astype(np.float32)
    run = _make_run_ok(pcm)

    audio_path = tmp_path / "one_sec.wav"
    audio_path.touch()

    result = decode_to_pcm(audio_path, run=run)

    assert result.shape == (16000,)
    assert result.dtype == np.float32
