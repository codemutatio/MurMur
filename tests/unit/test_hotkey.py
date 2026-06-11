"""Tests for app/hotkey.py — T-18, R-13, E-2."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from murmur.app.hotkey import HotkeyListener, to_pynput_spec

# ---------------------------------------------------------------------------
# to_pynput_spec
# ---------------------------------------------------------------------------


def test_spec_parsing() -> None:
    """ctrl+alt+cmd+m converts to pynput GlobalHotKeys format."""
    spec = to_pynput_spec("ctrl+alt+cmd+m")
    assert spec == "<ctrl>+<alt>+<cmd>+m"


def test_spec_parsing_shift() -> None:
    """shift token converts correctly."""
    assert to_pynput_spec("shift+f5") == "<shift>+f5"


def test_spec_single_char_preserved() -> None:
    """Single character tokens pass through unchanged."""
    assert to_pynput_spec("ctrl+m") == "<ctrl>+m"


def test_spec_unknown_token_raises() -> None:
    """Unknown multi-character token raises ValueError."""
    with pytest.raises(ValueError, match="Unknown hotkey token"):
        to_pynput_spec("ctrl+win+m")


# ---------------------------------------------------------------------------
# HotkeyListener
# ---------------------------------------------------------------------------


def test_untrusted_degrades_gracefully() -> None:  # [E-2]
    """start() returns False when Accessibility is not trusted."""
    with patch("murmur.mac.permissions.accessibility_trusted", return_value=False):
        listener = HotkeyListener("ctrl+alt+cmd+m", lambda: None)
        result = listener.start()

    assert result is False, "start() should return False when Accessibility is not granted"


def test_trusted_registers_hotkey() -> None:
    """start() returns True and registers GlobalHotKeys when Accessibility is granted."""
    mock_global_hotkeys_instance = MagicMock()
    mock_global_hotkeys_cls = MagicMock(return_value=mock_global_hotkeys_instance)

    with (
        patch("murmur.mac.permissions.accessibility_trusted", return_value=True),
        patch.dict(
            "sys.modules",
            {
                "pynput": MagicMock(),
                "pynput.keyboard": MagicMock(GlobalHotKeys=mock_global_hotkeys_cls),
            },
        ),
    ):
        called: list[bool] = []
        listener = HotkeyListener("ctrl+alt+cmd+m", lambda: called.append(True))
        result = listener.start()

    assert result is True
    mock_global_hotkeys_instance.start.assert_called_once()


def test_pynput_error_returns_false() -> None:
    """start() returns False if pynput raises during GlobalHotKeys construction."""
    with (
        patch("murmur.mac.permissions.accessibility_trusted", return_value=True),
        patch(
            "pynput.keyboard.GlobalHotKeys",
            side_effect=RuntimeError("pynput init failed"),
        ),
    ):
        listener = HotkeyListener("ctrl+alt+cmd+m", lambda: None)
        result = listener.start()

    assert result is False


def test_callback_toggles() -> None:
    """When GlobalHotKeys fires the hotkey, on_toggle is called."""
    toggle_calls: list[bool] = []

    def on_toggle() -> None:
        toggle_calls.append(True)

    # Capture the hotkey mapping passed to GlobalHotKeys
    captured_mapping: dict[str, object] = {}

    class FakeGlobalHotKeys:
        def __init__(self, mapping: dict[str, object]) -> None:
            captured_mapping.update(mapping)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    with (
        patch("murmur.mac.permissions.accessibility_trusted", return_value=True),
        patch("pynput.keyboard.GlobalHotKeys", FakeGlobalHotKeys),
    ):
        listener = HotkeyListener("ctrl+alt+cmd+m", on_toggle)
        listener.start()

    # Verify the callback key is the pynput spec
    assert "<ctrl>+<alt>+<cmd>+m" in captured_mapping

    # Call the registered callback directly
    captured_mapping["<ctrl>+<alt>+<cmd>+m"]()  # type: ignore[operator]
    assert len(toggle_calls) == 1, "on_toggle should have been called once"


def test_stop_is_idempotent() -> None:
    """stop() can be called multiple times without raising."""
    listener = HotkeyListener("ctrl+alt+cmd+m", lambda: None)
    listener.stop()  # never started
    listener.stop()  # again — should not raise
