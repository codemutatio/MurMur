"""Transcription worker thread.

Consumes spool WAV files in strict FIFO order (R-4, R-16), transcribes with
the local engine, applies the hallucination guard, appends to the transcript,
and deletes the WAV (unless keep_audio=true, AC-1/AC-2).

Failure policy (E-9):
  - Consecutive local failures on the SAME segment increment a counter.
  - On 3rd consecutive failure: use fallback if provided (AC-8), else leave
    WAV in spool and increment error count (AC-7). No outbound network object
    is ever constructed when fallback is None.
  - API fallback failure → leave WAV, increment errors, sleep backoff (E-10).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from murmur.audio.spool import list_spool, load_wav, parse_segment_start
from murmur.control import StatusBoard
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import Transcriber, TranscriptionError
from murmur.transcribe.guard import clean_result

if TYPE_CHECKING:
    from murmur.audio.context import SegmentContext
    from murmur.output.health import HealthReporter
    from murmur.voices.tagger import SpeakerTagger

_log = logging.getLogger(__name__)


class ContextLabeler(Protocol):
    def label(self, start: float, end: float) -> SegmentContext | None: ...


def _marker(seg_ctx: SegmentContext | None, speaker: str | None) -> str | None:
    """Comment text for an entry: call/media context and/or speaker."""
    parts = []
    if seg_ctx is not None:
        parts.append(seg_ctx.marker())
    if speaker:
        parts.append(f'speaker="{speaker.replace(chr(34), "")}"')
    return " ".join(parts) or None


class TranscribeWorker(threading.Thread):
    """Worker thread: spool → transcribe → guard → append → delete."""

    def __init__(
        self,
        *,
        spool_dir: Path,
        work_q: queue.Queue[Path],
        transcriber: Transcriber,
        fallback: Transcriber | None,
        writer: TranscriptWriter,
        status: StatusBoard,
        keep_audio: bool,
        language: str,
        initial_prompt: str | None,
        backlog_drained: threading.Event,
        stop: threading.Event,
        max_local_failures: int = 3,
        backoff_s: float = 30.0,
        context: ContextLabeler | None = None,
        skip_media: bool = False,
        health: HealthReporter | None = None,
        speakers: SpeakerTagger | None = None,
    ) -> None:
        super().__init__(name="transcribe-worker", daemon=True)
        self._speakers = speakers
        self._context = context
        self._skip_media = skip_media
        self._health = health
        self._spool_dir = spool_dir
        self._work_q = work_q
        self._transcriber = transcriber
        self._fallback = fallback
        self._writer = writer
        self._status = status
        self._keep_audio = keep_audio
        self._language = language
        self._initial_prompt = initial_prompt
        self._backlog_drained = backlog_drained
        self._stop_event = stop
        self._max_local_failures = max_local_failures
        self._backoff_s = backoff_s

    def run(self) -> None:
        # Startup: drain existing spool before signalling backlog_drained (E-6, R-16)
        if self._speakers is not None:
            self._speakers.maintenance(force=True)
        existing = list_spool(self._spool_dir)
        _log.info("Startup scan: %d spool files", len(existing))
        for wav_path in existing:
            if self._stop_event.is_set():
                break
            self._process(wav_path)

        self._backlog_drained.set()
        _log.info("Backlog drained; entering live loop")

        # Live loop: drain work_q until stop.
        # The outer try/except ensures that any unexpected exception that escapes
        # _process (e.g. an error from work_q.task_done itself) is logged and
        # the loop continues rather than dying silently (F-4).
        while not self._stop_event.is_set():
            try:
                wav_path = self._work_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(wav_path)
            except Exception as exc:
                _log.exception("Unhandled exception in worker loop body (continuing): %s", exc)
            finally:
                self._work_q.task_done()

    def _process(self, wav_path: Path) -> None:
        """Process one WAV file: load → transcribe → guard → append → maybe delete."""
        if not wav_path.exists():
            _log.debug("WAV already gone: %s", wav_path)
            return

        try:
            audio = load_wav(wav_path)
        except Exception as exc:
            _log.error("Failed to load %s: %s", wav_path, exc)
            self._error(f"load failed: {exc}")
            return

        # Guard against non-conforming filenames (security finding folded into F-4).
        # parse_segment_start raises ValueError on bad stems; skip and log rather than die.
        try:
            start_at = parse_segment_start(wav_path)
        except ValueError as exc:
            _log.warning(
                "Skipping spool file with non-conforming filename %s: %s",
                wav_path.name,
                exc,
            )
            return

        audio_s = len(audio) / 16_000.0

        # Was a call app using the mic, or an app playing audio, while this was
        # captured? (murmur.audio.context) — tags the entry; optionally skips media.
        seg_ctx = None
        if self._context is not None:
            start_ts = start_at.timestamp()
            seg_ctx = self._context.label(start_ts, start_ts + audio_s)
        if seg_ctx is not None and seg_ctx.kind == "media" and self._skip_media:
            _log.info("Skipping %s: media playing (%s)", wav_path.name, seg_ctx.app)
            if self._health is not None:
                self._health.record_context("media", skipped=True)
            if not self._keep_audio:
                wav_path.unlink(missing_ok=True)
            return

        t0 = time.monotonic()

        # Try local transcription, with retry/fallback policy
        consecutive_local = 0
        result = None
        used_fallback = False

        while True:
            try:
                result = self._transcriber.transcribe(
                    audio,
                    language=self._language,
                    initial_prompt=self._initial_prompt,
                )
                break  # success
            except TranscriptionError as exc:
                consecutive_local += 1
                _log.warning(
                    "Local transcription failed (%d/%d) for %s: %s",
                    consecutive_local,
                    self._max_local_failures,
                    wav_path.name,
                    exc,
                )

                if consecutive_local < self._max_local_failures:
                    # Brief sleep between retries to avoid a tight spin on repeated failures.
                    time.sleep(0.05)
                    continue

                # max_local_failures reached — try fallback or give up
                if self._fallback is not None:
                    try:
                        result = self._fallback.transcribe(
                            audio,
                            language=self._language,
                            initial_prompt=self._initial_prompt,
                        )
                        used_fallback = True
                        break
                    except TranscriptionError as api_exc:
                        _log.error("API fallback failed for %s: %s", wav_path.name, api_exc)
                        self._error(f"API fallback failed: {api_exc}")
                        _log.info("Backing off %.0f s after fallback failure", self._backoff_s)
                        # Sleep in small chunks so stop event is honoured
                        chunk = min(0.05, self._backoff_s)
                        elapsed = 0.0
                        while elapsed < self._backoff_s and not self._stop_event.is_set():
                            time.sleep(chunk)
                            elapsed += chunk
                        return
                else:
                    # No fallback — leave WAV in spool, increment errors (AC-7)
                    _log.error("No fallback configured; leaving %s in spool", wav_path.name)
                    self._error(f"local transcription failed: {exc}")
                    return
            except Exception as exc:
                # Unexpected exception (numpy/mlx panic, MemoryError, etc.) — log,
                # skip this segment, and let the worker continue.  A daemon thread
                # that dies silently (F-4) leaves the menubar showing ● while nothing
                # transcribes for the rest of the process lifetime.
                _log.exception(
                    "Unexpected error transcribing %s (skipping segment): %s",
                    wav_path.name,
                    exc,
                )
                self._error(f"unexpected: {exc!r}")
                return

        if result is None:
            return
        if self._health is not None:
            self._health.record_success()

        wall_s = time.monotonic() - t0
        rtf = audio_s / wall_s if wall_s > 0 else 0.0
        _log.info(
            "Transcribed %s: %.1f s audio in %.1f s wall (RTF %.2fx)%s",
            wav_path.name,
            audio_s,
            wall_s,
            rtf,
            " [fallback]" if used_fallback else "",
        )

        # One entry per speaker turn when speaker ID is on and the engine gave
        # segment timings; otherwise the whole clip is one entry as before.
        turns = None
        if self._speakers is not None:
            turns = self._speakers.turns(
                audio,
                start_at.timestamp(),
                result.segments,
                seg_ctx.kind if seg_ctx is not None else None,
            )
        if turns is not None:
            entries = [
                (start_at + timedelta(seconds=t.offset_s), t.text, _marker(seg_ctx, t.speaker))
                for t in turns
            ]
        else:
            text = clean_result(result)
            entries = [(start_at, text, _marker(seg_ctx, None))] if text else []

        if entries:
            try:
                for at, text, marker in entries:
                    self._writer.append(at, text, context=marker)
            except OSError as exc:
                _log.error("Failed to write transcript: %s", exc)
                self._error(f"transcript write failed: {exc}")
                return
            if seg_ctx is not None and self._health is not None:
                self._health.record_context(seg_ctx.kind)
        else:
            _log.debug("Guard dropped all segments for %s", wav_path.name)
        if self._speakers is not None:
            self._speakers.maintenance()

        # Delete WAV unless keep_audio (AC-1/AC-2)
        if not self._keep_audio:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("Could not delete %s: %s", wav_path.name, exc)

    def _error(self, message: str) -> None:
        """Count a failure on the status board and, if enabled, the heartbeat."""
        self._status.increment_errors()
        if self._health is not None:
            self._health.record_error(message)
