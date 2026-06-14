"""Tests added during validation fix-list iteration 1.

Each test is tagged with the fix-list item it proves.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from murmur.server.config import ServerConfig, ServerSettings

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
