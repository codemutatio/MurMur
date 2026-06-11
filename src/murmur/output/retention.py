"""Transcript file retention pruning (R-26).

Deletes files matching the daily transcript pattern (YYYY-MM-DD.md) that are
strictly older than ``retention_days`` days. 0 means keep forever (AC-10).
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path

_log = logging.getLogger(__name__)

# Pattern for daily transcript filenames
_DATE_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


def prune_transcripts(
    transcripts_dir: Path,
    retention_days: int,
    today: date,
) -> list[Path]:
    """Delete transcript files older than *retention_days*.

    Args:
        transcripts_dir: Directory containing transcript Markdown files.
        retention_days: Keep files within this many days; 0 = keep forever.
        today: The current date (injected so tests can control time).

    Returns:
        List of Paths that were deleted.

    Note:
        ``retention_days = 0`` is a no-op (AC-10).
        Strictly-older-than semantics: with ``retention_days = 7``, an
        8-day-old file is deleted; a 7-day-old file is kept (AC-9).
        OSError on individual files → logged and skipped (E-22).
    """
    if retention_days == 0:
        return []

    if not transcripts_dir.exists():
        return []

    cutoff = today - timedelta(days=retention_days)
    deleted: list[Path] = []

    for path in transcripts_dir.iterdir():
        if not _DATE_PAT.match(path.name):
            continue
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            continue

        if file_date < cutoff:
            try:
                path.unlink()
                deleted.append(path)
                _log.info("Pruned transcript %s (retention %d days)", path.name, retention_days)
            except OSError as exc:
                _log.warning("Could not prune %s: %s", path.name, exc)

    return deleted
