"""Capture engine thread.

Owns: InputStream lifecycle, device poll, watchdog, disk gate, mic-permission
gate, Silero VAD + Segmenter + spool WAV writes.

Gate ordering (checked each tick, in priority order):
  1. Paused → ensure closed, icon ⏸
  2. Mic permission ≠ GRANTED → ensure closed, icon ⚠ + one-shot notification
  3. Disk < 2 GiB → flush, close, icon ⚠ + one-shot notification
  4. All clear → open stream if not open, icon ●
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from murmur.audio import (
    DEVICE_POLL_S,
    DISK_MIN_FREE_BYTES,
    DISK_POLL_S,
    PERMISSION_RECHECK_S,
    WATCHDOG_STALL_S,
)
from murmur.audio.segmenter import Segmenter
from murmur.audio.source import AudioSource, AudioSourceError
from murmur.audio.spool import write_segment_wav
from murmur.control import Controller, IconState, StatusBoard
from murmur.mac.permissions import PermissionStatus, request_microphone_access

_log = logging.getLogger(__name__)


class CaptureEngine(threading.Thread):
    """Capture-engine thread: manages audio stream lifecycle and segment production."""

    def __init__(
        self,
        *,
        source: AudioSource,
        segmenter: Segmenter,
        spool_dir: Path,
        work_q: queue.Queue[Path],
        controller: Controller,
        status: StatusBoard,
        notifier: object,  # Notifier protocol
        mic_permission: Callable[[], PermissionStatus],
        disk_free: Callable[[], int],
        now: Callable[[], datetime],
        backlog_drained: threading.Event,
        stop: threading.Event,
        device: str = "default",
        request_mic_access: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="capture-engine", daemon=True)
        self._source = source
        self._segmenter = segmenter
        self._spool_dir = spool_dir
        self._work_q = work_q
        self._controller = controller
        self._status = status
        self._notifier = notifier
        self._mic_permission = mic_permission
        # Triggers the macOS TCC microphone prompt when status is UNDETERMINED.
        # Injected for tests; defaults to the real AVFoundation request.
        self._request_mic_access = request_mic_access or request_microphone_access
        self._disk_free = disk_free
        self._now = now
        self._backlog_drained = backlog_drained
        self._stop_event = stop
        self._device = device

    def run(self) -> None:
        # Wait for the backlog to drain before opening the mic stream (R-16/E-6)
        _log.info("Capture engine waiting for backlog_drained ...")
        self._backlog_drained.wait()
        _log.info("Capture engine starting")

        stream_open = False
        last_device_sig = ""
        last_frame_time = 0.0
        last_device_check = 0.0
        last_disk_check = 0.0
        last_perm_check = 0.0

        # One-shot notification flags (avoid notification storms)
        notified_permission = False
        notified_disk = False
        # Fire the TCC permission prompt at most once per blocked stretch.
        requested_mic_access = False

        while not self._stop_event.is_set():
            try:
                now_mono = time.monotonic()

                # --- Gate 1: Paused ---
                if self._controller.effective_paused():
                    if stream_open:
                        self._flush_and_close()
                        stream_open = False
                        last_device_sig = ""
                    self._status.set_icon(IconState.PAUSED)
                    self._stop_event.wait(timeout=0.5)
                    continue

                # --- Gate 2: Mic permission ---
                if now_mono - last_perm_check >= PERMISSION_RECHECK_S or not stream_open:
                    perm = self._mic_permission()
                    last_perm_check = now_mono
                    if perm != PermissionStatus.GRANTED:
                        if stream_open:
                            self._flush_and_close()
                            stream_open = False
                        self._status.set_icon(IconState.ERROR)
                        # UNDETERMINED means no TCC decision has ever been
                        # recorded — the macOS prompt has never been shown, so
                        # Murmur never appears in System Settings → Microphone.
                        # Trigger the prompt once; without this the gate would
                        # block forever waiting on a permission nothing requests.
                        if perm == PermissionStatus.UNDETERMINED and not requested_mic_access:
                            self._request_mic_access()
                            requested_mic_access = True
                        if not notified_permission:
                            if perm == PermissionStatus.UNDETERMINED:
                                _log.warning(
                                    "Microphone permission not yet granted; requested "
                                    "access. Approve the macOS prompt, or grant it in "
                                    "System Settings → Privacy & Security → Microphone. "
                                    "Capture is blocked until granted."
                                )
                            else:  # DENIED / RESTRICTED — cannot re-prompt
                                _log.warning(
                                    "Microphone permission denied; capture is blocked. "
                                    "Enable it in System Settings → Privacy & Security "
                                    "→ Microphone, then restart Murmur."
                                )
                            self._notifier.notify(  # type: ignore[attr-defined]
                                "Murmur",
                                "Microphone permission needed. "
                                "Open System Settings → Privacy & Security → Microphone.",
                            )
                            notified_permission = True
                        self._stop_event.wait(timeout=0.5)
                        continue
                    if notified_permission:
                        _log.info("Microphone permission granted; resuming capture.")
                    notified_permission = False  # reset on grant
                    requested_mic_access = False  # reset on grant

                # --- Gate 3: Disk space ---
                if now_mono - last_disk_check >= DISK_POLL_S or not stream_open:
                    free = self._disk_free()
                    last_disk_check = now_mono
                    if free < DISK_MIN_FREE_BYTES:
                        if stream_open:
                            self._flush_and_close()
                            stream_open = False
                        self._status.set_icon(IconState.ERROR)
                        if not notified_disk:
                            self._notifier.notify(  # type: ignore[attr-defined]
                                "Murmur",
                                f"Disk space low ({free // (1024**3):.1f} GiB free). "
                                "Capture paused until space recovers above 2 GiB.",
                            )
                            notified_disk = True
                        self._stop_event.wait(timeout=0.5)
                        continue
                    if notified_disk:
                        # Space recovered
                        self._notifier.notify("Murmur", "Disk space recovered. Resuming capture.")  # type: ignore[attr-defined]
                    notified_disk = False  # reset on recovery

                # --- All gates clear: open stream if needed ---
                if not stream_open:
                    sig = self._source.default_device_signature()
                    last_device_sig = sig
                    try:
                        self._source.open(self._device)
                        stream_open = True
                        last_frame_time = time.monotonic()
                        self._status.set_icon(IconState.RECORDING)
                        self._status.set_notice(None)
                        _log.info("Stream opened on device signature: %r", sig)
                    except AudioSourceError as exc:
                        self._status.set_icon(IconState.ERROR)
                        self._status.set_notice("No input device")
                        _log.warning("Stream open failed: %s", exc)
                        self._stop_event.wait(timeout=5.0)
                        continue

                # --- Device poll ---
                if now_mono - last_device_check >= DEVICE_POLL_S:
                    last_device_check = now_mono
                    current_sig = self._source.default_device_signature()
                    if current_sig != last_device_sig:
                        _log.info(
                            "Device signature changed: %r → %r; reopening",
                            last_device_sig,
                            current_sig,
                        )
                        self._flush_and_close()
                        stream_open = False
                        last_device_sig = ""
                        continue

                # --- Read one frame ---
                frame = self._source.read(0.5)

                if frame is None:
                    # Timeout — check watchdog
                    stall_s = time.monotonic() - last_frame_time
                    if stall_s > WATCHDOG_STALL_S:
                        _log.warning("No frame for %.1f s; reopening stream (watchdog)", stall_s)
                        self._flush_and_close()
                        stream_open = False
                        last_device_sig = ""
                    continue

                last_frame_time = time.monotonic()

                # Feed to segmenter
                try:
                    segments = self._segmenter.push(frame)
                except Exception as exc:
                    _log.error("Segmenter error: %s", exc)
                    continue

                for seg in segments:
                    self._emit_segment(seg)

            except Exception as exc:
                # Outer guard (F-5): an unexpected exception from any of the above
                # (e.g. sd._terminate()/_initialize() raises during reopen, plan Risk 3)
                # must NOT kill the capture thread silently.  Log it, sleep briefly
                # to avoid a tight spin if the error is persistent, and continue.
                _log.exception(
                    "Unexpected exception in capture loop (continuing after brief sleep): %s",
                    exc,
                )
                time.sleep(1.0)

        # Shutdown
        if stream_open:
            self._flush_and_close()
        _log.info("Capture engine stopped")

    def _flush_and_close(self) -> None:
        """Flush any partial segment and close the stream."""
        seg = self._segmenter.flush()
        if seg is not None:
            self._emit_segment(seg)
        self._source.close()
        self._segmenter.reset_vad()

    def _emit_segment(self, seg: object) -> None:
        """Write segment to spool and enqueue for transcription."""
        from murmur.audio.segmenter import Segment

        assert isinstance(seg, Segment)
        try:
            wav_path = write_segment_wav(self._spool_dir, seg)
        except OSError as exc:
            _log.error("Spool write failed: %s; dropping segment", exc)
            self._status.increment_errors()
            self._notifier.notify("Murmur", f"Spool write error: {exc}")  # type: ignore[attr-defined]
            return

        # Non-blocking put (F-8): if the worker is dead or in backoff the queue may
        # be full.  The WAV file is already on disk, so dropping it from the in-process
        # queue is safe — the startup scan will pick it up on next launch.
        try:
            self._work_q.put_nowait(wav_path)
        except queue.Full:
            _log.warning(
                "work_q full; WAV %s stays in spool for startup-scan recovery",
                wav_path.name,
            )

        self._status.set_queue_depth(self._work_q.qsize())
