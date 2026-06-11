"""End-to-end pipeline integration tests — T-16, AC-4, AC-5, AC-16, R-16."""

from __future__ import annotations

import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from murmur.audio import FRAME_SAMPLES, POST_ROLL_S, PRE_ROLL_S, SAMPLE_RATE
from murmur.audio.engine import CaptureEngine
from murmur.audio.segmenter import Segment, Segmenter, SegmenterConfig
from murmur.audio.spool import write_segment_wav
from murmur.control import Controller, StateStore, StatusBoard
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.worker import TranscribeWorker
from tests.fakes import (
    AmplitudeVAD,
    FakeAudioSource,
    FakeDiskFree,
    FakePermission,
    FakeTranscriber,
    FixedClock,
    RecordingNotifier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DISK_PLENTY = 10 * 1024**3  # 10 GiB


def _speech_frame() -> np.ndarray:
    """Return a frame of high-amplitude audio (triggers AmplitudeVAD)."""
    return np.ones(FRAME_SAMPLES, dtype=np.float32) * 0.5


def _silence_frame() -> np.ndarray:
    """Return a frame of near-digital silence."""
    return np.zeros(FRAME_SAMPLES, dtype=np.float32)


def _make_runtime_components(
    tmp_path: Path,
    *,
    frames: list[np.ndarray | None],
    transcription: str = "hello world",
    clock_dt: datetime | None = None,
) -> tuple[
    FakeAudioSource,
    Segmenter,
    TranscribeWorker,
    TranscriptWriter,
    StatusBoard,
    Controller,
    threading.Event,  # stop
    threading.Event,  # backlog_drained
    queue.Queue[Path],
]:
    spool = tmp_path / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    transcripts = tmp_path / "Transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)

    clock = FixedClock(clock_dt or datetime(2026, 6, 12, 9, 30, 0, tzinfo=UTC))
    notifier = RecordingNotifier()
    status = StatusBoard()
    store = StateStore(tmp_path / "state.json", clock)
    controller = Controller(store, notifier, clock)
    work_q: queue.Queue[Path] = queue.Queue()
    stop = threading.Event()
    backlog_drained = threading.Event()

    seg_cfg = SegmenterConfig(
        vad_threshold=0.5,
        silence_split_s=1.5,
        max_segment_s=120.0,
        pre_roll_s=PRE_ROLL_S,
        post_roll_s=POST_ROLL_S,
    )
    vad = AmplitudeVAD(threshold=0.01)
    segmenter = Segmenter(seg_cfg, vad, clock)

    writer = TranscriptWriter(transcripts, clock)
    transcriber = FakeTranscriber([transcription])

    worker = TranscribeWorker(
        spool_dir=spool,
        work_q=work_q,
        transcriber=transcriber,
        fallback=None,
        writer=writer,
        status=status,
        keep_audio=False,
        language="en",
        initial_prompt=None,
        backlog_drained=backlog_drained,
        stop=stop,
        backoff_s=0.01,
    )

    source = FakeAudioSource(frames=frames)

    return source, segmenter, worker, writer, status, controller, stop, backlog_drained, work_q


def _run_pipeline(
    source: FakeAudioSource,
    segmenter: Segmenter,
    worker: TranscribeWorker,
    status: StatusBoard,
    controller: Controller,
    stop: threading.Event,
    backlog_drained: threading.Event,
    work_q: queue.Queue[Path],
    tmp_path: Path,
    *,
    timeout: float = 5.0,
) -> None:
    """Start worker + capture engine, wait for transcription, then stop."""
    spool = tmp_path / "spool"
    notifier = RecordingNotifier()
    clock = FixedClock()
    mic_perm = FakePermission(["GRANTED"])
    disk_probe = FakeDiskFree([DISK_PLENTY])

    engine = CaptureEngine(
        source=source,
        segmenter=segmenter,
        spool_dir=spool,
        work_q=work_q,
        controller=controller,
        status=status,
        notifier=notifier,
        mic_permission=mic_perm,
        disk_free=disk_probe,
        now=clock,
        backlog_drained=backlog_drained,
        stop=stop,
        device="default",
    )

    worker.start()
    backlog_drained.wait(timeout=timeout)
    engine.start()

    # Give the pipeline time to process
    time.sleep(0.5)
    stop.set()
    engine.join(timeout=timeout)
    worker.join(timeout=timeout)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_speech_to_transcript_end_to_end(tmp_path: Path) -> None:  # [AC-4, AC-1]
    """Speech frames → VAD → spool → worker → transcript line in correct format."""
    # 3 s of speech followed by 2 s of silence (enough to close the segment)
    speech_count = int(3.0 * SAMPLE_RATE / FRAME_SAMPLES)
    silence_count = int(2.0 * SAMPLE_RATE / FRAME_SAMPLES)
    frames: list[np.ndarray | None] = [_speech_frame()] * speech_count + [
        _silence_frame()
    ] * silence_count

    (source, segmenter, worker, writer, status, controller, stop, backlog_drained, work_q) = (
        _make_runtime_components(tmp_path, frames=frames, transcription="hello world")
    )

    _run_pipeline(
        source,
        segmenter,
        worker,
        status,
        controller,
        stop,
        backlog_drained,
        work_q,
        tmp_path,
    )

    # Check transcript was created
    # writer uses clock = 2026-06-12
    transcript = tmp_path / "Transcripts" / "2026-06-12.md"
    assert transcript.exists(), "Transcript file should have been created"
    content = transcript.read_text(encoding="utf-8")
    assert "hello world" in content, f"Expected transcript text in {content!r}"
    # AC-4: format check
    import re

    assert re.search(r"\*\*\[\d{2}:\d{2}\]\*\*", content), (
        f"Expected **[HH:MM]** format in {content!r}"
    )


def test_silent_day_no_file(tmp_path: Path) -> None:  # [AC-5]
    """All-silence stream produces no transcript file."""
    frames: list[np.ndarray | None] = [_silence_frame()] * 200

    (source, segmenter, worker, writer, status, controller, stop, backlog_drained, work_q) = (
        _make_runtime_components(tmp_path, frames=frames)
    )

    _run_pipeline(
        source,
        segmenter,
        worker,
        status,
        controller,
        stop,
        backlog_drained,
        work_q,
        tmp_path,
    )

    transcript = tmp_path / "Transcripts" / "2026-06-12.md"
    assert not transcript.exists(), "No transcript file should exist for a silent day"


def test_starts_paused_from_state(tmp_path: Path) -> None:  # [AC-16]
    """App starts paused when state.json contains a REST_OF_DAY pause that is still active."""
    import json

    clock = FixedClock(datetime(2026, 6, 12, 9, 30, 0, tzinfo=UTC))
    # Set until = midnight of 2026-06-13 (well in the future from our clock)
    local_midnight = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)

    state_path = tmp_path / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "pause": {
                    "mode": "rest_of_day",
                    "until": local_midnight.isoformat(),
                },
            }
        )
    )

    store = StateStore(state_path, clock)
    notifier = RecordingNotifier()
    controller = Controller(store, notifier, clock)

    # The controller should report as paused without any explicit pause call
    assert controller.effective_paused(), (
        "App should start in paused state when state.json says rest_of_day"
    )


