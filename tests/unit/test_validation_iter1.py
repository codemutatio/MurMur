"""Adversarial tests added by the test-engineer in validation iteration 1.

All tests tagged [test-engineer]. These probe gaps not covered by the
existing suite.
"""

from __future__ import annotations

import math
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from murmur.audio import DISK_MIN_FREE_BYTES, FRAME_SAMPLES, SAMPLE_RATE
from murmur.audio.segmenter import Segmenter, SegmenterConfig
from murmur.control import (
    Controller,
    IconState,
    PauseMode,
    PauseState,
    StateStore,
    StatusBoard,
    is_pause_active,
)
from murmur.output.retention import prune_transcripts
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import SegmentScore, TranscriptionError, TranscriptionResult
from murmur.transcribe.guard import clean_result
from tests.fakes import AmplitudeVAD, FixedClock, RecordingNotifier

# ---------------------------------------------------------------------------
# AC-19 completeness: icon AND resume notification [test-engineer]
# ---------------------------------------------------------------------------


def test_disk_low_icon_changes_to_error(tmp_path: Path) -> None:  # [test-engineer, AC-19]
    """When disk is low, capture engine sets icon to ERROR (⚠) — not just notification.

    The existing test_disk_low_pauses_then_resumes_with_notifications only checks the
    notification payload. This test verifies the spec's 'menubar state changes to ⚠'
    requirement in AC-19.
    """
    import queue

    from murmur.audio.engine import CaptureEngine
    from tests.fakes import FakeAudioSource, FakeDiskFree, FakePermission

    spool = tmp_path / "spool"
    spool.mkdir()
    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    notifier = RecordingNotifier()
    backlog_drained = threading.Event()
    backlog_drained.set()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))

    src = FakeAudioSource()
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(vad_threshold=0.5, silence_split_s=1.5, max_segment_s=120.0)
    segmenter = Segmenter(cfg, vad, clock)

    state_file = tmp_path / "state.json"
    store = StateStore(state_file, clock)
    ctrl = Controller(store, notifier, clock)

    # Always below threshold
    disk_values = [DISK_MIN_FREE_BYTES - 1] * 50

    engine = CaptureEngine(
        source=src,
        segmenter=segmenter,
        spool_dir=spool,
        work_q=work_q,
        controller=ctrl,
        status=status,
        notifier=notifier,
        mic_permission=FakePermission(["GRANTED"]),
        disk_free=FakeDiskFree(disk_values),
        now=clock,
        backlog_drained=backlog_drained,
        stop=stop,
    )

    engine.start()
    time.sleep(0.4)
    stop.set()
    engine.join(timeout=3.0)

    assert status.snapshot().icon == IconState.ERROR, (
        "Icon must be ERROR (⚠) when disk is below threshold — AC-19 requires 'menubar state changes to ⚠'"
    )


def test_disk_recovery_resume_notification(tmp_path: Path) -> None:  # [test-engineer, AC-19]
    """When disk space recovers, a resume notification is fired — AC-19 requires this.

    This probes the recovery branch of the disk gate: 'capture resumes when free space
    is restored above 2 GB' (spec) — specifically the notification for that event.
    """
    import queue

    from murmur.audio.engine import CaptureEngine
    from tests.fakes import FakeAudioSource, FakeDiskFree, FakePermission

    spool = tmp_path / "spool"
    spool.mkdir()
    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    notifier = RecordingNotifier()
    backlog_drained = threading.Event()
    backlog_drained.set()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))

    src = FakeAudioSource()
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(vad_threshold=0.5, silence_split_s=1.5, max_segment_s=120.0)
    segmenter = Segmenter(cfg, vad, clock)

    state_file = tmp_path / "state.json"
    store = StateStore(state_file, clock)
    ctrl = Controller(store, notifier, clock)

    # First check: low; second: recovered; thereafter: fine
    disk_values = [DISK_MIN_FREE_BYTES - 1, DISK_MIN_FREE_BYTES + 1] + [10 * 1024**3] * 20

    engine = CaptureEngine(
        source=src,
        segmenter=segmenter,
        spool_dir=spool,
        work_q=work_q,
        controller=ctrl,
        status=status,
        notifier=notifier,
        mic_permission=FakePermission(["GRANTED"]),
        disk_free=FakeDiskFree(disk_values),
        now=clock,
        backlog_drained=backlog_drained,
        stop=stop,
    )

    engine.start()
    time.sleep(0.8)
    stop.set()
    engine.join(timeout=3.0)

    # Must have a disk-low notification AND a recovery notification
    titles_msgs = notifier.notifications
    has_low = any("Disk space low" in msg for _, msg in titles_msgs)
    has_recovery = any("recovered" in msg.lower() for _, msg in titles_msgs)
    assert has_low, "Expected disk-low notification"
    assert has_recovery, (
        "Expected disk-recovery notification when space exceeds threshold — "
        "spec says 'capture resumes automatically when space recovers above threshold'"
    )


