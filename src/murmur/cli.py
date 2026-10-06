"""CLI entry point for murmur.

Commands:
  murmur run [--headless] [--config PATH]
  murmur cat today [--config PATH]
  murmur copy today [--config PATH]
  murmur bench FILE.WAV [--config PATH]
  murmur install-agent
  murmur uninstall-agent
  murmur voices [list | name VOICE NAME | merge A B | forget VOICE | wipe | enroll]
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
    sub.add_parser("install-agent", help="Install launchd user agent (bare murmur run).")

    # murmur install-app-agent
    app_agent_p = sub.add_parser(
        "install-app-agent", help="Install launchd agent for the Murmur.app bundle."
    )
    app_agent_p.add_argument(
        "--app",
        type=Path,
        metavar="PATH",
        help="Path to Murmur.app (default: /Applications/Murmur.app).",
    )

    # murmur uninstall-agent
    sub.add_parser("uninstall-agent", help="Uninstall launchd user agent.")

    # murmur voices …
    voices_p = sub.add_parser("voices", help="Manage the speaker-recognition voice library.")
    voices_p.add_argument("--config", type=Path, metavar="PATH", help="Config file path.")
    vsub = voices_p.add_subparsers(dest="voices_cmd", metavar="<action>")
    vsub.add_parser("list", help="List known voices.")
    v_name = vsub.add_parser("name", help='Name a voice, e.g. murmur voices name voice-7 "Tolu Abimbola".')
    v_name.add_argument("voice")
    v_name.add_argument("name")
    v_merge = vsub.add_parser("merge", help="Merge two voices that are the same person.")
    v_merge.add_argument("source")
    v_merge.add_argument("into")
    v_forget = vsub.add_parser("forget", help="Erase one person's voiceprint and history.")
    v_forget.add_argument("voice")
    v_wipe = vsub.add_parser("wipe", help="Erase the entire voice library.")
    v_wipe.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")
    v_enroll = vsub.add_parser("enroll", help="Teach a voice from a clean sample (default: you).")
    v_enroll.add_argument("--name", help="Whose voice (default: voices.self_name).")
    v_enroll.add_argument("--seconds", type=float, default=30.0, help="Mic recording length.")
    v_enroll.add_argument("--file", type=Path, help="Use a WAV/AIFF file instead of the mic.")

    return parser


def isolate_app_sys_path() -> list[str]:
    """Inside Murmur.app, make Murmur's own environment win over stray copies.

    The py2app alias bundle launches the framework Python, whose startup puts the
    system-wide site-packages and the personal ``~/Library/Python/X.Y`` one on
    sys.path *before* the bundle appends Murmur's uv-tool environment. Any
    package installed in those places then silently shadows Murmur's pinned
    version inside the app (observed: numpy 1.26 instead of 2.4, a ruamel.yaml
    that broke SpeechBrain, older pydantic/requests/...). Must run before any of
    those are imported. No-op outside the bundle. Returns the removed entries.
    """
    import os
    import site

    if getattr(sys, "frozen", None) != "macosx_app":
        return []
    own = os.path.realpath(str(Path(__file__).resolve().parents[1]))  # dir holding murmur/
    ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    strays = [
        os.path.realpath(p)
        for p in (
            site.getusersitepackages(),
            os.path.join(sys.base_prefix, "lib", ver, "site-packages"),
        )
        if p
    ]
    strays = [s for s in strays if s != own]

    def is_stray(p: str) -> bool:
        rp = os.path.realpath(p) if p else p
        return any(rp == s or rp.startswith(s + os.sep) for s in strays)

    removed = [p for p in sys.path if is_stray(p)]
    sys.path[:] = [own] + [p for p in sys.path if p and not is_stray(p) and os.path.realpath(p) != own]
    return removed


_ISOLATION_REMOVED: list[str] = []


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns exit code."""
    _ISOLATION_REMOVED[:] = isolate_app_sys_path()
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
    if args.command == "install-app-agent":
        return _cmd_install_app_agent(args)
    if args.command == "uninstall-agent":
        return _cmd_uninstall_agent()
    if args.command == "voices":
        return _cmd_voices(args)

    parser.print_help()
    return 2


