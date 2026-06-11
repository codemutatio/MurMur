"""Integration test: single-instance enforcement — T-13, AC-6, E-15."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def test_second_instance_exits_fast(tmp_path: Path) -> None:  # [AC-6]
    """Second `murmur run --headless` against a held lock exits 1 within 2 s.

    Strategy: hold the lock file used by resolve_paths($MURMUR_HOME) in a
    background subprocess, then launch a second subprocess via `murmur run
    --headless`. The second must exit within 2 s with exit code 1 and log
    the "already running" message to stderr.
    """
    murmur_home = tmp_path / "home"
    murmur_home.mkdir(parents=True)
    # The lock file path matches what resolve_paths produces for MURMUR_HOME.
    lock_path = murmur_home / "murmur.lock"

    # Hold the lock in a background subprocess.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import fcntl, os, time, sys
lock_path = r"{lock_path}"
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.lseek(fd, 0, os.SEEK_SET)
os.write(fd, str(os.getpid()).encode())
sys.stdout.write("LOCKED\\n")
sys.stdout.flush()
time.sleep(30)
""",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # readline() blocks until the child writes "LOCKED\n" — confirms the lock is held.
        holder.stdout.readline()  # type: ignore[union-attr]
        # Brief pause to let the OS register the flock before we try to take it.
        time.sleep(0.05)

        env = dict(os.environ)
        env["MURMUR_HOME"] = str(murmur_home)

        t0 = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "murmur", "run", "--headless"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(tmp_path),
        )
        elapsed = time.monotonic() - t0

        assert result.returncode == 1, (
            f"Expected exit code 1, got {result.returncode}. "
            f"stderr: {result.stderr!r}"
        )
        assert elapsed < 2.0, f"Second instance took {elapsed:.2f} s (expected < 2 s)"
        assert "already running" in result.stderr.lower(), (
            f"Expected 'already running' in stderr, got: {result.stderr!r}"
        )

    finally:
        holder.terminate()
        holder.wait(timeout=5)
