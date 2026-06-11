"""Tests for murmur.output.retention — T-5."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from murmur.output.retention import prune_transcripts


def _touch(dir: Path, name: str) -> Path:
    p = dir / name
    p.write_text("content", encoding="utf-8")
    return p


# AC-9
def test_prunes_8d_keeps_7d(tmp_path: Path) -> None:  # [AC-9]
    """8-day-old file deleted; 7-day-old file kept (retention=7)."""
    today = date(2026, 6, 12)
    _touch(tmp_path, "2026-06-04.md")  # 8 days ago → should be deleted
    _touch(tmp_path, "2026-06-05.md")  # 7 days ago → should be kept
    _touch(tmp_path, "2026-06-12.md")  # today → kept

    deleted = prune_transcripts(tmp_path, retention_days=7, today=today)

    assert len(deleted) == 1
    assert deleted[0].name == "2026-06-04.md"
    assert (tmp_path / "2026-06-05.md").exists()
    assert (tmp_path / "2026-06-12.md").exists()


# AC-10
def test_zero_keeps_forever(tmp_path: Path) -> None:  # [AC-10]
    """retention_days=0 → no files deleted regardless of age."""
    today = date(2026, 6, 12)
    _touch(tmp_path, "2020-01-01.md")  # very old
    _touch(tmp_path, "2026-06-12.md")

    deleted = prune_transcripts(tmp_path, retention_days=0, today=today)
    assert deleted == []
    assert (tmp_path / "2020-01-01.md").exists()


# E-22
def test_skips_locked_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # [E-22]
    """OSError on deletion → logged + skipped, no crash."""
    today = date(2026, 6, 12)
    _touch(tmp_path, "2026-06-04.md")  # 8 days ago

    original_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("file busy")

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    # Should not raise
    deleted = prune_transcripts(tmp_path, retention_days=7, today=today)
    assert deleted == []  # nothing was actually deleted


def test_ignores_non_transcript_files(tmp_path: Path) -> None:
    """Files not matching YYYY-MM-DD.md pattern are not touched."""
    today = date(2026, 6, 12)
    _touch(tmp_path, "notes.md")
    _touch(tmp_path, "spool")
    _touch(tmp_path, "README.txt")

    deleted = prune_transcripts(tmp_path, retention_days=1, today=today)
    assert deleted == []
    assert (tmp_path / "notes.md").exists()


def test_missing_dir_is_noop(tmp_path: Path) -> None:
    """Missing directory → no-op, no crash."""
    deleted = prune_transcripts(tmp_path / "nonexistent", retention_days=7, today=date.today())
    assert deleted == []


def test_boundary_exact_retention_days_kept(tmp_path: Path) -> None:
    """File exactly retention_days old is NOT deleted."""
    today = date(2026, 6, 12)
    _touch(tmp_path, "2026-06-05.md")  # exactly 7 days ago
    deleted = prune_transcripts(tmp_path, retention_days=7, today=today)
    assert deleted == []
    assert (tmp_path / "2026-06-05.md").exists()