def _cmd_voices(args: argparse.Namespace) -> int:
    import os
    from datetime import datetime

    from murmur.config import load_config
    from murmur.paths import resolve_paths
    from murmur.voices.store import VoiceStore, wipe

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    config, _ = load_config(paths.config_file)
    db = paths.home / "voices" / "voices.db"
    export = config.storage.transcripts_dir / ".voices.json"
    action = args.voices_cmd

    if action == "wipe":
        if not args.yes:
            reply = input("Erase ALL stored voiceprints? This cannot be undone. [y/N] ")
            if reply.strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return 1
        removed = wipe(db)
        export.unlink(missing_ok=True)
        print("Voice library erased." if removed else "No voice library to erase.")
        return 0

    store = VoiceStore(db)
    try:
        if action in (None, "list"):
            voices = store.voices()
            if not voices:
                print("No voices yet — they appear as Murmur hears people.")
                return 0
            print(f"{'voice':<12} {'name':<24} {'named by':<10} {'talk':>8}  last heard")
            for v in voices:
                last = datetime.fromtimestamp(v.last_seen).strftime("%Y-%m-%d %H:%M")
                print(
                    f"voice-{v.id:<6} {v.name or '—':<24} {v.name_source or '':<10} "
                    f"{v.talk_s / 60:>6.1f}m  {last}"
                )
            return 0
        if action == "name":
            vid = store.name(args.voice, args.name, source="user")
            print(f"voice-{vid} is now {args.name}")
        elif action == "merge":
            vid = store.merge(args.source, args.into)
            print(f"Merged {args.source} into voice-{vid}")
        elif action == "forget":
            if not store.forget(args.voice):
                print(f"No such voice: {args.voice}", file=sys.stderr)
                return 1
            print(f"Erased {args.voice}")
        elif action == "enroll":
            rc = _enroll(store, args, config.voices.self_name)
            if rc != 0:
                return rc
        store.export_json(export)
        return 0
    except KeyError as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 1
    finally:
        store.close()


def _enroll(store: object, args: argparse.Namespace, self_name: str) -> int:
    import numpy as np

    from murmur.voices.embedder import SAMPLE_RATE, SpeakerEmbedder
    from murmur.voices.store import VoiceStore

    assert isinstance(store, VoiceStore)
    name = args.name or self_name
    if args.file:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "enroll.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(args.file),
                 "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", str(wav)],
                check=True,
            )
            audio = np.fromfile(wav, dtype=np.float32)
    else:
        import sounddevice as sd

        print(f"Recording {args.seconds:.0f}s — read anything aloud in your normal voice…")
        audio = sd.rec(int(args.seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1,
                       dtype="float32")
        sd.wait()
        audio = audio[:, 0]
    win = 3 * SAMPLE_RATE
    chunks = [audio[i : i + win] for i in range(0, len(audio) - win + 1, win)]
    chunks = [c for c in chunks if float(np.sqrt(np.mean(c**2))) > 0.005]  # skip silence
    if len(chunks) < 3:
        print("Not enough speech captured — try again with a longer or louder sample.", file=sys.stderr)
        return 1
    emb = SpeakerEmbedder()
    source = "self" if name == self_name else "user"
    vid = store.enroll([emb.embed(c) for c in chunks], name, source=source)
    print(f"Enrolled {name} as voice-{vid} from {len(chunks)} samples.")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    import os

    from murmur.config import load_config
    from murmur.lock import AlreadyRunningError, SingleInstanceLock
    from murmur.log import configure_logging
    from murmur.paths import resolve_paths
    from murmur.runtime import Runtime

    paths = resolve_paths(os.environ, getattr(args, "config", None))
    configure_logging(paths.log_file)
    if _ISOLATION_REMOVED:
        import logging

        import numpy

        logging.getLogger("murmur.cli").info(
            "Murmur.app: using Murmur's own environment (numpy %s); dropped stray paths: %s",
            numpy.__version__,
            ", ".join(_ISOLATION_REMOVED),
        )

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
            import rumps

            from murmur.app.menubar import MurmurApp

            app = MurmurApp(
                runtime.controller,
                runtime.status,
                runtime,
                config.storage.transcripts_dir,
                paths.config_file,
            )
            # A signal alone cannot break the rumps run loop, so route SIGTERM/
            # SIGINT to the app's teardown. rumps' run() installs its own SIGINT
            # mach interrupt; re-install ours via before_start (which fires after
            # that) so both signals reach the runtime handler.
            runtime.set_signal_shutdown_hook(app.request_shutdown)
            rumps.events.before_start.register(runtime.install_signal_handlers)
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


def _cmd_install_app_agent(args: argparse.Namespace) -> int:
    from murmur.mac.launchd import install_app_agent

    app = getattr(args, "app", None)
    try:
        plist_path = install_app_agent(app) if app is not None else install_app_agent()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Installed app agent: {plist_path}")
    return 0


def _cmd_uninstall_agent() -> int:
    from murmur.mac.launchd import uninstall_agent

    uninstall_agent()
    print("Uninstalled murmur launchd agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
