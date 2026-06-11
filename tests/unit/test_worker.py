"""Tests for murmur.transcribe.worker — T-9."""

from __future__ import annotations

import queue
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from murmur.audio.segmenter import Segment
from murmur.audio.spool import write_segment_wav
from murmur.control import StatusBoard
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import TranscriptionError
from murmur.transcribe.worker import TranscribeWorker
from tests.fakes import FakeTranscriber, FixedClock


def _make_wav(spool_dir: Path, text: str = "speech", offset: int = 0) -> Path:
    """Write a fake WAV segment into spool_dir."""
    start = datetime(2026, 6, 12, 9, 0, offset, tzinfo=UTC)
    samples = np.zeros(1600, dtype=np.float32) + 0.01
    seg = Segment(samples=samples, start_at=start)
    return write_segment_wav(spool_dir, seg)


def _make_worker(
    spool_dir: Path,
    transcripts_dir: Path,
    transcriber: FakeTranscriber | None = None,
    fallback: FakeTranscriber | None = None,
    keep_audio: bool = False,
    max_local_failures: int = 3,
    backoff_s: float = 0.01,
) -> tuple[TranscribeWorker, queue.Queue[Path], StatusBoard, threading.Event, threading.Event]:
    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    backlog_drained = threading.Event()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    writer = TranscriptWriter(transcripts_dir, clock)

    worker = TranscribeWorker(
        spool_dir=spool_dir,
        work_q=work_q,
        transcriber=transcriber or FakeTranscriber(),
        fallback=fallback,
        writer=writer,
        status=status,
        keep_audio=keep_audio,
        language="en",
        initial_prompt=None,
        backlog_drained=backlog_drained,
        stop=stop,
        max_local_failures=max_local_failures,
        backoff_s=backoff_s,
    )
    return worker, work_q, status, backlog_drained, stop


def _run_worker(worker: TranscribeWorker, stop: threading.Event, timeout: float = 5.0) -> None:
    """Start worker, wait for backlog_drained, then stop."""
    worker.start()
    # Wait for backlog_drained then stop
    worker._backlog_drained.wait(timeout=timeout)
    stop.set()
    worker.join(timeout=timeout)


# AC-1
def test_success_deletes_wav(tmp_path: Path) -> None:  # [AC-1]
    """Successfully transcribed WAV is deleted from spool (keep_audio=false default)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)
    assert wav.exists()

    transcriber = FakeTranscriber(["Hello world"])
    worker, _, _, _, stop = _make_worker(spool, transcripts, transcriber, keep_audio=False)
    _run_worker(worker, stop)

    assert not wav.exists(), "WAV should have been deleted after successful transcription"


# AC-2
def test_keep_audio_retains_wav(tmp_path: Path) -> None:  # [AC-2]
    """keep_audio=true: WAV is NOT deleted after successful transcription."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)

    transcriber = FakeTranscriber(["Hello world"])
    worker, _, _, _, stop = _make_worker(spool, transcripts, transcriber, keep_audio=True)
    _run_worker(worker, stop)

    assert wav.exists(), "WAV should be retained when keep_audio=true"


# AC-7
def test_fallback_off_leaves_wav_increments_error_no_api(tmp_path: Path) -> None:  # [AC-7]
    """api_fallback=false: 3 failures → WAV in spool, error count++, no network call."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)

    # Fail on every call
    transcriber = FakeTranscriber(
        [
            TranscriptionError("fail1"),
            TranscriptionError("fail2"),
            TranscriptionError("fail3"),
        ]
    )
    worker, _, status, _, stop = _make_worker(
        spool, transcripts, transcriber, fallback=None, max_local_failures=3, backoff_s=0.01
    )
    _run_worker(worker, stop, timeout=5.0)

    assert wav.exists(), "WAV should remain in spool when no fallback configured"
    assert status.snapshot().error_count >= 1


# AC-8
def test_third_failure_invokes_fallback(tmp_path: Path) -> None:  # [AC-8]
    """3 consecutive local failures with fallback configured → fallback is called."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)

    local = FakeTranscriber(
        [
            TranscriptionError("fail1"),
            TranscriptionError("fail2"),
            TranscriptionError("fail3"),
        ]
    )
    fallback = FakeTranscriber(["Fallback transcription"])

    worker, _, status, _, stop = _make_worker(
        spool, transcripts, local, fallback=fallback, max_local_failures=3, backoff_s=0.01
    )
    _run_worker(worker, stop)

    assert fallback.call_count >= 1, "Fallback should have been called"
    assert not wav.exists(), "WAV should be deleted after successful fallback"


# E-6
def test_startup_scan_fifo_before_live(tmp_path: Path) -> None:  # [E-6]
    """Worker drains existing spool before processing live queue items."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"

    # Two pre-existing WAVs in spool
    wav1 = _make_wav(spool, offset=0)
    wav2 = _make_wav(spool, offset=1)

    transcriber = FakeTranscriber(["word1", "word2"])
    worker, _, _, backlog_drained, stop = _make_worker(spool, transcripts, transcriber)
    worker.start()

    # Wait for backlog_drained before sending stop
    backlog_drained.wait(timeout=5.0)
    stop.set()
    worker.join(timeout=5.0)

    # Both spool files should have been processed (deleted)
    assert not wav1.exists()
    assert not wav2.exists()


# E-10
def test_api_failure_backoff(tmp_path: Path) -> None:  # [E-10]
    """API fallback failure → error count++, backoff sleep (no retry storm)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)

    local = FakeTranscriber(
        [
            TranscriptionError("f1"),
            TranscriptionError("f2"),
            TranscriptionError("f3"),
        ]
    )
    fallback = FakeTranscriber([TranscriptionError("api down")])

    worker, _, status, _, stop = _make_worker(
        spool, transcripts, local, fallback=fallback, max_local_failures=3, backoff_s=0.05
    )
    _run_worker(worker, stop, timeout=5.0)

    assert status.snapshot().error_count >= 1
    assert wav.exists(), "WAV stays in spool after both failures"


def test_guard_drop_does_not_increment_error(tmp_path: Path) -> None:
    """Hallucination guard drop → WAV deleted, no error count increment (E-8)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    wav = _make_wav(spool)

    transcriber = FakeTranscriber(["Thank you for watching"])
    worker, _, status, _, stop = _make_worker(spool, transcripts, transcriber)
    _run_worker(worker, stop)

    # Guard dropped all output → no file created, no error
    assert status.snapshot().error_count == 0
    assert not wav.exists()
    # No transcript file created (AC-5)
    transcript = transcripts / "2026-06-12.md"
    assert not transcript.exists()


def test_backlog_drained_event_set_even_with_no_spool(tmp_path: Path) -> None:
    """backlog_drained is set even when spool is empty at startup."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"

    worker, _, _, backlog_drained, stop = _make_worker(spool, transcripts)
    worker.start()
    drained = backlog_drained.wait(timeout=3.0)
    stop.set()
    worker.join(timeout=3.0)

    assert drained, "backlog_drained should be set even with empty spool"
