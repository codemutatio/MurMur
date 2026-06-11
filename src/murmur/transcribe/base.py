"""Base types for the transcription sub-system."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


class TranscriptionError(Exception):
    """Raised by Transcriber.transcribe() on unrecoverable failure."""


@dataclass(frozen=True)
class SegmentScore:
    """Whisper segment with quality metrics used by the hallucination guard."""

    text: str
    no_speech_prob: float
    compression_ratio: float
    avg_logprob: float


@dataclass(frozen=True)
class TranscriptionResult:
    """Result of a transcription call."""

    text: str
    segments: tuple[SegmentScore, ...]


class Transcriber(Protocol):
    """Protocol for a speech-to-text engine."""

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        """Transcribe *audio* (float32 mono 16 kHz numpy array).

        Raises:
            TranscriptionError: on unrecoverable failure.
        """
        ...
