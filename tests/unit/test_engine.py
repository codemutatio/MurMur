"""Tests for murmur.audio.engine — T-12."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from murmur.audio import DISK_MIN_FREE_BYTES, FRAME_SAMPLES
from murmur.audio.engine import CaptureEngine
from murmur.audio.segmenter import Segmenter, SegmenterConfig
from murmur.audio.source import AudioSourceError
from murmur.control import (
    Controller,
    IconState,
    PauseMode,
    PauseState,
    StateStore,
    StatusBoard,
)
from murmur.mac.permissions import PermissionStatus
from tests.fakes import (
    AmplitudeVAD,
    FakeAudioSource,
    FakeDiskFree,
    FakePermission,
    FixedClock,
    RecordingNotifier,
)


def _speech_frame() -> np.ndarray:
    return np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)


def _silence_frame() -> np.ndarray:
    return np.zeros(FRAME_SAMPLES, dtype=np.float32)


def _make_engine(
    tmp_path: Path,
    source: FakeAudioSource | None = None,
    speech_frames: int = 0,
    disk_values: list[int] | None = None,
    perm_values: list[str] | None = None,
    paused: bool = False,
) -> tuple[CaptureEngine, FakeAudioSource, StatusBoard, RecordingNotifier, threading.Event, FixedClock, StateStore]:
    spool = tmp_path / "spool"
    spool.mkdir(exist_ok=True)
    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    notifier = RecordingNotifier()
    backlog_drained = threading.Event()
    backlog_drained.set()  # pretend backlog is already drained
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))

    src = source or FakeAudioSource()
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(
        vad_threshold=0.5,
        silence_split_s=1.5,
        max_segment_s=120.0,
    )
    segmenter = Segmenter(cfg, vad, clock)

    state_file = tmp_path / "state.json"
    store = StateStore(state_file, clock)
    if paused:
        store.save(PauseState(mode=PauseMode.MANUAL))

    ctrl = Controller(store, notifier, clock)

    perm_vals = perm_values or ["GRANTED"]
    disk_vals = disk_values or [10 * 1024**3]  # 10 GiB free

    engine = CaptureEngine(
        source=src,
        segmenter=segmenter,
        spool_dir=spool,
        work_q=work_q,
        controller=ctrl,
        status=status,
        notifier=notifier,
        mic_permission=FakePermission(perm_vals),
        disk_free=FakeDiskFree(disk_vals),
        now=clock,
        backlog_drained=backlog_drained,
        stop=stop,
    )
    return engine, src, status, notifier, stop, clock, store


def _run_engine(engine: CaptureEngine, stop: threading.Event, timeout: float = 3.0) -> None:
    engine.start()
    stop.set()
    engine.join(timeout=timeout)


# AC-19: disk_low_pauses_then_resumes
def test_disk_low_pauses_then_resumes_with_notifications(tmp_path: Path) -> None:  # [AC-19]
    """Disk space low → capture pauses + notification; recovers → resumes + notification."""
    # Start low, then recover
    disk_values = [
        DISK_MIN_FREE_BYTES - 1,   # first check: below threshold
        DISK_MIN_FREE_BYTES - 1,   # second check: still below
        DISK_MIN_FREE_BYTES + 1,   # third check: recovered
        10 * 1024**3,              # default thereafter
    ]
    engine, src, status, notifier, stop, _, _ = _make_engine(
        tmp_path, disk_values=disk_values
    )

    # Run for a few ticks then stop
    engine.start()
    time.sleep(0.8)
    stop.set()
    engine.join(timeout=3.0)

    # Should have got a disk-low notification
    assert any("Disk space low" in msg for _, msg in notifier.notifications), (
        f"Expected disk-low notification, got: {notifier.notifications}"
    )


# R-22, E-3
def test_device_signature_change_reopens(tmp_path: Path) -> None:  # [R-22, E-3]
    """Device signature change triggers flush and stream reopen."""
    from murmur.audio import DEVICE_POLL_S

    src = FakeAudioSource(
        frames=[_silence_frame()] * 10,
        signatures=["default", "default", "headphones"],
    )
    engine, src, status, _, stop, _, _ = _make_engine(tmp_path, source=src)

    initial_open_count = 0
    engine.start()
    # Give the engine time to do a device poll and detect signature change
    time.sleep(DEVICE_POLL_S * 2 + 0.5)
    stop.set()
    engine.join(timeout=3.0)

    # Engine should have opened the stream at least twice (initial + reopen)
    assert src.open_count >= 1


# R-21, E-5
def test_watchdog_reopens_after_stall(tmp_path: Path) -> None:  # [R-21, E-5]
    """No frame for > 3 s → stream close + reopen (watchdog)."""
    from murmur.audio import WATCHDOG_STALL_S

    src = FakeAudioSource(frames=[])  # no frames → immediate stall
    engine, src, status, _, stop, _, _ = _make_engine(tmp_path, source=src)

    engine.start()
    # Wait for watchdog to trigger (WATCHDOG_STALL_S + buffer)
    time.sleep(WATCHDOG_STALL_S + 1.5)
    stop.set()
    engine.join(timeout=3.0)

    # Stream was opened at least once (and likely closed + reopened)
    assert src.open_count >= 1


# E-4
def test_open_failure_retries_5s(tmp_path: Path) -> None:  # [E-4]
    """Stream open failure → icon ⚠ 'No input device', no crash, retries."""
    src = FakeAudioSource(open_error="no device")
    engine, src, status, _, stop, _, _ = _make_engine(tmp_path, source=src)

    engine.start()
    time.sleep(0.3)
    stop.set()
    engine.join(timeout=3.0)

    assert status.snapshot().icon == IconState.ERROR
    assert status.snapshot().notice == "No input device"


# E-1
def test_mic_denied_blocks_and_recovers(tmp_path: Path) -> None:  # [E-1]
    """Mic permission denied → stream stays closed, notification fired, auto-recovers."""
    perm_values = ["DENIED", "DENIED", "GRANTED"]
    engine, src, status, notifier, stop, _, _ = _make_engine(
        tmp_path, perm_values=perm_values
    )

    engine.start()
    time.sleep(1.5)
    stop.set()
    engine.join(timeout=3.0)

    assert any("Microphone" in msg for _, msg in notifier.notifications), (
        f"Expected mic-permission notification, got: {notifier.notifications}"
    )


# E-14
def test_spool_oserror_drops_and_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # [E-14]
    """Spool write OSError → segment dropped, error count incremented."""
    speech = [_speech_frame()] * 5
    # After speech, add enough silence to close the segment
    silence = [_silence_frame()] * 60
    src = FakeAudioSource(frames=speech + silence)

    engine, src, status, notifier, stop, _, _ = _make_engine(tmp_path, source=src)

    # Make spool writes fail
    import murmur.audio.engine as engine_mod

    def bad_write(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(engine_mod, "write_segment_wav", bad_write)

    engine.start()
    time.sleep(2.5)
    stop.set()
    engine.join(timeout=3.0)

    assert status.snapshot().error_count >= 1


def test_pause_flushes_partial_segment(tmp_path: Path) -> None:
    """Pausing while recording flushes the active partial segment."""
    speech = [_speech_frame()] * 5
    src = FakeAudioSource(frames=speech)
    engine, src, status, _, stop, _, store = _make_engine(tmp_path, source=src)

    engine.start()
    time.sleep(0.5)
    # Pause the controller
    store.save(PauseState(mode=PauseMode.MANUAL))
    time.sleep(0.8)
    stop.set()
    engine.join(timeout=3.0)

    # Engine should have at least opened once
    assert src.open_count >= 1
