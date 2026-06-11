"""Segmenter: state machine for converting a frame stream into speech segments.

Pure logic — no I/O. Uses the VoiceDetector protocol, so it runs with
AmplitudeVAD in tests and SileroVAD in production.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

import numpy as np

from murmur.audio import FRAME_SAMPLES, POST_ROLL_S, PRE_ROLL_S, SAMPLE_RATE
from murmur.audio.vad import VoiceDetector

# Pre/post-roll in frames
_PRE_ROLL_FRAMES = round(PRE_ROLL_S * SAMPLE_RATE / FRAME_SAMPLES)  # 10 @ 16kHz/512
_POST_ROLL_FRAMES = round(POST_ROLL_S * SAMPLE_RATE / FRAME_SAMPLES)  # 10 @ 16kHz/512


@dataclass(frozen=True)
class SegmenterConfig:
    vad_threshold: float
    silence_split_s: float
    max_segment_s: float
    pre_roll_s: float = PRE_ROLL_S
    post_roll_s: float = POST_ROLL_S
    offset_hysteresis: float = 0.15
    """Frame is "silence" only when prob < vad_threshold - offset_hysteresis."""


@dataclass(frozen=True)
class Segment:
    """A completed speech segment, ready to be written to the spool."""

    samples: np.ndarray
    """float32 mono 16 kHz, pre/post-roll included."""

    start_at: datetime
    """Wall clock of the first included sample (drives the **[HH:MM]** stamp)."""


class _State(Enum):
    IDLE = auto()
    ACTIVE = auto()


class Segmenter:
    """Convert a stream of 512-sample frames into speech segments.

    State machine:
    - IDLE: ring buffer keeps the last ``pre_roll_frames`` frames for pre-roll.
      Silence is discarded (R-2).
    - ACTIVE → onset when prob ≥ threshold. Segment starts at ring-buffer content
      (≤ 320 ms pre-roll; less if ring buffer not yet full).
    - ACTIVE → close after ≥ silence_frames consecutive silence frames (silence is
      ``prob < threshold - hysteresis``). Content ends at last-speech-frame +
      post_roll_frames; trailing silence beyond post-roll discarded (R-3).
    - Force-split when active buffer ≥ max_segment_s: split at min-RMS frame in
      active region; part 1 emitted, remainder stays ACTIVE (E-19, AC-13).
    """

    def __init__(
        self,
        cfg: SegmenterConfig,
        vad: VoiceDetector,
        now: Callable[[], datetime],
    ) -> None:
        self._cfg = cfg
        self._vad = vad
        self._now = now

        self._silence_frames = round(cfg.silence_split_s * SAMPLE_RATE / FRAME_SAMPLES)
        self._max_frames = round(cfg.max_segment_s * SAMPLE_RATE / FRAME_SAMPLES)
        self._pre_roll_frames = round(cfg.pre_roll_s * SAMPLE_RATE / FRAME_SAMPLES)
        self._post_roll_frames = round(cfg.post_roll_s * SAMPLE_RATE / FRAME_SAMPLES)
        self._silence_threshold = cfg.vad_threshold - cfg.offset_hysteresis

        # Ring buffer for pre-roll (IDLE state)
        self._ring: deque[np.ndarray] = deque(maxlen=self._pre_roll_frames)

        self._state: _State = _State.IDLE

        # ACTIVE state buffers
        self._active_frames: list[np.ndarray] = []
        self._active_probs: list[float] = []
        self._consecutive_silence: int = 0
        self._last_speech_idx: int = 0
        self._segment_start: datetime | None = None

    def push(self, frame: np.ndarray) -> list[Segment]:
        """Process one 512-sample frame. Returns 0..n completed segments."""
        prob = self._vad.speech_prob(frame)
        segments: list[Segment] = []

        if self._state is _State.IDLE:
            if prob >= self._cfg.vad_threshold:
                # Transition to ACTIVE: grab ring buffer as pre-roll
                self._active_frames = list(self._ring) + [frame]
                self._active_probs = [0.0] * len(self._ring) + [prob]
                self._segment_start = self._now()
                self._consecutive_silence = 0
                self._last_speech_idx = len(self._active_frames) - 1
                self._state = _State.ACTIVE
            else:
                self._ring.append(frame)
        else:
            # ACTIVE
            self._active_frames.append(frame)
            self._active_probs.append(prob)

            is_speech = prob >= self._cfg.vad_threshold
            is_silence = prob < self._silence_threshold

            if is_speech:
                self._last_speech_idx = len(self._active_frames) - 1
                self._consecutive_silence = 0
            elif is_silence:
                self._consecutive_silence += 1
            # else: prob in hysteresis zone → do not advance silence counter

            # Check silence-split condition
            if self._consecutive_silence >= self._silence_frames:
                seg = self._close_segment()
                if seg is not None:
                    segments.append(seg)
            # Check max-duration force-split condition
            elif len(self._active_frames) >= self._max_frames:
                split_segs = self._force_split()
                segments.extend(split_segs)

        return segments

    def flush(self) -> Segment | None:
        """Close the current segment (called on pause, shutdown, or device change)."""
        if self._state is _State.IDLE:
            return None
        return self._close_segment()

    def reset_vad(self) -> None:
        """Reset the VAD internal state (called on stream reopen)."""
        self._vad.reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close_segment(self) -> Segment | None:
        """Emit the current ACTIVE buffer as a Segment and reset to IDLE."""
        end_idx = min(
            self._last_speech_idx + self._post_roll_frames,
            len(self._active_frames) - 1,
        )
        frames = self._active_frames[: end_idx + 1]

        self._state = _State.IDLE
        self._ring.clear()
        self._active_frames = []
        self._active_probs = []
        self._consecutive_silence = 0

        if not frames:
            return None

        samples = np.concatenate(frames).astype(np.float32)
        seg = Segment(samples=samples, start_at=self._segment_start or self._now())
        self._segment_start = None
        return seg

    def _force_split(self) -> list[Segment]:
        """Split at the quietest (min-RMS) frame; emit part 1, keep remainder ACTIVE."""
        frames = self._active_frames
        probs = self._active_probs

        # Find min-RMS frame across the active buffer.
        # Lower-bound to 1 so part 1 always contains at least one frame (F-7):
        # argmin returning 0 on uniform-RMS audio would otherwise emit a zero-frame
        # segment and leave the entire buffer in part 2, causing pathological re-splits.
        rms_values = [float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) for f in frames]
        split_idx = max(1, int(np.argmin(rms_values)))

        # Part 1: frames[0..split_idx]
        part1_frames = frames[: split_idx + 1]
        part1_samples = np.concatenate(part1_frames).astype(np.float32)
        seg1 = Segment(
            samples=part1_samples,
            start_at=self._segment_start or self._now(),
        )

        # Part 2: frames[split_idx+1..] stays ACTIVE
        self._active_frames = frames[split_idx + 1 :]
        self._active_probs = probs[split_idx + 1 :]
        self._segment_start = self._now()
        self._consecutive_silence = 0
        # Update last_speech_idx relative to new start
        self._last_speech_idx = max(0, self._last_speech_idx - (split_idx + 1))

        return [seg1]
