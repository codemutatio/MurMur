"""Tests for the self-healing transcriber and the persistent model location."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from murmur.transcribe.base import SegmentScore, TranscriptionError, TranscriptionResult
from murmur.transcribe.healing import SelfHealingTranscriber, _looks_like_missing_model
from murmur.transcribe.models import default_model_cache_dir

AUDIO = np.zeros(1600, dtype=np.float32)


def _result(text: str) -> TranscriptionResult:
    return TranscriptionResult(text=text, segments=(SegmentScore(text, 0.0, 1.0, 0.0),))


# --- persistent model location -------------------------------------------------


def test_default_model_cache_dir_is_not_in_dot_cache() -> None:
    d = default_model_cache_dir()
    assert ".cache" not in d.parts, f"models must not live under ~/.cache: {d}"
    assert d.name == "models"


def test_default_model_cache_dir_honors_murmur_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MURMUR_HOME", "/tmp/murmur-test-home")
    assert default_model_cache_dir() == Path("/tmp/murmur-test-home/models")


# --- missing-model detection ---------------------------------------------------


def test_looks_like_missing_model_matches_the_real_error() -> None:
    assert _looks_like_missing_model(
        "Repo id must be in the form 'repo_name' or 'namespace/repo_name': '/abs/path'"
    )
    assert _looks_like_missing_model("[load_npz] Input must be a zip file")
    assert not _looks_like_missing_model("CUDA out of memory")
    assert not _looks_like_missing_model("some unrelated transcription error")


# --- self-healing behavior -----------------------------------------------------


def test_passes_through_when_healthy() -> None:
    calls: dict[str, int] = {"ensure": 0, "build": 0}

    class _Inner:
        def transcribe(self, audio, *, language, initial_prompt):  # type: ignore[no-untyped-def]
            return _result("ok")

    def ensure(model: str) -> str:
        calls["ensure"] += 1
        return "/models/snap"

    def build(ref: str):  # type: ignore[no-untyped-def]
        calls["build"] += 1
        return _Inner()

    t = SelfHealingTranscriber(model_name="large-v3", ensure=ensure, build_inner=build)
    assert calls == {"ensure": 1, "build": 1}  # eager build at construction
    r = t.transcribe(AUDIO, language="en", initial_prompt=None)
    assert r.text == "ok"
    assert calls == {"ensure": 1, "build": 1}  # no re-ensure on success


def test_reensures_and_retries_on_missing_model() -> None:
    calls: dict[str, int] = {"ensure": 0, "build": 0}

    class _Inner:
        def __init__(self, fail: bool) -> None:
            self._fail = fail

        def transcribe(self, audio, *, language, initial_prompt):  # type: ignore[no-untyped-def]
            if self._fail:
                raise TranscriptionError(
                    "Repo id must be in the form 'repo_name' ...: '/models/snap'"
                )
            return _result("recovered")

    def ensure(model: str) -> str:
        calls["ensure"] += 1
        return "/models/snap"

    def build(ref: str):  # type: ignore[no-untyped-def]
        calls["build"] += 1
        return _Inner(fail=calls["build"] == 1)  # first inner fails, second works

    t = SelfHealingTranscriber(model_name="large-v3", ensure=ensure, build_inner=build)
    r = t.transcribe(AUDIO, language="en", initial_prompt=None)
    assert r.text == "recovered"
    assert calls["ensure"] == 2  # re-ensured (re-downloaded) after the missing-model failure
    assert calls["build"] == 2  # rebuilt the inner transcriber


def test_unrelated_error_is_not_retried() -> None:
    calls = {"ensure": 0}

    class _Inner:
        def transcribe(self, audio, *, language, initial_prompt):  # type: ignore[no-untyped-def]
            raise TranscriptionError("genuinely bad audio")

    def ensure(model: str) -> str:
        calls["ensure"] += 1
        return "/models/snap"

    t = SelfHealingTranscriber(
        model_name="large-v3", ensure=ensure, build_inner=lambda ref: _Inner()
    )
    with pytest.raises(TranscriptionError, match="bad audio"):
        t.transcribe(AUDIO, language="en", initial_prompt=None)
    assert calls["ensure"] == 1  # NOT re-ensured for an unrelated failure


def test_close_closes_inner() -> None:
    closed = {"v": False}

    class _Inner:
        def transcribe(self, audio, *, language, initial_prompt):  # type: ignore[no-untyped-def]
            return _result("ok")

        def close(self) -> None:
            closed["v"] = True

    t = SelfHealingTranscriber(
        model_name="large-v3", ensure=lambda m: "/s", build_inner=lambda ref: _Inner()
    )
    t.close()
    assert closed["v"] is True
