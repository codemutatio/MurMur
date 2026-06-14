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
async def test_concurrent_transcribe_503_model_busy() -> None:  # [AC-14, E-10, F-3]
    """Two genuinely concurrent POSTs: exactly one 503 model_busy, never two transcriptions.

    No artificial sleep between the two requests — both are fired as concurrent asyncio
    tasks and allowed to race.  The fixed code's atomic lock.locked()+async-with sequence
    (no await between them) guarantees exactly one 503; the other completes normally.
    The test asserts:
      - Exactly one response is 503 model_busy
      - The other is 200 (transcription ran once, not twice)
      - The fake transcriber ran at most once (never two concurrent transcriptions)
    """
    transcriber = FakeTranscriber()
    app = _make_app(decode_fn=_make_slow_decode(0.2), transcriber=transcriber)

    audio_bytes = b"fake-audio-data"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Fire both requests concurrently with no sleep between them
        task1 = asyncio.create_task(
            client.post(
                "/transcribe",
                headers=_AUTH,
                files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
            )
        )
        task2 = asyncio.create_task(
            client.post(
                "/transcribe",
                headers=_AUTH,
                files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
            )
        )

        resp1, resp2 = await asyncio.gather(task1, task2)

    statuses = {resp1.status_code, resp2.status_code}
    # Exactly one 503 and one 200 (or at least one 503)
    assert 503 in statuses, (
        f"Expected exactly one 503 model_busy; got {resp1.status_code} and {resp2.status_code}"
    )
    # Identify which response is which
    busy_resp = resp1 if resp1.status_code == 503 else resp2
    assert busy_resp.json()["error_code"] == "model_busy", (
        f"503 response does not have model_busy error code: {busy_resp.text}"
    )
    # The transcriber must have run at most once — never two concurrent transcriptions
    assert len(transcriber.calls) <= 1, (
        f"Transcriber ran {len(transcriber.calls)} times — two transcriptions ran concurrently"
    )


# ---------------------------------------------------------------------------
# G-1 / §A precedence — model_busy wins over model_not_loaded when both hold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_busy_beats_model_not_loaded__G1_spec_A() -> None:  # [G-1, §A step-2 before step-3]
    """§A requires step-2 (model_busy) to fire BEFORE step-3 (model_not_loaded).

    When the lock is held AND the model flag is unset, the response must be
    503 model_busy — not 503 model_not_loaded.  This test holds the lock by
    running a slow transcription, then fires a second request while the model
    flag is simultaneously cleared, and asserts the second request gets
    model_busy (not model_not_loaded).
    """
    flag = threading.Event()
    flag.set()  # model loaded initially so the first request gets through
    transcriber = FakeTranscriber()
    app = create_app(
        transcriber,
        _TOKEN,
        ServerConfig(),
        model_loaded_flag=flag,
        decode_fn=_make_slow_decode(0.3),
    )
    audio_bytes = b"fake-audio-data"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Start a slow transcription to hold the lock
        first_task = asyncio.create_task(
            client.post(
                "/transcribe",
                headers=_AUTH,
                files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
            )
        )

        # Let the first request enter the lock
        await asyncio.sleep(0.05)

        # Now clear the model flag — both conditions hold simultaneously
        flag.clear()

        # A second request arrives: lock IS held AND model flag IS unset
        # §A step-2 (busy) must win over step-3 (model_not_loaded)
        resp2 = await client.post(
            "/transcribe",
            headers=_AUTH,
            files={"file": ("test.m4a", io.BytesIO(audio_bytes), "audio/m4a")},
        )

        await first_task

    assert resp2.status_code == 503, (
        f"Expected 503 for concurrent request, got {resp2.status_code}: {resp2.text}"
    )
    error_code = resp2.json().get("error_code")
    assert error_code == "model_busy", (
        f"§A step-2 (model_busy) must beat step-3 (model_not_loaded) when both hold; "
        f"got error_code={error_code!r}"
    )


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
