"""Unit tests for CLI argument parsing and missing-file paths — T-14."""

from __future__ import annotations

from pathlib import Path

import pytest

from murmur.cli import _build_parser, main


def test_no_command_returns_2() -> None:
    """No subcommand → exit code 2 (usage)."""
    assert main([]) == 2


def test_help_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """--help exits with code 0 (argparse raises SystemExit(0))."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0


def _empty_transcripts_config(tmp_path: Path) -> Path:
    """Write a config pointing transcripts_dir at an empty tmp dir.

    Without this the command falls back to the real ~/Transcripts and would
    find the user's actual transcript for today (MURMUR_HOME does not redirect
    transcripts_dir).
    """
    transcripts = tmp_path / "Transcripts"  # intentionally empty: no file for today
    config = tmp_path / "config.toml"
    config.write_text(f'[storage]\ntranscripts_dir = "{transcripts}"\n', encoding="utf-8")
    return config


def test_cat_missing_file_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cat today exits 1 when no transcript file exists."""
    monkeypatch.setenv("MURMUR_HOME", str(tmp_path / "home"))
    config = _empty_transcripts_config(tmp_path)

    result = main(["cat", "today", "--config", str(config)])
    assert result == 1


def test_copy_missing_file_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """copy today exits 1 when no transcript file exists."""
    monkeypatch.setenv("MURMUR_HOME", str(tmp_path / "home"))
    config = _empty_transcripts_config(tmp_path)

    result = main(["copy", "today", "--config", str(config)])
    assert result == 1


def test_bench_missing_wav_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """bench exits 1 when the WAV file does not exist."""
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))

    result = main(["bench", str(tmp_path / "nonexistent.wav")])
    assert result == 1


def test_parser_run_headless() -> None:
    """run --headless sets headless=True."""
    parser = _build_parser()
    args = parser.parse_args(["run", "--headless"])
    assert args.headless is True
    assert args.command == "run"


def test_parser_run_config() -> None:
    """run --config sets config path."""
    parser = _build_parser()
    args = parser.parse_args(["run", "--config", "/tmp/my.toml"])
    assert args.config == Path("/tmp/my.toml")


def test_parser_cat_today() -> None:
    """cat today sets period='today'."""
    parser = _build_parser()
    args = parser.parse_args(["cat", "today"])
    assert args.period == "today"
    assert args.command == "cat"


def test_parser_copy_today() -> None:
    """copy today sets period='today'."""
    parser = _build_parser()
    args = parser.parse_args(["copy", "today"])
    assert args.period == "today"
    assert args.command == "copy"
