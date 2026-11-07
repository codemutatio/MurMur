"""Tests for murmur.audio.source — ADR-1 conformance (F-6)."""

from __future__ import annotations

import threading
import time

import pytest

from murmur.audio.source import AudioSourceError, AudioSourceStuckError, SounddeviceSource


# F-6: ADR-1 — callback must not write instance state
def test_sounddevice_source_callback_owns_no_instance_state_f6() -> None:
    """SounddeviceSource has no dropped_frames counter on the instance.

    ADR-1: the PortAudio callback does exactly one thing — put_nowait.  It must
    not touch any instance state, including drop counters that would be mutated
    from the PortAudio C-callback thread (violating thread ownership).

    F-6 fix: the counter was removed; the callback only puts to frames_q.
    This test verifies the attribute no longer exists on the instance so the fix
    cannot silently regress.
    """
    src = SounddeviceSource()
    assert not hasattr(src, "_dropped_frames"), (
        "SounddeviceSource._dropped_frames must not exist: callback must not write "
        "instance state (ADR-1 thread ownership rule, F-6)"
    )
    assert not hasattr(src, "dropped_frames"), (
        "SounddeviceSource.dropped_frames property must not exist after F-6 fix"
    )


# --- hung CoreAudio calls (2026-11-04: stop() deadlocked after sleep/wake) -------------


class _HangingStream:
    def __init__(self) -> None:
        self.release = threading.Event()

    def stop(self) -> None:
        self.release.wait(5)

    def close(self) -> None:
        pass


def test_hung_stream_close_raises_stuck_quickly() -> None:
    src = SounddeviceSource(call_timeout_s=0.2)
    hanging = _HangingStream()
    src._stream = hanging
    t0 = time.monotonic()
    with pytest.raises(AudioSourceStuckError):
        src.close()
    assert time.monotonic() - t0 < 2
    hanging.release.set()


def test_after_a_hang_portaudio_is_never_touched_again(monkeypatch: pytest.MonkeyPatch) -> None:
    src = SounddeviceSource(call_timeout_s=0.2)
    hanging = _HangingStream()
    src._stream = hanging
    with pytest.raises(AudioSourceStuckError):
        src.close()
    touched: list[str] = []
    monkeypatch.setattr(src, "_open_stream", lambda device: touched.append(device))
    with pytest.raises(AudioSourceStuckError):
        src.open("default")
    assert touched == []  # a second PortAudio call could hang the capture thread too
    hanging.release.set()


def test_stuck_is_an_audio_source_error() -> None:
    assert issubclass(AudioSourceStuckError, AudioSourceError)
