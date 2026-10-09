"""Transcript writer: atomic append to daily Markdown files.

ADR-2: read-modify-replace via os.replace for crash safety (R-9).
The writer thread is the ONLY writer — no concurrent-writer hazard.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class TranscriptWriter:
    """Append speech segments to daily Markdown files in *transcripts_dir*.

    File naming: ``{transcripts_dir}/{start_at:%Y-%m-%d}.md``
    Header (on creation): ``# Transcript — YYYY-MM-DD\\n\\n``
    Line format: ``**[HH:MM]** <text>\\n\\n``

    Created on first append only (AC-5). If the file is deleted mid-day the
    next append recreates it (E-12). All writes are crash-safe: write to a temp
    file in the same directory, flush+fsync, then os.replace (ADR-2).
    """

    def __init__(self, transcripts_dir: Path, now: Callable[[], datetime]) -> None:
        self._dir = transcripts_dir
        self._now = now

    def append(self, start_at: datetime, text: str, context: str | None = None) -> Path:
        """Append *text* to the transcript for *start_at*'s date.

        *context* (e.g. ``"media app=com.google.Chrome"``) is written as an HTML
        comment on the line above the entry: invisible when the Markdown is
        rendered, ignored by every line parser (they match only ``**[HH:MM]**``
        lines), but readable by the nightly processing task.

        Returns:
            Path to the transcript file.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        date_str = f"{start_at:%Y-%m-%d}"
        target = self._dir / f"{date_str}.md"

        # Read existing content (or start fresh with header — E-12)
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError:
                existing = f"# Transcript — {date_str}\n\n"
        else:
            existing = f"# Transcript — {date_str}\n\n"

        # Format new line: **[HH:MM]** text  (AC-4, local time)
        # Convert to local timezone before formatting so the stamp reflects the
        # wall-clock time on the user's machine, not the datetime's stored tz.
        local_start = start_at.astimezone()
        new_line = f"**[{local_start:%H:%M}]** {text}\n\n"
        if context:
            new_line = f"<!-- murmur: {context.replace('--', '-')} -->\n" + new_line
        content = existing + new_line

        # Write to temp, fsync, then atomically replace (ADR-2, R-9)
        fd, tmp_str = tempfile.mkstemp(dir=self._dir, suffix=".md.tmp")
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        os.replace(tmp_path, target)
        return target