def test_backlog_drained_before_capture(tmp_path: Path) -> None:  # [R-16]
    """Worker sets backlog_drained before capture engine opens the stream."""
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"
    transcripts.mkdir()

    clock = FixedClock()
    work_q: queue.Queue[Path] = queue.Queue()
    backlog_drained = threading.Event()
    stop = threading.Event()
    status = StatusBoard()
    writer = TranscriptWriter(transcripts, clock)

    # Write one pre-existing spool file
    wav_path = write_segment_wav(
        spool,
        Segment(
            samples=np.zeros(1600, dtype=np.float32),
            start_at=datetime(2026, 6, 12, 8, 0, 0, tzinfo=UTC),
        ),
    )
    assert wav_path.exists()

    transcriber = FakeTranscriber(["pre-existing"])
    worker = TranscribeWorker(
        spool_dir=spool,
        work_q=work_q,
        transcriber=transcriber,
        fallback=None,
        writer=writer,
        status=status,
        keep_audio=False,
        language="en",
        initial_prompt=None,
        backlog_drained=backlog_drained,
        stop=stop,
        backoff_s=0.01,
    )

    worker.start()
    drained = backlog_drained.wait(timeout=5.0)
    stop.set()
    worker.join(timeout=5.0)

    assert drained, "backlog_drained event should be set after processing pre-existing WAVs"
    # Pre-existing WAV should have been transcribed and deleted
    assert not wav_path.exists(), "Pre-existing WAV should have been consumed by worker"
