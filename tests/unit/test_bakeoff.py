"""Tests for scripts/bakeoff.py — T-10."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from murmur.transcribe.base import SegmentScore, TranscriptionResult


def _fake_transcription(text: str) -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        segments=(
            SegmentScore(text=text, no_speech_prob=0.0, compression_ratio=1.0, avg_logprob=-0.3),
        ),
    )


def _run_bakeoff(tmp_path: Path, wav_path: Path, extra_env: dict | None = None) -> tuple[int, str]:
    """Run bakeoff.py as a subprocess with --input."""
    import os

    env = dict(os.environ)
    env["MURMUR_HOME"] = str(tmp_path / "murmur_home")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/bakeoff.py",
            "--input",
            str(wav_path),
            "--output",
            str(tmp_path / "out.md"),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, (tmp_path / "out.md").read_text(encoding="utf-8") if (
        tmp_path / "out.md"
    ).exists() else ""


def _make_wav(path: Path, duration_s: float = 0.5) -> Path:
    """Create a minimal 16 kHz mono WAV file."""
    import wave

    samples = np.zeros(int(duration_s * 16000), dtype=np.float32)
    pcm = (samples * 32767).astype("int16")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())
    return path


def test_four_sections_with_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bakeoff.py produces a Markdown file with four labeled sections (using mocked transcribers)."""
    wav = _make_wav(tmp_path / "test.wav")

    fake_result = _fake_transcription("hello world")

    # Patch the transcribers

    mock_manager = MagicMock()
    mock_manager.ensure.return_value = "fake-model"
    monkeypatch.setattr("murmur.transcribe.models.ModelManager", lambda **kw: mock_manager)

    mock_local = MagicMock()
    mock_local.transcribe.return_value = fake_result
    monkeypatch.setattr("murmur.transcribe.local.MlxWhisperTranscriber", lambda r: mock_local)
    monkeypatch.setattr("murmur.transcribe.local.assert_apple_silicon", lambda: None)

    mock_api = MagicMock()
    mock_api.transcribe.return_value = fake_result
    monkeypatch.setattr("murmur.transcribe.api.OpenAITranscriber", lambda **kw: mock_api)
    monkeypatch.setattr("murmur.mac.keychain.get_openai_api_key", lambda *a, **kw: "sk-fake")

    # Run via subprocess (tests the script is importable and runnable)
    # Do in-process test instead since we're mocking
    import importlib.util

    spec = importlib.util.spec_from_file_location("bakeoff", "scripts/bakeoff.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)

    # Patch ModelManager in the script's namespace
    with (
        patch("murmur.transcribe.models.ModelManager") as mm_mock,
        patch("murmur.transcribe.local.MlxWhisperTranscriber") as local_mock,
        patch("murmur.transcribe.local.assert_apple_silicon"),
        patch("murmur.transcribe.api.OpenAITranscriber") as api_mock,
        patch("murmur.mac.keychain.get_openai_api_key", return_value="sk-fake"),
    ):
        mm_mock.return_value.ensure.return_value = "fake-model"
        local_instance = MagicMock()
        local_instance.transcribe.return_value = fake_result
        local_mock.return_value = local_instance

        api_instance = MagicMock()
        api_instance.transcribe.return_value = fake_result
        api_mock.return_value = api_instance

        output = tmp_path / "bakeoff.md"

        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        # Invoke main directly
        old_argv = sys.argv
        sys.argv = ["bakeoff.py", "--input", str(wav), "--output", str(output)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv

    assert rc == 0, "bakeoff.main() should return 0"
    assert output.exists()
    content = output.read_text(encoding="utf-8")

    # Check four sections
    assert "## mlx large-v3" in content
    assert "## mlx large-v3-turbo" in content
    assert "## OpenAI gpt-4o-mini-transcribe" in content
    assert "## OpenAI gpt-4o-transcribe" in content


def test_missing_key_sections_marked_skipped(tmp_path: Path) -> None:
    """When OPENAI_API_KEY is absent, API sections say SKIPPED."""
    wav = _make_wav(tmp_path / "test.wav")

    fake_result = _fake_transcription("hello")

    import importlib.util

    spec = importlib.util.spec_from_file_location("bakeoff2", "scripts/bakeoff.py")
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)

    with (
        patch("murmur.transcribe.models.ModelManager") as mm_mock,
        patch("murmur.transcribe.local.MlxWhisperTranscriber") as local_mock,
        patch("murmur.transcribe.local.assert_apple_silicon"),
        patch("murmur.mac.keychain.get_openai_api_key", return_value=None),
    ):
        mm_mock.return_value.ensure.return_value = "fake-model"
        local_instance = MagicMock()
        local_instance.transcribe.return_value = fake_result
        local_mock.return_value = local_instance

        output = tmp_path / "bakeoff_no_key.md"
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        old_argv = sys.argv
        sys.argv = ["bakeoff.py", "--input", str(wav), "--output", str(output)]
        try:
            rc = mod.main()
        finally:
            sys.argv = old_argv

    assert rc == 0
    content = output.read_text(encoding="utf-8")
    assert "SKIPPED" in content
