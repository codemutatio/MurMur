"""ffmpeg-based audio decoder for the Murmur server.

Converts uploaded audio (m4a, aac, wav, mp3, ogg, opus) to a float32 mono
16 kHz numpy array by shelling out to ffmpeg (ADR-3, AC-5).

The subprocess runner is injected so tests can mock it without ffmpeg installed
(R-14).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import numpy as np


class AudioDecodeError(Exception):
    """Raised when ffmpeg cannot decode the supplied audio file (AC-11, E-4, E-19)."""


def decode_to_pcm(
    path: Path,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> np.ndarray:
    """Decode *path* to a float32 mono 16 kHz numpy array via ffmpeg.

    ffmpeg is invoked as a subprocess with::

        ffmpeg -i <path> -ac 1 -ar 16000 -f f32le -

    The raw float32 PCM bytes are captured from stdout and converted to a numpy
    array with dtype float32 (AC-5).

    Args:
        path: Path to the input audio file.
        run:  Injectable subprocess runner (default: subprocess.run).

    Returns:
        float32 numpy array, mono, sample-rate 16000.

    Raises:
        AudioDecodeError: if ffmpeg is not installed (E-19), exits with a
            non-zero return code (AC-11, E-4), or times out.
    """
    cmd = [
        "ffmpeg",
        "-y",          # overwrite without asking
        "-i", str(path),
        "-ac", "1",    # mono
        "-ar", "16000",  # 16 kHz
        "-f", "f32le",  # raw float32 little-endian PCM
        "-",           # output to stdout
    ]

    try:
        result = run(
            cmd,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise AudioDecodeError(
            "ffmpeg binary not found. Install ffmpeg (e.g. `brew install ffmpeg`)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioDecodeError("ffmpeg timed out while decoding audio.") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace") if result.stderr else ""
        raise AudioDecodeError(f"ffmpeg exited with code {result.returncode}: {stderr[:200]}")

    raw_bytes: bytes = result.stdout
    if not raw_bytes:
        raise AudioDecodeError("ffmpeg produced no output (empty audio stream).")

    # Convert raw little-endian float32 bytes to numpy array
    audio = np.frombuffer(raw_bytes, dtype=np.float32)
    return audio
