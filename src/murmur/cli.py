"""CLI entry point for murmur.

Commands:
  murmur run [--headless] [--config PATH]
  murmur cat today [--config PATH]
  murmur copy today [--config PATH]
  murmur bench FILE.WAV [--config PATH]
  murmur install-agent
  murmur uninstall-agent
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murmur",
        description="Personal always-on voice transcription for macOS.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # murmur run
    run_p = sub.add_parser("run", help="Start the murmur daemon.")
    run_p.add_argument(
        "--headless",
        action="store_true",
        help="Run without menubar (terminal use).",
    )
    run_p.add_argument("--config", type=Path, metavar="PATH", help="Config file path.")

    # murmur cat today
    cat_p = sub.add_parser("cat", help="Print transcript to stdout.")
    cat_p.add_argument("period", choices=["today"], metavar="today")
    cat_p.add_argument("--config", type=Path, metavar="PATH")

    # murmur copy today
    copy_p = sub.add_parser("copy", help="Copy transcript to clipboard.")
    copy_p.add_argument("period", choices=["today"], metavar="today")
    copy_p.add_argument("--config", type=Path, metavar="PATH")

    # murmur bench
    bench_p = sub.add_parser("bench", help="Benchmark transcription RTF on a WAV file.")
    bench_p.add_argument("wav_file", type=Path, metavar="FILE.WAV")
    bench_p.add_argument("--config", type=Path, metavar="PATH")

    # murmur install-agent
    sub.add_parser("install-agent", help="Install launchd user agent.")

    # murmur uninstall-agent
    sub.add_parser("uninstall-agent", help="Uninstall launchd user agent.")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    # Lazy import of heavy modules — lock is acquired before any torch/mlx import (AC-6).
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "cat":
        return _cmd_cat(args)
    if args.command == "copy":
        return _cmd_copy(args)
    if args.command == "bench":
        return _cmd_bench(args)
    if args.command == "install-agent":
        return _cmd_install_agent()
    if args.command == "uninstall-agent":
        return _cmd_uninstall_agent()

    parser.print_help()
    return 2


def _cmd_run(args: argparse.Namespace) -> int:
    import os

    from murmur.config import load_config
    from murmur.lock import AlreadyRunningError, SingleInstanceLock
    from murmur.log import configure_logging
    from murmur.paths import resolve_paths
    from murmur.runtime import Runtime

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    configure_logging(paths.log_file)

    lock = SingleInstanceLock(paths.lock_file)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"Murmur already running (PID: {exc.pid})", file=sys.stderr)
        return 1

    config, warnings = load_config(paths.config_file)
    for w in warnings:
        import logging

        logging.getLogger("murmur.config").warning(w)

    runtime = Runtime(config, paths)
    try:
        runtime.start()
        if getattr(args, "headless", False):
            runtime.wait()
        else:
            # main-thread menubar (imports here so lock is already held)
            from murmur.app.menubar import MurmurApp

            app = MurmurApp(
                runtime.controller,
                runtime.status,
                runtime,
                config.storage.transcripts_dir,
            )
            app.run()
    finally:
        runtime.stop()
        lock.release()
    return 0


def _cmd_cat(args: argparse.Namespace) -> int:
    import os
    from datetime import date

    from murmur.config import load_config
    from murmur.paths import resolve_paths

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    config, _ = load_config(paths.config_file)
    today = date.today()
    transcript = config.storage.transcripts_dir / f"{today:%Y-%m-%d}.md"
    if not transcript.exists():
        print(f"No transcript for today ({transcript})", file=sys.stderr)
        return 1
    print(transcript.read_text(encoding="utf-8"), end="")
    return 0


def _cmd_copy(args: argparse.Namespace) -> int:
    import os
    from datetime import date

    from murmur.config import load_config
    from murmur.mac.clipboard import copy_to_clipboard
    from murmur.paths import resolve_paths

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    config, _ = load_config(paths.config_file)
    today = date.today()
    transcript = config.storage.transcripts_dir / f"{today:%Y-%m-%d}.md"
    if not transcript.exists():
        print(f"No transcript for today ({transcript})", file=sys.stderr)
        return 1
    text = transcript.read_text(encoding="utf-8")
    copy_to_clipboard(text)
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    import os
    import time

    import numpy as np

    from murmur.audio.spool import load_wav
    from murmur.config import load_config
    from murmur.log import configure_logging
    from murmur.paths import resolve_paths
    from murmur.transcribe.guard import clean_result
    from murmur.transcribe.local import MlxWhisperTranscriber
    from murmur.transcribe.models import ModelManager
    from murmur.transcribe.prompt import build_initial_prompt

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    configure_logging(paths.log_file)
    config, _ = load_config(paths.config_file)

    wav_path: Path = args.wav_file
    if not wav_path.exists():
        print(f"File not found: {wav_path}", file=sys.stderr)
        return 1

    audio = load_wav(wav_path)
    audio_s = len(audio) / 16_000.0

    manager = ModelManager()
    model_ref = manager.ensure(config.stt.local_model)
    transcriber = MlxWhisperTranscriber(model_ref)
    prompt = build_initial_prompt(config.stt.vocab_hints)

    t0 = time.monotonic()
    result = transcriber.transcribe(audio, language=config.stt.language, initial_prompt=prompt)
    wall_s = time.monotonic() - t0

    text = clean_result(result) or ""
    rtf = audio_s / wall_s if wall_s > 0 else 0.0

    print(f"Audio:    {audio_s:.1f} s")
    print(f"Wall:     {wall_s:.1f} s")
    print(f"RTF:      {rtf:.2f}x")
    print(f"Segments: {len(result.segments)}")
    if text:
        print(f"\nTranscript:\n{text}")

    return 0 if rtf >= 2.0 else 1


def _cmd_install_agent() -> int:
    from murmur.mac.launchd import install_agent

    plist_path = install_agent()
    print(f"Installed: {plist_path}")
    return 0


def _cmd_uninstall_agent() -> int:
    from murmur.mac.launchd import uninstall_agent

    uninstall_agent()
    print("Uninstalled murmur launchd agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
