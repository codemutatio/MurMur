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
    # 4. Background model load (sets flag; /health reads flag non-blocking)
    # ------------------------------------------------------------------
    model_loaded_flag = threading.Event()

    def _load_model() -> None:
        try:
            from murmur.transcribe.models import ModelManager

            manager = ModelManager()
            model_ref = manager.ensure(config.stt.model)
            _log.info("Model ready: %s", model_ref)
            model_loaded_flag.set()
        except Exception as exc:
            _log.error("Model load failed: %s", exc)
            # model_loaded_flag stays unset; /health returns model_loaded: false.

    threading.Thread(target=_load_model, daemon=True).start()

    # ------------------------------------------------------------------
    # 5. Build app with production transcriber
    # ------------------------------------------------------------------
    from murmur.transcribe.local import MlxWhisperTranscriber

    # The transcriber is constructed lazily once the model flag is set.
    # For the app factory we pass a placeholder that won't be called until
    # the model is loaded (model_loaded_flag guards Step 3 in the endpoint).
    try:
        transcriber = MlxWhisperTranscriber(config.stt.model)
    except RuntimeError as exc:
        _log.warning("Transcriber construction deferred: %s", exc)
        # On non-Apple-Silicon or CI — still create a stub so /health works.
        import numpy as np

        from murmur.transcribe.base import TranscriptionResult

        class _StubTranscriber:
            def transcribe(
                self,
                audio: np.ndarray,
                *,
                language: str,
                initial_prompt: str | None,
            ) -> TranscriptionResult:
                raise RuntimeError("Transcriber not available on this platform.")

        transcriber = _StubTranscriber()  # type: ignore[assignment]

    app = create_app(
        transcriber,
        token,
        config,
        model_loaded_flag=model_loaded_flag,
    )

    # ------------------------------------------------------------------
    # 6. Serve (loopback only — R-8)
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
        lock.release()


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
