"""Tests for murmur.output.writer — T-5."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from murmur.output.writer import TranscriptWriter
from tests.fakes import FixedClock


def _make_writer(tmp_path: Path, clock: FixedClock | None = None) -> TranscriptWriter:
    c = clock or FixedClock(datetime(2026, 6, 12, 9, 30, 0, tzinfo=timezone.utc))
    return TranscriptWriter(tmp_path, c)


# AC-4
def test_line_format_local_time(tmp_path: Path) -> None:  # [AC-4]
    """Transcript line matches **[HH:MM]** <text> format with local time."""
    start = datetime(2026, 6, 12, 14, 30, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    path = writer.append(start, "Hello world")
    content = path.read_text(encoding="utf-8")
    # The exact HH:MM depends on the local timezone; we check the format
    import re
    assert re.search(r"\*\*\[\d{2}:\d{2}\]\*\* Hello world", content), (
        f"Line format mismatch in: {content!r}"
    )


# AC-5
def test_no_file_until_first_segment(tmp_path: Path) -> None:  # [AC-5]
    """Transcript file is not created until the first segment is appended."""
    writer = _make_writer(tmp_path)
    transcript = tmp_path / "2026-06-12.md"
    assert not transcript.exists(), "File should not exist before any append"


def test_file_created_on_first_append(tmp_path: Path) -> None:
    """File is created on the first append for the day."""
    start = datetime(2026, 6, 12, 9, 30, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    path = writer.append(start, "First utterance")
    assert path.exists()


def test_header_on_first_append(tmp_path: Path) -> None:
    """First append creates the file with a Markdown header."""
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    path = writer.append(start, "text")
    content = path.read_text(encoding="utf-8")
    assert "# Transcript — 2026-06-12" in content


def test_multiple_appends_accumulate(tmp_path: Path) -> None:
    """Multiple appends accumulate in the same file."""
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    writer.append(start, "first")
    writer.append(start, "second")
    writer.append(start, "third")
    content = (tmp_path / "2026-06-12.md").read_text(encoding="utf-8")
    assert "first" in content
    assert "second" in content
    assert "third" in content


# E-12
def test_recreates_deleted_file(tmp_path: Path) -> None:  # [E-12]
    """If the transcript file is deleted mid-day, it is recreated on next append."""
    start = datetime(2026, 6, 12, 10, 0, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    path = writer.append(start, "before deletion")
    assert path.exists()
    path.unlink()
    assert not path.exists()
    writer.append(start, "after deletion")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "after deletion" in content


# E-11
def test_creates_dirs(tmp_path: Path) -> None:  # [E-11]
    """TranscriptWriter creates the transcripts directory if absent."""
    nested = tmp_path / "a" / "b" / "Transcripts"
    assert not nested.exists()
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    writer = TranscriptWriter(nested, FixedClock())
    writer.append(start, "test")
    assert nested.exists()


def test_no_tmp_files_left_after_append(tmp_path: Path) -> None:
    """No temporary files are left visible after append (atomic write)."""
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    writer = _make_writer(tmp_path)
    writer.append(start, "test")
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_separate_file_per_day(tmp_path: Path) -> None:
    """Different dates produce different files."""
    writer = _make_writer(tmp_path)
    d1 = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 13, 9, 0, 0, tzinfo=timezone.utc)
    p1 = writer.append(d1, "day1")
    p2 = writer.append(d2, "day2")
    assert p1 != p2
    assert "2026-06-12" in p1.name
    assert "2026-06-13" in p2.name
