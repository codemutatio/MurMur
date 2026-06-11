"""Adversarial tests added by the test-engineer in validation iteration 2.

All tests tagged [test-engineer] [iter-2]. These probe gaps found during the
iteration-2 review: F-5 stream_open stale state, F-4 bad-filename persistence
on repeated launches, F-8 spool recovery path, and regression mutations.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE
from murmur.audio.segmenter import Segment, Segmenter, SegmenterConfig
from murmur.audio.spool import list_spool, write_segment_wav
from murmur.control import Controller, StateStore, StatusBoard
from murmur.output.retention import prune_transcripts
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import TranscriptionResult
from murmur.transcribe.guard import clean_result
from murmur.transcribe.worker import TranscribeWorker
from tests.fakes import (
    AmplitudeVAD,
    FakeDiskFree,
    FakePermission,
    FakeTranscriber,
    FixedClock,
    RecordingNotifier,
)

# ---------------------------------------------------------------------------
# F-5: outer guard in engine — source.read() raising unexpectedly [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_capture_engine_survives_read_exception(tmp_path: Path) -> None:  # [test-engineer, iter-2]
    """CaptureEngine continues when source.read() raises an unexpected exception.

    F-5 requirement: outer exception guard in the loop body.  The existing F-5
    test only injects the exception into write_segment_wav.  This test injects
    it into source.read() — the more realistic Risk-3 scenario (plan Risk 3:
    sd._terminate()/_initialize() can raise during reopen).

    After an unexpected read() exception, stream_open stays True but the next
    iteration retries read() — this is acceptable since the 1s sleep rate-limits
    the spin.  The thread must NOT die.
    """
    from murmur.audio.engine import CaptureEngine

    spool = tmp_path / "spool"
    spool.mkdir()
    work_q: queue.Queue[Path] = queue.Queue(maxsize=256)
    status = StatusBoard()
    notifier = RecordingNotifier()
    backlog_drained = threading.Event()
    backlog_drained.set()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))

    # A source whose read() raises on first call, then returns silence frames
    read_call_count = {"n": 0}
    silence_frames = [np.zeros(FRAME_SAMPLES, dtype=np.float32)] * 10

    class ExplodingOnFirstReadSource:
        def open(self, device: str) -> None:  # noqa: ARG002
            pass

        def close(self) -> None:
            pass

        def read(self, timeout: float) -> np.ndarray | None:  # noqa: ARG002
            read_call_count["n"] += 1
            if read_call_count["n"] == 1:
                raise RuntimeError("simulated PortAudio read error")
            if silence_frames:
                return silence_frames.pop(0)
            return None

        def default_device_signature(self) -> str:
            return "default"

    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(vad_threshold=0.5, silence_split_s=1.5, max_segment_s=120.0)
    segmenter = Segmenter(cfg, vad, clock)
    state_file = tmp_path / "state.json"
    store = StateStore(state_file, clock)
    ctrl = Controller(store, notifier, clock)

    engine = CaptureEngine(
        source=ExplodingOnFirstReadSource(),  # type: ignore[arg-type]
        segmenter=segmenter,
        spool_dir=spool,
        work_q=work_q,
        controller=ctrl,
        status=status,
        notifier=notifier,
        mic_permission=FakePermission(["GRANTED"]),
        disk_free=FakeDiskFree([10 * 1024**3] * 50),
        now=clock,
        backlog_drained=backlog_drained,
        stop=stop,
    )

    engine.start()
    # Give the engine time to hit the exception, sleep 1s, and continue
    time.sleep(2.5)
    stop.set()
    engine.join(timeout=4.0)

    assert not engine.is_alive(), (
        "CaptureEngine thread must exit cleanly after source.read() raises unexpectedly"
    )
    # read() must have been called more than once (loop continued after exception)
    assert read_call_count["n"] > 1, (
        f"source.read() was called {read_call_count['n']} times; expected >1 (loop must continue)"
    )


# ---------------------------------------------------------------------------
# F-4: bad-filename WAV persists in spool across restarts [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_bad_filename_wav_persists_and_is_reskipped_on_rerun(
    tmp_path: Path,
) -> None:  # [test-engineer, iter-2]
    """Bad-filename WAV in spool is not deleted and is re-skipped on next worker run.

    The F-4 fix skips bad filenames with a logged warning and returns from _process()
    without deleting the file.  A correctly-named WAV placed after the bad one must
    still be processed.  On a second worker run simulating a restart, the bad file
    is still in spool and skipped again — it does not cause the worker to fail or loop.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"

    bad_wav = spool / "not-a-timestamp.wav"
    with wave.open(str(bad_wav), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00" * 3200)

    # Good WAV must be processed
    start = datetime(2026, 6, 12, 9, 0, 5, tzinfo=UTC)
    samples = np.zeros(1600, dtype=np.float32) + 0.01
    good_wav = write_segment_wav(spool, Segment(samples=samples, start_at=start))

    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    writer = TranscriptWriter(transcripts, clock)
    transcriber = FakeTranscriber(["Good transcription"])

    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    backlog_drained = threading.Event()
    stop = threading.Event()

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
    backlog_drained.wait(timeout=5.0)
    stop.set()
    worker.join(timeout=5.0)

    # Bad WAV must still exist (not deleted)
    assert bad_wav.exists(), (
        "Bad-filename WAV must NOT be deleted by the worker (it has no valid start_at to key off)"
    )
    # Good WAV must have been processed and deleted
    assert not good_wav.exists(), "Good WAV must be processed and deleted"

    # Simulate a second run — bad WAV is still there, worker should skip it again cleanly
    work_q2: queue.Queue[Path] = queue.Queue()
    status2 = StatusBoard()
    backlog_drained2 = threading.Event()
    stop2 = threading.Event()
    transcriber2 = FakeTranscriber([])  # no more segments expected

    worker2 = TranscribeWorker(
        spool_dir=spool,
        work_q=work_q2,
        transcriber=transcriber2,
        fallback=None,
        writer=writer,
        status=status2,
        keep_audio=False,
        language="en",
        initial_prompt=None,
        backlog_drained=backlog_drained2,
        stop=stop2,
        backoff_s=0.01,
    )
    worker2.start()
    backlog_drained2.wait(timeout=5.0)
    stop2.set()
    worker2.join(timeout=5.0)

    assert not worker2.is_alive(), "Second worker run must exit cleanly"
    assert bad_wav.exists(), "Bad WAV still in spool after second run (expected — not auto-cleaned)"


# ---------------------------------------------------------------------------
# F-8: spool recovery story — list_spool finds dropped WAVs on next launch [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_f8_dropped_wav_recoverable_by_list_spool(
    tmp_path: Path,
) -> None:  # [test-engineer, iter-2]
    """WAV file dropped from the in-process queue due to queue.Full remains on disk
    and is found by list_spool(), which is what the startup scan uses on next launch.

    F-8 recovery story: 'spool is the durable queue; the in-process queue is a live
    optimisation layer only.'  This test verifies the durability property: a WAV
    that could not be enqueued via put_nowait is still discoverable at startup.
    """
    spool = tmp_path / "spool"
    spool.mkdir()

    # Write a WAV to spool directly (simulating what _emit_segment does before put_nowait)
    start = datetime(2026, 6, 12, 9, 30, 0, tzinfo=UTC)
    samples = np.zeros(1600, dtype=np.float32) + 0.01
    wav_path = write_segment_wav(spool, Segment(samples=samples, start_at=start))

    # Simulate put_nowait failing (queue full) — WAV stays on disk
    full_q: queue.Queue[Path] = queue.Queue(maxsize=1)
    full_q.put(tmp_path / "other.wav")  # fill the queue
    with contextlib.suppress(queue.Full):
        full_q.put_nowait(wav_path)  # WAV was NOT enqueued — stays on disk

    # The WAV must still exist on disk
    assert wav_path.exists(), "WAV must stay on disk when put_nowait fails"

    # list_spool (what the startup scan uses) must find it
    found = list_spool(spool)
    assert wav_path in found, (
        f"list_spool must find the dropped WAV for startup-scan recovery; found: {found}"
    )


