"""Tests for murmur.output.reader.TranscriptReader / parse_transcript."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from murmur.output.reader import TranscriptEntry, TranscriptReader, parse_transcript
from murmur.output.writer import TranscriptWriter

HEADER = "# Transcript — 2026-06-13\n\n"


def test_parse_transcript_extracts_entries() -> None:
    content = HEADER + "**[09:05]** Hello world\n\n**[09:07]** Second line\n\n"
    assert parse_transcript(content) == [
        TranscriptEntry("09:05", "Hello world"),
        TranscriptEntry("09:07", "Second line"),
    ]


def test_parse_transcript_ignores_header_and_blanks() -> None:
    assert parse_transcript(HEADER) == []
    assert parse_transcript("") == []
    assert parse_transcript("not a transcript line") == []


def test_entries_for_missing_file_is_empty(tmp_path: Path) -> None:
    reader = TranscriptReader(tmp_path)
    assert reader.entries_for(date(2026, 6, 13)) == []
    assert reader.read_raw(date(2026, 6, 13)) is None


def test_reader_roundtrips_with_writer(tmp_path: Path) -> None:
    """The reader parses exactly what the writer produces (format contract)."""
    writer = TranscriptWriter(tmp_path, lambda: datetime.now().astimezone())
    start = datetime(2026, 6, 13, 9, 5).astimezone()
    writer.append(start, "Rivy uses BigQuery")

    reader = TranscriptReader(tmp_path)
    entries = reader.entries_for(date(2026, 6, 13))
    assert entries == [TranscriptEntry("09:05", "Rivy uses BigQuery")]


def test_history_dates_newest_first_and_filtered(tmp_path: Path) -> None:
    for name in ("2026-06-10.md", "2026-06-13.md", "2026-06-11.md"):
        (tmp_path / name).write_text(HEADER, encoding="utf-8")
    # Non-conforming names and a spool subdir must be ignored.
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    (tmp_path / "2026-13-99.md").write_text("x", encoding="utf-8")  # invalid date
    (tmp_path / "spool").mkdir()
    (tmp_path / "spool" / "20260613-090000-001.wav").write_bytes(b"\x00")

    dates = TranscriptReader(tmp_path).history_dates()
    assert dates == [date(2026, 6, 13), date(2026, 6, 11), date(2026, 6, 10)]


def test_history_dates_missing_dir_is_empty(tmp_path: Path) -> None:
    assert TranscriptReader(tmp_path / "nope").history_dates() == []
