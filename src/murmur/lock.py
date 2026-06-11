"""Single-instance enforcement via fcntl.flock.

The lock file is opened and held for the lifetime of the process. flock is
stale-safe: if the holding process dies without releasing, the lock is
automatically released by the OS when the file descriptor is closed.
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)


class AlreadyRunningError(Exception):
    """Raised when another murmur instance is already running."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"Murmur already running (PID: {pid})")
        self.pid = pid


class SingleInstanceLock:
    """Acquire an exclusive flock on *lock_file*.

    Raises:
        AlreadyRunningError: if another process holds the lock.
    """

    def __init__(self, lock_file: Path) -> None:
        self._path = lock_file
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the lock. Raises AlreadyRunningError if already held."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process holds the lock — read its PID
            pid = self._read_pid(fd)
            os.close(fd)
            raise AlreadyRunningError(pid)
        # Write our PID to the file
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        _log.debug("Lock acquired (PID: %d)", os.getpid())

    def release(self) -> None:
        """Release the lock. Idempotent."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    @staticmethod
    def _read_pid(fd: int) -> int:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 32).decode(errors="replace").strip()
            return int(data) if data.isdigit() else -1
        except Exception:
            return -1
