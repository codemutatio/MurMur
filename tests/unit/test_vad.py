"""Tests for murmur.audio.vad (SileroVAD).

The key regression: speech_prob must run the (stateful) model with autograd
disabled. Without torch.no_grad(), each frame's graph chains to the previous
frame's recurrent state and the autograd graph grows without bound — ~26 KB per
frame, several GB over a multi-hour always-on capture session.
"""

from __future__ import annotations

import numpy as np
import torch

from murmur.audio.vad import SileroVAD


def test_speech_prob_disables_autograd() -> None:
    """The model must be invoked inside a no-grad context (leak regression).

    Uses __new__ to bypass __init__ (which would load the real Silero model)
    and injects a fake that records whether grad was enabled when called.
    """
    vad = SileroVAD.__new__(SileroVAD)
    seen: dict[str, object] = {}

    class _FakeModel:
        def __call__(self, tensor: torch.Tensor, sample_rate: int) -> torch.Tensor:
            seen["grad_enabled"] = torch.is_grad_enabled()
            # A grad-requiring tensor would keep a graph alive if not for no_grad.
            return torch.tensor(0.5)

    vad._model = _FakeModel()  # type: ignore[attr-defined]

    prob = vad.speech_prob(np.zeros(512, dtype=np.float32))

    assert prob == 0.5
    assert seen["grad_enabled"] is False, (
        "speech_prob ran the stateful VAD model with autograd ENABLED — the "
        "graph will grow unbounded across frames (memory leak). Wrap the call "
        "in torch.no_grad()."
    )


def test_speech_prob_leaves_outer_grad_state_untouched() -> None:
    """no_grad must be scoped: the caller's grad mode is restored afterwards."""
    vad = SileroVAD.__new__(SileroVAD)
    vad._model = lambda tensor, sr: torch.tensor(0.1)  # type: ignore[attr-defined]

    assert torch.is_grad_enabled()  # default on
    vad.speech_prob(np.zeros(512, dtype=np.float32))
    assert torch.is_grad_enabled(), "outer autograd state must be restored"