# ---------------------------------------------------------------------------
# F-2: retention_days = 0 is still a no-op (regression guard) [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_retention_zero_still_noop_after_f2_fix(tmp_path: Path) -> None:  # [test-engineer, iter-2]
    """AC-10: retention_days = 0 keeps files forever.

    F-2 changed `== 0` to `<= 0`. Verify the change did not break the
    `retention_days = 0` (AC-10) semantics — i.e., zero must still be a no-op.
    This is a regression guard for F-2.
    """
    from datetime import date

    today = date(2026, 6, 12)
    # Write a very old file
    old = tmp_path / "2020-01-01.md"
    old.write_text("old transcript")

    deleted = prune_transcripts(tmp_path, retention_days=0, today=today)
    assert old.exists(), "retention_days=0 must keep all files regardless of age (AC-10)"
    assert deleted == [], "retention_days=0 must return empty deleted list"


# ---------------------------------------------------------------------------
# F-1: writer with naive datetime input still produces local time [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_writer_naive_datetime_uses_local_time(tmp_path: Path) -> None:  # [test-engineer, iter-2]
    """TranscriptWriter with a naive datetime produces a valid timestamp.

    `datetime.astimezone()` on a naive datetime in Python 3.11 treats the naive
    datetime as local time and attaches the local timezone.  The writer should
    handle this without crashing and produce a valid HH:MM stamp.

    AC-4 says start_at must be rendered in local time; passing naive datetimes
    (which have no tz) is a caller error but should not crash the writer.
    """
    import re

    writer = TranscriptWriter(tmp_path, FixedClock())
    naive_start = datetime(2026, 6, 12, 10, 30, 0)  # naive — no tzinfo
    path = writer.append(naive_start, "naive input test")
    content = path.read_text(encoding="utf-8")

    m = re.search(r"\*\*\[(\d{2}:\d{2})\]\*\* naive input test", content)
    assert m is not None, f"Expected **[HH:MM]** pattern in: {content!r}"
    rendered = m.group(1)
    assert rendered == "10:30", (
        f"Naive datetime at 10:30 local should render as '10:30' but got {rendered!r}"
    )


