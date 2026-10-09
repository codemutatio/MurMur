"""Label captured speech segments as "call" or "media" from live audio activity.

A background thread samples which apps are using the mic / playing audio once a
second (murmur.mac.audio_activity) and keeps a rolling timeline. When the
transcription worker handles a segment it asks for the label covering that
segment's time window; the label is decided by majority over the samples in the
window, so a one-second notification sound does not tag a whole segment.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass

from murmur.mac.audio_activity import ContextKind, classify, list_audio_processes

_log = logging.getLogger(__name__)

Probe = Callable[[], tuple[ContextKind | None, str | None]]


@dataclass(frozen=True)
class SegmentContext:
    kind: ContextKind
    app: str

    def marker(self) -> str:
        """Comment text written above the transcript entry."""
        return f"{self.kind} app={self.app}"


def _default_probe() -> tuple[ContextKind | None, str | None]:
    return classify(list_audio_processes())


class AudioContextMonitor(threading.Thread):
    """Samples audio activity and answers ``label(start, end)`` queries."""

    def __init__(
        self,
        *,
        stop: threading.Event,
        probe: Probe | None = None,
        interval_s: float = 1.0,
        window_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(name="audio-context", daemon=True)
        self._stop_event = stop
        self._probe = probe or _default_probe
        self._interval_s = interval_s
        self._clock = clock
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, ContextKind | None, str | None]] = deque(
            maxlen=max(1, int(window_s / interval_s))
        )
        self._failed = False

    def sample_once(self) -> None:
        try:
            kind, app = self._probe()
        except Exception as exc:  # unsupported OS / transient CoreAudio error
            if not self._failed:
                _log.warning("Audio context probe unavailable; segments won't be tagged: %s", exc)
                self._failed = True
            return
        if self._failed:
            _log.info("Audio context probe recovered")
            self._failed = False
        with self._lock:
            self._samples.append((self._clock(), kind, app))

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.sample_once()
            self._stop_event.wait(self._interval_s)

    def label(self, start: float, end: float) -> SegmentContext | None:
        """Majority context over [start, end] (epoch seconds), or None.

        None when there are no samples in the window (e.g. a backlog segment
        recorded before this process started) or when most samples show neither
        a call nor media.
        """
        with self._lock:
            window = [(k, a) for t, k, a in self._samples if start <= t <= end]
        if not window:
            return None
        kinds = Counter(k for k, _ in window)
        kind: ContextKind
        for kind in ("call", "media"):  # a call outranks media playing alongside it
            if kinds[kind] * 2 >= len(window):
                app = Counter(a for k, a in window if k == kind).most_common(1)[0][0]
                return SegmentContext(kind=kind, app=app or "unknown")
        return None
