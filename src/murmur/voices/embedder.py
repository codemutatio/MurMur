"""Speaker embeddings (voiceprints) with SpeechBrain's ECAPA-TDNN model.

The model (~90 MB) lives in the persistent models directory — downloaded as real
files, not symlinks into ``~/.cache`` — so a cache sweep can't break it (see
murmur.transcribe.models.default_model_cache_dir). Inference runs under
``torch.inference_mode()``: without it a long-running process accumulates
autograd state (the Silero VAD leak taught us that).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from murmur.transcribe.models import default_model_cache_dir

_log = logging.getLogger(__name__)

MODEL_REPO = "speechbrain/spkrec-ecapa-voxceleb"
SAMPLE_RATE = 16_000


def model_dir() -> Path:
    return default_model_cache_dir() / "spkrec-ecapa-voxceleb"


class SpeakerEmbedder:
    """Lazily-loaded ECAPA encoder: 16 kHz mono float32 → unit 192-d vector."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or model_dir()
        self._lock = threading.Lock()
        self._enc: Any = None

    def _load(self) -> Any:
        if self._enc is not None:
            return self._enc
        if not (self._path / "hyperparams.yaml").exists():
            from huggingface_hub import snapshot_download

            _log.info("Downloading speaker model %s → %s", MODEL_REPO, self._path)
            snapshot_download(MODEL_REPO, local_dir=str(self._path))
        from speechbrain.inference.speaker import EncoderClassifier

        self._enc = EncoderClassifier.from_hparams(
            source=str(self._path), savedir=str(self._path), run_opts={"device": "cpu"}
        )
        return self._enc

    def embed(self, audio: np.ndarray) -> np.ndarray:
        import torch

        with self._lock:
            enc = self._load()
            with torch.inference_mode():
                t = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
                e: np.ndarray = np.asarray(enc.encode_batch(t).squeeze().cpu(), dtype=np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n > 0 else e
