"""Split a transcribed clip into speaker turns and label each one.

For each Whisper segment long enough to fingerprint (>= ``min_embed_s``), the
audio slice is embedded and matched against the voice library; shorter
segments inherit the previous speaker. Consecutive segments with the same
speaker become one turn, written as its own transcript entry.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from murmur.transcribe.base import SegmentScore
from murmur.transcribe.guard import segment_survives
from murmur.voices.embedder import SAMPLE_RATE, SpeakerEmbedder
from murmur.voices.store import VoiceStore

_log = logging.getLogger(__name__)

UNKNOWN = "unknown"


@dataclass(frozen=True)
class Turn:
    offset_s: float  # from the start of the clip
    text: str
    speaker: str  # a name, "voice-N", or "unknown"


class SpeakerTagger:
    def __init__(
        self,
        store: VoiceStore,
        embedder: SpeakerEmbedder,
        *,
        self_name: str,
        export_path: Path | None = None,
        hints_dir: Path | None = None,
        min_embed_s: float = 1.0,
        maintenance_interval_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._embedder = embedder
        self._self_name = self_name
        self._export_path = export_path
        self._hints_dir = hints_dir
        self._min_embed_s = min_embed_s
        self._interval = maintenance_interval_s
        self._clock = clock
        self._last_maintenance = float("-inf")
        self._embed_failed = False

    def turns(
        self,
        audio: np.ndarray,
        start_ts: float,
        segments: Sequence[SegmentScore],
        context: str | None,
    ) -> list[Turn] | None:
        """Speaker turns for one clip; None if the engine gave no timings."""
        segs = [s for s in segments if segment_survives(s)]
        if not segs:
            return []
        if all(s.end <= 0 for s in segs):
            return None
        labelled: list[tuple[SegmentScore, str]] = []
        prev = UNKNOWN
        for s in segs:
            label = prev
            dur = max(0.0, s.end - s.start)
            if dur >= self._min_embed_s:
                a = audio[int(s.start * SAMPLE_RATE) : int(s.end * SAMPLE_RATE)]
                if len(a) >= self._min_embed_s * SAMPLE_RATE:
                    label = self._identify(a, start_ts + s.start, len(a) / SAMPLE_RATE, context, prev)
            labelled.append((s, label))
            prev = label

        result: list[Turn] = []
        for s, label in labelled:
            text = s.text.strip()
            if result and result[-1].speaker == label:
                last = result[-1]
                result[-1] = Turn(last.offset_s, f"{last.text} {text}", label)
            else:
                result.append(Turn(max(0.0, s.start), text, label))
        return result

    def _identify(
        self, audio: np.ndarray, ts: float, dur: float, context: str | None, prev: str
    ) -> str:
        try:
            ident = self.store.identify(self._embedder.embed(audio), ts=ts, dur=dur, context=context)
        except Exception as exc:
            if not self._embed_failed:
                _log.warning("Speaker identification unavailable: %s", exc)
                self._embed_failed = True
            return prev
        self._embed_failed = False
        return ident.label or UNKNOWN

    def maintenance(self, *, force: bool = False) -> None:
        """Learn names (self, calendar hints) and export the voice list.

        Throttled; called after each processed clip and at startup.
        """
        now = self._clock()
        if not force and now - self._last_maintenance < self._interval:
            return
        self._last_maintenance = now
        try:
            self.store.bootstrap_self(self._self_name)
            if self._hints_dir is not None:
                n = self.store.apply_hint_files(self._hints_dir, self._self_name)
                if n:
                    _log.info("Named %d voice(s) from calendar hints", n)
            if self._export_path is not None:
                self.store.export_json(self._export_path)
        except Exception as exc:  # never let bookkeeping break transcription
            _log.warning("Voice maintenance failed: %s", exc)
