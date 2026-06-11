"""Global hotkey listener using pynput.

Converts the human-readable toggle spec (e.g. "ctrl+alt+cmd+m") to pynput's
GlobalHotKeys format and registers a listener thread.

Degrades gracefully when Accessibility permission is not granted (E-2, R-19):
start() returns False and the app continues without the hotkey.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from murmur.mac import permissions as _permissions_module

_log = logging.getLogger(__name__)

# Token map: modifier names → pynput bracket representations
_MODIFIER_MAP: dict[str, str] = {
    "ctrl": "<ctrl>",
    "alt": "<alt>",
    "cmd": "<cmd>",
    "shift": "<shift>",
    "super": "<super>",
}

# Known function/special key names allowed as multi-character non-modifier tokens
_SPECIAL_KEYS: frozenset[str] = frozenset(
    [f"f{n}" for n in range(1, 20)]
    + [
        "tab",
        "space",
        "esc",
        "delete",
        "backspace",
        "enter",
        "return",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "page_up",
        "page_down",
    ]
)


def to_pynput_spec(toggle: str) -> str:
    """Convert "ctrl+alt+cmd+m" → "<ctrl>+<alt>+<cmd>+m".

    Rules:
    - Known modifier tokens (ctrl, alt, cmd, shift, super) → bracketed form.
    - Single-character tokens kept as-is.
    - Known special key names (f1-f19, tab, esc, ...) kept as-is.
    - Unknown multi-character tokens raise ValueError.
    """
    parts = toggle.split("+")
    converted: list[str] = []
    for part in parts:
        part = part.strip()
        if part in _MODIFIER_MAP:
            converted.append(_MODIFIER_MAP[part])
        elif len(part) == 1 or part in _SPECIAL_KEYS:
            converted.append(part)
        else:
            raise ValueError(
                f"Unknown hotkey token '{part}' in spec '{toggle}'. "
                f"Known modifiers: {list(_MODIFIER_MAP)}"
            )
    return "+".join(converted)


class HotkeyListener:
    """Registers a global hotkey that calls *on_toggle* when pressed.

    Thread: pynput.GlobalHotKeys runs its own internal thread. The callback
    only calls controller.toggle_pause() which is thread-safe.
    """

    def __init__(self, toggle: str, on_toggle: Callable[[], Any]) -> None:
        self._toggle = toggle
        self._on_toggle = on_toggle
        self._listener: Any = None

    def start(self) -> bool:
        """Register the hotkey listener.

        Returns:
            True if the hotkey was successfully registered.
            False if Accessibility is not granted or pynput raises an error —
            app continues without hotkey (E-2).
        """
        if not _permissions_module.accessibility_trusted():
            _log.warning(
                "Accessibility permission not granted; global hotkey disabled. "
                "Grant access in System Settings → Privacy & Security → Accessibility."
            )
            return False

        try:
            from pynput.keyboard import GlobalHotKeys

            spec = to_pynput_spec(self._toggle)
            listener = GlobalHotKeys({spec: self._on_toggle})
            listener.start()
            self._listener = listener
            _log.info("Global hotkey registered: %s", spec)
            return True
        except Exception as exc:
            _log.warning("Could not register global hotkey: %s", exc)
            return False

    def stop(self) -> None:
        """Stop the hotkey listener. Idempotent."""
        if self._listener is not None:
            with contextlib.suppress(Exception):
                self._listener.stop()
            self._listener = None
