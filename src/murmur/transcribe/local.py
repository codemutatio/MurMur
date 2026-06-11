"""mlx-whisper local transcriber.

Audio is always passed as a float32 numpy array — NO ffmpeg on this machine.
"""
from __future__ import annotations

import platform
import sys

import numpy as np

from murmur.transcribe.base import SegmentScore, TranscriptionError, TranscriptionResult


def assert_apple_silicon() -> None:
    """Raise with an actionable message if not running on Apple Silicon (E-24)."""
    if not (sys.platform == "darwin" and platform.machine() == "arm64"):
        raise RuntimeError(
            "murmur requires Apple Silicon (arm64 macOS). "
            f"Detected: {sys.platform} / {platform.machine()}. "
            "mlx-whisper uses the Apple Neural Engine / Metal Performance Shaders "
            "and is not functional on Intel or non-Apple hardware."
        )


class MlxWhisperTranscriber:
    """Transcriber backed by mlx-whisper (Apple Silicon, Metal/ANE).

    The model is loaded once at construction time. Audio is passed as a
    float32 numpy array — ffmpeg is never called (plan constraint).
    """

    def __init__(self, model_ref: str) -> None:
        """Initialize with a local snapshot path or Hugging Face repo ID.

        Args:
            model_ref: Path to a local snapshot directory or HF repo id
                (e.g. ``"mlx-community/whisper-large-v3-mlx"``).
        """
        assert_apple_silicon()
        self._model_ref = model_ref

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        """Transcribe *audio* via mlx-whisper.

        Args:
            audio: float32 mono 16 kHz numpy array.
            language: BCP-47 language code (e.g. ``"en"``).
            initial_prompt: Whisper initial_prompt string or None.

        Returns:
            TranscriptionResult with per-segment scores.

        Raises:
            TranscriptionError: on any mlx-whisper failure.
        """
        try:
            import mlx_whisper  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TranscriptionError(
                "mlx_whisper is not installed. Install it with: pip install mlx-whisper"
            ) from exc

        try:
            raw = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self._model_ref,
                language=language,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                verbose=None,
            )
        except Exception as exc:
            raise TranscriptionError(str(exc)) from exc

        segments = tuple(
            SegmentScore(
                text=s.get("text", ""),
                no_speech_prob=float(s.get("no_speech_prob", 0.0)),
                compression_ratio=float(s.get("compression_ratio", 1.0)),
                avg_logprob=float(s.get("avg_logprob", 0.0)),
            )
            for s in raw.get("segments", [])
        )
        text: str = raw.get("text", "")
        return TranscriptionResult(text=text, segments=segments)
