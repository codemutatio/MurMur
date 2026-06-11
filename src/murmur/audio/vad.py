"""Voice Activity Detection protocol and Silero adapter."""
from __future__ import annotations

from typing import Protocol

import numpy as np


class VoiceDetector(Protocol):
    """Protocol for a frame-level voice activity detector."""

    def speech_prob(self, frame: np.ndarray) -> float:
        """Return speech probability (0..1) for one 512-sample frame."""
        ...

    def reset(self) -> None:
        """Reset any internal state (call on stream reopen)."""
        ...


class SileroVAD:
    """Production VAD backed by the silero-vad pip package.

    The model is bundled in the silero-vad wheel — no network call required.

    Thread affinity: construct and use only on the capture-engine thread.
    ``torch.set_num_threads(1)`` is set at construction time to keep idle
    CPU below AC-21's 5% threshold.
    """

    def __init__(self) -> None:
        import torch  # type: ignore[import-untyped]
        from silero_vad import load_silero_vad  # type: ignore[import-untyped]

        torch.set_num_threads(1)
        self._model = load_silero_vad()

    def speech_prob(self, frame: np.ndarray) -> float:
        import torch  # type: ignore[import-untyped]

        from murmur.audio import SAMPLE_RATE

        tensor = torch.from_numpy(frame)
        return float(self._model(tensor, SAMPLE_RATE).item())

    def reset(self) -> None:
        self._model.reset_states()
