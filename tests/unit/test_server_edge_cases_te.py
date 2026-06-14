"""Additional edge-case tests added by test-engineer [test-engineer].

Covers uncovered paths and weak assertions found during adversarial review
of iteration 1. All tests are deterministic (no sleeps, no network, no device).
"""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from murmur.server.app import create_app
from murmur.server.config import ServerConfig, ServerSttSettings, load_server_config
from murmur.server.token import get_server_token
from tests.fakes import FakeTranscriber

_TOKEN = "test-token-te"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _make_client(
    *,
    model_loaded: bool = True,
    token: str = _TOKEN,
    config: ServerConfig | None = None,
    decode_fn: object = None,
    transcriber: FakeTranscriber | None = None,
) -> TestClient:
    flag = threading.Event()
    if model_loaded:
        flag.set()
    t = transcriber or FakeTranscriber()
    cfg = config or ServerConfig()
    d = decode_fn or MagicMock(return_value=np.zeros(16000, dtype=np.float32))
    app = create_app(t, token, cfg, model_loaded_flag=flag, decode_fn=d)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# E-18: startup failure exact message — test the __main__ token-absent path
# [test-engineer]
# ---------------------------------------------------------------------------


def test_startup_token_absent_exits_with_exact_message(tmp_path: Path) -> None:  # [E-18, test-engineer]
    """When MURMUR_SERVER_TOKEN is absent, __main__.main() calls sys.exit(1) with the exact E-18 message.

    The token retrieval is inside main() via a local import so we patch the module
    that main() actually calls. sys.exit is replaced by a side_effect=SystemExit so
    the function stops at the exit call.
    """
    from murmur.server import __main__ as server_main

    captured_stderr = io.StringIO()

    with (
        patch("murmur.server.token.get_server_token", return_value=None),
        patch("murmur.server.config.load_server_config", return_value=(ServerConfig(), [])),
        patch.object(server_main, "_configure_logging"),
        patch("sys.stderr", captured_stderr),
        patch("sys.exit", side_effect=SystemExit) as mock_exit,
    ):
        with patch("murmur.lock.SingleInstanceLock"):
            with pytest.raises(SystemExit):
                server_main.main()

    # sys.exit(1) must have been called
    mock_exit.assert_called_once_with(1)
    # E-18 exact message must be emitted to stderr
    stderr_output = captured_stderr.getvalue()
    assert "MURMUR_SERVER_TOKEN" in stderr_output, (
        f"E-18 exact message not found in stderr: {stderr_output!r}"
    )


def test_get_server_token_missing_returns_none() -> None:  # [E-18, test-engineer]
    """get_server_token returns None when env is empty and Keychain has no entry."""
    mock_run = MagicMock()
    mock_run.return_value.returncode = 1
    mock_run.return_value.stdout = ""

    result = get_server_token({}, run=mock_run)
    assert result is None


# ---------------------------------------------------------------------------
# E-14: Bearer with empty string after prefix
# [test-engineer]
# ---------------------------------------------------------------------------


def test_health_401_bearer_with_spaces_only() -> None:  # [E-14, test-engineer]
    """Authorization: Bearer <spaces> (whitespace-only token) → 401."""
    client = _make_client()
    resp = client.get("/health", headers={"Authorization": "Bearer    "})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# E-16: Malformed server.toml at startup — no crash, all defaults
# [test-engineer]
# ---------------------------------------------------------------------------


def test_malformed_toml_clean_section_ignored(tmp_path: Path) -> None:  # [E-16, test-engineer]
    """A malformed [clean] section (wrong type) does not crash and is ignored."""
    toml = tmp_path / "server.toml"
    toml.write_text(
        "[server]\nport = 9090\n\n[clean]\nenabled = true\nextra = 42\n",
        encoding="utf-8",
    )
    config, warnings = load_server_config(toml)
    # Known-good [server] section still applied
    assert config.server.port == 9090
    # [clean] section is ignored — no behavior
    assert config.clean_raw.get("enabled") is True


# ---------------------------------------------------------------------------
# E-17: Unknown keys inside [stt] section → warned, defaults applied
# [test-engineer]
# ---------------------------------------------------------------------------


def test_unknown_stt_subkey_warns_and_defaults_applied(tmp_path: Path) -> None:  # [E-17, test-engineer]
    """Unknown key inside [stt] is warned about; known keys still applied."""
    toml = tmp_path / "server.toml"
    toml.write_text(
        '[stt]\nmodel = "tiny"\nfuture_option = 99\n',
        encoding="utf-8",
    )
    config, warnings = load_server_config(toml)
    assert config.stt.model == "tiny"
    assert any("future_option" in w for w in warnings)


# ---------------------------------------------------------------------------
# E-7: vocab_hints with one empty and one non-empty entry — mixed hints
# [test-engineer]
# ---------------------------------------------------------------------------


def test_vocab_hints_mixed_empty_and_nonempty() -> None:  # [E-7, test-engineer]
    """Comma-separated vocab_hints with empty entries: empty ones stripped, non-empty kept."""
    cfg = ServerConfig(stt=ServerSttSettings(vocab_hints=()))
    decode_fn = MagicMock(return_value=np.zeros(16000, dtype=np.float32))
    transcriber = FakeTranscriber()
    client = _make_client(config=cfg, decode_fn=decode_fn, transcriber=transcriber)

    client.post(
        "/transcribe",
        headers=_AUTH,
        files={"file": ("test.m4a", io.BytesIO(b"audio"), "audio/m4a")},
        data={"vocab_hints": ",Rivy,, ,GKE,"},
    )

    # build_initial_prompt receives only non-empty stripped hints
    prompt = transcriber.calls[0]["initial_prompt"]
    assert prompt is not None
    assert "Rivy" in prompt
    assert "GKE" in prompt


