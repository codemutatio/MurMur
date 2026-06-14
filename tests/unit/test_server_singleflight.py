"""Tests for single-flight asyncio.Lock — T-6, AC-13, AC-14, E-10, E-11."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from murmur.server.app import create_app
from murmur.server.config import ServerConfig
from tests.fakes import FakeTranscriber

_TOKEN = "test-secret-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _make_slow_decode(delay: float) -> MagicMock:
    """Return a decode function that blocks for *delay* seconds then returns PCM."""

    def slow_decode(path: Path) -> np.ndarray:
        time.sleep(delay)
        return np.zeros(16000, dtype=np.float32)

    return slow_decode  # type: ignore[return-value]


def _make_app(
    *,
    decode_fn: object = None,
    transcriber: FakeTranscriber | None = None,
) -> object:
    flag = threading.Event()
    flag.set()
    t = transcriber or FakeTranscriber()
    d = decode_fn or MagicMock(return_value=np.zeros(16000, dtype=np.float32))
    return create_app(t, _TOKEN, ServerConfig(), model_loaded_flag=flag, decode_fn=d)


# ---------------------------------------------------------------------------
# AC-14 / E-10 — second concurrent transcribe → 503 model_busy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_transcribe_503_model_busy() -> None:  # [AC-14, E-10]
    """While one transcription holds the lock, a second POST gets 503 model_busy."""
    app = _make_app(decode_fn=_make_slow_decode(0.3))

    audio_bytes = b"fake-audio-data"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Start the first request (will hold the lock for ~0.3 s)
        task1 = asyncio.create_task(
            client.post(
                "/transcribe",
                headers=_AUTH,
                files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
            )
        )

        # Give the first request a moment to acquire the lock
        await asyncio.sleep(0.05)

        # Second request should get 503 model_busy immediately
        resp2 = await client.post(
            "/transcribe",
            headers=_AUTH,
            files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
        )

        resp1 = await task1

    # First request should succeed (or at least not 503)
    assert resp1.status_code == 200, f"First request failed: {resp1.text}"
    # Second request must get 503 model_busy
    assert resp2.status_code == 503, f"Expected 503, got {resp2.status_code}: {resp2.text}"
    assert resp2.json()["error_code"] == "model_busy"


# ---------------------------------------------------------------------------
# AC-13 / E-11 — /health stays fast while transcription lock is held
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_not_blocked_by_lock() -> None:  # [AC-13, E-11]
    """GET /health completes in < 500 ms even while the transcription lock is held."""
    app = _make_app(decode_fn=_make_slow_decode(1.0))  # 1 second decode

    audio_bytes = b"fake-audio-data"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Start a transcription (will hold the lock for ~1 s)
        task = asyncio.create_task(
            client.post(
                "/transcribe",
                headers=_AUTH,
                files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
            )
        )

        # Give the transcription a moment to acquire the lock
        await asyncio.sleep(0.05)

        # Health check must complete well within 500 ms
        t0 = time.monotonic()
        resp_health = await client.get("/health", headers=_AUTH)
        elapsed = time.monotonic() - t0

        await task  # let the background transcription finish

    assert resp_health.status_code == 200
    assert elapsed < 0.5, (
        f"GET /health took {elapsed:.3f}s while lock was held (expected < 0.5s)"
    )
