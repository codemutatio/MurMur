"""Tests for murmur.runtime.Runtime — T-16, AC-5, AC-16."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from murmur.config import CaptureConfig, Config, HotkeyConfig, NotesConfig, StorageConfig, SttConfig
from murmur.paths import AppPaths
from murmur.runtime import Runtime
from tests.fakes import (
    AmplitudeVAD,
    FakeAudioSource,
    FakeDiskFree,
    FakePermission,
    FakeTranscriber,
    FixedClock,
    RecordingNotifier,
)

FRAME_SAMPLES = 512
SAMPLE_RATE = 16000
DISK_PLENTY = 10 * 1024**3


def _make_paths(tmp_path: Path) -> AppPaths:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return AppPaths(
        home=home,
        config_file=home / "config.toml",
        state_file=home / "state.json",
        lock_file=home / "murmur.lock",
        log_file=home / "murmur.log",
    )


def _make_config(tmp_path: Path) -> Config:
    transcripts = tmp_path / "Transcripts"
    spool = tmp_path / "Transcripts" / "spool"
    return Config(
        capture=CaptureConfig(silence_split_s=1.5, max_segment_s=120.0),
        stt=SttConfig(api_fallback=False),
        storage=StorageConfig(
            transcripts_dir=transcripts,
            spool_dir=spool,
            keep_audio=False,
            retention_days=365,
        ),
        hotkey=HotkeyConfig(),
        notes=NotesConfig(),
    )


def test_runtime_starts_and_stops_headless(tmp_path: Path) -> None:
    """Runtime.start() and stop() complete without error with all fakes injected."""
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    notifier = RecordingNotifier()
    transcriber = FakeTranscriber()
    # Provide enough silence frames to let the engine run briefly
    frames = [np.zeros(FRAME_SAMPLES, dtype=np.float32)] * 20
    source = FakeAudioSource(frames=frames)
    vad = AmplitudeVAD(threshold=0.01)
    mic_perm = FakePermission(["GRANTED"])
    disk_probe = FakeDiskFree([DISK_PLENTY])

    config = _make_config(tmp_path)
    paths = _make_paths(tmp_path)

    runtime = Runtime(
        config,
        paths,
        source=source,
        vad=vad,
        transcriber=transcriber,
        notifier=notifier,
        now=clock,
        disk_free=disk_probe,
        mic_permission=mic_perm,
    )

    runtime.start()
    time.sleep(0.3)
    runtime.stop(timeout=5.0)


def test_runtime_status_board_accessible(tmp_path: Path) -> None:
    """Runtime exposes .status and .controller after construction."""
    config = _make_config(tmp_path)
    paths = _make_paths(tmp_path)
    clock = FixedClock()
    notifier = RecordingNotifier()

    runtime = Runtime(config, paths, notifier=notifier, now=clock)
    assert runtime.status is not None
    assert runtime.controller is not None


def test_runtime_respects_paused_state_on_startup(tmp_path: Path) -> None:
    """Runtime honors paused state loaded from state.json at startup."""
    import json

    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    paths = _make_paths(tmp_path)
    config = _make_config(tmp_path)

    # Write a rest_of_day pause that has not expired
    midnight = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "pause": {"mode": "rest_of_day", "until": midnight.isoformat()},
            }
        )
    )

    runtime = Runtime(config, paths, now=clock)
    assert runtime.controller.effective_paused(), "Controller should be paused at startup"


def test_runtime_prunes_retention_on_start(tmp_path: Path) -> None:
    """Runtime prunes old transcript files at startup based on retention_days."""
    from datetime import date, timedelta

    clock = FixedClock()
    notifier = RecordingNotifier()
    transcriber = FakeTranscriber()
    source = FakeAudioSource(frames=[])
    vad = AmplitudeVAD()
    mic_perm = FakePermission(["GRANTED"])
    disk_probe = FakeDiskFree([DISK_PLENTY])

    config = _make_config(tmp_path)
    paths = _make_paths(tmp_path)

    # Create an old transcript that should be pruned (8 days old, retention=7)
    old_date = date.today() - timedelta(days=8)
    old_transcript = config.storage.transcripts_dir / f"{old_date:%Y-%m-%d}.md"
    old_transcript.parent.mkdir(parents=True, exist_ok=True)
    old_transcript.write_text("# Old transcript\n")

    # Config with retention_days=7
    config2 = Config(
        capture=config.capture,
        stt=config.stt,
        storage=StorageConfig(
            transcripts_dir=config.storage.transcripts_dir,
            spool_dir=config.storage.spool_dir,
            keep_audio=False,
            retention_days=7,
        ),
        hotkey=config.hotkey,
        notes=config.notes,
    )

    runtime = Runtime(
        config2,
        paths,
        source=source,
        vad=vad,
        transcriber=transcriber,
        notifier=notifier,
        now=clock,
        disk_free=disk_probe,
        mic_permission=mic_perm,
    )
    runtime.start()
    time.sleep(0.2)
    runtime.stop(timeout=3.0)

    assert not old_transcript.exists(), "Old transcript should have been pruned at startup"


def test_runtime_stop_without_start_is_safe(tmp_path: Path) -> None:
    """Calling stop() before start() does not raise."""
    config = _make_config(tmp_path)
    paths = _make_paths(tmp_path)
    runtime = Runtime(config, paths)
    runtime.stop()  # Should not raise
