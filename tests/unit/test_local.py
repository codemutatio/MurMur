"""Tests for murmur.transcribe.local (MlxWhisperTranscriber) — T-7."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from murmur.transcribe.base import TranscriptionError
from murmur.transcribe.local import MlxWhisperTranscriber, assert_apple_silicon


def _make_mlx_result(text: str = "hello world") -> dict:
    return {
        "text": text,
        "segments": [
            {
                "text": text,
                "no_speech_prob": 0.05,
                "compression_ratio": 1.2,
                "avg_logprob": -0.3,
            }
        ],
    }


# E-24
def test_non_arm64_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:  # [E-24]
    """assert_apple_silicon raises a clear message on non-Apple Silicon."""
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        assert_apple_silicon()


def test_arm64_darwin_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """assert_apple_silicon passes on arm64 macOS."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")
    assert_apple_silicon()  # should not raise


# AC-18 (via call inspection)
def test_initial_prompt_passed(monkeypatch: pytest.MonkeyPatch) -> None:  # [AC-18]
    """MlxWhisperTranscriber passes initial_prompt to mlx_whisper.transcribe."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")

    mock_transcribe = MagicMock(return_value=_make_mlx_result())
    mock_mlx = MagicMock()
    mock_mlx.transcribe = mock_transcribe

    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        t = MlxWhisperTranscriber("some-model-ref")
        audio = np.zeros(16000, dtype=np.float32)
        t.transcribe(audio, language="en", initial_prompt="Glossary: Rivy, BigQuery.")

    call_kwargs = mock_transcribe.call_args
    assert call_kwargs.kwargs.get("initial_prompt") == "Glossary: Rivy, BigQuery."


def test_array_not_path_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """mlx_whisper.transcribe receives the numpy array directly, not a file path."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")

    mock_transcribe = MagicMock(return_value=_make_mlx_result())
    mock_mlx = MagicMock()
    mock_mlx.transcribe = mock_transcribe

    audio = np.zeros(8000, dtype=np.float32)
    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        t = MlxWhisperTranscriber("test-model")
        t.transcribe(audio, language="en", initial_prompt=None)

    call_args = mock_transcribe.call_args
    first_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("audio")
    assert isinstance(first_arg, np.ndarray), "Should pass numpy array, not a path"


def test_mlx_exception_raises_transcription_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """mlx-whisper exception → TranscriptionError."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")

    mock_mlx = MagicMock()
    mock_mlx.transcribe.side_effect = RuntimeError("CUDA error")

    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        t = MlxWhisperTranscriber("test-model")
        with pytest.raises(TranscriptionError):
            t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt=None)


def test_result_has_segment_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """TranscriptionResult contains SegmentScores with correct values."""
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    monkeypatch.setattr("sys.platform", "darwin")

    raw = {
        "text": "test",
        "segments": [
            {
                "text": "test",
                "no_speech_prob": 0.1,
                "compression_ratio": 1.3,
                "avg_logprob": -0.5,
            }
        ],
    }
    mock_mlx = MagicMock()
    mock_mlx.transcribe.return_value = raw

    with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
        t = MlxWhisperTranscriber("test-model")
        result = t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt=None)

    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.no_speech_prob == pytest.approx(0.1)
    assert seg.compression_ratio == pytest.approx(1.3)
    assert seg.avg_logprob == pytest.approx(-0.5)