# ---------------------------------------------------------------------------
# AC-4: writer uses LOCAL time, not UTC [test-engineer]
# ---------------------------------------------------------------------------


def test_writer_local_time_not_utc(tmp_path: Path) -> None:  # [test-engineer, AC-4]
    """TranscriptWriter uses local time for the HH:MM stamp, not UTC.

    The existing test_line_format_local_time only checks the regex pattern
    \\d{2}:\\d{2} — it does NOT verify local vs UTC. This test forces a UTC
    datetime at a known UTC hour and verifies the written hour is NOT the UTC
    hour when the local timezone differs.

    AC-4 spec text: 'HH:MM is the local-time hour and minute of the segment start'.
    The start_at passed to append() must be rendered in local time.
    """
    # Use a fixed start_at that is UTC midnight (00:00) but will be non-zero in
    # any non-UTC timezone. We test the formatting code, not the system timezone.
    # The writer's format string is: f"**[{start_at:%H:%M}]** {text}"
    # With a timezone-aware datetime, %H:%M renders the datetime's own tzinfo.
    # If start_at is UTC, this renders as UTC time (may differ from local).
    # The spec says the stamp must be local time — so the writer should convert.

    writer = TranscriptWriter(tmp_path, FixedClock())
    # Deliberately pass UTC midnight (00:00 UTC)
    utc_midnight = datetime(2026, 6, 12, 0, 0, 0, tzinfo=UTC)
    path = writer.append(utc_midnight, "test utterance")
    content = path.read_text(encoding="utf-8")

    # If the writer uses start_at directly without local conversion, it will
    # render UTC hour. The spec requires local hour.
    # We can detect the pattern but verify the hour encoded matches the
    # LOCAL representation of utc_midnight, not UTC 00:00.
    import re

    m = re.search(r"\*\*\[(\d{2}:\d{2})\]\*\* test utterance", content)
    assert m is not None, f"Expected **[HH:MM]** pattern in {content!r}"
    rendered_time = m.group(1)

    # The rendered time should equal utc_midnight converted to local time
    local_time = utc_midnight.astimezone()
    expected_local = f"{local_time:%H:%M}"
    assert rendered_time == expected_local, (
        f"Writer rendered {rendered_time!r} but local-time of {utc_midnight} is {expected_local!r}. "
        "AC-4 requires local-time stamp, not UTC."
    )


# ---------------------------------------------------------------------------
# AC-13 boundary: exactly 120 s (not 121 s) [test-engineer]
# ---------------------------------------------------------------------------


