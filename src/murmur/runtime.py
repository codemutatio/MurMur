"""Runtime supervisor: wires all threads and manages the application lifecycle.

Startup sequence:
  1. Acquire lock (done by cli.py before importing Runtime — AC-6 2 s bound)
  2. load config + state
  3. Prune retention
  4. Start TranscribeWorker (startup scan → backlog_drained event → live loop)
  5. Start CaptureEngine (blocks on backlog_drained before first stream open)
  6. Start HotkeyListener (if Accessibility granted; else set notice)
  7. Caller runs menubar on main thread (rumps) or calls wait() for headless

Stop sequence (stop()):
  flush partial segment → stop events → join threads → release resources
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import signal
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from murmur.audio import POST_ROLL_S, PRE_ROLL_S
from murmur.audio.segmenter import Segmenter, SegmenterConfig
from murmur.config import Config
from murmur.control import Controller, StateStore, StatusBoard
from murmur.mac.notify import Notifier, OsascriptNotifier
from murmur.mac.permissions import PermissionStatus, microphone_permission
from murmur.output.retention import prune_transcripts
from murmur.output.writer import TranscriptWriter
from murmur.paths import AppPaths
from murmur.transcribe.base import Transcriber
from murmur.transcribe.prompt import build_initial_prompt
from murmur.transcribe.worker import TranscribeWorker

_log = logging.getLogger(__name__)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _disk_free() -> int:
    stat = os.statvfs(Path.home())
    return stat.f_frsize * stat.f_bavail


class Runtime:
    """Supervisor: wires config → components → threads.

    All hardware/OS adapters default to production implementations.
    Pass fakes via the keyword arguments for testing.
    """

    def __init__(
        self,
        config: Config,
        paths: AppPaths,
        *,
        source: Any | None = None,
        vad: Any | None = None,
        transcriber: Transcriber | None = None,
        fallback: Transcriber | None = None,
        notifier: Notifier | None = None,
        now: Callable[[], datetime] = _local_now,
        disk_free: Callable[[], int] | None = None,
        mic_permission: Callable[[], PermissionStatus] | None = None,
        model_manager: Any | None = None,
    ) -> None:
        self._config = config
        self._paths = paths
        self._now = now
        self._disk_free = disk_free or _disk_free
        self._mic_permission = mic_permission or microphone_permission
        self._notifier: Notifier = notifier or OsascriptNotifier()

        # State
        self._store = StateStore(paths.state_file, now)
        self.status = StatusBoard()
        self.controller = Controller(self._store, self._notifier, now)

        # Thread control
        self._stop_event = threading.Event()
        self._backlog_drained = threading.Event()
        # Bounded work queue (F-8): if the worker is dead or in backoff the queue
        # would otherwise grow without bound.  Bound at 256 path slots (~8+ minutes
        # of segments at the normal 2 s/segment rate).  If full, the engine logs a
        # warning and drops the enqueue — the WAV file remains on disk and will be
        # picked up by the startup scan on next launch (recovery story: spool is the
        # durable queue; the in-process queue is a live optimisation layer only).
        self._work_q: queue.Queue[Path] = queue.Queue(maxsize=256)

        # Injected or deferred production adapters
        self._source = source
        self._vad = vad
        self._transcriber = transcriber
        self._fallback = fallback
        self._model_manager = model_manager

        self._capture_engine: Any = None
        self._worker: TranscribeWorker | None = None
        self._hotkey_listener: Any = None

    def start(self) -> None:
        """Start all threads per the startup sequence."""
        cfg = self._config

        # Ensure spool and transcripts dirs exist
        cfg.storage.spool_dir.mkdir(parents=True, exist_ok=True)
        cfg.storage.transcripts_dir.mkdir(parents=True, exist_ok=True)

        # Retention prune
        from datetime import date

        prune_transcripts(
            cfg.storage.transcripts_dir,
            cfg.storage.retention_days,
            date.today(),
        )

        # Build the transcription writer and prompt
        writer = TranscriptWriter(cfg.storage.transcripts_dir, self._now)
        prompt = build_initial_prompt(cfg.stt.vocab_hints)

        # Resolve transcriber
        if self._transcriber is None:
            self._transcriber = self._build_local_transcriber()

        # Resolve fallback
        if cfg.stt.api_fallback and self._fallback is None:
            self._fallback = self._build_fallback_transcriber()

        # Start worker thread
        self._worker = TranscribeWorker(
            spool_dir=cfg.storage.spool_dir,
            work_q=self._work_q,
            transcriber=self._transcriber,
            fallback=self._fallback,
            writer=writer,
            status=self.status,
            keep_audio=cfg.storage.keep_audio,
            language=cfg.stt.language,
            initial_prompt=prompt,
            backlog_drained=self._backlog_drained,
            stop=self._stop_event,
        )
        self._worker.start()
        _log.info("TranscribeWorker started")

        # Start capture engine
        vad = self._vad
        if vad is None:
            from murmur.audio.vad import SileroVAD

            vad = SileroVAD()

        seg_cfg = SegmenterConfig(
            vad_threshold=cfg.capture.vad_threshold,
            silence_split_s=cfg.capture.silence_split_s,
            max_segment_s=cfg.capture.max_segment_s,
            pre_roll_s=PRE_ROLL_S,
            post_roll_s=POST_ROLL_S,
        )
        segmenter = Segmenter(seg_cfg, vad, self._now)

        source = self._source
        if source is None:
            from murmur.audio.source import SounddeviceSource

            source = SounddeviceSource()

        from murmur.audio.engine import CaptureEngine

        self._capture_engine = CaptureEngine(
            source=source,
            segmenter=segmenter,
            spool_dir=cfg.storage.spool_dir,
            work_q=self._work_q,
            controller=self.controller,
            status=self.status,
            notifier=self._notifier,
            mic_permission=self._mic_permission,
            disk_free=self._disk_free,
            now=self._now,
            backlog_drained=self._backlog_drained,
            stop=self._stop_event,
            device=cfg.capture.device,
        )
        self._capture_engine.start()
        _log.info("CaptureEngine started")

        # Start hotkey listener
        from murmur.app.hotkey import HotkeyListener

        listener = HotkeyListener(
            cfg.hotkey.toggle,
            self.controller.toggle_pause,
        )
        if not listener.start():
            self.status.set_notice("Accessibility permission needed for hotkey")
            _log.warning("HotkeyListener not started (Accessibility not granted)")
        self._hotkey_listener = listener

        # Install signal handlers for headless mode
        try:
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGINT, self._signal_handler)
        except (OSError, ValueError):
            pass  # Not on main thread or signals unavailable

    def stop(self, timeout: float = 10.0) -> None:
        """Stop all threads gracefully."""
        _log.info("Runtime stopping ...")
        self._stop_event.set()

        if self._hotkey_listener is not None:
            with contextlib.suppress(Exception):
                self._hotkey_listener.stop()

        if self._capture_engine is not None:
            with contextlib.suppress(Exception):
                self._capture_engine.join(timeout=timeout)

        if self._worker is not None:
            with contextlib.suppress(Exception):
                self._worker.join(timeout=timeout)

        _log.info("Runtime stopped")

    def wait(self) -> None:
        """Block the calling thread until the stop event is set (headless mode).

        Used by `murmur run --headless`.
        """
        try:
            self._stop_event.wait()
        except KeyboardInterrupt:
            _log.info("KeyboardInterrupt received, stopping")
            self._stop_event.set()

    def _signal_handler(self, signum: int, frame: object) -> None:
        _log.info("Signal %d received; stopping", signum)
        self._stop_event.set()

    # ------------------------------------------------------------------

    def _build_local_transcriber(self) -> Transcriber:
        from murmur.transcribe.local import MlxWhisperTranscriber, assert_apple_silicon
        from murmur.transcribe.models import ModelManager

        assert_apple_silicon()
        manager = self._model_manager
        if manager is None:
            manager = ModelManager(
                progress=lambda done, total: self.status.set_download(done, total),
            )
        model_ref = manager.ensure(self._config.stt.local_model)
        self.status.clear_download()
        return MlxWhisperTranscriber(model_ref)

    def _build_fallback_transcriber(self) -> Transcriber | None:
        from murmur.mac.keychain import get_openai_api_key
        from murmur.transcribe.api import OpenAITranscriber

        api_key = get_openai_api_key(dict(os.environ))
        if not api_key:
            _log.warning(
                "api_fallback=true but OPENAI_API_KEY is not set (env or Keychain). "
                "API fallback is disabled for this session."
            )
            self.status.set_notice("API fallback enabled but no key found")
            return None
        return OpenAITranscriber(self._config.stt.api_model, api_key)
