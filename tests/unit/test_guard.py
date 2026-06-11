"""Tests for murmur.transcribe.guard — T-6."""
from __future__ import annotations

import pytest

from murmur.transcribe.base import SegmentScore, TranscriptionResult
from murmur.transcribe.guard import (
    AVG_LOGPROB_MIN,
    COMPRESSION_RATIO_MAX,
    NO_SPEECH_PROB_MAX,
    clean_result,
)


def _result(*segs: SegmentScore) -> TranscriptionResult:
    text = " ".join(s.text for s in segs if s.text.strip())
    return TranscriptionResult(text=text, segments=segs)


def _seg(
    text: str = "Hello world",
    no_speech_prob: float = 0.0,
    compression_ratio: float = 1.0,
    avg_logprob: float = -0.3,
) -> SegmentScore:
    return SegmentScore(
        text=text,
        no_speech_prob=no_speech_prob,
        compression_ratio=compression_ratio,
        avg_logprob=avg_logprob,
    )


# AC-3
def test_drops_high_no_speech_prob() -> None:  # [AC-3]
    """Drops segment when no_speech_prob > 0.6 AND avg_logprob < -1.0."""
    r = _result(_seg(no_speech_prob=0.7, avg_logprob=-1.5))
    assert clean_result(r) is None


def test_keeps_high_no_speech_prob_if_logprob_ok() -> None:
    """Does NOT drop when no_speech_prob > 0.6 but avg_logprob >= -1.0."""
    r = _result(_seg(no_speech_prob=0.7, avg_logprob=-0.5))
    assert clean_result(r) == "Hello world"


def test_keeps_low_no_speech_prob() -> None:
    """Normal segment (no_speech_prob <= 0.6) is kept."""
    r = _result(_seg(no_speech_prob=0.3))
    assert clean_result(r) == "Hello world"


# AC-3
def test_drops_filler_phrase() -> None:  # [AC-3]
    """Drops segments that match known filler phrases."""
    r = _result(_seg(text="Thank you for watching"))
    assert clean_result(r) is None


def test_drops_filler_phrase_case_insensitive() -> None:
    """Filler phrase matching is case-insensitive."""
    r = _result(_seg(text="THANK YOU FOR WATCHING"))
    assert clean_result(r) is None


def test_drops_high_compression_ratio() -> None:
    """Drops segment when compression_ratio > 2.4."""
    r = _result(_seg(compression_ratio=3.0))
    assert clean_result(r) is None


def test_keeps_compression_ratio_at_max() -> None:
    """Does NOT drop when compression_ratio == 2.4 (boundary not dropped)."""
    r = _result(_seg(compression_ratio=COMPRESSION_RATIO_MAX))
    assert clean_result(r) == "Hello world"


# E-7
def test_empty_text_is_no_speech() -> None:  # [E-7]
    """Empty or whitespace-only text → treated as no-speech (returns None)."""
    r = _result(_seg(text="  "), _seg(text=""))
    assert clean_result(r) is None


# E-8
def test_partial_drop_keeps_rest() -> None:  # [E-8]
    """When some segments are hallucinations, survivors are returned."""
    bad = _seg(text="Thank you for watching")
    good = _seg(text="This is real speech")
    r = _result(bad, good)
    result = clean_result(r)
    assert result == "This is real speech"


def test_all_bad_returns_none() -> None:
    """All segments filtered → None."""
    r = _result(
        _seg(text="Thank you for watching"),
        _seg(no_speech_prob=0.9, avg_logprob=-2.0),
        _seg(compression_ratio=5.0),
    )
    assert clean_result(r) is None


def test_multiple_survivors_joined() -> None:
    """Multiple good segments are joined with a space."""
    r = _result(_seg(text="First"), _seg(text="second"))
    assert clean_result(r) == "First second"


def test_empty_segments_tuple() -> None:
    """Result with no segments → None."""
    r = TranscriptionResult(text="", segments=())
    assert clean_result(r) is None


def test_threshold_boundary_no_speech_exactly_at_max() -> None:
    """no_speech_prob == NO_SPEECH_PROB_MAX is NOT dropped (boundary)."""
    r = _result(_seg(no_speech_prob=NO_SPEECH_PROB_MAX, avg_logprob=-2.0))
    assert clean_result(r) == "Hello world"