def test_segment_exactly_120s_triggers_split(tmp_path: Path) -> None:  # [test-engineer, AC-13]
    """A segment accumulating exactly 120 s of frames must trigger the force-split.

    The existing test uses 121 s. The spec says 'max_segment_s = 120' and the
    engine must split when the buffer reaches 120 s. Mutate: if the condition
    were len >= max_frames + 1 instead of >= max_frames, this test would fail.
    """
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(
        vad_threshold=0.5,
        silence_split_s=200.0,  # never silence-close
        max_segment_s=120.0,
    )
    seg = Segmenter(cfg, vad, clock)

    # Exactly 120 s of speech (no silence)
    n_frames = math.ceil(120.0 * SAMPLE_RATE / FRAME_SAMPLES)
    results = []
    for i in range(n_frames):
        amp = 0.5 + 0.1 * math.sin(i * 0.1)
        frame = np.full(FRAME_SAMPLES, amp, dtype=np.float32)
        results.extend(seg.push(frame))

    # The split should have fired at or before the 120-s mark
    assert len(results) >= 1, (
        "Expected at least one segment emitted by force-split at 120 s boundary; got none."
    )
    for s in results:
        dur = len(s.samples) / SAMPLE_RATE
        assert dur <= 120.5, f"Segment duration {dur:.2f} s exceeds 120 s spec limit"


# ---------------------------------------------------------------------------
# is_pause_active: exact midnight boundary [test-engineer]
# ---------------------------------------------------------------------------


def test_pause_active_exact_midnight_is_expired(tmp_path: Path) -> None:  # [test-engineer, AC-17]
    """When now == until (exactly midnight), is_pause_active returns False.

    Spec says a relaunch 'after midnight' resumes recording. The implementation
    uses now < until, so at the exact moment of until, the pause is expired.
    This tests the fence-post: 00:00:00 on the next day is NOT paused.
    """
    until = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)
    pause = PauseState(mode=PauseMode.REST_OF_DAY, until=until)

    # Exactly at midnight: now == until
    assert not is_pause_active(pause, until), (
        "At exactly midnight (now == until), the REST_OF_DAY pause should be expired"
    )


def test_pause_active_one_second_before_midnight_still_active() -> None:  # [test-engineer, AC-16]
    """One second before midnight the pause is still active."""
    until = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)
    pause = PauseState(mode=PauseMode.REST_OF_DAY, until=until)
    one_before = until - timedelta(seconds=1)

    assert is_pause_active(pause, one_before), (
        "One second before midnight the REST_OF_DAY pause should still be active"
    )


# ---------------------------------------------------------------------------
# Worker: AC-8 — fallback called on THIRD failure, not first or second [test-engineer]
# ---------------------------------------------------------------------------


def test_fallback_not_called_before_third_failure(tmp_path: Path) -> None:  # [test-engineer, AC-8]
    """Fallback must be called on the THIRD consecutive failure, not the 1st or 2nd.

    The existing test_third_failure_invokes_fallback only checks that fallback
    call_count >= 1 after the worker runs. It doesn't verify the failure threshold.
    This test mutates the threshold: with max_local_failures=3, the fallback should
    not be invoked after 2 failures.
    """
    import queue

    from murmur.audio.segmenter import Segment
    from murmur.audio.spool import write_segment_wav
    from murmur.transcribe.worker import TranscribeWorker

    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"

    # Write a WAV to spool
    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    samples = np.zeros(1600, dtype=np.float32) + 0.01
    segment = Segment(samples=samples, start_at=start)
    wav = write_segment_wav(spool, segment)

    # Local fails exactly twice, then succeeds on third attempt (fallback should NOT be called)
    from tests.fakes import FakeTranscriber

    local = FakeTranscriber(
        [
            TranscriptionError("fail1"),
            TranscriptionError("fail2"),
            "Success on third local try",
        ]
    )
    fallback = FakeTranscriber(["Fallback result"])

    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    backlog_drained = threading.Event()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    writer = TranscriptWriter(transcripts, clock)

    worker = TranscribeWorker(
        spool_dir=spool,
        work_q=work_q,
        transcriber=local,
        fallback=fallback,
        writer=writer,
        status=status,
        keep_audio=False,
        language="en",
        initial_prompt=None,
        backlog_drained=backlog_drained,
        stop=stop,
        max_local_failures=3,
        backoff_s=0.01,
    )

    worker.start()
    backlog_drained.wait(timeout=5.0)
    stop.set()
    worker.join(timeout=5.0)

    assert fallback.call_count == 0, (
        f"Fallback should NOT be called when local succeeds on the 3rd attempt "
        f"(max_local_failures=3). Got {fallback.call_count} fallback calls."
    )
    assert not wav.exists(), "WAV should be deleted after successful local transcription"


