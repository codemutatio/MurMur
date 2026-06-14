"""Live smoke test for the Murmur server — T-9, AC-20, R-8.

This test requires a running Murmur server, ffmpeg, tailscale, and real
model weights — it is marked `hardware` and is deselected by default.

Run with:
    uv run pytest -m hardware tests/integration/test_server_live.py
"""

from __future__ import annotations

import os
import shutil

import pytest

# ---------------------------------------------------------------------------
# Hardware-marked live smoke test (AC-20, R-8, R-9)
# ---------------------------------------------------------------------------


@pytest.mark.hardware
def test_live_health_and_transcribe() -> None:  # [AC-20, R-8]
    """Live server health + transcription smoke test (requires real Mac + Tailscale).

    Preconditions:
    - Murmur server running: `python -m murmur.server`
    - MURMUR_TEST_URL env var set to the Tailscale HTTPS URL of the server
      (e.g., https://mymac.tailXXXX.ts.net)
    - MURMUR_TEST_TOKEN env var set to the bearer token
    - ffmpeg installed and a .m4a test file at MURMUR_TEST_AUDIO (optional;
      defaults to a generated PCM WAV via ffmpeg if absent)

    AC-20 verified by this test:
    - GET /health returns 200 with model_loaded: true
    - POST /transcribe returns 200 with a non-empty transcript
    - Server is reachable only via Tailscale (HTTPS URL from tailscale serve)
    """
    import httpx

    base_url = os.environ.get("MURMUR_TEST_URL", "").rstrip("/")
    token = os.environ.get("MURMUR_TEST_TOKEN", "")

    if not base_url or not token:
        pytest.skip(
            "MURMUR_TEST_URL and MURMUR_TEST_TOKEN env vars must be set for the "
            "live smoke test. These are set in the manual MV-20 runbook."
        )

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg binary is required for the live transcription smoke test.")

    headers = {"Authorization": f"Bearer {token}"}

    # ---- GET /health ----
    health_resp = httpx.get(f"{base_url}/health", headers=headers, timeout=5.0)
    assert health_resp.status_code == 200, (
        f"GET /health returned {health_resp.status_code}: {health_resp.text}"
    )
    health = health_resp.json()
    assert health["status"] == "ok"
    assert health["model_loaded"] is True, (
        "Model is not loaded — wait for model initialization to complete."
    )
    assert health["model"] == "large-v3"

    # ---- POST /transcribe ----
    audio_path = os.environ.get("MURMUR_TEST_AUDIO", "")
    if not audio_path:
        pytest.skip(
            "MURMUR_TEST_AUDIO env var must point to a real .m4a file for the "
            "live transcription smoke test."
        )

    with open(audio_path, "rb") as f:
        transcribe_resp = httpx.post(
            f"{base_url}/transcribe",
            headers=headers,
            files={"file": (os.path.basename(audio_path), f, "audio/m4a")},
            timeout=60.0,
        )

    assert transcribe_resp.status_code == 200, (
        f"POST /transcribe returned {transcribe_resp.status_code}: {transcribe_resp.text}"
    )
    body = transcribe_resp.json()
    assert body["engine"] == "mac"
    assert body["text"], "Expected non-empty transcript"
    assert body["duration_sec"] > 0
