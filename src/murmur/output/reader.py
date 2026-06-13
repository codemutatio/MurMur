"""Read-only access to daily transcript Markdown files.

The window UI uses this to render today's live transcript and to browse the
history of past days. It is the read-side counterpart to
:class:`murmur.output.writer.TranscriptWriter` and parses the same
``**[HH:MM]** <text>`` line format (see writer.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Matches a single transcript line: **[HH:MM]** text
_ENTRY_RE = re.compile(r"^\*\*\[(\d{2}:\d{2})\]\*\*\s(.*)$")


@dataclass(frozen=True)
class TranscriptEntry:
    """One transcribed segment: a local HH:MM stamp and its text."""

    time: str
    text: str


def parse_transcript(content: str) -> list[TranscriptEntry]:
    """Parse the body of a daily transcript file into entries.

    Non-matching lines (the header, blank lines) are ignored, so this is safe
    to call on a partially written or hand-edited file.
    """
    entries: list[TranscriptEntry] = []
    for line in content.splitlines():
        m = _ENTRY_RE.match(line)
        if m is not None:
            entries.append(TranscriptEntry(time=m.group(1), text=m.group(2)))
    return entries


class TranscriptReader:
    """Read daily transcript files from *transcripts_dir*.

    Never raises on missing/unreadable files — those surface as ``None`` or an
    empty list so the UI can render an empty state.
    """

    def __init__(self, transcripts_dir: Path) -> None:
        self._dir = transcripts_dir

    def path_for(self, day: date) -> Path:
        """Return the transcript path for *day* (whether or not it exists)."""
        return self._dir / f"{day:%Y-%m-%d}.md"

    def read_raw(self, day: date) -> str | None:
        """Return the raw file contents for *day*, or None if absent/unreadable."""
        try:
            return self.path_for(day).read_text(encoding="utf-8")
        except OSError:
            return None

    def entries_for(self, day: date) -> list[TranscriptEntry]:
        """Return parsed entries for *day* (empty list if the file is absent)."""
        raw = self.read_raw(day)
        return parse_transcript(raw) if raw is not None else []

    def history_dates(self) -> list[date]:
        """Return all dates that have a transcript file, newest first.

        Only files named ``YYYY-MM-DD.md`` are considered; anything else in the
        directory (e.g. the ``spool/`` subdir) is skipped.
        """
        dates: list[date] = []
        if not self._dir.exists():
            return dates
        for p in self._dir.glob("*.md"):
            try:
                d = datetime.strptime(p.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            dates.append(d)
        dates.sort(reverse=True)
        return dates