# ---------------------------------------------------------------------------
# Worker: empty transcription text treated as no-speech [test-engineer, E-7]
# ---------------------------------------------------------------------------


def test_empty_transcription_no_file_no_error(tmp_path: Path) -> None:  # [test-engineer, E-7]
    """Empty transcription text → no transcript append, no error increment, WAV deleted.

    The guard returns None for empty text. The worker should silently delete the WAV
    (keep_audio=False) and not increment errors. This is a distinct path from a
    transcription FAILURE (which does increment errors).
    """
    import queue

    from murmur.audio.segmenter import Segment
    from murmur.audio.spool import write_segment_wav
    from murmur.transcribe.worker import TranscribeWorker

    spool = tmp_path / "spool"
    spool.mkdir()
    transcripts = tmp_path / "Transcripts"

    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    samples = np.zeros(1600, dtype=np.float32) + 0.01
    wav = write_segment_wav(spool, Segment(samples=samples, start_at=start))

    # Transcriber returns empty string
    from tests.fakes import FakeTranscriber

    transcriber = FakeTranscriber([""])

    work_q: queue.Queue[Path] = queue.Queue()
    status = StatusBoard()
    backlog_drained = threading.Event()
    stop = threading.Event()
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    writer = TranscriptWriter(transcripts, clock)

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

    assert status.snapshot().error_count == 0, (
        "Empty transcription (guard drop) must NOT increment error count"
    )
    assert not wav.exists(), (
        "WAV must be deleted even when guard drops the result (keep_audio=False)"
    )
    transcript = transcripts / "2026-06-12.md"
    assert not transcript.exists(), "No transcript file should be created for empty transcription"


# ---------------------------------------------------------------------------
# Guard: FILLER_PHRASES edge — period-only text [test-engineer]
# ---------------------------------------------------------------------------


def test_guard_drops_period_only_text() -> None:  # [test-engineer]
    """A segment with text '.' is dropped by the filler-phrase guard."""
    r = TranscriptionResult(
        text=".",
        segments=(
            SegmentScore(text=".", no_speech_prob=0.0, compression_ratio=1.0, avg_logprob=-0.3),
        ),
    )
    assert clean_result(r) is None, "Period-only text '.' must be treated as a filler phrase"


def test_guard_boundary_no_speech_prob_just_over(tmp_path: Path) -> None:  # [test-engineer, AC-3]
    """no_speech_prob=0.6+epsilon AND avg_logprob=-1.0-epsilon → dropped.

    The existing test uses values well above the boundary. This test sits one
    float ulp above both thresholds to verify the strict > and < operators.
    """
    epsilon = 1e-7
    r = TranscriptionResult(
        text="Hello world",
        segments=(
            SegmentScore(
                text="Hello world",
                no_speech_prob=0.6 + epsilon,
                compression_ratio=1.0,
                avg_logprob=-1.0 - epsilon,
            ),
        ),
    )
    assert clean_result(r) is None, (
        "Segment just above both no_speech_prob and avg_logprob thresholds must be dropped"
    )


# ---------------------------------------------------------------------------
# Retention: negative retention_days (defensive) [test-engineer]
# ---------------------------------------------------------------------------


def test_retention_negative_days_is_noop(tmp_path: Path) -> None:  # [test-engineer]
    """retention_days < 0 is not specified but should not crash or delete anything.

    The spec only mentions 0 (keep forever) and positive values. A negative value
    passed by misconfiguration should not silently delete all files.
    """
    from datetime import date

    today = date(2026, 6, 12)
    p = tmp_path / "2026-06-12.md"
    p.write_text("content")

    # With retention_days < 0, the cutoff is in the future so nothing should match
    deleted = prune_transcripts(tmp_path, retention_days=-1, today=today)
    assert p.exists(), "Negative retention_days must not delete files"
    assert deleted == []


