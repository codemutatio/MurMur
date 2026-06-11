"""Build the Whisper initial_prompt from vocab hints (AC-18)."""
from __future__ import annotations

from collections.abc import Sequence


def build_initial_prompt(vocab_hints: Sequence[str]) -> str | None:
    """Construct the Whisper initial_prompt from user-configured vocabulary hints.

    Every hint is included verbatim (AC-18).
    Returns None when the hints list is empty (Whisper omits the prompt field).

    Format: ``Glossary: Rivy, BigQuery, Yassir.``
    """
    hints = [h.strip() for h in vocab_hints if h.strip()]
    if not hints:
        return None
    joined = ", ".join(hints)
    return f"Glossary: {joined}."
