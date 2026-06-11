"""Audio source protocol and sounddevice adapter."""

from __future__ import annotations

import contextlib
import queue
import threading
from typing import Any, Protocol

import numpy as np


class AudioSourceError(Exception):
    """Raised when audio stream open fails or an unrecoverable error occurs."""


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
    """

    def __init__(self) -> None:
        self._stream: Any = None
        self._frames_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)
        self._lock = threading.Lock()

    def open(self, device: str) -> None:
        import sounddevice as sd

        from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE

        self.close()
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

        with self._lock:
            self._stream = stream

    def close(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
                stream.close()

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
