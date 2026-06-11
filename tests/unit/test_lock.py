"""Tests for murmur.lock — T-13."""

from __future__ import annotations

from pathlib import Path

import pytest

from murmur.lock import AlreadyRunningError, SingleInstanceLock


def test_acquire_and_release(tmp_path: Path) -> None:
    """Lock acquires and releases without error."""
    lock = SingleInstanceLock(tmp_path / "murmur.lock")
    lock.acquire()
    lock.release()


def test_lock_file_created(tmp_path: Path) -> None:
    """Lock file is created on acquire."""
    lock_path = tmp_path / "murmur.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    lock.release()


def test_pid_written_to_lock_file(tmp_path: Path) -> None:
    """PID is written to the lock file on acquire."""
    import os

    lock_path = tmp_path / "murmur.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    content = lock_path.read_text().strip()
    assert content == str(os.getpid())
    lock.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    """Calling release() multiple times does not raise."""
    lock = SingleInstanceLock(tmp_path / "murmur.lock")
    lock.acquire()
    lock.release()
    lock.release()  # should not raise


def test_flock_stale_safe(tmp_path: Path) -> None:
    """After a lock is released, a new instance can acquire it (stale-safe)."""
    lock_path = tmp_path / "murmur.lock"
    lock1 = SingleInstanceLock(lock_path)
    lock1.acquire()
    lock1.release()

    lock2 = SingleInstanceLock(lock_path)
    lock2.acquire()
    lock2.release()


def test_second_acquire_on_held_lock_raises(tmp_path: Path) -> None:
    """Attempting to acquire a held lock raises AlreadyRunningError with a PID."""
    import os

    lock_path = tmp_path / "murmur.lock"
    lock1 = SingleInstanceLock(lock_path)
    lock1.acquire()

    try:
        lock2 = SingleInstanceLock(lock_path)
        with pytest.raises(AlreadyRunningError) as exc_info:
            lock2.acquire()
        # PID should be our own PID (the holding process)
        assert exc_info.value.pid == os.getpid()
    finally:
        lock1.release()


def test_already_running_error_message(tmp_path: Path) -> None:
    """AlreadyRunningError message includes PID."""
    lock_path = tmp_path / "murmur.lock"
    lock1 = SingleInstanceLock(lock_path)
    lock1.acquire()

    try:
        lock2 = SingleInstanceLock(lock_path)
        with pytest.raises(AlreadyRunningError) as exc_info:
            lock2.acquire()
        assert "Murmur already running" in str(exc_info.value)
        assert str(exc_info.value.pid) in str(exc_info.value)
    finally:
        lock1.release()


def test_parent_dir_created_if_absent(tmp_path: Path) -> None:
    """Lock creates parent directory if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "c"
    lock = SingleInstanceLock(nested / "murmur.lock")
    lock.acquire()
    assert nested.exists()
    lock.release()
