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


def test_cat_missing_file_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cat today exits 1 when no transcript file exists."""
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))

    # Override transcripts_dir to tmp_path so no real file is present
    result = main(["cat", "today"])
    assert result == 1


def test_copy_missing_file_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """copy today exits 1 when no transcript file exists."""
    home = tmp_path / "home"
    monkeypatch.setenv("MURMUR_HOME", str(home))

    result = main(["copy", "today"])
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
