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
from pathlib import Path

from murmur.audio.spool import list_spool, load_wav, parse_segment_start
from murmur.control import StatusBoard
from murmur.output.writer import TranscriptWriter
from murmur.transcribe.base import Transcriber, TranscriptionError
from murmur.transcribe.guard import clean_result

_log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__(name="transcribe-worker", daemon=True)
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
        existing = list_spool(self._spool_dir)
        _log.info("Startup scan: %d spool files", len(existing))
        for wav_path in existing:
            if self._stop_event.is_set():
                break
            self._process(wav_path)

        self._backlog_drained.set()
        _log.info("Backlog drained; entering live loop")

        # Live loop: drain work_q until stop
        while not self._stop_event.is_set():
            try:
                wav_path = self._work_q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._process(wav_path)
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
            self._status.increment_errors()
            return

        start_at = parse_segment_start(wav_path)
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
                    # Retry local
                    continue

                # 3rd consecutive failure — try fallback or give up
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
                        self._status.increment_errors()
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
                    self._status.increment_errors()
                    return

        if result is None:
            return

        wall_s = time.monotonic() - t0
        audio_s = len(audio) / 16_000.0
        rtf = audio_s / wall_s if wall_s > 0 else 0.0
        _log.info(
            "Transcribed %s: %.1f s audio in %.1f s wall (RTF %.2fx)%s",
            wav_path.name,
            audio_s,
            wall_s,
            rtf,
            " [fallback]" if used_fallback else "",
        )

        text = clean_result(result)
        if text:
            try:
                self._writer.append(start_at, text)
            except OSError as exc:
                _log.error("Failed to write transcript: %s", exc)
                self._status.increment_errors()
                return
        else:
            _log.debug("Guard dropped all segments for %s", wav_path.name)

        # Delete WAV unless keep_audio (AC-1/AC-2)
        if not self._keep_audio:
            try:
                wav_path.unlink(missing_ok=True)
            except OSError as exc:
                _log.warning("Could not delete %s: %s", wav_path.name, exc)
