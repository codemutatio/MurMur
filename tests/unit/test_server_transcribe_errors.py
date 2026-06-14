"""Tests for POST /transcribe validation + error paths — T-5, AC-8..12, AC-18, E-1..6, E-19, E-20, E-42, E-43."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
from fastapi.testclient import TestClient

from murmur.server.app import create_app
from murmur.server.audio import AudioDecodeError
from murmur.server.config import ServerConfig
from murmur.transcribe.base import TranscriptionError
from tests.fakes import FakeTranscriber

_TOKEN = "test-secret-token"
_PCM = np.zeros(16000, dtype=np.float32)


def _make_decode_fn(
    pcm: np.ndarray | None = None,
    error: Exception | None = None,
) -> MagicMock:
    if error is not None:
        return MagicMock(side_effect=error)
    arr = pcm if pcm is not None else _PCM.copy()
    return MagicMock(return_value=arr)


def _make_client(
    *,
    transcriber: FakeTranscriber | None = None,
    config: ServerConfig | None = None,
    decode_fn: MagicMock | None = None,
    model_loaded: bool = True,
    token: str = _TOKEN,
) -> TestClient:
    flag = threading.Event()
    if model_loaded:
        flag.set()
    t = transcriber or FakeTranscriber()
    cfg = config or ServerConfig()
    d = decode_fn or _make_decode_fn()
    app = create_app(t, token, cfg, model_loaded_flag=flag, decode_fn=d)
    return TestClient(app, raise_server_exceptions=False)


def _audio_post(
    client: TestClient,
    *,
    filename: str = "test.m4a",
    content_type: str = "audio/m4a",
    body: bytes = b"audio-data",
    extra_data: dict[str, str] | None = None,
    auth: str | None = None,
) -> object:
    headers: dict[str, str] = {}
    if auth is not None:
        headers["Authorization"] = auth
    else:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    kwargs: dict[str, object] = {
        "headers": headers,
        "files": {"file": (filename, io.BytesIO(body), content_type)},
    }
    if extra_data:
        kwargs["data"] = extra_data
    return client.post("/transcribe", **kwargs)


# ---------------------------------------------------------------------------
# AC-9 — Auth checked before any ffmpeg or transcriber invocation
# ---------------------------------------------------------------------------


def test_401_before_ffmpeg() -> None:  # [AC-9]
    """Auth failure returns 401 before ffmpeg is invoked."""
    decode_fn = _make_decode_fn()
    transcriber = FakeTranscriber()
    client = _make_client(decode_fn=decode_fn, transcriber=transcriber)

    resp = _audio_post(client, auth="Bearer wrong-token")

    assert resp.status_code == 401
    assert resp.json()["error_code"] == "unauthorized"
    decode_fn.assert_not_called()
    assert transcriber.call_count == 0


# ---------------------------------------------------------------------------
# AC-8 / E-1 / E-2 — 413 when size > max; 25 MB exactly accepted
# ---------------------------------------------------------------------------

_MB = 1024 * 1024


def test_413_at_25mb_plus_one() -> None:  # [AC-8, E-2]
    """File of 25 MB + 1 byte is rejected with 413 before ffmpeg."""
    decode_fn = _make_decode_fn()
    client = _make_client(decode_fn=decode_fn)

    over_limit = b"x" * (25 * _MB + 1)
    resp = _audio_post(client, body=over_limit)

    assert resp.status_code == 413
    assert resp.json()["error_code"] == "file_too_large"
    decode_fn.assert_not_called()


def test_25mb_exactly_accepted() -> None:  # [AC-8, E-1]
    """File of exactly 25 MB is accepted and processed normally."""
    # We need a FakeTranscriber that returns a valid result for clean_result to pass
    pcm = np.ones(16000, dtype=np.float32) * 0.1
    decode_fn = _make_decode_fn(pcm=pcm)
    client = _make_client(decode_fn=decode_fn)

    at_limit = b"x" * (25 * _MB)
    resp = _audio_post(client, body=at_limit)

    assert resp.status_code == 200
    decode_fn.assert_called_once()


# ---------------------------------------------------------------------------
# AC-10 / E-5 / E-6 — 415 on unsupported type; .m4a + octet-stream accepted
# ---------------------------------------------------------------------------


def test_415_unsupported_type() -> None:  # [AC-10, E-6]
    """File with .txt extension and text/plain MIME is rejected with 415."""
    client = _make_client()
    resp = _audio_post(client, filename="note.txt", content_type="text/plain")

    assert resp.status_code == 415
    assert resp.json()["error_code"] == "unsupported_media_type"


def test_octet_stream_m4a_accepted() -> None:  # [E-5]
    """m4a extension + application/octet-stream MIME is accepted (E-5)."""
    decode_fn = _make_decode_fn()
    client = _make_client(decode_fn=decode_fn)

    resp = _audio_post(
        client,
        filename="voice.m4a",
        content_type="application/octet-stream",
    )

    assert resp.status_code == 200
    decode_fn.assert_called_once()


def test_415_no_extension_and_bad_mime() -> None:  # [AC-10]
    """File with no extension and text/html MIME is rejected."""
    client = _make_client()
    resp = _audio_post(client, filename="noext", content_type="text/html")

    assert resp.status_code == 415


def test_wav_mime_accepted() -> None:
    """audio/wav MIME type is accepted."""
    decode_fn = _make_decode_fn()
    client = _make_client(decode_fn=decode_fn)
    resp = _audio_post(client, filename="test.wav", content_type="audio/wav")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# E-42 / E-43 — missing file field and zero-byte file
# ---------------------------------------------------------------------------


def test_missing_file_400() -> None:  # [E-42]
    """Request with no file field returns 400 missing_file."""
    flag = threading.Event()
    flag.set()
    app = create_app(
        FakeTranscriber(), _TOKEN, ServerConfig(), model_loaded_flag=flag
    )
    client = TestClient(app, raise_server_exceptions=False)

    # Send multipart without a 'file' field
    resp = client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        data={"language": "en"},  # form data only, no file
    )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "missing_file"


def test_empty_file_400() -> None:  # [E-43]
    """Request with a zero-byte file returns 400 empty_file."""
    client = _make_client()
    resp = _audio_post(client, body=b"")

    assert resp.status_code == 400
    assert resp.json()["error_code"] == "empty_file"


# ---------------------------------------------------------------------------
# AC-11 / E-4 / E-19 — ffmpeg fails → 422 audio_decode_failed
# ---------------------------------------------------------------------------


def test_decode_failed_422() -> None:  # [AC-11, E-4]
    """ffmpeg non-zero exit → 422 audio_decode_failed."""
    decode_fn = _make_decode_fn(error=AudioDecodeError("ffmpeg exited with code 1"))
    client = _make_client(decode_fn=decode_fn)

    resp = _audio_post(client)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "audio_decode_failed"


def test_ffmpeg_missing_returns_422() -> None:  # [E-19]
    """ffmpeg binary missing → 422 audio_decode_failed."""
    decode_fn = _make_decode_fn(error=AudioDecodeError("ffmpeg binary not found"))
    client = _make_client(decode_fn=decode_fn)

    resp = _audio_post(client)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "audio_decode_failed"


# ---------------------------------------------------------------------------
# AC-12 — TranscriptionError → 422 transcription_failed
# ---------------------------------------------------------------------------


def test_transcription_failed_422() -> None:  # [AC-12]
    """Transcriber raises TranscriptionError → 422 transcription_failed."""
    fake = FakeTranscriber()
    fake.push_result(TranscriptionError("model crashed"))
    client = _make_client(transcriber=fake)

    resp = _audio_post(client)

    assert resp.status_code == 422
    assert resp.json()["error_code"] == "transcription_failed"


# ---------------------------------------------------------------------------
# E-20 — model not yet loaded → 503 model_not_loaded
# ---------------------------------------------------------------------------


def test_503_model_not_loaded() -> None:  # [E-20]
    """POST /transcribe before model is ready returns 503 model_not_loaded."""
    client = _make_client(model_loaded=False)
    resp = _audio_post(client)

    assert resp.status_code == 503
    assert resp.json()["error_code"] == "model_not_loaded"


# ---------------------------------------------------------------------------
# AC-18 — temp file deleted in both success and error paths
# ---------------------------------------------------------------------------


def test_temp_deleted_on_success() -> None:  # [AC-18]
    """Temp audio file is deleted after a successful transcription."""
    created_paths: list[Path] = []
    original_decode = _make_decode_fn()

    def tracking_decode(path: Path) -> np.ndarray:
        created_paths.append(path)
        return original_decode(path)

    client = _make_client(decode_fn=tracking_decode)  # type: ignore[arg-type]
    resp = _audio_post(client)

    assert resp.status_code == 200
    assert len(created_paths) == 1
    # File must be gone after the response
    assert not created_paths[0].exists(), f"Temp file still exists: {created_paths[0]}"


def test_temp_deleted_on_error() -> None:  # [AC-18]
    """Temp audio file is deleted after a transcription error."""
    created_paths: list[Path] = []

    def tracking_decode(path: Path) -> np.ndarray:
        created_paths.append(path)
        raise AudioDecodeError("forced error")

    client = _make_client(decode_fn=tracking_decode)  # type: ignore[arg-type]
    resp = _audio_post(client)

    assert resp.status_code == 422
    assert len(created_paths) == 1
    assert not created_paths[0].exists(), f"Temp file still exists: {created_paths[0]}"
