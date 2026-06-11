"""Smoke tests for T-1: project scaffold, imports, and entry-point. — T-1"""

from __future__ import annotations

import subprocess
import sys


def test_murmur_help_runs() -> None:
    """murmur --help exits 0 and mentions the program name."""
    result = subprocess.run(
        [sys.executable, "-m", "murmur", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "murmur" in result.stdout.lower()


def test_package_importable() -> None:
    """The murmur package is importable and has a version."""
    import murmur

    assert hasattr(murmur, "__version__")


def test_audio_constants_sane() -> None:
    """Audio constants are present and have expected values."""
    from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE

    assert SAMPLE_RATE == 16_000
    assert FRAME_SAMPLES == 512


def test_paths_importable() -> None:
    from murmur.paths import AppPaths, resolve_paths  # noqa: F401


def test_log_importable() -> None:
    from murmur.log import configure_logging, get_logger  # noqa: F401
