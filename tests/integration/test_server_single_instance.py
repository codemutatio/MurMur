"""Integration test: server single-instance enforcement — T-8, AC-19, R-17, OQ-8."""

from __future__ import annotations

from pathlib import Path

import pytest

from murmur.lock import AlreadyRunningError, SingleInstanceLock


def test_second_instance_blocked(tmp_path: Path) -> None:  # [R-17, OQ-8]
    """A second SingleInstanceLock on the same PID file raises AlreadyRunningError.

    This simulates the belt guard that prevents two murmur-server processes from
    running simultaneously (OQ-8: launchd + PID-file belt).
    """
    lock_file = tmp_path / "murmur-server.pid"

    first_lock = SingleInstanceLock(lock_file)
    first_lock.acquire()

    try:
        second_lock = SingleInstanceLock(lock_file)
        with pytest.raises(AlreadyRunningError):
            second_lock.acquire()
    finally:
        first_lock.release()


def test_lock_released_on_release(tmp_path: Path) -> None:  # [R-17]
    """After the first lock is released, the second can acquire it."""
    lock_file = tmp_path / "murmur-server.pid"

    first_lock = SingleInstanceLock(lock_file)
    first_lock.acquire()
    first_lock.release()

    second_lock = SingleInstanceLock(lock_file)
    second_lock.acquire()  # Should not raise
    second_lock.release()


def test_lock_file_path_constant() -> None:  # [OQ-8]
    """SERVER_PID_FILE is the expected path used for the belt guard."""
    from murmur.server.launchd_server import SERVER_PID_FILE

    assert Path("/tmp/murmur-server.pid") == SERVER_PID_FILE