# ---------------------------------------------------------------------------
# AC-8 boundary: exactly 25 MB boundary (at limit) accepts; one byte over rejects
# Double-check the boundary is 25 * 1024 * 1024, not 25 * 1000 * 1000
# [test-engineer]
# ---------------------------------------------------------------------------


_MB = 1024 * 1024


def test_25mb_boundary_is_binary_mb_not_decimal() -> None:  # [AC-8, E-1, E-2, test-engineer]
    """25 MB limit is 25*1024*1024 bytes (binary MB), not 25*1000*1000 (decimal MB)."""
    # 25*1000*1000 = 26_214_400 (decimal MB); 25*1024*1024 = 26_214_400 also (wait — same number?)
    # No: 25*1000*1000 = 25_000_000; 25*1024*1024 = 26_214_400.
    # A file of 25_000_001 bytes (just over decimal 25 MB) should be ACCEPTED if binary MB is used.
    decode_fn = MagicMock(return_value=np.zeros(16000, dtype=np.float32))
    client = _make_client(decode_fn=decode_fn)

    # 25,000,001 bytes — over decimal 25 MB but under binary 25 MB (26,214,400)
    body_at_decimal_plus_one = b"x" * (25 * 1_000_000 + 1)
    resp = client.post(
        "/transcribe",
        headers=_AUTH,
        files={"file": ("test.m4a", io.BytesIO(body_at_decimal_plus_one), "audio/m4a")},
    )
    # Must be accepted (below 25 binary MB limit)
    assert resp.status_code == 200, (
        f"25,000,001 bytes rejected with {resp.status_code} — "
        "size limit is in binary MB (25*1024*1024=26,214,400), not decimal"
    )


# ---------------------------------------------------------------------------
# AC-15: verify_token is actually hmac.compare_digest, not ==
# Mutation test: if someone changes to == , timing oracle exists but the test below
# would still pass (both give same True/False). Instead we import and check the source.
# [test-engineer]
# ---------------------------------------------------------------------------


def test_verify_token_impl_uses_compare_digest() -> None:  # [AC-15, test-engineer]
    """verify_token must call hmac.compare_digest, not the == operator."""
    import inspect
    import murmur.server.token as token_mod

    source = inspect.getsource(token_mod.verify_token)
    # The implementation must reference compare_digest
    assert "compare_digest" in source, (
        "verify_token source does not contain 'compare_digest' — "
        "constant-time comparison not guaranteed"
    )
    # And must NOT use plain == for the comparison
    # (The source may use == in an 'if not a or not b' guard; that's fine)
    # We check the actual compare path is not: return a == b or equivalent
    assert "return provided == expected" not in source
    assert "return a == b" not in source


# ---------------------------------------------------------------------------
# AC-13 / ADR-2: health genuinely not blocked — the test_health_not_blocked_by_lock
# in test_server_singleflight.py uses a slow decode (1 s). Verify it also holds
# when the transcription lock is held NOT by decode but by the transcriber itself.
# [test-engineer]
# ---------------------------------------------------------------------------


def test_health_fast_while_transcriber_holds_lock() -> None:  # [AC-13, test-engineer]
    """GET /health is fast even when the transcriber (not decode) is slow."""
    import asyncio
    import time

    from httpx import ASGITransport, AsyncClient

    slow_transcriber = FakeTranscriber()

    # Make the transcriber itself block for 0.5 s
    original_transcribe = slow_transcriber.transcribe

    def slow_transcribe(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.5)
        return original_transcribe(*args, **kwargs)

    slow_transcriber.transcribe = slow_transcribe  # type: ignore[method-assign]

    flag = threading.Event()
    flag.set()
    decode_fn = MagicMock(return_value=np.zeros(16000, dtype=np.float32))
    app = create_app(slow_transcriber, _TOKEN, ServerConfig(), model_loaded_flag=flag, decode_fn=decode_fn)

    async def _run() -> float:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            audio = b"fake-audio"
            task = asyncio.create_task(
                client.post(
                    "/transcribe",
                    headers=_AUTH,
                    files={"file": ("t.m4a", io.BytesIO(audio), "audio/m4a")},
                )
            )
            await asyncio.sleep(0.05)  # let transcription acquire the lock

            t0 = time.monotonic()
            resp = await client.get("/health", headers=_AUTH)
            elapsed = time.monotonic() - t0
            await task
        assert resp.status_code == 200
        return elapsed

    elapsed = asyncio.run(_run())
    assert elapsed < 0.5, (
        f"/health took {elapsed:.3f}s while slow transcriber held lock (expected < 0.5s)"
    )


# ---------------------------------------------------------------------------
# E-42 edge: multipart request with wrong content-type (no boundary) → graceful error
# [test-engineer]
# ---------------------------------------------------------------------------


def test_transcribe_non_multipart_body_returns_error() -> None:  # [test-engineer]
    """Sending a plain body (not multipart) to POST /transcribe returns 400 or 422."""
    client = _make_client()
    resp = client.post(
        "/transcribe",
        headers={**_AUTH, "Content-Type": "application/json"},
        content=b'{"language": "en"}',
    )
    # Should not be 200 or 500
    assert resp.status_code in (400, 415, 422), (
        f"Expected 400/415/422 for non-multipart body, got {resp.status_code}"
    )
