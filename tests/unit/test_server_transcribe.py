"""Tests for POST /transcribe happy path — T-4, AC-4, AC-5, AC-6, AC-7, E-7, E-8, E-9."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
from fastapi.testclient import TestClient

from murmur.server.app import create_app
from murmur.server.config import ServerConfig, ServerSttSettings
from tests.fakes import FakeTranscriber

_TOKEN = "test-secret-token"

# Minimal float32 PCM: 1 second at 16 kHz
_PCM = np.zeros(16000, dtype=np.float32)


def _make_decode_fn(pcm: np.ndarray = _PCM) -> MagicMock:
    """Return a mock decode function that succeeds and yields *pcm*."""
    fn = MagicMock(return_value=pcm.copy())
    return fn


def _make_client(
    *,
    transcriber: FakeTranscriber | None = None,
    config: ServerConfig | None = None,
    decode_fn: MagicMock | None = None,
    model_loaded: bool = True,
) -> tuple[TestClient, FakeTranscriber, MagicMock]:
    flag = threading.Event()
    if model_loaded:
        flag.set()
    t = transcriber or FakeTranscriber()
    cfg = config or ServerConfig()
    d = decode_fn or _make_decode_fn()
    app = create_app(t, _TOKEN, cfg, model_loaded_flag=flag, decode_fn=d)
    client = TestClient(app, raise_server_exceptions=False)
    return client, t, d


def _post_audio(client: TestClient, filename: str = "test.m4a") -> object:
    return client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        files={"file": (filename, io.BytesIO(b"fake-audio-content"), "audio/m4a")},
    )


# ---------------------------------------------------------------------------
# AC-4 — 200 body has all required keys
# ---------------------------------------------------------------------------


def test_transcribe_200_shape() -> None:  # [AC-4]
    """POST /transcribe 200 response contains all required fields per §A."""
    client, _, _ = _make_client()
    resp = _post_audio(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "text" in body
    assert "segments" in body
    assert isinstance(body["segments"], list)
    assert "language" in body
    assert "duration_sec" in body
    assert body["duration_sec"] > 0
    assert body["model"] == "large-v3"
    assert body["engine"] == "mac"
    assert body["text"]  # non-empty (AC-4)


def test_transcribe_200_language_field() -> None:  # [AC-4]
    """language field in response matches the requested language."""
    client, _, _ = _make_client()
    resp = client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        files={"file": ("test.m4a", io.BytesIO(b"audio"), "audio/m4a")},
        data={"language": "fr"},
    )

    assert resp.status_code == 200
    assert resp.json()["language"] == "fr"


# ---------------------------------------------------------------------------
# AC-5 — ffmpeg decode called, transcriber receives float32 16k array
# ---------------------------------------------------------------------------


def test_passes_float32_16k_to_transcriber() -> None:  # [AC-5]
    """The transcriber receives a float32 array with the correct shape from PCM."""
    pcm = np.ones(32000, dtype=np.float32) * 0.5  # 2 seconds
    client, transcriber, _ = _make_client(decode_fn=_make_decode_fn(pcm))

    resp = _post_audio(client)

    assert resp.status_code == 200
    assert transcriber.call_count == 1
    call = transcriber.calls[0]
    assert call["audio_len"] == 32000


def test_decode_fn_called_with_temp_path(tmp_path: Path) -> None:  # [AC-5]
    """decode_fn is called with a Path argument (the temp file)."""
    client, _, decode_fn = _make_client()
    _post_audio(client)

    decode_fn.assert_called_once()
    arg = decode_fn.call_args[0][0]
    assert isinstance(arg, Path)


def test_duration_sec_from_pcm_length() -> None:  # [AC-5, Assumption #6]
    """duration_sec = len(pcm) / 16000."""
    pcm = np.zeros(48000, dtype=np.float32)  # 3 seconds
    client, _, _ = _make_client(decode_fn=_make_decode_fn(pcm))

    resp = _post_audio(client)

    assert resp.status_code == 200
    assert abs(resp.json()["duration_sec"] - 3.0) < 1e-6


# ---------------------------------------------------------------------------
# AC-6 — vocab_hints → initial_prompt via build_initial_prompt
# ---------------------------------------------------------------------------


def test_initial_prompt_from_vocab_hints() -> None:  # [AC-6]
    """vocab_hints field is built into initial_prompt passed to transcriber."""
    client, transcriber, _ = _make_client()

    client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        files={"file": ("test.m4a", io.BytesIO(b"audio"), "audio/m4a")},
        data={"vocab_hints": "Rivy,Yassir"},
    )

    assert transcriber.call_count == 1
    initial_prompt = transcriber.calls[0]["initial_prompt"]
    assert initial_prompt is not None
    assert "Rivy" in initial_prompt
    assert "Yassir" in initial_prompt


def test_default_vocab_when_absent() -> None:  # [AC-6, E-8]
    """When vocab_hints is absent, server defaults from config are used."""
    cfg = ServerConfig(
        stt=ServerSttSettings(vocab_hints=("FooHint", "BarHint"))
    )
    client, transcriber, _ = _make_client(config=cfg)

    _post_audio(client)  # no vocab_hints in request

    initial_prompt = transcriber.calls[0]["initial_prompt"]
    assert initial_prompt is not None
    assert "FooHint" in initial_prompt


def test_empty_vocab_hints_skipped(tmp_path: Path) -> None:  # [E-7]
    """Empty/whitespace-only hints produce initial_prompt=None."""
    # Override config with empty default hints to isolate the request hints
    cfg = ServerConfig(stt=ServerSttSettings(vocab_hints=()))
    client, transcriber, _ = _make_client(config=cfg)

    client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        files={"file": ("test.m4a", io.BytesIO(b"audio"), "audio/m4a")},
        data={"vocab_hints": "  ,  , "},  # whitespace only
    )

    assert transcriber.calls[0]["initial_prompt"] is None


# ---------------------------------------------------------------------------
# AC-6 — language defaults to "en" when absent (E-9)
# ---------------------------------------------------------------------------


def test_language_defaults_en() -> None:  # [AC-6, E-9]
    """When language field is absent, transcriber is called with language='en'."""
    client, transcriber, _ = _make_client()

    _post_audio(client)  # no language field

    assert transcriber.calls[0]["language"] == "en"


# ---------------------------------------------------------------------------
# AC-7 — clean_result None → 422 no_speech_detected (E-3)
# ---------------------------------------------------------------------------


def test_no_speech_returns_422() -> None:  # [AC-7, E-3]
    """When clean_result returns None, the endpoint returns 422 no_speech_detected."""
    # Use a result that will be filtered by clean_result (filler phrase)
    fake = FakeTranscriber()

    fake.push_result(
        # Override: push a TranscriptionResult directly via a custom approach.
        # FakeTranscriber.push_result accepts str | Exception.
        # Simplest: push a str that maps to a filler phrase that clean_result filters.
        "thank you for watching"
    )
    # "thank you for watching" is in FILLER_PHRASES, so clean_result returns None.
    client, _, _ = _make_client(transcriber=fake)

    resp = _post_audio(client)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "no_speech_detected"


def test_segments_in_200_response() -> None:  # [AC-4]
    """Each segment in the response has the expected keys."""
    client, _, _ = _make_client()
    resp = _post_audio(client)

    assert resp.status_code == 200
    segments = resp.json()["segments"]
    assert len(segments) >= 1
    seg = segments[0]
    assert "text" in seg
    assert "no_speech_prob" in seg
    assert "compression_ratio" in seg
    assert "avg_logprob" in seg
