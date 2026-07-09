"""Tests for murmur.transcribe.isolated (SubprocessTranscriber).

These exercise the real socketpair IPC and process lifecycle without MLX or
Apple Silicon by pointing the transcriber at a fake worker that speaks the same
framed-pickle protocol. Mode is passed as an argv before the socket fd.
"""

from __future__ import annotations

import os
import socket
import sys
import time

import numpy as np
import pytest

from murmur.transcribe.base import TranscriptionError, TranscriptionResult
from murmur.transcribe.isolated import (
    SubprocessTranscriber,
    _child_env,
    _recv,
    _send,
    _worker_python,
    default_memory_limit_bytes,
)

# Fake worker: `python -c FAKE_WORKER <mode> <fd>`.
FAKE_WORKER = r"""
import sys, socket
from murmur.transcribe.isolated import _send, _recv
from murmur.transcribe.base import TranscriptionResult

mode = sys.argv[1]
sock = socket.socket(fileno=int(sys.argv[2]))
_recv(sock)  # init dict

if mode == "startup_fail":
    _send(sock, ("startup_error", "nope"))
    sys.exit(1)

_send(sock, ("ready", None))
if mode == "crash_after_ready":
    sys.exit(0)

while True:
    try:
        req = _recv(sock)
    except (OSError, EOFError):
        break
    if not req or req[0] == "shutdown":
        break
    if req[0] == "transcribe":
        if mode == "error":
            _send(sock, ("error", "boom"))
        elif mode == "die_on_request":
            sys.exit(0)
        else:
            lang = req[2]
            _send(sock, ("ok", TranscriptionResult(text="fake:" + lang, segments=())))
"""


def _make(mode: str, **kw: object) -> SubprocessTranscriber:
    kw.setdefault("idle_timeout_s", 999.0)
    return SubprocessTranscriber(
        "dummy-model",
        command=[sys.executable, "-c", FAKE_WORKER, mode],
        **kw,  # type: ignore[arg-type]
    )


AUDIO = np.zeros(1600, dtype=np.float32)


def test_frame_roundtrip() -> None:
    a, b = socket.socketpair()
    try:
        _send(a, ("ok", TranscriptionResult(text="hi", segments=())))
        kind, payload = _recv(b)  # type: ignore[misc]
        assert kind == "ok"
        assert payload.text == "hi"
    finally:
        a.close()
        b.close()


def test_ok_roundtrip() -> None:
    t = _make("ok")
    try:
        result = t.transcribe(AUDIO, language="en", initial_prompt=None)
        assert isinstance(result, TranscriptionResult)
        assert result.text == "fake:en"
    finally:
        t.close()


def test_error_reply_is_retryable_and_keeps_child() -> None:
    t = _make("error")
    try:
        with pytest.raises(TranscriptionError, match="boom"):
            t.transcribe(AUDIO, language="en", initial_prompt=None)
        # Child survives a per-segment error; a second call reuses it.
        assert t._proc is not None and t._proc.poll() is None
        with pytest.raises(TranscriptionError, match="boom"):
            t.transcribe(AUDIO, language="en", initial_prompt=None)
    finally:
        t.close()


def test_startup_failure_raises() -> None:
    t = _make("startup_fail")
    try:
        with pytest.raises(TranscriptionError, match="startup error|nope"):
            t.transcribe(AUDIO, language="en", initial_prompt=None)
    finally:
        t.close()


def test_dead_child_raises_then_respawns() -> None:
    t = _make("die_on_request")
    try:
        with pytest.raises(TranscriptionError):
            t.transcribe(AUDIO, language="en", initial_prompt=None)
        # A fresh child is spawned on the next call (which also dies → raises).
        with pytest.raises(TranscriptionError):
            t.transcribe(AUDIO, language="en", initial_prompt=None)
    finally:
        t.close()


def test_idle_reaper_terminates_child() -> None:
    t = _make("ok", idle_timeout_s=0.5)
    try:
        t.transcribe(AUDIO, language="en", initial_prompt=None)
        assert t._proc is not None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with t._lock:
                if t._proc is None:
                    break
            time.sleep(0.1)
        with t._lock:
            assert t._proc is None, "idle child was not reaped"
    finally:
        t.close()


def test_close_terminates_and_rejects() -> None:
    t = _make("ok")
    t.transcribe(AUDIO, language="en", initial_prompt=None)
    proc = t._proc
    t.close()
    assert proc is not None and proc.poll() is not None  # terminated
    with pytest.raises(TranscriptionError, match="closed"):
        t.transcribe(AUDIO, language="en", initial_prompt=None)


def test_default_memory_limit_is_positive_or_none() -> None:
    cap = default_memory_limit_bytes()
    assert cap is None or cap > 0


def test_worker_python_resolves_out_of_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dev/venv sys.executable is used as-is.
    monkeypatch.setattr("sys.executable", sys.executable)
    resolved = _worker_python()
    assert os.path.exists(resolved)
    assert ".app/" not in resolved  # never hand back an in-bundle interpreter path


def test_child_env_strips_bundle_python_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # py2app injects these into the app process; inheriting PYTHONHOME makes the
    # (different) worker interpreter abort with "No module named 'encodings'".
    monkeypatch.setenv("PYTHONHOME", "/Applications/Murmur.app/Contents/Resources")
    monkeypatch.setenv("PYTHONPATH", "/Applications/Murmur.app/Contents/Resources")
    monkeypatch.setenv("DYLD_FRAMEWORK_PATH", "/Applications/Murmur.app/Contents/Frameworks")
    monkeypatch.setenv("PATH", "/usr/bin")  # unrelated var must survive
    env = _child_env()
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env
    assert "DYLD_FRAMEWORK_PATH" not in env
    assert env["PATH"] == "/usr/bin"
