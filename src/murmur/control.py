"""Controller, StateStore, PauseState, StatusBoard, IconState.

This module is the sole cross-thread mutable state hub. Every field is guarded
by a threading.Lock. No other module may directly mutate shared state.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pause state
# ---------------------------------------------------------------------------


class PauseMode(StrEnum):
    MANUAL = "manual"
    TIMED = "timed"
    REST_OF_DAY = "rest_of_day"


@dataclass(frozen=True)
class PauseState:
    """Persisted pause state.

    MANUAL: until=None (paused until explicit resume).
    TIMED: until=now+seconds.
    REST_OF_DAY: until=next local midnight at set time.
    Corrupt/missing file → PauseState() → not paused (recording).
    """

    mode: PauseMode | None = None
    until: datetime | None = None


def is_pause_active(s: PauseState, now: datetime) -> bool:
    """Return True when *s* represents an active pause at *now*."""
    if s.mode is None:
        return False
    if s.mode == PauseMode.MANUAL:
        return True
    if s.until is None:
        return False
    # until is tz-aware; now must also be tz-aware
    return now < s.until


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class StateStore:
    """Read/write PauseState to a JSON file, crash-safe (tmp+fsync+replace)."""

    # JSON schema: {"version": 1, "pause": {"mode": "rest_of_day", "until": "..."}}

    def __init__(self, path: Path, now: Callable[[], datetime]) -> None:
        self._path = path
        self._now = now

    def load(self) -> PauseState:
        """Load PauseState. Never raises — corrupt/missing → not paused."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            pause = raw.get("pause", {})
            mode_raw = pause.get("mode")
            mode = PauseMode(mode_raw) if mode_raw in PauseMode._value2member_map_ else None
            until_raw = pause.get("until")
            until: datetime | None = None
            if until_raw:
                until = datetime.fromisoformat(until_raw)
            return PauseState(mode=mode, until=until)
        except Exception:
            return PauseState()

    def save(self, s: PauseState) -> None:
        """Persist *s* atomically (tmp+fsync+os.replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict[str, object] = {
            "version": 1,
            "pause": {
                "mode": s.mode.value if s.mode else None,
                "until": s.until.isoformat() if s.until else None,
            },
        }
        text = json.dumps(doc, indent=2)
        fd, tmp_str = tempfile.mkstemp(dir=self._path.parent, suffix=".json.tmp")
        tmp_path = Path(tmp_str)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        os.replace(tmp_path, self._path)


# ---------------------------------------------------------------------------
# Icon / Status
# ---------------------------------------------------------------------------


class IconState(StrEnum):
    RECORDING = "●"
    PAUSED = "⏸"
    ERROR = "⚠"


@dataclass
class StatusSnapshot:
    icon: IconState
    queue_depth: int
    error_count: int
    pause_until: datetime | None
    notice: str | None
    download: tuple[int, int] | None  # (bytes_done, bytes_total)


class StatusBoard:
    """Thread-safe status object. Only the menubar timer reads from it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._icon = IconState.RECORDING
        self._queue_depth: int = 0
        self._error_count: int = 0
        self._pause_until: datetime | None = None
        self._notice: str | None = None
        self._download: tuple[int, int] | None = None

    def snapshot(self) -> StatusSnapshot:
        with self._lock:
            return StatusSnapshot(
                icon=self._icon,
                queue_depth=self._queue_depth,
                error_count=self._error_count,
                pause_until=self._pause_until,
                notice=self._notice,
                download=self._download,
            )

    def set_icon(self, icon: IconState) -> None:
        with self._lock:
            self._icon = icon

    def set_queue_depth(self, depth: int) -> None:
        with self._lock:
            self._queue_depth = depth

    def increment_errors(self) -> None:
        with self._lock:
            self._error_count += 1

    def set_notice(self, notice: str | None) -> None:
        with self._lock:
            self._notice = notice

    def set_download(self, done: int, total: int) -> None:
        with self._lock:
            self._download = (done, total)

    def clear_download(self) -> None:
        with self._lock:
            self._download = None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class Controller:
    """Thread-safe. The ONLY entry point for pause state changes."""

    def __init__(
        self,
        store: StateStore,
        notifier: object,  # Notifier protocol
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._notifier = notifier
        self._now = now
        self._lock = threading.Lock()

    def toggle_pause(self) -> bool:
        """Toggle between recording and manual pause. Returns new paused state.

        Fires a "Paused"/"Recording" notification (R-13).
        """
        with self._lock:
            current = self._store.load()
            if is_pause_active(current, self._now()):
                new_state = PauseState()
                self._store.save(new_state)
                self._notifier.notify("Murmur", "Recording")  # type: ignore[attr-defined]
                return False
            else:
                new_state = PauseState(mode=PauseMode.MANUAL)
                self._store.save(new_state)
                self._notifier.notify("Murmur", "Paused")  # type: ignore[attr-defined]
                return True

    def pause_for(self, seconds: float) -> None:
        """TIMED pause: until = now + seconds."""
        from datetime import timedelta

        with self._lock:
            until = self._now() + timedelta(seconds=seconds)
            state = PauseState(mode=PauseMode.TIMED, until=until)
            self._store.save(state)
            self._notifier.notify("Murmur", f"Paused for {int(seconds // 60)} min")  # type: ignore[attr-defined]

    def pause_rest_of_day(self) -> None:
        """REST_OF_DAY pause: until = next local midnight."""
        now = self._now()
        # Compute start of the next local day (next midnight)
        local_now = now.astimezone()
        from datetime import timedelta

        next_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )
        with self._lock:
            state = PauseState(mode=PauseMode.REST_OF_DAY, until=next_day)
            self._store.save(state)
            self._notifier.notify("Murmur", "Paused for rest of day")  # type: ignore[attr-defined]

    def resume(self) -> None:
        """Explicitly resume recording."""
        with self._lock:
            self._store.save(PauseState())
            self._notifier.notify("Murmur", "Recording")  # type: ignore[attr-defined]

    def effective_paused(self) -> bool:
        """Return True if currently paused (auto-expiry check — no timers)."""
        return is_pause_active(self._store.load(), self._now())
