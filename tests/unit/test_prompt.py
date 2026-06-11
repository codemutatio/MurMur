"""Tests for murmur.transcribe.prompt — T-7."""

from __future__ import annotations

from murmur.transcribe.prompt import build_initial_prompt


# AC-18
def test_hints_verbatim() -> None:  # [AC-18]
    """Every vocab hint appears verbatim in the initial_prompt."""
    hints = ["Rivy", "BigQuery", "Yassir"]
    prompt = build_initial_prompt(hints)
    assert prompt is not None
    for hint in hints:
        assert hint in prompt, f"{hint!r} not in prompt {prompt!r}"


def test_empty_hints_returns_none() -> None:
    """Empty hints list → None (no prompt passed to Whisper)."""
    assert build_initial_prompt([]) is None


def test_whitespace_only_hints_filtered() -> None:
    """Whitespace-only hints are ignored; empty list → None."""
    assert build_initial_prompt(["  ", "\t", ""]) is None


def test_prompt_format() -> None:
    """Prompt starts with 'Glossary: ' and ends with '.'."""
    prompt = build_initial_prompt(["Rivy", "BigQuery"])
    assert prompt is not None
    assert prompt.startswith("Glossary: ")
    assert prompt.endswith(".")


def test_single_hint() -> None:
    """Single hint produces a valid prompt."""
    prompt = build_initial_prompt(["Murmur"])
    assert prompt == "Glossary: Murmur."
