"""Tests for murmur.transcribe.api (OpenAITranscriber) — T-8."""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from murmur.transcribe.api import OpenAITranscriber
from murmur.transcribe.base import TranscriptionError


def _make_fake_client(
    response: str | Exception = "transcription text",
) -> tuple[MagicMock, MagicMock]:
    """Return (client_instance, factory) where factory() returns client_instance."""
    client = MagicMock()
    if isinstance(response, Exception):
        client.audio.transcriptions.create.side_effect = response
    else:
        client.audio.transcriptions.create.return_value = response
    factory = MagicMock(return_value=client)
    return client, factory


def test_request_shape() -> None:
    """Fake client receives correct model, language, and a file-like object."""
    client, factory = _make_fake_client("hello world")
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    audio = np.zeros(16000, dtype=np.float32)
    result = t.transcribe(audio, language="en", initial_prompt=None)

    client.audio.transcriptions.create.assert_called_once()
    call_kwargs = client.audio.transcriptions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-transcribe"
    assert call_kwargs["language"] == "en"
    # The file argument should be a file-like object
    file_arg = call_kwargs["file"]
    assert hasattr(file_arg, "read"), "Expected a file-like object"
    assert result.text == "hello world"


def test_initial_prompt_sent_when_set() -> None:
    """initial_prompt is passed as 'prompt' to the API when not None."""
    client, factory = _make_fake_client("result")
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt="Glossary: Rivy.")
    call_kwargs = client.audio.transcriptions.create.call_args.kwargs
    assert call_kwargs.get("prompt") == "Glossary: Rivy."


def test_no_prompt_kwarg_when_none() -> None:
    """'prompt' key is absent from the API call when initial_prompt is None."""
    client, factory = _make_fake_client("result")
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt=None)
    call_kwargs = client.audio.transcriptions.create.call_args.kwargs
    assert "prompt" not in call_kwargs


# E-10
def test_error_maps_to_transcription_error() -> None:  # [E-10]
    """API errors are wrapped as TranscriptionError."""
    _, factory = _make_fake_client(RuntimeError("rate limit exceeded"))
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    with pytest.raises(TranscriptionError, match="rate limit"):
        t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt=None)


def test_sentinel_scores_in_result() -> None:
    """API results have sentinel quality scores (no_speech_prob=0, compression_ratio=1)."""
    _, factory = _make_fake_client("hello")
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    result = t.transcribe(np.zeros(100, dtype=np.float32), language="en", initial_prompt=None)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.no_speech_prob == 0.0
    assert seg.compression_ratio == 1.0


def test_wav_encoding_is_in_memory() -> None:
    """Audio is encoded in-memory; no temp files are created."""
    client, factory = _make_fake_client("text")
    t = OpenAITranscriber("gpt-4o-transcribe", "sk-fake", client_factory=factory)
    import io

    audio = np.random.default_rng(1).uniform(-0.5, 0.5, 16000).astype(np.float32)
    t.transcribe(audio, language="en", initial_prompt=None)
    file_arg = client.audio.transcriptions.create.call_args.kwargs["file"]
    assert isinstance(file_arg, io.BytesIO)
