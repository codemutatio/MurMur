"""FastAPI application factory for the Murmur server.

Creates a FastAPI app with:
  - GET /health  — auth-gated, never acquires the transcription lock (AC-1..3, AC-13)
  - POST /transcribe — auth-gated, single-flight asyncio.Lock (AC-4..18)

The app is constructed with injected dependencies (Transcriber, server token, config)
so tests can inject FakeTranscriber without production test-conditionals (R-14).

Blocking transcription runs in a threadpool via run_in_executor to keep the
event loop free for concurrent /health requests (ADR-2, AC-13, Risks note).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import numpy as np
from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from murmur.server.audio import AudioDecodeError, decode_to_pcm
from murmur.server.config import ServerConfig
from murmur.server.token import verify_token
from murmur.transcribe.base import Transcriber, TranscriptionError
from murmur.transcribe.guard import clean_result
from murmur.transcribe.prompt import build_initial_prompt

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Error-code registry (frozen per §A / ADR-8)
# ---------------------------------------------------------------------------

_E_UNAUTHORIZED = "unauthorized"
_E_MODEL_BUSY = "model_busy"
_E_MODEL_NOT_LOADED = "model_not_loaded"
_E_MISSING_FILE = "missing_file"
_E_EMPTY_FILE = "empty_file"
_E_FILE_TOO_LARGE = "file_too_large"
_E_UNSUPPORTED_MEDIA = "unsupported_media_type"
_E_AUDIO_DECODE_FAILED = "audio_decode_failed"
_E_TRANSCRIPTION_FAILED = "transcription_failed"
_E_NO_SPEECH = "no_speech_detected"

# ---------------------------------------------------------------------------
# Accepted extensions and MIME types (AC-10, E-5, E-6)
# ---------------------------------------------------------------------------

_ACCEPTED_EXTENSIONS: frozenset[str] = frozenset(
    {"m4a", "aac", "wav", "mp3", "ogg", "opus"}
)
_ACCEPTED_MIMES: frozenset[str] = frozenset(
    {
        "audio/m4a",
        "audio/aac",
        "audio/x-aac",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/opus",
        "audio/x-opus",
        "audio/webm",
        "video/mp4",       # some clients send m4a as video/mp4
        "audio/mp4",
    }
)

# ---------------------------------------------------------------------------
# Token redaction log filter
# ---------------------------------------------------------------------------


class _TokenRedactFilter(logging.Filter):
    """Replace the bearer token string in log records with [REDACTED] (AC-17)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        # Operate on getMessage() (which expands %s args) so the token in args is caught,
        # then store the fully-formatted+redacted string in msg and clear args (CR-10).
        formatted = record.getMessage()
        if self._token in formatted:
            record.msg = formatted.replace(self._token, "[REDACTED]")
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    transcriber: Transcriber,
    token: str,
    config: ServerConfig,
    *,
    model_loaded_flag: threading.Event,
    decode_fn: Callable[[Path], np.ndarray] | None = None,
) -> FastAPI:
    """Create and return the configured FastAPI application.

    Args:
        transcriber:       The transcription engine (injected; FakeTranscriber in tests).
        token:             The bearer token for auth (never logged).
        config:            Loaded ServerConfig.
        model_loaded_flag: threading.Event set when the model is ready.
                           /health reads this non-blocking; no lock involved.
        decode_fn:         Injectable audio-decode function (default: decode_to_pcm).
                           Tests inject a mock to avoid ffmpeg.

    Returns:
        A FastAPI application instance.
    """
    if decode_fn is None:
        decode_fn = decode_to_pcm

    app = FastAPI(title="Murmur Server", docs_url=None, redoc_url=None)

    # Single-flight lock: one transcription at a time (ADR-2, AC-14)
    _lock = asyncio.Lock()

    # Attach token redaction filter to the root logger (AC-17)
    if token:
        _filter = _TokenRedactFilter(token)
        logging.getLogger().addFilter(_filter)

    # Store state in app.state for access in route handlers
    app.state.token = token
    app.state.config = config
    app.state.transcriber = transcriber
    app.state.model_loaded_flag = model_loaded_flag
    app.state.lock = _lock
    app.state.decode_fn = decode_fn

    # -----------------------------------------------------------------------
    # GET /health
    # -----------------------------------------------------------------------

    @app.get("/health")
    async def health(request: Request) -> Response:
        """Return server health. Never acquires the model lock (AC-1, AC-3, AC-13)."""
        auth = request.headers.get("Authorization", "")
        if not _check_auth(auth, app.state.token):
            return _error(401, _E_UNAUTHORIZED, "Invalid or missing bearer token")

        model_loaded = app.state.model_loaded_flag.is_set()
        try:
            pkg_version = version("murmur")
        except Exception:
            pkg_version = "1.0.0"

        return JSONResponse(
            {
                "status": "ok",
                "model_loaded": model_loaded,
                "model": app.state.config.stt.model,
                "version": pkg_version,
            }
        )

    # -----------------------------------------------------------------------
    # POST /transcribe
    # -----------------------------------------------------------------------

    @app.post("/transcribe")
    async def transcribe(
        request: Request,
        file: Annotated[UploadFile | None, File()] = None,
        language: Annotated[str | None, Form()] = None,
        vocab_hints: Annotated[str | None, Form()] = None,
    ) -> Response:
        """Transcribe uploaded audio. 11-step validation order per §A."""
        # Step 1: Auth (AC-9, E-12..E-14)
        auth = request.headers.get("Authorization", "")
        if not _check_auth(auth, app.state.token):
            return _error(401, _E_UNAUTHORIZED, "Invalid or missing bearer token")

        lock: asyncio.Lock = app.state.lock

        # Step 2: Single-flight busy-check (§A step 2 — must precede the model-loaded
        # check at step 3). This is a SYNCHRONOUS read of lock.locked(); no await is
        # issued before or between this check and the later atomic acquire, so the
        # asyncio event loop cannot yield between them. Both conditions can be checked
        # in the correct §A order without a TOCTOU race because asyncio is
        # single-threaded: no other coroutine can run between synchronous statements.
        if lock.locked():
            return _error(503, _E_MODEL_BUSY, "A transcription is already in progress")

        # Step 3: Model not yet loaded (E-20, OQ-7)
        if not app.state.model_loaded_flag.is_set():
            return _error(503, _E_MODEL_NOT_LOADED, "Model is not yet loaded")

        # Step 4: file field absent (E-42)
        if file is None:
            return _error(400, _E_MISSING_FILE, "No file field in request")

        # Step 5: file zero bytes (E-43) — read first byte to check
        first_chunk = await file.read(1)
        if not first_chunk:
            return _error(400, _E_EMPTY_FILE, "Uploaded file is empty")

        # Read remaining bytes for size check
        max_bytes = int(app.state.config.server.max_upload_mb) * 1024 * 1024
        body = bytearray(first_chunk)
        while True:
            chunk = await file.read(65536)
            if not chunk:
                break
            body.extend(chunk)
            # Step 6: size > limit (AC-8, E-1, E-2) — checked incrementally
            if len(body) > max_bytes:
                return _error(
                    413,
                    _E_FILE_TOO_LARGE,
                    f"File exceeds maximum upload size of "
                    f"{app.state.config.server.max_upload_mb} MB",
                )

        # Step 7: MIME / extension check (AC-10, E-5, E-6)
        filename: str = file.filename or ""
        content_type_file = file.content_type or ""
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        ext_ok = ext in _ACCEPTED_EXTENSIONS
        mime_ok = any(content_type_file.startswith(m) for m in _ACCEPTED_MIMES)
        # E-5: application/octet-stream accepted when the file extension is valid
        octet_stream_ok = content_type_file == "application/octet-stream" and ext_ok

        if not (ext_ok or mime_ok or octet_stream_ok):
            return _error(
                415,
                _E_UNSUPPORTED_MEDIA,
                f"Unsupported media type: {content_type_file!r} (extension: {ext!r}). "
                f"Accepted: m4a, aac, wav, mp3, ogg, opus",
            )

        # Resolve language and vocab_hints before acquiring the lock
        resolved_language = (language or app.state.config.stt.language or "en").strip()
        resolved_hints = _resolve_hints(vocab_hints, app.state.config)

        # Steps 8+: Atomic single-flight acquire (AC-14, E-10, ADR-2).
        # All file I/O awaits are completed above.  The lock.locked() check at step 2
        # above and the async with lock: acquisition below have NO intervening await
        # (the model-not-loaded and file checks at steps 3-7 are all synchronous or
        # already-completed awaits), so the asyncio event loop cannot yield to another
        # coroutine between the early busy-check and the acquire.  The early check at
        # step 2 preserves the §A ordering contract (busy before model-not-loaded).

        # Step 8+: acquire lock → write temp file → decode → transcribe → clean
        async with lock:
            return await _run_transcription(
                app=app,
                audio_bytes=bytes(body),
                filename=filename,
                language=resolved_language,
                hints=resolved_hints,
            )

    async def _run_transcription(
        app: FastAPI,
        audio_bytes: bytes,
        filename: str,
        language: str,
        hints: list[str],
    ) -> Response:
        """Inner transcription logic — called only when the lock is held."""
        # SR-3: Strip directory components from client-supplied filename to prevent
        # mkstemp raising FileNotFoundError on filenames containing '/' characters.
        safe_name = Path(filename).name if filename else ""
        suffix = f"_{safe_name}" if safe_name else ".audio"
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=suffix)
        tmp_path = Path(tmp_path_str)

        try:
            # Write audio to temp file (AC-18: file exists from here → deleted in finally)
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(audio_bytes)

            # Step 8: ffmpeg decode (AC-5, AC-11, E-4, E-19)
            decode: Callable[[Path], np.ndarray] = app.state.decode_fn
            loop = asyncio.get_event_loop()
            try:
                pcm: np.ndarray = await loop.run_in_executor(
                    None, lambda: decode(tmp_path)
                )
            except AudioDecodeError as exc:
                _log.warning("Audio decode failed: %s", exc)
                return _error(422, _E_AUDIO_DECODE_FAILED, str(exc))

            # Step 9: Transcribe (AC-5, AC-6)
            initial_prompt = build_initial_prompt([h for h in hints if h.strip()])

            transcriber: Transcriber = app.state.transcriber
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: transcriber.transcribe(
                        pcm, language=language, initial_prompt=initial_prompt
                    ),
                )
            except TranscriptionError as exc:
                _log.warning("Transcription failed: %s", exc)
                return _error(422, _E_TRANSCRIPTION_FAILED, str(exc))

            # Step 10: Hallucination guard (AC-7, E-3)
            clean_text = clean_result(result)
            if clean_text is None:
                return _error(422, _E_NO_SPEECH, "No speech detected in audio")

            # Step 11 / success: build 200 body per §A
            duration_sec = len(pcm) / 16000.0
            segments = [
                {
                    "text": seg.text,
                    "no_speech_prob": seg.no_speech_prob,
                    "compression_ratio": seg.compression_ratio,
                    "avg_logprob": seg.avg_logprob,
                }
                for seg in result.segments
            ]
            return JSONResponse(
                {
                    "text": clean_text,
                    "segments": segments,
                    "language": language,
                    "duration_sec": duration_sec,
                    "model": app.state.config.stt.model,
                    "engine": "mac",
                }
            )

        finally:
            # AC-18: temp file always deleted regardless of success or error
            tmp_path.unlink(missing_ok=True)

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_hints(vocab_hints_raw: str | None, config: ServerConfig) -> list[str]:
    """Parse the vocab_hints form field; fall back to config defaults if absent (E-7, E-8).

    Accepts a comma-separated string.  Returns a list of stripped non-empty hints.
    """
    if vocab_hints_raw is not None:
        # Field was present — parse comma-separated list (E-7: empty/whitespace stripped)
        return [h.strip() for h in vocab_hints_raw.split(",")]
    # E-8: absent field → server defaults
    return list(config.stt.vocab_hints)


def _check_auth(authorization: str, expected_token: str) -> bool:
    """Validate 'Authorization: Bearer <token>' header (AC-2, AC-9, E-12..E-14)."""
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        return False
    provided = authorization[len(prefix):]
    if not provided:
        return False
    return verify_token(provided, expected_token)


def _error(status: int, error_code: str, detail: str) -> JSONResponse:
    """Return a uniform error JSON body per §A contract."""
    return JSONResponse(
        {"error_code": error_code, "detail": detail},
        status_code=status,
    )
