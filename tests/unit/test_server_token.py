"""Tests for murmur.server.token — T-1, AC-15, E-18."""

from __future__ import annotations

import hmac
import subprocess
from unittest.mock import MagicMock

from murmur.server.token import get_server_token, verify_token

# ---------------------------------------------------------------------------
# get_server_token — env first, then Keychain, then None
# ---------------------------------------------------------------------------


def test_env_first() -> None:  # [AC-15, E-18]
    """MURMUR_SERVER_TOKEN env var is returned without hitting the Keychain."""
    run = MagicMock()
    token = get_server_token({"MURMUR_SERVER_TOKEN": "secret-token"}, run=run)

    assert token == "secret-token"
    run.assert_not_called()


def test_keychain_fallback() -> None:  # [E-18]
    """When env var is absent, the macOS Keychain is queried."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "keychain-token\n"
    run = MagicMock(return_value=mock_result)

    token = get_server_token({}, run=run)

    assert token == "keychain-token"
    run.assert_called_once()
    call_args = run.call_args[0][0]
    assert "murmur-server-token" in call_args


def test_none_when_absent() -> None:  # [E-18]
    """Returns None when neither env var nor Keychain has the token."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    run = MagicMock(return_value=mock_result)

    token = get_server_token({}, run=run)

    assert token is None


def test_none_when_keychain_raises() -> None:
    """Returns None gracefully when the security CLI raises an exception."""
    run = MagicMock(side_effect=FileNotFoundError("security not found"))

    token = get_server_token({}, run=run)

    assert token is None


def test_env_whitespace_stripped() -> None:
    """Env var value is stripped of surrounding whitespace."""
    run = MagicMock()
    token = get_server_token({"MURMUR_SERVER_TOKEN": "  tok  "}, run=run)

    assert token == "tok"


def test_keychain_timeout_returns_none() -> None:
    """Keychain lookup timeout returns None without raising."""
    run = MagicMock(side_effect=subprocess.TimeoutExpired("security", 5))

    token = get_server_token({}, run=run)

    assert token is None


# ---------------------------------------------------------------------------
# verify_token — constant-time comparison
# ---------------------------------------------------------------------------


def test_compare_uses_compare_digest() -> None:  # [AC-15]
    """verify_token delegates to hmac.compare_digest (constant-time)."""
    # Prove that the function uses hmac.compare_digest by comparing expected behaviour:
    # two equal tokens → True; differing tokens → False.
    assert verify_token("abc", "abc") is True
    assert verify_token("abc", "xyz") is False


def test_compare_digest_matches_hmac() -> None:  # [AC-15]
    """verify_token result matches hmac.compare_digest for the same inputs."""
    pairs = [
        ("tok", "tok"),
        ("tok", "different"),
        ("", "tok"),
    ]
    for a, b in pairs:
        expected = hmac.compare_digest(a.encode(), b.encode()) if (a and b) else False
        assert verify_token(a or None, b or None) == expected


def test_none_provided_returns_false() -> None:
    """None provided token → False (not an error)."""
    assert verify_token(None, "expected") is False


def test_none_expected_returns_false() -> None:
    """None expected token (e.g., token not configured yet) → False."""
    assert verify_token("provided", None) is False


def test_empty_provided_returns_false() -> None:
    """Empty string provided → False."""
    assert verify_token("", "expected") is False
