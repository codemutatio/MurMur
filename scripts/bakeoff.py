#!/usr/bin/env python3
"""Bake-off script: compare Whisper engines on the same audio file.

Usage:
    uv run python scripts/bakeoff.py [--input FILE.wav] [--duration 600] [--output bakeoff-<date>.md]

Without --input, records fresh audio for --duration seconds (default 600 = 10 min)
at 16 kHz mono via sounddevice. A confirmation prompt is shown before recording.

Transcribes the SAME audio through:
  1. mlx-community/whisper-large-v3-mlx
  2. mlx-community/whisper-large-v3-turbo
  3. OpenAI gpt-4o-mini-transcribe
  4. OpenAI gpt-4o-transcribe

Outputs a single Markdown file with four labeled sections + per-engine wall-time.
Missing OPENAI_API_KEY → API sections contain "SKIPPED: ..." (script still completes).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    today = date.today().isoformat()
    parser = argparse.ArgumentParser(
        description="Side-by-side bake-off of Whisper transcription engines."
    )
    parser.add_argument("--input", type=Path, metavar="FILE.wav", help="Input WAV file.")
    parser.add_argument(
        "--duration",
        type=int,
        default=600,
        metavar="SECONDS",
        help="Recording duration in seconds (default 600 = 10 min).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        metavar="FILE.md",
        default=Path(f"bakeoff-{today}.md"),
        help="Output Markdown file.",
    )
    return parser.parse_args()


def _record_audio(duration: int) -> object:
    """Record *duration* seconds of mono 16 kHz audio. Returns float32 numpy array."""
    import sounddevice as sd  # type: ignore[import-untyped]

    sample_rate = 16_000
    print(f"Recording {duration} s of audio at 16 kHz mono ...")
    print("Speak naturally. Press Ctrl+C to abort.")
    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    return audio.reshape(-1)  # type: ignore[return-value]


def _load_audio(path: Path) -> object:
    from murmur.audio.spool import load_wav

    return load_wav(path)


def _transcribe_local(audio: object, model_key: str, label: str) -> tuple[str, float]:
    """Transcribe with mlx-whisper. Returns (text, wall_s)."""
    from murmur.transcribe.local import MlxWhisperTranscriber
    from murmur.transcribe.models import ModelManager

    manager = ModelManager()
    model_ref = manager.ensure(model_key)
    transcriber = MlxWhisperTranscriber(model_ref)

    t0 = time.monotonic()
    result = transcriber.transcribe(audio, language="en", initial_prompt=None)  # type: ignore[arg-type]
    wall_s = time.monotonic() - t0

    text = result.text.strip() or "(no output)"
    return text, wall_s


def _transcribe_api(audio: object, model: str, api_key: str) -> tuple[str, float]:
    """Transcribe via OpenAI API. Returns (text, wall_s)."""
    from murmur.transcribe.api import OpenAITranscriber

    transcriber = OpenAITranscriber(model=model, api_key=api_key)
    t0 = time.monotonic()
    result = transcriber.transcribe(audio, language="en", initial_prompt=None)  # type: ignore[arg-type]
    wall_s = time.monotonic() - t0
    return result.text.strip() or "(no output)", wall_s


def main() -> int:
    args = _parse_args()

    # --- Acquire audio ---
    if args.input is not None:
        if not args.input.exists():
            print(f"Error: input file not found: {args.input}", file=sys.stderr)
            return 1
        print(f"Loading audio from {args.input} ...")
        audio = _load_audio(args.input)
    else:
        ans = (
            input(f"\nReady to record {args.duration} s of audio. Continue? [y/N] ").strip().lower()
        )
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0
        audio = _record_audio(args.duration)

    audio_s = len(audio) / 16_000.0  # type: ignore[arg-type]
    print(f"\nAudio duration: {audio_s:.1f} s\n")

    # --- OpenAI key ---
    from murmur.mac.keychain import get_openai_api_key

    api_key = get_openai_api_key(dict(os.environ))

    # --- Run all four engines ---
    sections: dict[str, str] = {}

    print("== Engine 1: mlx large-v3 ==")
    try:
        text, wall = _transcribe_local(audio, "large-v3", "mlx large-v3")
        sections["mlx large-v3"] = (
            f"**Wall time: {wall:.1f} s (RTF {audio_s / wall:.2f}x)**\n\n{text}"
        )
    except Exception as exc:
        sections["mlx large-v3"] = f"ERROR: {exc}"
    print("Done.")

    print("== Engine 2: mlx large-v3-turbo ==")
    try:
        text, wall = _transcribe_local(audio, "large-v3-turbo", "mlx large-v3-turbo")
        sections["mlx large-v3-turbo"] = (
            f"**Wall time: {wall:.1f} s (RTF {audio_s / wall:.2f}x)**\n\n{text}"
        )
    except Exception as exc:
        sections["mlx large-v3-turbo"] = f"ERROR: {exc}"
    print("Done.")

    for model_name in ["gpt-4o-mini-transcribe", "gpt-4o-transcribe"]:
        label = f"OpenAI {model_name}"
        print(f"== Engine: {label} ==")
        if api_key is None:
            sections[label] = (
                "SKIPPED: OPENAI_API_KEY not set and no Keychain item 'murmur-openai' found. "
                "Set the key to run this engine."
            )
        else:
            try:
                text, wall = _transcribe_api(audio, model_name, api_key)
                sections[label] = f"**Wall time: {wall:.1f} s**\n\n{text}"
            except Exception as exc:
                sections[label] = f"ERROR: {exc}"
        print("Done.")

    # --- Write output ---
    lines: list[str] = [f"# Bake-off — {date.today().isoformat()}\n\n"]
    lines.append(f"Audio: {audio_s:.1f} s\n\n")
    for engine, content in sections.items():
        lines.append(f"## {engine}\n\n{content}\n\n")

    output_text = "".join(lines)
    args.output.write_text(output_text, encoding="utf-8")
    print(f"\nOutput written to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
