"""Integration tests for CLI cat today / copy today — T-14, AC-11, AC-12."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path


def _murmur_home_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MURMUR_HOME"] = str(home)
    return env


def test_cat_today_stdout(tmp_path: Path) -> None:  # [AC-11]
    """murmur cat today prints today's transcript to stdout and exits 0."""
    home = tmp_path / "home"
    home.mkdir()
    transcripts = tmp_path / "Transcripts"
    transcripts.mkdir()

    # Create a transcript for today
    today = date.today()
    transcript_path = transcripts / f"{today:%Y-%m-%d}.md"
    content = f"# Transcript — {today:%Y-%m-%d}\n\n**[09:00]** Hello world\n\n"
    transcript_path.write_text(content, encoding="utf-8")

    # Write a config pointing to our tmp transcripts dir
    config_path = home / "config.toml"
    config_path.write_text(
        f'[storage]\ntranscripts_dir = "{transcripts}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "murmur", "cat", "today"],
        env=_murmur_home_env(home),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stderr}"
    assert "Hello world" in result.stdout
    assert result.stdout.strip() == content.strip()


def test_cat_today_missing_file_exit_1(tmp_path: Path) -> None:
    """murmur cat today exits 1 when no transcript file exists for today."""
    home = tmp_path / "home"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, "-m", "murmur", "cat", "today"],
        env=_murmur_home_env(home),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "transcript" in result.stderr.lower() or "No" in result.stderr


def test_copy_today_pbpaste_roundtrip(tmp_path: Path) -> None:  # [AC-12]
    """murmur copy today writes today's transcript to the clipboard (pbpaste roundtrip)."""
    home = tmp_path / "home"
    home.mkdir()
    transcripts = tmp_path / "Transcripts"
    transcripts.mkdir()

    today = date.today()
    transcript_path = transcripts / f"{today:%Y-%m-%d}.md"
    content = f"# Transcript — {today:%Y-%m-%d}\n\n**[10:30]** Clipboard test\n\n"
    transcript_path.write_text(content, encoding="utf-8")

    config_path = home / "config.toml"
    config_path.write_text(
        f'[storage]\ntranscripts_dir = "{transcripts}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "murmur", "copy", "today"],
        env=_murmur_home_env(home),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stderr}"

    # Verify clipboard content via pbpaste
    pbpaste = subprocess.run(
        ["pbpaste"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert "Clipboard test" in pbpaste.stdout, (
        f"Clipboard did not contain expected text. Got: {pbpaste.stdout!r}"
    )
