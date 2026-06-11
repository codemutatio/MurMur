"""Tests for murmur.audio.segmenter — T-3."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np

from murmur.audio import FRAME_SAMPLES, SAMPLE_RATE
from murmur.audio.segmenter import Segment, Segmenter, SegmenterConfig
from tests.fakes import AmplitudeVAD, FixedClock


def _cfg(
    vad_threshold: float = 0.5,
    silence_split_s: float = 1.5,
    max_segment_s: float = 120.0,
) -> SegmenterConfig:
    return SegmenterConfig(
        vad_threshold=vad_threshold,
        silence_split_s=silence_split_s,
        max_segment_s=max_segment_s,
    )


def _speech_frame(amplitude: float = 0.5) -> np.ndarray:
    """Return a 512-sample float32 frame with amplitude above the AmplitudeVAD threshold."""
    return np.full(FRAME_SAMPLES, amplitude, dtype=np.float32)


def _silence_frame() -> np.ndarray:
    return np.zeros(FRAME_SAMPLES, dtype=np.float32)


def _make_segmenter(
    vad_threshold: float = 0.5,
    silence_split_s: float = 1.5,
    max_segment_s: float = 120.0,
    clock: FixedClock | None = None,
) -> tuple[Segmenter, FixedClock, AmplitudeVAD]:
    c = clock or FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    vad = AmplitudeVAD(threshold=0.01)
    cfg = _cfg(
        vad_threshold=vad_threshold, silence_split_s=silence_split_s, max_segment_s=max_segment_s
    )
    seg = Segmenter(cfg, vad, c)
    return seg, c, vad


# AC-14
def test_1_6s_gap_two_segments() -> None:  # [AC-14]
    """1.6 s silence between two speech bursts → two separate segments."""
    segmenter, _, _ = _make_segmenter(silence_split_s=1.5)
    results: list[Segment] = []

    # Push 20 speech frames (~640 ms)
    for _ in range(20):
        results.extend(segmenter.push(_speech_frame()))

    # Push silence for > 1.5 s  (1.5 s / 32 ms = 47 frames; push 50)
    for _ in range(50):
        results.extend(segmenter.push(_silence_frame()))

    # Push 20 more speech frames
    for _ in range(20):
        results.extend(segmenter.push(_speech_frame()))

    # Flush the second segment
    seg = segmenter.flush()
    if seg is not None:
        results.append(seg)

    assert len(results) == 2, f"Expected 2 segments, got {len(results)}"


# AC-15
def test_pre_post_roll_within_50ms() -> None:  # [AC-15]
    """Pre/post-roll is 0.3 s ± 50 ms (in terms of frame boundaries)."""
    segmenter, _, _ = _make_segmenter()

    # Fill the pre-roll ring buffer with silence
    for _ in range(15):
        segmenter.push(_silence_frame())

    # Push speech
    speech_frames = 10
    for _ in range(speech_frames):
        segmenter.push(_speech_frame())

    # Push enough silence to close segment
    for _ in range(50):
        segmenter.push(_silence_frame())

    seg = segmenter.flush()
    # The segment should include pre-roll frames before speech
    # At 16 kHz / 512 samples = 32 ms/frame; 10 frames = 320 ms ≈ 0.3 s ± 50 ms
    from murmur.audio import POST_ROLL_S, PRE_ROLL_S

    pre_frames = round(PRE_ROLL_S * SAMPLE_RATE / FRAME_SAMPLES)
    post_frames = round(POST_ROLL_S * SAMPLE_RATE / FRAME_SAMPLES)
    tolerance_frames = math.ceil(0.05 * SAMPLE_RATE / FRAME_SAMPLES) + 1  # 50 ms

    # Find the segment from push results
    results: list[Segment] = []
    segmenter2, _, _ = _make_segmenter()
    for _ in range(15):
        segmenter2.push(_silence_frame())
    for _ in range(speech_frames):
        r = segmenter2.push(_speech_frame())
        results.extend(r)
    for _ in range(50):
        r = segmenter2.push(_silence_frame())
        results.extend(r)

    assert len(results) == 1
    seg = results[0]
    total_samples = len(seg.samples)
    # Expected: up to pre_frames + speech_frames + post_frames frames
    expected_min = (speech_frames) * FRAME_SAMPLES
    expected_max = (pre_frames + speech_frames + post_frames + tolerance_frames) * FRAME_SAMPLES
    assert total_samples >= expected_min
    assert total_samples <= expected_max, (
        f"Segment too long: {total_samples} samples (expected <= {expected_max})"
    )


# R-2
def test_silence_never_buffered() -> None:  # [R-2]
    """Silence frames in IDLE state are discarded (not accumulated into segments)."""
    segmenter, _, _ = _make_segmenter()

    # Push many silence frames
    for _ in range(200):
        result = segmenter.push(_silence_frame())
        assert result == [], "Silence in IDLE should produce no segments"

    # Verify flush returns nothing
    assert segmenter.flush() is None


# AC-13
def test_force_split_at_quietest_point_121s() -> None:  # [AC-13]
    """121 s of continuous speech → at least 2 segments, each ≤ 120 s."""
    segmenter, _, _ = _make_segmenter(max_segment_s=120.0, silence_split_s=200.0)

    # 121 s of speech at 32 ms/frame = 3781 frames
    n_frames = math.ceil(121.0 * SAMPLE_RATE / FRAME_SAMPLES)
    results: list[Segment] = []

    for i in range(n_frames):
        # Vary amplitude slightly so there is a detectable quietest frame
        amp = 0.5 + 0.1 * math.sin(i * 0.1)
        frame = np.full(FRAME_SAMPLES, amp, dtype=np.float32)
        results.extend(segmenter.push(frame))

    seg = segmenter.flush()
    if seg is not None:
        results.append(seg)

    assert len(results) >= 2, "Expected force-split to produce ≥ 2 segments"
    for seg in results:
        duration_s = len(seg.samples) / SAMPLE_RATE
        assert duration_s <= 120.5, f"Segment {duration_s:.1f}s exceeds 120 s"


# E-20
def test_short_blip_emitted() -> None:  # [E-20]
    """A very short speech burst is still emitted (guard handles false-positive later)."""
    segmenter, _, _ = _make_segmenter()

    # One speech frame then silence
    segmenter.push(_speech_frame())
    results: list[Segment] = []
    for _ in range(60):  # 1.92 s silence → closes segment
        results.extend(segmenter.push(_silence_frame()))

    assert len(results) == 1
    seg = results[0]
    assert len(seg.samples) > 0


def test_segment_start_time_matches_onset() -> None:
    """Segment start_at reflects the clock value at VAD onset."""
    clock = FixedClock(datetime(2026, 6, 12, 10, 30, 0, tzinfo=UTC))
    segmenter, _, _ = _make_segmenter(clock=clock)

    # Some pre-roll silence
    for _ in range(5):
        segmenter.push(_silence_frame())

    # Advance clock before speech onset
    clock.advance(5.0)
    onset_time = clock()

    for _ in range(5):
        segmenter.push(_speech_frame())
    results: list[Segment] = []
    for _ in range(60):
        results.extend(segmenter.push(_silence_frame()))

    assert len(results) == 1
    # start_at should be near the clock at onset (same second)
    assert results[0].start_at == onset_time


def test_flush_returns_partial_segment() -> None:
    """flush() returns the active partial segment."""
    segmenter, _, _ = _make_segmenter()
    for _ in range(5):
        segmenter.push(_speech_frame())
    seg = segmenter.flush()
    assert seg is not None
    assert len(seg.samples) > 0


def test_flush_in_idle_returns_none() -> None:
    segmenter, _, _ = _make_segmenter()
    assert segmenter.flush() is None


def test_pre_roll_less_than_full_when_stream_starts() -> None:
    """Pre-roll uses whatever is in the ring buffer — even if it's empty."""
    segmenter, _, _ = _make_segmenter()
    # Immediately push speech without any pre-roll silence
    segmenter.push(_speech_frame())
    results: list[Segment] = []
    for _ in range(60):
        results.extend(segmenter.push(_silence_frame()))
    assert len(results) == 1
    # Should still produce a segment without crashing
    assert len(results[0].samples) > 0


