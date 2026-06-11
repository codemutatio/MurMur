"""Shared pytest fixtures for murmur test suite."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fakes import (
    AmplitudeVAD,
    FakeAudioSource,
    FakeDiskFree,
    FakePermission,
    FakeTranscriber,
    FixedClock,
    RecordingNotifier,
)


@pytest.fixture()
def tmp_home(tmp_path: Path) -> Path:
    """A temporary MURMUR_HOME directory."""
    home = tmp_path / "murmur_home"
    home.mkdir()
    return home


@pytest.fixture()
def tmp_transcripts(tmp_path: Path) -> Path:
    """A temporary transcripts directory."""
    d = tmp_path / "Transcripts"
    d.mkdir()
    return d


@pytest.fixture()
def tmp_spool(tmp_transcripts: Path) -> Path:
    """A temporary spool directory inside the transcripts dir."""
    s = tmp_transcripts / "spool"
    s.mkdir()
    return s


@pytest.fixture()
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture()
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture()
def fake_source() -> FakeAudioSource:
    return FakeAudioSource()


@pytest.fixture()
def amplitude_vad() -> AmplitudeVAD:
    return AmplitudeVAD()


@pytest.fixture()
def fake_transcriber() -> FakeTranscriber:
    return FakeTranscriber()
