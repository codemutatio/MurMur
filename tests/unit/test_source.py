"""Tests for murmur.audio.source — ADR-1 conformance (F-6)."""

from __future__ import annotations

from murmur.audio.source import SounddeviceSource


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
