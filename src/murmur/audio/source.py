"""Audio source protocol and sounddevice adapter."""

from __future__ import annotations

import contextlib
import queue
import threading
from typing import Any, Protocol

import numpy as np


class AudioSourceError(Exception):
    """Raised when audio stream open fails or an unrecoverable error occurs."""


class AudioSourceStuckError(AudioSourceError):
    """A PortAudio/CoreAudio call never returned.

    Seen after sleep/wake (2026-11-04): ``stream.stop()`` deadlocked against
    CoreAudio's IO thread and capture silently stopped for three days. The
    stuck thread can't be cancelled and PortAudio is unusable in this process
    afterwards, so the only recovery is a fresh process.
    """


# A healthy stop/close/open takes milliseconds; this is generous.
CALL_TIMEOUT_S: float = 10.0


def _bounded(fn: Any, what: str, timeout: float) -> Any:
    """Run ``fn`` on a helper thread; raise AudioSourceStuckError if it doesn't return."""
    done = threading.Event()
    out: list[Any] = []
    err: list[BaseException] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as exc:  # re-raised on the caller's thread
            err.append(exc)
        finally:
            done.set()

    threading.Thread(target=run, name=f"audio-{what}", daemon=True).start()
    if not done.wait(timeout):
        raise AudioSourceStuckError(f"{what} did not return within {timeout:.0f} s")
    if err:
        raise err[0]
    return out[0] if out else None


class AudioSource(Protocol):
    """Protocol for an audio input source."""

    def open(self, device: str) -> None:
        """Open the audio stream. Raises AudioSourceError on failure."""
        ...

    def close(self) -> None:
        """Close the audio stream. Idempotent."""
        ...

    def read(self, timeout: float) -> np.ndarray | None:
        """Return one float32 frame of shape (512,), or None on timeout."""
        ...

    def default_device_signature(self) -> str:
        """Opaque identifier of current default input device. Empty string if none."""
        ...


class SounddeviceSource:
    """Production AudioSource backed by sounddevice / PortAudio.

    Notes:
        - open() calls sd._terminate() + sd._initialize() before opening the stream
          because PortAudio caches its device list at init time (Risk 3 in plan).
        - ADR-1: the PortAudio callback does exactly one thing — put_nowait into
          frames_q.  It touches no instance state (F-6: callback ownership rule).
        - Thread affinity: constructed and used only on the capture-engine thread.
          Stream calls run on a short-lived helper thread only so they can be
          timed out (see AudioSourceStuckError); the caller always waits for them.
    """

    def __init__(self, call_timeout_s: float = CALL_TIMEOUT_S) -> None:
        self._stream: Any = None
        self._frames_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)
        self._lock = threading.Lock()
        self._timeout = call_timeout_s
        self._stuck: str | None = None  # once set, PortAudio is not touched again

    def _check_stuck(self) -> None:
        if self._stuck is not None:
            raise AudioSourceStuckError(self._stuck)

    def open(self, device: str) -> None:
        self.close()
        self._check_stuck()
        try:
            stream = _bounded(lambda: self._open_stream(device), "stream open", self._timeout)
        except AudioSourceStuckError as exc:
            self._stuck = str(exc)
            raise
        with self._lock:
            self._stream = stream

    def _open_stream(self, device: str) -> Any:
        import sounddevice as sd

        from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE

        # Re-initialize PortAudio so it re-reads the device list.
        sd._terminate()  # private PortAudio API — reinitialises device list
        sd._initialize()

        frames_q = self._frames_q

        # ADR-1: the PortAudio callback does exactly one thing — put_nowait.
        # It must not touch any instance state (including counters) because it
        # runs on a C-thread owned by PortAudio, not the capture-engine thread.
        # A drop counter transferred here would violate the ownership rule (F-6).
        def _callback(
            indata: np.ndarray,
            frames: int,  # noqa: ARG001
            time: object,  # noqa: ARG001
            status: object,  # noqa: ARG001
        ) -> None:
            import contextlib

            with contextlib.suppress(queue.Full):
                frames_q.put_nowait(indata.copy().reshape(-1))

        dev = None if device == "default" else device
        try:
            stream = sd.InputStream(
                device=dev,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=FRAME_SAMPLES,
                callback=_callback,
            )
            stream.start()
        except Exception as exc:
            raise AudioSourceError(str(exc)) from exc
        return stream

    def close(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        self._check_stuck()

        def stop_and_close() -> None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()

        try:
            _bounded(stop_and_close, "stream close", self._timeout)
        except AudioSourceStuckError as exc:
            self._stuck = str(exc)
            raise

    def read(self, timeout: float) -> np.ndarray | None:
        try:
            return self._frames_q.get(timeout=timeout)
        except queue.Empty:
            return None

    def default_device_signature(self) -> str:
        try:
            import sounddevice as sd

            info = sd.query_devices(kind="input")
            return str(info.get("name", "")) if isinstance(info, dict) else ""
        except Exception:
            return ""
