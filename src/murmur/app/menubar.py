"""Menubar application — main thread only.

MurmurApp is a rumps.App subclass. It must only be instantiated and
run on the main thread (AppKit requirement, ADR-1).

Menu structure (R-12):
  Pause/Resume
  Pause for 30 min
  Pause for 1 h
  Pause for rest of day
  ---
  Open today's transcript
  Open transcripts folder
  ---
  Generate notes now  (disabled in v1 — no-op)
  ---
  Quit
"""

from __future__ import annotations

import logging
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import rumps

from murmur.control import Controller, IconState, StatusBoard

_log = logging.getLogger(__name__)


class MurmurApp(rumps.App):  # type: ignore[misc]
    """Menubar application for Murmur."""

    def __init__(
        self,
        controller: Controller,
        status: StatusBoard,
        runtime: Any,
        transcripts_dir: Path,
        config_file: Path | None = None,
    ) -> None:
        super().__init__("Murmur", title=IconState.RECORDING.value)
        self._controller = controller
        self._status = status
        self._runtime = runtime
        self._transcripts_dir = transcripts_dir
        self._config_file = config_file
        self._window_controller: Any | None = None  # built lazily on first open

        # Menu items (order matches R-12)
        self._item_pause_resume = rumps.MenuItem("Pause", callback=self._on_pause_resume)
        self._item_pause_30 = rumps.MenuItem("Pause for 30 min", callback=self._on_pause_30)
        self._item_pause_1h = rumps.MenuItem("Pause for 1 h", callback=self._on_pause_1h)
        self._item_pause_day = rumps.MenuItem("Pause for rest of day", callback=self._on_pause_day)
        self._item_open_window = rumps.MenuItem("Open Murmur Window", callback=self._on_open_window)
        self._item_open_today = rumps.MenuItem(
            "Open today's transcript", callback=self._on_open_today
        )
        self._item_open_folder = rumps.MenuItem(
            "Open transcripts folder", callback=self._on_open_folder
        )
        # v2 placeholder — callback=None renders it disabled in rumps
        self._item_notes = rumps.MenuItem("Generate notes now", callback=None)
        self._item_quit = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._item_pause_resume,
            self._item_pause_30,
            self._item_pause_1h,
            self._item_pause_day,
            rumps.separator,
            self._item_open_window,
            self._item_open_today,
            self._item_open_folder,
            rumps.separator,
            self._item_notes,
            rumps.separator,
            self._item_quit,
        ]

        # Pull status every 1 s
        self._timer = rumps.Timer(self._on_tick, 1)
        self._timer.start()

    # ------------------------------------------------------------------
    # Timer callback — pull-only status update (ADR-1)

    def _on_tick(self, _timer: object) -> None:
        snap = self._status.snapshot()
        self.title = snap.icon.value

        # Update Pause/Resume label
        paused = snap.icon == IconState.PAUSED
        self._item_pause_resume.title = "Resume" if paused else "Pause"

        # Dropdown header line (queue depth + error count)
        lines = [f"Queue: {snap.queue_depth} · Errors: {snap.error_count}"]
        if snap.download is not None:
            done, total = snap.download
            pct = int(done / total * 100) if total > 0 else 0
            lines.append(f"Downloading model... {pct}%")
        elif snap.notice:
            lines.append(snap.notice)

    # ------------------------------------------------------------------
    # Menu callbacks

    def _on_pause_resume(self, _sender: object) -> None:
        self._controller.toggle_pause()

    def _on_pause_30(self, _sender: object) -> None:
        self._controller.pause_for(30 * 60)

    def _on_pause_1h(self, _sender: object) -> None:
        self._controller.pause_for(60 * 60)

    def _on_pause_day(self, _sender: object) -> None:
        self._controller.pause_rest_of_day()

    def _on_open_window(self, _sender: object) -> None:
        # Built lazily: the NSWindow needs the NSApplication that rumps creates
        # in run(), so we cannot construct it at __init__ time.
        if self._window_controller is None:
            from murmur.app.window import make_window_controller
            from murmur.output.reader import TranscriptReader

            self._window_controller = make_window_controller(
                controller=self._controller,
                status=self._status,
                reader=TranscriptReader(self._transcripts_dir),
                config_file=self._config_file,
            )
        self._window_controller.show()

    def _on_open_today(self, _sender: object) -> None:
        today = date.today()
        transcript = self._transcripts_dir / f"{today:%Y-%m-%d}.md"
        if not transcript.exists():
            from murmur.mac.notify import OsascriptNotifier

            OsascriptNotifier().notify("Murmur", f"No transcript yet for today ({today:%Y-%m-%d}).")
            return
        subprocess.run(["open", str(transcript)], check=False)

    def _on_open_folder(self, _sender: object) -> None:
        subprocess.run(["open", str(self._transcripts_dir)], check=False)

    def _on_quit(self, _sender: object) -> None:
        try:
            self._runtime.stop()
        except Exception as exc:
            _log.error("Error during stop: %s", exc)
        rumps.quit_application()
