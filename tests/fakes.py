"""Test fakes: injectable stand-ins for all hardware/OS boundaries.

Every fake is deterministic and side-effect-free in the default test suite.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np

from murmur.audio.source import AudioSourceError
from murmur.transcribe.base import TranscriptionError, TranscriptionResult

# ---------------------------------------------------------------------------
# FakeAudioSource
# ---------------------------------------------------------------------------


class FakeAudioSource:
    """Scripted audio source: frame sequences, stalls, signature changes, open failures.

    Usage::

        src = FakeAudioSource(frames=[frame1, frame2, None])  # None = timeout
        src.set_open_error("simulated failure")
        src.set_signatures(["default", "default", "headphones"])
    """

    def __init__(
        self,
        frames: Sequence[np.ndarray | None] | None = None,
        signatures: Sequence[str] | None = None,
        open_error: str | None = None,
    ) -> None:
        self._frames: deque[np.ndarray | None] = deque(frames or [])
        self._signatures: deque[str] = deque(signatures or [])
        self._open_error: str | None = open_error
        self._open_count: int = 0
        self._closed: bool = True
        self._lock = threading.Lock()

    # --- AudioSource protocol ---

    def open(self, device: str) -> None:
        with self._lock:
            if self._open_error is not None:
                raise AudioSourceError(self._open_error)
            self._closed = False
            self._open_count += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def read(self, timeout: float) -> np.ndarray | None:  # noqa: ARG002
        with self._lock:
            if not self._frames:
                return None
            return self._frames.popleft()

    def default_device_signature(self) -> str:
        with self._lock:
            if not self._signatures:
                return "default"
            return self._signatures[0]

    # --- Test helpers ---

    def push_frame(self, frame: np.ndarray | None) -> None:
        with self._lock:
            self._frames.append(frame)

    def push_signature(self, sig: str) -> None:
        with self._lock:
            self._signatures.append(sig)

    def set_open_error(self, msg: str | None) -> None:
        with self._lock:
            self._open_error = msg

    def advance_signature(self) -> None:
        """Move to the next signature (simulate device change)."""
        with self._lock:
            if self._signatures:
                self._signatures.popleft()

    @property
    def open_count(self) -> int:
        with self._lock:
            return self._open_count

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed


# ---------------------------------------------------------------------------
# AmplitudeVAD
# ---------------------------------------------------------------------------


class AmplitudeVAD:
    """Deterministic VAD: speech_prob = 1.0 when RMS > threshold, else 0.0.

    Makes segmenter tests predictable without torch or Silero.
    """

    def __init__(self, threshold: float = 0.01) -> None:
        self._threshold = threshold
        self.reset_count: int = 0

    def speech_prob(self, frame: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        return 1.0 if rms > self._threshold else 0.0

    def reset(self) -> None:
        self.reset_count += 1


# ---------------------------------------------------------------------------
# FakeTranscriber
# ---------------------------------------------------------------------------


class FakeTranscriber:
    """Scripted transcriber: returns preset results or raises TranscriptionError."""

    def __init__(
        self,
        results: Sequence[str | Exception] | None = None,
    ) -> None:
        self._results: deque[str | Exception] = deque(results or [])
        self.calls: list[dict[str, object]] = []

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        from murmur.transcribe.base import SegmentScore

        self.calls.append(
            {"audio_len": len(audio), "language": language, "initial_prompt": initial_prompt}
        )

        if not self._results:
            return TranscriptionResult(
                text="fake transcription",
                segments=(
                    SegmentScore(
                        text="fake transcription",
                        no_speech_prob=0.0,
                        compression_ratio=1.0,
                        avg_logprob=-0.3,
                    ),
                ),
            )

        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        if isinstance(result, TranscriptionError):
            raise result
        return TranscriptionResult(
            text=result,
            segments=(
                SegmentScore(
                    text=result,
                    no_speech_prob=0.0,
                    compression_ratio=1.0,
                    avg_logprob=-0.3,
                ),
            ),
        )

    def push_result(self, result: str | Exception) -> None:
        self._results.append(result)

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------------------------------------------------------------------------
# RecordingNotifier
# ---------------------------------------------------------------------------


class RecordingNotifier:
    """Captures notifications for assertion in tests."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def notify(self, title: str, message: str) -> None:
        with self._lock:
            self.notifications.append((title, message))

    def clear(self) -> None:
        with self._lock:
            self.notifications.clear()

    def titles(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.notifications]


# ---------------------------------------------------------------------------
# FixedClock
# ---------------------------------------------------------------------------


class FixedClock:
    """Injectable clock that advances on demand."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now: datetime = initial or datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)

    def set(self, dt: datetime) -> None:
        self._now = dt


# ---------------------------------------------------------------------------
# FakePermission helper
# ---------------------------------------------------------------------------


class FakePermission:
    """Returns a scripted PermissionStatus sequence."""

    def __init__(self, statuses: Sequence[str]) -> None:
        from murmur.mac.permissions import PermissionStatus

        self._statuses: deque[PermissionStatus] = deque(PermissionStatus(s) for s in statuses)
        self._default: str = statuses[-1] if statuses else "GRANTED"

    def __call__(self) -> object:
        from murmur.mac.permissions import PermissionStatus

        if self._statuses:
            return self._statuses.popleft()
        return PermissionStatus(self._default)


# ---------------------------------------------------------------------------
# FakeDiskFree
# ---------------------------------------------------------------------------


class FakeDiskFree:
    """Injectable disk-free probe that returns scripted values."""

    def __init__(self, values: Sequence[int]) -> None:
        self._values: deque[int] = deque(values)
        self._default: int = values[-1] if values else 10 * 1024**3

    def __call__(self) -> int:
        if self._values:
            return self._values.popleft()
        return self._default
