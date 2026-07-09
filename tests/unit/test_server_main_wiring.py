"""Regression test for the live deploy bug: transcriber must be built from the
RESOLVED snapshot path returned by ModelManager.ensure(), NOT from the logical
model name (e.g. "large-v3").

Bug: __main__.py previously constructed the transcriber with config.stt.model
where config.stt.model == "large-v3".  mlx_whisper then tried to fetch a
HuggingFace repo literally named "large-v3" and failed with
  "Repository Not Found for url: .../api/models/large-v3/revision/main"
because "large-v3" is not a valid HF repo slug.

Fix: _build_and_wire_transcriber() calls ModelManager().ensure() to resolve the
logical name to a local snapshot path, constructs the transcriber
(SubprocessTranscriber) with THAT path, assigns it to app.state.transcriber, and
only then sets model_loaded_flag.
"""

from __future__ import annotations

import io
import threading
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from murmur.server import __main__ as server_main
from murmur.server.app import create_app
from murmur.server.config import ServerConfig

_SENTINEL_PATH = "/tmp/snap/large-v3"
_TOKEN = "wiring-test-token"


# ---------------------------------------------------------------------------
# Helper: minimal FastAPI app whose state.transcriber we can inspect
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    flag = threading.Event()
    return create_app(
        server_main._PlaceholderTranscriber(),
        _TOKEN,
        ServerConfig(),
        model_loaded_flag=flag,
    )


# ---------------------------------------------------------------------------
# Core regression: resolved path, not logical name, is passed to transcriber
# ---------------------------------------------------------------------------


def test_transcriber_built_from_resolved_snapshot_path__deploy_bug() -> None:
    """_build_and_wire_transcriber passes the ModelManager.ensure() result to
    the transcriber, NOT config.stt.model (the logical name).

    Asserts model_ref captured by the fake constructor == SENTINEL_PATH.
    """
    flag = threading.Event()
    app = _make_app()

    captured: dict[str, Any] = {}

    class _FakeTranscriber:
        def __init__(self, model_ref: str, **kwargs: object) -> None:
            captured["model_ref"] = model_ref

        def transcribe(self, audio: object, *, language: str, initial_prompt: object) -> object:
            raise NotImplementedError

    with (
        patch(
            "murmur.server.__main__.ModelManager",
            autospec=True,
        ) as mock_manager,
        patch(
            "murmur.server.__main__.SubprocessTranscriber",
            _FakeTranscriber,
        ),
    ):
        mock_manager.return_value.ensure.return_value = _SENTINEL_PATH

        server_main._build_and_wire_transcriber(app, ServerConfig(), flag)

    # The transcriber must have been constructed with the RESOLVED path
    assert captured.get("model_ref") == _SENTINEL_PATH, (
        f"transcriber was built with {captured.get('model_ref')!r} "
        f"instead of the resolved snapshot path {_SENTINEL_PATH!r}. "
        "The live bug (logical name passed directly) is still present."
    )


def test_transcriber_not_built_with_logical_model_name__deploy_bug() -> None:
    """Complementary assertion: the logical name 'large-v3' must NOT be used
    as the model_ref argument (that is what caused the live failure).
    """
    flag = threading.Event()
    app = _make_app()

    captured: dict[str, Any] = {}

    class _FakeTranscriber:
        def __init__(self, model_ref: str, **kwargs: object) -> None:
            captured["model_ref"] = model_ref

        def transcribe(self, audio: object, *, language: str, initial_prompt: object) -> object:
            raise NotImplementedError

    config = ServerConfig()
    logical_name = config.stt.model  # e.g. "large-v3"

    with (
        patch("murmur.server.__main__.ModelManager", autospec=True) as mock_manager,
        patch("murmur.server.__main__.SubprocessTranscriber", _FakeTranscriber),
    ):
        mock_manager.return_value.ensure.return_value = _SENTINEL_PATH

        server_main._build_and_wire_transcriber(app, config, flag)

    assert captured.get("model_ref") != logical_name, (
        f"transcriber was built with the logical model name "
        f"{logical_name!r} — this reproduces the live HuggingFace 404 bug."
    )


# ---------------------------------------------------------------------------
# Post-wire state: app.state.transcriber is replaced + flag is set
# ---------------------------------------------------------------------------


