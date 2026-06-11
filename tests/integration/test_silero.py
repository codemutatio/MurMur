"""Integration tests for SileroVAD adapter — T-4.

Uses macOS `say` to generate real speech without binary fixtures or downloads.
Model is bundled in the silero-vad wheel — no network required.
"""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE
from murmur.audio.vad import SileroVAD


def _generate_speech_wav(path: Path, text: str = "Hello this is a test") -> None:
    """Use macOS `say` to generate speech, resampled to 16kHz mono via afconvert."""
    aiff_path = path.with_suffix(".aiff")
    subprocess.run(
        ["say", "-o", str(aiff_path), text],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "afconvert",
            "-f", "WAVE",
            "-d", "LEI16@16000",
            "-c", "1",
            str(aiff_path),
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    aiff_path.unlink(missing_ok=True)


def _load_frames(path: Path) -> list[np.ndarray]:
    """Load a WAV into 512-sample float32 frames."""
    with wave.open(str(path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(samples) // FRAME_SAMPLES
    return [samples[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES] for i in range(n)]


def _silence_frames(n: int) -> list[np.ndarray]:
    return [np.zeros(FRAME_SAMPLES, dtype=np.float32) for _ in range(n)]


def test_detects_say_speech(tmp_path: Path) -> None:
    """SileroVAD detects speech in a `say`-generated WAV (real VAD conformance)."""
    wav = tmp_path / "speech.wav"
    _generate_speech_wav(wav, "Testing one two three four five")
    frames = _load_frames(wav)
    assert len(frames) > 0, "No frames loaded from generated speech"

    vad = SileroVAD()
    probs = [vad.speech_prob(f) for f in frames]
    max_prob = max(probs)
    assert max_prob > 0.5, (
        f"SileroVAD did not detect speech in say-generated audio "
        f"(max_prob={max_prob:.3f})"
    )


def test_rejects_silence(tmp_path: Path) -> None:
    """SileroVAD returns low probability on digital silence."""
    vad = SileroVAD()
    # 30 frames = ~1 s of digital silence
    probs = [vad.speech_prob(f) for f in _silence_frames(30)]
    avg_prob = sum(probs) / len(probs)
    assert avg_prob < 0.3, (
        f"SileroVAD falsely detected speech in silence (avg_prob={avg_prob:.3f})"
    )


def test_reset_clears_state(tmp_path: Path) -> None:
    """reset() can be called without error and subsequent calls work normally."""
    vad = SileroVAD()
    frame = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    vad.speech_prob(frame)
    vad.reset()
    prob = vad.speech_prob(frame)
    assert 0.0 <= prob <= 1.0
