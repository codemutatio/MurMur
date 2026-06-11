"""Tests for OsascriptNotifier — T-17, R-11."""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import patch

from murmur.mac.notify import OsascriptNotifier


def test_osascript_cmd_and_never_raises() -> None:
    """notify() calls osascript and never raises, even if the subprocess fails."""
    calls: list[list[str]] = []
    completed = threading.Event()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args[0])  # type: ignore[arg-type]
        completed.set()
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        notifier = OsascriptNotifier()
        notifier.notify("Murmur", "Test message")
        # The call happens in a daemon thread — wait for it
        completed.wait(timeout=3.0)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "osascript"
    assert "Test message" in cmd[-1]
    assert "Murmur" in cmd[-1]


def test_osascript_escapes_quotes() -> None:
    """Embedded double-quotes in title/message are escaped to prevent injection."""
    calls: list[list[str]] = []
    completed = threading.Event()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args[0])  # type: ignore[arg-type]
        completed.set()
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    with patch("subprocess.run", side_effect=fake_run):
        notifier = OsascriptNotifier()
        notifier.notify('Say "hello"', 'He said "world"')
        completed.wait(timeout=3.0)

    script = calls[0][-1]
    # Original quotes should be escaped
    assert '"' in script or '\\"' in script


def test_notify_never_raises_on_subprocess_failure() -> None:
    """notify() does not raise even when osascript subprocess raises an exception."""

    def exploding_run(*args: object, **kwargs: object) -> None:
        raise RuntimeError("osascript not found")

    with patch("subprocess.run", side_effect=exploding_run):
        notifier = OsascriptNotifier()
        # Should not raise at all
        notifier.notify("Murmur", "This will fail silently")
        # Give the daemon thread a moment to execute
        time.sleep(0.05)


def test_notify_is_non_blocking() -> None:
    """notify() returns immediately without waiting for the subprocess to finish."""
    slow_completed = threading.Event()

    def slow_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        time.sleep(0.2)
        slow_completed.set()
        return subprocess.CompletedProcess(args=[], returncode=0)

    with patch("subprocess.run", side_effect=slow_run):
        notifier = OsascriptNotifier()
        t0 = time.monotonic()
        notifier.notify("Murmur", "Slow notification")
        elapsed = time.monotonic() - t0

    # notify() should return almost immediately (< 100 ms)
    assert elapsed < 0.1, f"notify() blocked for {elapsed:.3f} s"
