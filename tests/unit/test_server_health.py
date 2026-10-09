"""Tests for GET /health — T-3, AC-1, AC-2, AC-3, AC-13."""

from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from murmur.server.app import create_app
from murmur.server.config import ServerConfig
from tests.fakes import FakeTranscriber

_TOKEN = "test-secret-token"


def _make_client(
    *,
    model_loaded: bool = True,
    transcriber: FakeTranscriber | None = None,
    token: str = _TOKEN,
) -> TestClient:
    flag = threading.Event()
    if model_loaded:
        flag.set()
    t = transcriber or FakeTranscriber()
    app = create_app(t, token, ServerConfig(), model_loaded_flag=flag)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# AC-1 — 200 + correct JSON shape
# ---------------------------------------------------------------------------


def test_health_ok_shape() -> None:  # [AC-1]
    """GET /health returns 200 and the exact JSON shape from §A."""
    client = _make_client(model_loaded=True)
    resp = client.get("/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["model_loaded"] is True
    assert body["model"] == "large-v3"
    assert "version" in body
    # version should be a non-empty semver-like string
    assert body["version"]


# ---------------------------------------------------------------------------
# AC-2 — 401 on missing / wrong token (no internals leaked)
# ---------------------------------------------------------------------------


def test_health_401_missing_token() -> None:  # [AC-2]
    """GET /health without Authorization header returns 401."""
    client = _make_client()
    resp = client.get("/health")

    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "unauthorized"
    # No internal paths, module names, or token in the response
    for key in ("traceback", "stack", _TOKEN):
        assert key not in str(body)


def test_health_401_wrong_token() -> None:  # [AC-2]
    """GET /health with a wrong token returns 401."""
    client = _make_client()
    resp = client.get("/health", headers={"Authorization": "Bearer wrong-token"})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error_code"] == "unauthorized"


def test_health_401_empty_bearer() -> None:  # [E-14]
    """GET /health with 'Bearer ' (empty token after prefix) returns 401."""
    client = _make_client()
    resp = client.get("/health", headers={"Authorization": "Bearer "})

    assert resp.status_code == 401


def test_health_401_no_bearer_prefix() -> None:  # [E-13]
    """GET /health with a token but no 'Bearer ' prefix returns 401."""
    client = _make_client()
    resp = client.get("/health", headers={"Authorization": _TOKEN})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# AC-3 — 200 with model_loaded: false when flag is not set
# ---------------------------------------------------------------------------


def test_health_model_loaded_false_when_initializing() -> None:  # [AC-3]
    """GET /health returns model_loaded: false while the model flag is not set."""
    client = _make_client(model_loaded=False)
    resp = client.get("/health", headers={"Authorization": f"Bearer {_TOKEN}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["model_loaded"] is False
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# AC-13 — /health stays fast while the lock is held
# ---------------------------------------------------------------------------


def test_health_fast_while_lock_held() -> None:  # [AC-13]
    """GET /health completes in < 500 ms even while the asyncio.Lock is held."""
    flag = threading.Event()
    flag.set()
    app = create_app(FakeTranscriber(), _TOKEN, ServerConfig(), model_loaded_flag=flag)

    # Manually acquire the lock and hold it while we call /health
    async def _hold_lock_and_check_health() -> float:
        lock: asyncio.Lock = app.state.lock
        async with lock:
            # Lock is now held; /health should not wait for it
            with TestClient(app, raise_server_exceptions=False) as client:
                t0 = time.monotonic()
                resp = client.get("/health", headers={"Authorization": f"Bearer {_TOKEN}"})
                elapsed = time.monotonic() - t0
            assert resp.status_code == 200
            assert resp.json()["model_loaded"] is True
        return elapsed

    elapsed = asyncio.run(_hold_lock_and_check_health())
    assert elapsed < 0.5, f"/health took {elapsed:.3f}s while lock was held (expected < 0.5s)"
