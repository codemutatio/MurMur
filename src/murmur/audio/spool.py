"""Spool directory management: writing, reading, and listing WAV files.

Uses stdlib wave (16-bit PCM mono 16 kHz). NO ffmpeg.
Filenames encode the segment start time: YYYYMMDD-HHMMSS-mmm.wav
"""
from __future__ import annotations

import os
import tempfile
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from murmur.audio import SAMPLE_RATE
from murmur.audio.segmenter import Segment


def write_segment_wav(spool_dir: Path, seg: Segment) -> Path:
    """Write *seg* to a new WAV file in *spool_dir*.

    Uses tmp + fsync + os.replace so no partial WAV is ever visible (R-9).
    The filename encodes the segment start time for crash-recovery ordering.

    Returns:
        Path to the final WAV file.

    Raises:
        OSError: if the write fails (callers handle E-14).
    """
    spool_dir.mkdir(parents=True, exist_ok=True)

    start = seg.start_at
    millis = start.microsecond // 1000
    name = f"{start:%Y%m%d-%H%M%S}-{millis:03d}.wav"
    dest = spool_dir / name

    # Convert float32 [-1, 1] to int16
    pcm = np.clip(seg.samples, -1.0, 1.0)
    pcm_int16 = (pcm * 32767).astype(np.int16)

    # Write to a temp file in the same directory, then atomically replace.
    fd, tmp_path_str = tempfile.mkstemp(dir=spool_dir, suffix=".wav.tmp")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as raw:
            with wave.open(raw, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm_int16.tobytes())
            raw.flush()
            os.fsync(raw.fileno())
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_path, dest)
    return dest


def parse_segment_start(path: Path) -> datetime:
    """Parse the segment start datetime from a spool WAV filename.

    Filename format: YYYYMMDD-HHMMSS-mmm.wav

    Returns tz-aware local datetime (preserving timestamps across crash recovery).
    """
    stem = path.stem  # YYYYMMDD-HHMMSS-mmm
    date_part, time_part, ms_part = stem.split("-")
    year = int(date_part[:4])
    month = int(date_part[4:6])
    day = int(date_part[6:8])
    hour = int(time_part[:2])
    minute = int(time_part[2:4])
    second = int(time_part[4:6])
    millis = int(ms_part)

    # Build a naive local datetime then attach the local timezone
    naive = datetime(year, month, day, hour, minute, second, millis * 1000)
    # astimezone() attaches the system local timezone to a naive datetime
    local_aware = naive.astimezone()
    return local_aware


def list_spool(spool_dir: Path) -> list[Path]:
    """Return all WAV files in *spool_dir* sorted chronologically by filename."""
    if not spool_dir.exists():
        return []
    return sorted(spool_dir.glob("*.wav"))


def load_wav(path: Path) -> np.ndarray:
    """Load a WAV file as a float32 mono array.

    Uses stdlib wave — NO ffmpeg required on this machine.
    """
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2**31
    elif sampwidth == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Unsupported sample width: {sampwidth}")

    # Mix down to mono if necessary
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    return samples
