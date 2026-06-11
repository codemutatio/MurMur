"""Tests for murmur bench CLI path — T-16, AC-20 de-risking."""

from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from murmur.cli import main


def _write_fake_wav(path: Path, duration_s: float = 1.0, sr: int = 16000) -> None:
    """Write a valid 16 kHz mono WAV file for bench testing."""
    samples = np.sin(np.linspace(0, 2 * np.pi * 440 * duration_s, int(sr * duration_s))) * 0.5
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def test_bench_reports_rtf_with_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """bench command with a fake transcriber prints RTF and exits correctly."""
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))

    wav_path = tmp_path / "test.wav"
    _write_fake_wav(wav_path, duration_s=2.0)

    # Patch assert_apple_silicon to not raise on non-arm64 CI
    # Patch ModelManager.ensure to return a fake path
    # Patch MlxWhisperTranscriber to return a fast fake result
    from murmur.transcribe.base import SegmentScore, TranscriptionResult

    fake_result = TranscriptionResult(
        text="bench test",
        segments=(
            SegmentScore(
                text="bench test", no_speech_prob=0.0, compression_ratio=1.0, avg_logprob=-0.3
            ),
        ),
    )

    with (
        patch("murmur.transcribe.local.assert_apple_silicon"),
        patch("murmur.transcribe.models.ModelManager.ensure", return_value="fake/model"),
        patch(
            "murmur.transcribe.local.MlxWhisperTranscriber.transcribe",
            return_value=fake_result,
        ),
    ):
        main(["bench", str(wav_path)])

    out = capsys.readouterr().out
    assert "RTF" in out
    assert "Audio" in out
    assert "Wall" in out


def test_bench_nonexistent_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bench exits 1 when the WAV file does not exist."""
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))

    result = main(["bench", str(tmp_path / "missing.wav")])
    assert result == 1