# ---------------------------------------------------------------------------
# StateStore: save with mode=None (un-paused state) persists correctly [test-engineer]
# ---------------------------------------------------------------------------


def test_state_store_save_none_mode_roundtrip(tmp_path: Path) -> None:  # [test-engineer]
    """Saving PauseState(mode=None) — the 'recording' state — round-trips cleanly."""
    state_file = tmp_path / "state.json"
    clock = FixedClock()
    store = StateStore(state_file, clock)

    store.save(PauseState())  # default: mode=None, until=None
    loaded = store.load()
    assert loaded.mode is None
    assert loaded.until is None
    assert not is_pause_active(loaded, clock())


# ---------------------------------------------------------------------------
# Segmenter: flush in IDLE returns None (not an exception) [test-engineer]
# ---------------------------------------------------------------------------


def test_segmenter_flush_idle_is_none_after_reset() -> None:  # [test-engineer]
    """After closing a segment (back to IDLE), flush() returns None."""
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    vad = AmplitudeVAD(threshold=0.01)
    cfg = SegmenterConfig(vad_threshold=0.5, silence_split_s=1.5, max_segment_s=120.0)
    seg = Segmenter(cfg, vad, clock)

    # Create and close a segment
    speech = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)
    silence = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    seg.push(speech)
    for _ in range(60):
        seg.push(silence)

    # Now in IDLE; flush should return None
    result = seg.flush()
    assert result is None, "flush() after returning to IDLE must return None"


# ---------------------------------------------------------------------------
# Writer: crash-safe — no tmp files leaked on OSError during write [test-engineer]
# ---------------------------------------------------------------------------


def test_writer_no_tmp_leak_on_fsync_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # [test-engineer, R-9]
    """If os.fsync raises during write, no .tmp file is left visible (ADR-2 crash safety)."""
    import os as _os

    original_fsync = _os.fsync
    calls = {"count": 0}

    def bad_fsync(fd: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("simulated fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr("os.fsync", bad_fsync)

    start = datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC)
    writer = TranscriptWriter(tmp_path, FixedClock())

    with pytest.raises(OSError):
        writer.append(start, "should fail")

    # No tmp files should remain
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Tmp files leaked after write failure: {tmp_files}"


# ---------------------------------------------------------------------------
# Controller: pause_rest_of_day at various times of day sets 'until' to next midnight [test-engineer]
# ---------------------------------------------------------------------------


def test_pause_rest_of_day_sets_until_to_next_midnight(
    tmp_path: Path,
) -> None:  # [test-engineer, R-14]
    """pause_rest_of_day() always computes until = the START of the next calendar day.

    The plan says: "REST_OF_DAY stores until = next local midnight at set time".
    This verifies the computed 'until' value is correct when pause is set mid-day.
    """
    # Use a clock in a known local timezone — use UTC for simplicity
    pause_time = datetime(2026, 6, 12, 14, 30, 0, tzinfo=UTC)
    clock = FixedClock(pause_time)
    state_file = tmp_path / "state.json"
    store = StateStore(state_file, clock)
    notifier = RecordingNotifier()
    ctrl = Controller(store, notifier, clock)

    ctrl.pause_rest_of_day()

    state = store.load()
    assert state.mode == PauseMode.REST_OF_DAY
    assert state.until is not None

    # 'until' must be after pause_time
    assert state.until > pause_time, "until must be in the future"

    # 'until' must be at the start of a day (hour=0, minute=0, second=0)
    until_local = state.until.astimezone()
    assert until_local.hour == 0, f"until.hour should be 0 (midnight), got {until_local.hour}"
    assert until_local.minute == 0
    assert until_local.second == 0