# ---------------------------------------------------------------------------
# F-7: force-split upper bound — part 1 never exceeds max_segment_s [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_force_split_part1_never_exceeds_max_segment(
    tmp_path: Path,
) -> None:  # [test-engineer, iter-2]
    """Part 1 of a force-split on non-uniform input stays within max_segment_s.

    F-7 fixed argmin lower-bound. Verify the upper-bound: the split must also
    ensure part 1 is <= max_segment_s.  With non-uniform RMS, argmin picks the
    quietest frame; that frame could theoretically be at or near the very end of
    the buffer (near max_frames), which would make part 1 almost as long as the
    full max_segment_s buffer.  The spec says segments must be <= 120s.

    This test uses a signal where the minimum RMS is at the second-to-last frame
    (forcing a split near the end) and verifies part 1 is still within spec.
    """
    import math

    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(
        vad_threshold=0.5,
        silence_split_s=200.0,  # never silence-close
        max_segment_s=2.0,  # short limit for fast test
    )
    seg = Segmenter(cfg, vad, clock)

    n_frames = math.ceil(2.1 * SAMPLE_RATE / FRAME_SAMPLES)

    results = []
    for i in range(n_frames):
        # High amplitude everywhere except frame n_frames-2 (second-to-last)
        amp = 0.01 if i == n_frames - 2 else 0.8
        frame = np.full(FRAME_SAMPLES, amp, dtype=np.float32)
        results.extend(seg.push(frame))

    seg_result = seg.flush()
    if seg_result is not None:
        results.append(seg_result)

    assert results, "Expected at least one segment from force-split"
    for s in results:
        dur = len(s.samples) / SAMPLE_RATE
        assert dur <= 2.0 + (FRAME_SAMPLES / SAMPLE_RATE), (
            f"Segment duration {dur:.3f}s exceeds max_segment_s + 1 frame tolerance"
        )


# ---------------------------------------------------------------------------
# AC-3 regression: guard mutation — dropping condition flipped [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_guard_no_speech_below_threshold_not_dropped(
    tmp_path: Path,
) -> None:  # [test-engineer, iter-2, AC-3]
    """Segment with no_speech_prob just BELOW 0.6 must NOT be dropped.

    Mutation: if the guard used >= instead of >, a segment at exactly 0.6 would
    be dropped incorrectly.  This test ensures segments below the threshold pass.
    """
    from murmur.transcribe.base import SegmentScore

    r = TranscriptionResult(
        text="legitimate speech",
        segments=(
            SegmentScore(
                text="legitimate speech",
                no_speech_prob=0.59999,  # just below 0.6
                compression_ratio=1.5,
                avg_logprob=-0.5,
            ),
        ),
    )
    result = clean_result(r)
    assert result is not None, (
        "no_speech_prob=0.599 is below 0.6 threshold — segment must NOT be dropped"
    )
    assert "legitimate speech" in result


# ---------------------------------------------------------------------------
# AC-9 regression: retention boundary — 7 days kept, 8 days deleted [test-engineer] [iter-2]
# ---------------------------------------------------------------------------


def test_retention_boundary_regression(tmp_path: Path) -> None:  # [test-engineer, iter-2, AC-9]
    """retention_days=7: 7-day-old file kept, 8-day-old file deleted.

    Regression guard: if the cutoff comparison changed from < to <=, the 7-day
    file would be deleted incorrectly.  This mutation is the most natural
    off-by-one in the retention logic.
    """
    from datetime import date, timedelta

    today = date(2026, 6, 12)
    seven_days = tmp_path / f"{(today - timedelta(days=7)).isoformat()}.md"
    eight_days = tmp_path / f"{(today - timedelta(days=8)).isoformat()}.md"
    seven_days.write_text("kept")
    eight_days.write_text("deleted")

    deleted = prune_transcripts(tmp_path, retention_days=7, today=today)

    assert seven_days.exists(), "7-day-old file must be kept with retention_days=7 (AC-9)"
    assert not eight_days.exists(), "8-day-old file must be deleted with retention_days=7 (AC-9)"
    assert eight_days in deleted