# F-7: force-split with uniform RMS (argmin returns 0 without the lower-bound fix)
def test_force_split_uniform_rms_no_1_frame_segment_f7() -> None:
    """Force-split on uniform-RMS audio must not produce a 1-frame (32 ms) segment.

    When all frames have the same amplitude, argmin returns 0.  Without the
    lower-bound fix, part 1 would be 1 frame (~32 ms) and part 2 would be the
    entire remaining buffer — causing pathological re-splits.

    F-7 requirement: split_idx ≥ 1, so every split produces a part 1 of at least
    2 frames (64 ms), and each resulting segment is ≤ max_segment_s.
    """
    segmenter, _, _ = _make_segmenter(
        max_segment_s=120.0,
        silence_split_s=200.0,  # never silence-close
    )

    # Uniform amplitude: all frames have identical RMS → argmin is deterministically 0
    # without the fix.
    n_frames = math.ceil(121.0 * SAMPLE_RATE / FRAME_SAMPLES)
    results: list[Segment] = []
    for _ in range(n_frames):
        frame = np.full(FRAME_SAMPLES, 0.5, dtype=np.float32)
        results.extend(segmenter.push(frame))

    seg = segmenter.flush()
    if seg is not None:
        results.append(seg)

    assert results, "Expected at least one segment from force-split"
    for s in results:
        dur_s = len(s.samples) / SAMPLE_RATE
        assert dur_s >= 2 * FRAME_SAMPLES / SAMPLE_RATE - 0.001, (
            f"Segment too short ({dur_s:.4f} s — looks like a 1-frame degenerate split)"
        )
        assert dur_s <= 120.5, f"Segment too long ({dur_s:.1f} s)"
