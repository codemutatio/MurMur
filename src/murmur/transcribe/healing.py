"""Self-healing transcriber: re-download the model if its files vanish.

A long-running process (the menubar app, the launchd server) resolves the
model's on-disk path once and caches it. If the weights are later deleted out
from under it — e.g. a cache sweep clears the model directory — every
transcription then fails with an mlx/huggingface error like "Repo id must be in
the form ...". This wrapper detects that class of failure, re-runs
``ModelManager.ensure()`` (which re-downloads the model), rebuilds the inner
transcriber, and retries once, so the process recovers on its own instead of
failing until a manual restart.

Pairs with murmur.transcribe.models.default_model_cache_dir, which keeps the
weights out of ``~/.cache`` in the first place.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable

import numpy as np

from murmur.transcribe.base import Transcriber, TranscriptionError, TranscriptionResult

_log = logging.getLogger(__name__)

# Substrings (matched case-insensitively) that indicate the model files are
# missing/corrupt rather than a genuine per-audio transcription failure.
_MISSING_MODEL_SIGNS: tuple[str, ...] = (
    "repo id must be in the form",  # mlx handed a path that no longer exists
    "no such file or directory",
    "does not appear to have",
    "load_npz",  # partial/missing weights npz
    "failed to resolve",
    "not a valid model",
)


def _looks_like_missing_model(message: str) -> bool:
    m = message.lower()
    return any(sign in m for sign in _MISSING_MODEL_SIGNS)


class SelfHealingTranscriber:
    """Transcriber that re-ensures (re-downloads) its model on a missing-model
    failure and retries once. Implements the Transcriber protocol.

    The inner transcriber is built eagerly at construction (so the first-run
    model download still happens at startup, preserving existing behaviour),
    then rebuilt on demand if the weights disappear.
    """

    def __init__(
        self,
        *,
        model_name: str,
        ensure: Callable[[str], str],
        build_inner: Callable[[str], Transcriber],
    ) -> None:
        """
        Args:
            model_name: Logical model name passed to ``ensure`` (e.g. "large-v3").
            ensure: ``ModelManager.ensure`` — resolves/downloads the model, returns
                its local snapshot path.
            build_inner: Builds the real transcriber from a resolved model path.
        """
        self._model_name = model_name
        self._ensure = ensure
        self._build_inner = build_inner
        self._lock = threading.Lock()
        self._inner: Transcriber | None = self._build_inner(self._ensure(self._model_name))

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        initial_prompt: str | None,
    ) -> TranscriptionResult:
        with self._lock:
            inner = self._inner
            try:
                assert inner is not None
                return inner.transcribe(audio, language=language, initial_prompt=initial_prompt)
            except TranscriptionError as exc:
                if not _looks_like_missing_model(str(exc)):
                    raise
                _log.warning(
                    "Transcription failed as if the model is missing; re-downloading "
                    "model %r and retrying once: %s",
                    self._model_name,
                    exc,
                )
                self._discard_inner()
                # Re-ensure forces a re-download when the snapshot is gone.
                model_ref = self._ensure(self._model_name)
                self._inner = self._build_inner(model_ref)
                return self._inner.transcribe(
                    audio, language=language, initial_prompt=initial_prompt
                )

    def close(self) -> None:
        with self._lock:
            self._discard_inner()

    def _discard_inner(self) -> None:
        inner, self._inner = self._inner, None
        close = getattr(inner, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
