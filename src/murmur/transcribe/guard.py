"""Hallucination guard for Whisper transcription results.

OQ-2 approved semantics:
  - Drop when no_speech_prob > 0.6 AND avg_logprob < -1.0
  - Drop when compression_ratio > 2.4 (repetition hallucination)
  - Drop when normalized text ∈ FILLER_PHRASES
  - Drop when text is empty

Survivors are joined with " ". Returns None when nothing survives (AC-3, E-7, E-8).
"""

from __future__ import annotations

from murmur.transcribe.base import SegmentScore, TranscriptionResult

# Thresholds (named constants per OQ-2; not user-facing config in v1)
NO_SPEECH_PROB_MAX: float = 0.6
AVG_LOGPROB_MIN: float = -1.0
COMPRESSION_RATIO_MAX: float = 2.4

# Normalized filler phrases (lowercase stripped)
FILLER_PHRASES: frozenset[str] = frozenset(
    {
        "thank you for watching",
        "thanks for watching",
        "subtitles by the amara.org community",
        "you",
        "thank you.",
        "thank you",
        "thanks.",
        "thanks",
        "please subscribe",
        "please like and subscribe",
        "like and subscribe",
        "don't forget to subscribe",
        "subscribe for more",
        "hit the like button",
        "thanks for watching.",
        "thank you for watching.",
        ".",
        "",
    }
)


def clean_result(r: TranscriptionResult) -> str | None:
    """Filter hallucination artifacts from *r* and return clean text.

    Per-segment filtering:
    1. Drop if empty text.
    2. Drop if normalized text ∈ FILLER_PHRASES.
    3. Drop if no_speech_prob > NO_SPEECH_PROB_MAX AND avg_logprob < AVG_LOGPROB_MIN.
    4. Drop if compression_ratio > COMPRESSION_RATIO_MAX.

    Returns:
        Joined text of surviving segments, or None if nothing survives.
        None is treated as no-speech by the caller (no append, no error count).
    """
    survivors = [seg.text.strip() for seg in r.segments if segment_survives(seg)]
    if not survivors:
        return None
    return " ".join(survivors)


def segment_survives(seg: SegmentScore) -> bool:
    """True if a single Whisper segment passes the hallucination guard."""
    text = seg.text.strip()
    if not text:
        return False
    normalized = text.lower().rstrip(".")
    if normalized in FILLER_PHRASES or text in FILLER_PHRASES:
        return False
    if seg.no_speech_prob > NO_SPEECH_PROB_MAX and seg.avg_logprob < AVG_LOGPROB_MIN:
        return False
    return seg.compression_ratio <= COMPRESSION_RATIO_MAX
