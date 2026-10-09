"""Tests added during validation fix-list iterations 1 and 2.

Each test is tagged with the fix-list item it proves.
"""

from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from murmur.server.app import create_app
from murmur.server.config import ServerConfig, ServerSettings
from tests.fakes import FakeTranscriber

_TOKEN = "fix-list-test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# F-10: Non-loopback host rejected at startup (SR-2)
# ---------------------------------------------------------------------------


def test_startup_rejects_non_loopback_host(tmp_path: Path) -> None:  # [F-10, SR-2, R-8]
    """startup refuses host=0.0.0.0 with the documented message; exits with code 1."""
    from murmur.server import __main__ as server_main

    captured_stderr = io.StringIO()

    with (
        patch("murmur.server.token.get_server_token", return_value="tok"),
        patch(
            "murmur.server.config.load_server_config",
            return_value=(
                ServerConfig(server=ServerSettings(host="0.0.0.0")),
                [],
            ),
        ),
        patch.object(server_main, "_configure_logging"),
        patch("sys.stderr", captured_stderr),
        patch("sys.exit", side_effect=SystemExit) as mock_exit,pytest.raises(SystemExit)
    ):
        server_main.main()

    mock_exit.assert_called_once_with(1)
    stderr_output = captured_stderr.getvalue()
    assert "127.0.0.1" in stderr_output or "non-loopback" in stderr_output, (
        f"Expected non-loopback rejection message in stderr; got: {stderr_output!r}"
    )
    assert "R-8" in stderr_output or "Refusing" in stderr_output, (
        f"Expected R-8 or 'Refusing' in rejection message; got: {stderr_output!r}"
    )


def test_startup_accepts_loopback_host(tmp_path: Path) -> None:  # [F-10, R-8]
    """startup accepts host=127.0.0.1 — does NOT call sys.exit with R-8 message."""
    from murmur.server import __main__ as server_main

    captured_stderr = io.StringIO()

    with (
        patch("murmur.server.token.get_server_token", return_value="tok"),
        patch(
            "murmur.server.config.load_server_config",
            return_value=(
                ServerConfig(server=ServerSettings(host="127.0.0.1")),
                [],
            ),
        ),
        patch.object(server_main, "_configure_logging"),
        patch("sys.stderr", captured_stderr),
        # Stop after lock acquisition by raising from uvicorn.run
        patch("uvicorn.run", side_effect=KeyboardInterrupt),
        patch("murmur.lock.SingleInstanceLock") as mock_lock_cls,
    ):
        mock_lock_inst = MagicMock()
        mock_lock_cls.return_value = mock_lock_inst

        with pytest.raises(KeyboardInterrupt):
            server_main.main()

    # The loopback rejection message must NOT appear — host was accepted
    stderr_output = captured_stderr.getvalue()
    assert "Refusing" not in stderr_output, (
        f"host=127.0.0.1 was incorrectly rejected: {stderr_output!r}"
    )
    # Lock must have been acquired (we got past the host check)
    mock_lock_inst.acquire.assert_called_once()


def test_startup_accepts_ipv6_loopback(tmp_path: Path) -> None:  # [F-10, R-8]
    """startup accepts host=::1 (IPv6 loopback) — does NOT reject with R-8 message."""
    from murmur.server import __main__ as server_main

    captured_stderr = io.StringIO()

    with (
        patch("murmur.server.token.get_server_token", return_value="tok"),
        patch(
            "murmur.server.config.load_server_config",
            return_value=(
                ServerConfig(server=ServerSettings(host="::1")),
                [],
            ),
        ),
        patch.object(server_main, "_configure_logging"),
        patch("sys.stderr", captured_stderr),
        patch("uvicorn.run", side_effect=KeyboardInterrupt),
        patch("murmur.lock.SingleInstanceLock") as mock_lock_cls,
    ):
        mock_lock_inst = MagicMock()
        mock_lock_cls.return_value = mock_lock_inst

        with pytest.raises(KeyboardInterrupt):
            server_main.main()

    stderr_output = captured_stderr.getvalue()
    assert "Refusing" not in stderr_output
    mock_lock_inst.acquire.assert_called_once()


