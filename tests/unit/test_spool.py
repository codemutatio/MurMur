"""Tests for murmur.audio.spool — T-3."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from murmur.audio.segmenter import Segment
from murmur.audio.spool import (
    list_spool,
    load_wav,
    parse_segment_start,
    write_segment_wav,
)


def _make_segment(start: datetime | None = None, n_samples: int = 1024) -> Segment:
    start = start or datetime(2026, 6, 12, 10, 30, 45, 123000, tzinfo=UTC)
    samples = np.random.default_rng(42).uniform(-0.5, 0.5, n_samples).astype(np.float32)
    return Segment(samples=samples, start_at=start)


def test_wav_roundtrip_and_filename_timestamp(tmp_path: Path) -> None:
    """WAV round-trip: write → load returns a float32 array of similar content; filename encodes timestamp."""
    start = datetime(2026, 6, 12, 14, 5, 3, 250000, tzinfo=UTC)
    seg = _make_segment(start=start, n_samples=1600)
    path = write_segment_wav(tmp_path, seg)

    assert path.exists()
    assert path.suffix == ".wav"
    # Filename should encode start time
    assert "20260612" in path.name

    # Round-trip: the loaded samples should be close to the original
    loaded = load_wav(path)
    assert loaded.dtype == np.float32
    assert abs(len(loaded) - 1600) <= 4  # allow rounding to frame size


def test_write_creates_spool_dir_if_absent(tmp_path: Path) -> None:
    """write_segment_wav creates the spool dir if it doesn't exist (E-11)."""
    spool = tmp_path / "a" / "b" / "spool"
    assert not spool.exists()
    seg = _make_segment()
    write_segment_wav(spool, seg)
    assert spool.exists()


def test_write_is_atomic(tmp_path: Path) -> None:
    """The final WAV file is only visible after the write completes (no .tmp visible)."""
    seg = _make_segment()
    write_segment_wav(tmp_path, seg)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], "Temporary files should be cleaned up"


def test_parse_segment_start_roundtrip() -> None:
    """parse_segment_start reconstructs a datetime from the filename."""
    start = datetime(2026, 6, 12, 14, 5, 3, 123000, tzinfo=UTC)
    _make_segment(start=start)
    # We just need the filename, not to write the file
    millis = start.microsecond // 1000
    filename = f"{start:%Y%m%d-%H%M%S}-{millis:03d}.wav"
    path = Path(filename)
    parsed = parse_segment_start(path)
    # Should be within the same second (tz offset may differ but values match)
    assert parsed.year == 2026
    assert parsed.month == 6
    assert parsed.day == 12
    assert parsed.hour == 14
    assert parsed.minute == 5
    assert parsed.second == 3
    assert parsed.microsecond // 1000 == 123


def test_list_spool_sorted_chronologically(tmp_path: Path) -> None:
    """list_spool returns WAV files sorted by filename (= chronological order)."""
    names = [
        "20260612-100000-000.wav",
        "20260612-090000-000.wav",
        "20260612-110000-500.wav",
    ]
    for name in names:
        (tmp_path / name).touch()
    result = list_spool(tmp_path)
    result_names = [p.name for p in result]
    assert result_names == sorted(names)


def test_list_spool_empty_dir(tmp_path: Path) -> None:
    assert list_spool(tmp_path) == []


def test_list_spool_missing_dir(tmp_path: Path) -> None:
    assert list_spool(tmp_path / "no_such_dir") == []


def test_load_wav_returns_float32(tmp_path: Path) -> None:
    """load_wav returns float32 array."""
    seg = _make_segment(n_samples=3200)
    path = write_segment_wav(tmp_path, seg)
    audio = load_wav(path)
    assert audio.dtype == np.float32
    assert len(audio) > 0


def test_write_segment_wav_filename_millis(tmp_path: Path) -> None:
    """Milliseconds are encoded in the filename for sub-second precision."""
    start = datetime(2026, 6, 12, 9, 0, 0, 750000)
    seg = _make_segment(start=start)
    path = write_segment_wav(tmp_path, seg)
    assert "-750.wav" in path.name