def test_app_state_transcriber_replaced_and_flag_set_after_wire() -> None:
    """After _build_and_wire_transcriber completes successfully:
    - app.state.transcriber is the real (resolved) transcriber instance.
    - model_loaded_flag.is_set() is True.
    """
    flag = threading.Event()
    app = _make_app()

    placeholder = app.state.transcriber

    class _FakeTranscriber:
        def __init__(self, model_ref: str, **kwargs: object) -> None:
            self.model_ref = model_ref

        def transcribe(self, audio: object, *, language: str, initial_prompt: object) -> object:
            raise NotImplementedError

    with (
        patch("murmur.server.__main__.ModelManager", autospec=True) as mock_manager,
        patch("murmur.server.__main__.SubprocessTranscriber", _FakeTranscriber),
    ):
        mock_manager.return_value.ensure.return_value = _SENTINEL_PATH

        server_main._build_and_wire_transcriber(app, ServerConfig(), flag)

    assert flag.is_set(), "model_loaded_flag must be set after successful wire"
    assert app.state.transcriber is not placeholder, (
        "app.state.transcriber must be replaced (still pointing at placeholder)"
    )
    assert isinstance(app.state.transcriber, _FakeTranscriber), (
        "app.state.transcriber must be the real transcriber after wiring"
    )
    assert app.state.transcriber.model_ref == _SENTINEL_PATH


# ---------------------------------------------------------------------------
# Pre-load gate: /transcribe returns 503 model_not_loaded before flag is set
# ---------------------------------------------------------------------------


def test_transcribe_returns_503_before_model_loaded__gated() -> None:
    """/transcribe must return 503 model_not_loaded when model_loaded_flag is
    not yet set (the flag is what _build_and_wire_transcriber sets last).
    """
    flag = threading.Event()  # NOT set — simulates pre-load state
    app = create_app(
        server_main._PlaceholderTranscriber(),
        _TOKEN,
        ServerConfig(),
        model_loaded_flag=flag,
    )
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/transcribe",
        headers={"Authorization": f"Bearer {_TOKEN}"},
        files={"file": ("audio.m4a", io.BytesIO(b"\x00" * 16), "audio/m4a")},
    )

    assert resp.status_code == 503, (
        f"Expected 503 before model loaded, got {resp.status_code}"
    )
    assert resp.json()["error_code"] == "model_not_loaded", (
        f"Wrong error_code: {resp.json()}"
    )


# ---------------------------------------------------------------------------
# Failure path: ensure() raises → flag stays unset, no crash
# ---------------------------------------------------------------------------


def test_model_load_failure_leaves_flag_unset_no_crash() -> None:
    """If ModelManager.ensure() raises, _build_and_wire_transcriber must:
    - NOT crash (no exception propagates out of the helper).
    - Leave model_loaded_flag unset.
    - Leave app.state.transcriber as the placeholder.
    """
    flag = threading.Event()
    app = _make_app()
    placeholder = app.state.transcriber

    with patch("murmur.server.__main__.ModelManager", autospec=True) as mock_manager:
        mock_manager.return_value.ensure.side_effect = RuntimeError(
            "No Apple Silicon — mlx not available"
        )

        # Must not raise
        server_main._build_and_wire_transcriber(app, ServerConfig(), flag)

    assert not flag.is_set(), "Flag must stay unset when model load fails"
    assert app.state.transcriber is placeholder, (
        "Placeholder transcriber must not be replaced when model load fails"
    )


# ---------------------------------------------------------------------------
# Transcriber construction failure → flag stays unset, no crash
# ---------------------------------------------------------------------------


def test_transcriber_construction_failure_leaves_flag_unset_no_crash() -> None:
    """If the transcriber constructor raises (e.g. non-Apple-Silicon),
    the flag stays unset and no exception escapes the helper.
    """
    flag = threading.Event()
    app = _make_app()

    def _raising_init(model_ref: str, **kwargs: object) -> None:
        raise RuntimeError("murmur requires Apple Silicon (arm64 macOS)")

    with (
        patch("murmur.server.__main__.ModelManager", autospec=True) as mock_manager,
        patch(
            "murmur.server.__main__.SubprocessTranscriber",
            side_effect=_raising_init,
        ),
    ):
        mock_manager.return_value.ensure.return_value = _SENTINEL_PATH

        server_main._build_and_wire_transcriber(app, ServerConfig(), flag)

    assert not flag.is_set(), "Flag must stay unset when transcriber construction fails"
