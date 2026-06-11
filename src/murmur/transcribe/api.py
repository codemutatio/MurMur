"""OpenAI API transcriber for the api_fallback path (R-7)."""

from __future__ import annotations

import io
import wave
from collections.abc import Callable
from typing import Any

import numpy as np

from murmur.audio import SAMPLE_RATE
from murmur.transcribe.base import SegmentScore, TranscriptionError, TranscriptionResult


def _array_to_wav_bytes(audio: np.ndarray) -> bytes:
    """Encode a float32 numpy array as 16-bit PCM WAV in memory."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm_int16 = (pcm * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


class OpenAITranscriber:
    """Transcriber backed by the OpenAI audio transcription API (E-10, AC-8).

    API results use sentinel scores (no_speech_prob=0.0, compression_ratio=1.0)
    because the API does not return per-segment quality metrics. The hallucination
    guard still checks filler phrases on the result text.

    NO OpenAI client object is ever constructed when this transcriber is not
    instantiated (AC-7 guarantee: callers only create this when api_fallback=True).
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if client_factory is None:
            from openai import OpenAI

            client_factory = OpenAI

        self._model = model
        self._client = client_factory(api_key=api_key)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        """Transcribe *audio* via the OpenAI audio API.

        Encodes audio as an in-memory 16 kHz WAV (no ffmpeg, no temp files).

        Raises:
            TranscriptionError: on any API failure.
        """
        wav_bytes = _array_to_wav_bytes(audio)
        wav_file = io.BytesIO(wav_bytes)
        wav_file.name = "audio.wav"

        try:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "file": wav_file,
                "language": language,
                "response_format": "text",
            }
            if initial_prompt:
                kwargs["prompt"] = initial_prompt

            response = self._client.audio.transcriptions.create(**kwargs)
        except Exception as exc:
            raise TranscriptionError(str(exc)) from exc

        text = response if isinstance(response, str) else str(response)

        # Sentinel scores: API does not return per-segment quality metrics
        segment = SegmentScore(
            text=text,
            no_speech_prob=0.0,
            compression_ratio=1.0,
            avg_logprob=0.0,
        )
        return TranscriptionResult(text=text, segments=(segment,))
