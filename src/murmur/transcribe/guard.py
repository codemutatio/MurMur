"""Hallucination guard for Whisper transcription results.

OQ-2 approved semantics, plus hardening for always-on capture where Whisper
hallucinates on silence/ambient noise (confident, fluent, but fabricated text):
  - Drop empty text.
  - Drop normalized text ∈ FILLER_PHRASES.
  - Drop text that is mostly non-Latin script (e.g. a Cyrillic burst on silence
    while transcribing English).
  - Drop text containing a known video/transcription hallucination phrase
    (HALLUCINATION_SUBSTRINGS) — curated to never appear in normal dictation.
  - Drop when no_speech_prob > NO_SPEECH_PROB_STRICT (model strongly signals
    no-speech even though it confidently emitted text — the common silence
    hallucination).
  - Drop when no_speech_prob > 0.6 AND avg_logprob < -1.0 (low-confidence noise).
  - Drop when compression_ratio > 2.4 (repetition hallucination).

Survivors are joined with " ". Returns None when nothing survives (AC-3, E-7, E-8).
"""

from __future__ import annotations

from murmur.transcribe.base import TranscriptionResult

# Thresholds (named constants per OQ-2; not user-facing config in v1)
NO_SPEECH_PROB_MAX: float = 0.6
AVG_LOGPROB_MIN: float = -1.0
COMPRESSION_RATIO_MAX: float = 2.4
# A no_speech_prob this high means "almost certainly silence" — drop even when
# the model confidently emitted fluent text (the classic silence hallucination).
# Kept well above the real-speech range (typically < 0.5) to avoid clipping
# genuine, quietly-spoken or accented speech.
NO_SPEECH_PROB_STRICT: float = 0.85

# Normalized filler phrases (lowercase stripped) — whole-segment exact matches.
FILLER_PHRASES: frozenset[str] = frozenset(
    {
        "thank you for watching",
        "thanks for watching",
        "thank you for listening",
        "thanks for listening",
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
        "see you next time",
        "goodbye",
        "bye bye",
        ".",
        "",
    }
)

# Substrings that mark a segment as a video/transcription hallucination even
# inside a longer string. Curated to be safe for technical/SaaS dictation:
# none of these appear when talking about subscriptions, billing, etc. (e.g.
# "subscribe to the plan" is intentionally NOT matched — only channel/video tics).
HALLUCINATION_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "thanks for watching",
        "thank you for watching",
        "thanks for listening",
        "thank you for listening",
        "subtitles by",
        "amara.org",
        "subscribe to my channel",
        "like and subscribe",
        "don't forget to subscribe",
        "see you in the next video",
    }
)


def _is_foreign_script(text: str) -> bool:
    """True when *text* is mostly non-Latin letters.

    Whisper, asked for English, sometimes emits a burst in another script
    (e.g. Cyrillic "нет нет нет") on silence/noise. Real English output is
    ASCII, so a high non-ASCII letter ratio is a reliable hallucination signal.
    Requires a few letters so stray symbols/accents don't trip it.
    """
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    non_ascii = sum(1 for c in letters if ord(c) > 0x7F)
    return non_ascii / len(letters) > 0.6


def clean_result(r: TranscriptionResult) -> str | None:
    """Filter hallucination artifacts from *r* and return clean text.

    Returns:
        Joined text of surviving segments, or None if nothing survives.
        None is treated as no-speech by the caller (no append, no error count).
    """
    survivors: list[str] = []
    for seg in r.segments:
        text = seg.text.strip()
        if not text:
            continue
        normalized = text.lower().rstrip(".")
        if normalized in FILLER_PHRASES or text in FILLER_PHRASES:
            continue
        if _is_foreign_script(text):
            continue
        if any(sub in normalized for sub in HALLUCINATION_SUBSTRINGS):
            continue
        if seg.no_speech_prob > NO_SPEECH_PROB_STRICT:
            continue
        if seg.no_speech_prob > NO_SPEECH_PROB_MAX and seg.avg_logprob < AVG_LOGPROB_MIN:
            continue
        if seg.compression_ratio > COMPRESSION_RATIO_MAX:
            continue
        survivors.append(text)

    if not survivors:
        return None
    return " ".join(survivors)
