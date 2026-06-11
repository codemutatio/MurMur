"""Tests for murmur.mac.keychain — T-2."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from murmur.mac.keychain import get_openai_api_key


def test_env_then_keychain_order() -> None:
    """Environment variable takes priority over Keychain lookup."""
    run_mock = MagicMock()
    key = get_openai_api_key({"OPENAI_API_KEY": "sk-from-env"}, run=run_mock)
    assert key == "sk-from-env"
    run_mock.assert_not_called()


def test_keychain_fallback_when_env_missing() -> None:
    """Falls back to Keychain when env var is absent."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="sk-from-keychain\n")

    key = get_openai_api_key({}, run=fake_run)
    assert key == "sk-from-keychain"


def test_returns_none_when_both_missing() -> None:
    """Returns None when neither env nor Keychain has a key."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="")

    key = get_openai_api_key({}, run=fake_run)
    assert key is None


def test_fallback_disabled_without_key(tmp_path: object) -> None:  # [E-21]
    """get_openai_api_key returns None when no key is present; callers disable fallback."""

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="")

    key = get_openai_api_key({}, run=fake_run)
    assert key is None  # caller treats api_fallback as off when this is None


def test_keychain_exception_returns_none() -> None:
    """Keychain subprocess exception → None (not raised)."""

    def bad_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("security not found")

    key = get_openai_api_key({}, run=bad_run)
    assert key is None
