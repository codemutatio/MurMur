"""Heartbeat file so downstream tooling can tell if Murmur is actually working.

A silent outage (e.g. model weights wiped, every segment failing) previously went
unnoticed for days because nothing outside the app could see its state. This
writes ``.murmur-health.json`` next to the daily transcripts — a folder the
nightly/morning Cowork tasks already read — every minute and on shutdown.

"No new transcript lines" is ambiguous (quiet day vs. broken). The heartbeat
disambiguates: a fresh ``updated_at`` with recent ``last_transcribed_at`` is
healthy; a fresh ``updated_at`` with climbing ``consecutive_errors`` is broken;
a stale ``updated_at`` means the app isn't running at all.

``memory`` records the app's and the transcription worker's footprint, so a
slow leak (the 15 GB one went unseen for weeks) shows up in the morning digest.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

HEALTH_FILENAME = ".murmur-health.json"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class HealthReporter:
    """Tracks transcription outcomes and periodically writes a JSON heartbeat."""

    def __init__(
        self,
        path: Path,
        *,
        model: str,
        now: Callable[[], datetime],
        is_paused: Callable[[], bool] = lambda: False,
        spool_dir: Path | None = None,
        worker_pid: Callable[[], int | None] = lambda: None,
        footprint: Callable[[int], Any] | None = None,
        interval_s: float = 60.0,
    ) -> None:
        self._path = path
        self._model = model
        self._now = now
        self._is_paused = is_paused
        self._spool_dir = spool_dir
        self._interval_s = interval_s
        self._worker_pid = worker_pid
        if footprint is None:
            from murmur.mac.memory import footprint as mac_footprint

            footprint = mac_footprint
        self._footprint: Callable[[int], Any] = footprint
        self._worker_peak = 0
        self._lock = threading.Lock()
        self._started_at = now()
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._transcribed = 0
        self._errors = 0
        self._consecutive_errors = 0
        self._media_tagged = 0
        self._call_tagged = 0
        self._media_skipped = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- recording ---------------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            self._last_success = self._now()
            self._transcribed += 1
            self._consecutive_errors = 0

    def record_error(self, message: str) -> None:
        with self._lock:
            self._errors += 1
            self._consecutive_errors += 1
            self._last_error = message[:300]
            self._last_error_at = self._now()

    def record_context(self, kind: str | None, *, skipped: bool = False) -> None:
        with self._lock:
            if kind == "media":
                self._media_tagged += 1
                if skipped:
                    self._media_skipped += 1
            elif kind == "call":
                self._call_tagged += 1

    # -- output ------------------------------------------------------------
    def snapshot(self, *, stopped: bool = False) -> dict[str, Any]:
        with self._lock:
            if stopped:
                state = "stopped"
            elif self._is_paused():
                state = "paused"
            elif self._consecutive_errors >= 3:
                state = "failing"
            else:
                state = "recording"
            backlog = None
            if self._spool_dir is not None:
                try:
                    backlog = sum(1 for _ in self._spool_dir.glob("*.wav"))
                except OSError:
                    backlog = None
            return {
                "schema": 1,
                "updated_at": _iso(self._now()),
                "state": state,
                "pid": os.getpid(),
                "model": self._model,
                "started_at": _iso(self._started_at),
                "last_transcribed_at": _iso(self._last_success),
                "transcribed_since_start": self._transcribed,
                "errors_since_start": self._errors,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "last_error_at": _iso(self._last_error_at),
                "spool_backlog": backlog,
                "segments_tagged_media": self._media_tagged,
                "segments_tagged_call": self._call_tagged,
                "media_segments_skipped": self._media_skipped,
                "memory": self._memory_locked(),
            }

    def _memory_locked(self) -> dict[str, int | None]:
        """Footprints in MB. The worker comes and goes (it's released when idle),
        so its peak is the largest seen across heartbeats since the app started."""
        mb = 1024 * 1024
        app = self._footprint(os.getpid())
        pid = self._worker_pid()
        worker = self._footprint(pid) if pid else None
        if worker is not None:
            self._worker_peak = max(self._worker_peak, worker.peak)
        return {
            "app_mb": round(app.current / mb) if app else None,
            "app_peak_mb": round(app.peak / mb) if app else None,
            "worker_mb": round(worker.current / mb) if worker else None,
            "worker_peak_mb": round(self._worker_peak / mb) if self._worker_peak else None,
        }

    def write(self, *, stopped: bool = False) -> None:
        data = json.dumps(self.snapshot(stopped=stopped), indent=2) + "\n"
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, self._path)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            _log.warning("Could not write health file %s: %s", self._path, exc)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.write()
        self._thread = threading.Thread(target=self._loop, name="health", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.write()

    def stop(self) -> None:
        self._stop.set()
        self.write(stopped=True)
