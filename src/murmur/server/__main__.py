"""Entry point for `python -m murmur.server`.

Startup sequence:
1. Load server.toml config (defaults if missing/malformed — AC-16).
2. Retrieve bearer token from env or Keychain (E-18: fail if absent).
3. Acquire single-instance PID-file lock (OQ-8 belt).
4. Start background thread to load the MLX model (sets model_loaded_flag).
5. Bind uvicorn to 127.0.0.1:<port> and serve (R-8).

Security notes:
- Token never logged (AC-17, R-7).
- Server binds to loopback only; TLS is handled by tailscale serve (R-8, R-9).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI

from murmur.server.config import ServerConfig
from murmur.transcribe.base import TranscriptionResult
from murmur.transcribe.healing import SelfHealingTranscriber
from murmur.transcribe.isolated import SubprocessTranscriber, default_memory_limit_bytes
from murmur.transcribe.models import ModelManager

_log = logging.getLogger(__name__)

_TOKEN_ABSENT_MSG = (
    "Bearer token not configured. "
    "Set MURMUR_SERVER_TOKEN env var or add to Keychain."
)

_LOOPBACK_ADDRS: frozenset[str] = frozenset({"127.0.0.1", "::1"})
_HOST_REJECT_MSG = (
    "server.toml: host must be 127.0.0.1 or ::1 (R-8). "
    "Refusing to bind to non-loopback interface."
)


def main() -> None:
    """Start the Murmur server."""
    import uvicorn

    from murmur.lock import AlreadyRunningError, SingleInstanceLock
    from murmur.server.app import create_app
    from murmur.server.config import load_server_config
    from murmur.server.launchd_server import SERVER_PID_FILE
    from murmur.server.token import get_server_token

    # ------------------------------------------------------------------
    # 1. Config
    # ------------------------------------------------------------------
    config_path = Path(
        os.environ.get(
            "MURMUR_SERVER_CONFIG",
            "~/Library/Application Support/Murmur/server.toml",
        )
    ).expanduser()

    config, config_warnings = load_server_config(config_path)

    # Configure logging early so warnings reach the log
    _configure_logging(config.server.log_path)

    for warning in config_warnings:
        _log.warning(warning)

    # ------------------------------------------------------------------
    # 2. Bearer token (must be present — E-18)
    # ------------------------------------------------------------------
    token = get_server_token(os.environ)
    if not token:
        _log.critical(_TOKEN_ABSENT_MSG)
        print(_TOKEN_ABSENT_MSG, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2b. Validate host is loopback-only (R-8, F-10, SR-2)
    # ------------------------------------------------------------------
    if config.server.host not in _LOOPBACK_ADDRS:
        _log.critical(_HOST_REJECT_MSG)
        print(_HOST_REJECT_MSG, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 3. Single-instance lock (OQ-8 belt)
    # ------------------------------------------------------------------
    lock = SingleInstanceLock(SERVER_PID_FILE)
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        msg = f"Murmur server is already running (PID: {exc.pid}). Exiting."
        _log.error(msg)
        print(msg, file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 4. Build app with placeholder transcriber; background thread wires
    #    the real transcriber once the model snapshot is resolved.
    # ------------------------------------------------------------------
    model_loaded_flag = threading.Event()
    app = create_app(
        _PlaceholderTranscriber(),
        token,
        config,
        model_loaded_flag=model_loaded_flag,
    )

    threading.Thread(
        target=_build_and_wire_transcriber,
        args=(app, config, model_loaded_flag),
        daemon=True,
    ).start()

    # ------------------------------------------------------------------
    # 5. Serve (loopback only — R-8)
    # ------------------------------------------------------------------
    _log.info(
        "Starting Murmur server on %s:%d",
        config.server.host,
        config.server.port,
    )
    try:
        uvicorn.run(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level="info",
        )
    finally:
        # Tear down the transcription subprocess (if the real one was wired)
        # so no child outlives the server across a launchd restart.
        close = getattr(getattr(app.state, "transcriber", None), "close", None)
        if callable(close):
            close()
        lock.release()


class _PlaceholderTranscriber:
    """Not-ready transcriber used only while the model is loading.

    model_loaded_flag gates /transcribe so this method is never called in
    normal operation.  It raises unconditionally to make any accidental call
    fail loudly rather than silently.
    """

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        raise RuntimeError(
            "Model not yet loaded — transcription request reached the placeholder "
            "transcriber, which should not happen while model_loaded_flag is unset."
        )


def _build_and_wire_transcriber(
    app: FastAPI,
    config: ServerConfig,
    model_loaded_flag: threading.Event,
) -> None:
    """Resolve the model snapshot, build MlxWhisperTranscriber, wire it into
    app.state, then set model_loaded_flag.

    Runs in a daemon thread so uvicorn can start immediately.  /health remains
    responsive during load; /transcribe returns 503 model_not_loaded until the
    flag is set.

    On non-Apple-Silicon or any other failure: logs the error and leaves the
    flag unset so the server degrades gracefully (503 on /transcribe,
    model_loaded:false on /health) rather than crashing.
    """
    try:
        # Resolve the logical model name (e.g. "large-v3") to the local
        # snapshot directory that mlx_whisper actually accepts as
        # path_or_hf_repo.  Using the logical name directly causes mlx to
        # attempt a HuggingFace lookup for a repo literally named "large-v3",
        # which fails with "Repository Not Found" (live bug).
        manager = ModelManager()
        mem_limit = default_memory_limit_bytes()

        def _ensure(model: str) -> str:
            ref = manager.ensure(model)
            _log.info("Model ready: %s", ref)
            return ref

        # Transcribe out-of-process: the MLX model would otherwise grow its
        # unmanaged Metal buffer cache to the peak working set and pin several GB
        # in this always-on server for its whole lifetime. SubprocessTranscriber
        # loads the model in a child that is torn down when idle, so memory is
        # reclaimed between requests (see murmur.transcribe.isolated).
        #
        # SelfHealingTranscriber re-downloads the model and retries if its files
        # vanish at runtime (e.g. a cache sweep), so this long-running server
        # recovers on its own instead of failing every request until restart.
        transcriber = SelfHealingTranscriber(
            model_name=config.stt.model,
            ensure=_ensure,
            build_inner=lambda ref: SubprocessTranscriber(ref, memory_limit_bytes=mem_limit),
        )
        app.state.transcriber = transcriber
        model_loaded_flag.set()
    except Exception as exc:
        _log.error("Model load failed: %s", exc)
        # model_loaded_flag stays unset; /health returns model_loaded:false
        # and /transcribe returns 503 model_not_loaded.


def _configure_logging(log_path_str: str) -> None:
    """Configure root logging to the specified path and stderr."""
    log_path = Path(log_path_str).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        print(f"Warning: could not open log file {log_path}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


if __name__ == "__main__":
    main()