# ---------------------------------------------------------------------------
# CR-10: _TokenRedactFilter operates on getMessage() and clears args
# ---------------------------------------------------------------------------


def test_token_redact_filter_on_format_string() -> None:  # [CR-10]
    """_TokenRedactFilter replaces token in getMessage() output and clears args."""
    import logging

    from murmur.server.app import _TokenRedactFilter

    token = "my-secret-token-xyz"
    filt = _TokenRedactFilter(token)

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="request from %s completed",
        args=(token,),
        exc_info=None,
    )

    result = filt.filter(record)
    assert result is True  # filter always passes the record through
    # After filtering, the formatted message must not contain the token
    assert token not in record.getMessage(), (
        f"Token still present in getMessage() after filter: {record.getMessage()!r}"
    )
    assert "[REDACTED]" in record.getMessage()
    # args must be cleared so a second getMessage() call doesn't re-expand the token
    assert record.args == ()


# ---------------------------------------------------------------------------
# G-5 / SR-1 — python-multipart resolved version ≥ 0.0.18 (CVE-2024-53981)
# ---------------------------------------------------------------------------


def test_python_multipart_version__CVE_2024_53981() -> None:  # [G-5, SR-1, F-9]
    """python-multipart resolved version must be ≥ 0.0.18 to fix CVE-2024-53981.

    The pyproject.toml constraint is python-multipart>=0.0.18.  This test asserts
    the installed version satisfies that minimum, so any regression (e.g. a lockfile
    downgrade or an incorrect constraint) is caught automatically.
    """
    from importlib.metadata import version as pkg_version

    from packaging.version import Version

    installed = pkg_version("python-multipart")
    assert Version(installed) >= Version("0.0.18"), (
        f"python-multipart {installed} is below 0.0.18 — CVE-2024-53981 is unpatched"
    )


# ---------------------------------------------------------------------------
# G-5 / SR-3 — tempfile suffix sanitized for filenames containing '/' (path injection)
# ---------------------------------------------------------------------------


def test_sanitized_tempfile_suffix__SR3() -> None:  # [G-5, SR-3]
    """A filename containing '/' must be sanitized to its basename before use as
    a mkstemp suffix, preventing FileNotFoundError / directory traversal.

    The fix in app.py uses Path(filename).name to strip directory components.
    This test calls POST /transcribe with a '/' in the filename and asserts:
      - The response is NOT a 500 Internal Server Error (which would indicate
        mkstemp received a path separator in the suffix argument).
      - The response is a legitimate application-level error (400/415/422),
        meaning the request was processed through the validation pipeline normally.
    """
    flag = threading.Event()
    flag.set()
    app = create_app(
        FakeTranscriber(),
        _TOKEN,
        ServerConfig(),
        model_loaded_flag=flag,
        decode_fn=MagicMock(return_value=np.zeros(16000, dtype=np.float32)),
    )

    async def _run() -> int:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/transcribe",
                headers=_AUTH,
                files={
                    "file": (
                        "../../etc/evil.m4a",   # filename with path separators
                        io.BytesIO(b"fake-audio"),
                        "audio/m4a",
                    )
                },
            )
            return resp.status_code

    status = asyncio.run(_run())
    # Must NOT be 500 — a 500 would indicate the unsanitized suffix crashed mkstemp
    assert status != 500, (
        "POST /transcribe returned 500 with a slash-containing filename — "
        "SR-3 path-sanitization fix may be missing (suffix contained '/')"
    )
    # Must be a valid HTTP error or success — not a server crash
    assert status in {200, 400, 401, 413, 415, 422}, (
        f"Unexpected status {status} for slash-in-filename upload"
    )
