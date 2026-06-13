"""Tests for app/menubar.py menu structure — T-17, R-11, R-12."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from murmur.control import IconState, StatusBoard, StatusSnapshot


def _make_snapshot(
    icon: IconState = IconState.RECORDING,
    queue_depth: int = 0,
    error_count: int = 0,
    pause_until: datetime | None = None,
    notice: str | None = None,
    download: tuple[int, int] | None = None,
) -> StatusSnapshot:
    return StatusSnapshot(
        icon=icon,
        queue_depth=queue_depth,
        error_count=error_count,
        pause_until=pause_until,
        notice=notice,
        download=download,
    )


@pytest.fixture
def mock_rumps(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Stub out rumps so tests run without a running NSApplication."""
    import sys

    # Build a Timer mock whose instances have a proper .start() method
    timer_instance = MagicMock()
    timer_instance.start = MagicMock()
    timer_cls = MagicMock(return_value=timer_instance)

    rumps_mock = MagicMock()
    rumps_mock.App = MagicMock
    rumps_mock.MenuItem = MagicMock
    rumps_mock.Timer = timer_cls
    rumps_mock.separator = "---"
    rumps_mock.quit_application = MagicMock()
    monkeypatch.setitem(sys.modules, "rumps", rumps_mock)
    # menubar.py does `import rumps` at its own import time, so its module global
    # is bound once and would otherwise be a stale mock from an earlier test.
    # Rebind it to this test's mock so calls like rumps.quit_application() are
    # observable here.
    import murmur.app.menubar as menubar_mod

    monkeypatch.setattr(menubar_mod, "rumps", rumps_mock)
    return rumps_mock


def _make_app(
    tmp_path: Path,
    mock_rumps: MagicMock,
) -> object:
    """Create a MurmurApp with fake dependencies."""
    from murmur.app.menubar import MurmurApp
    from murmur.control import Controller, StateStore
    from tests.fakes import FixedClock, RecordingNotifier

    clock = FixedClock()
    notifier = RecordingNotifier()
    store = StateStore(tmp_path / "state.json", clock)
    controller = Controller(store, notifier, clock)
    status = StatusBoard()
    runtime = MagicMock()
    return MurmurApp(controller, status, runtime, tmp_path / "Transcripts"), status


def test_menu_items_and_disabled_notes(tmp_path: Path, mock_rumps: MagicMock) -> None:  # [R-12]
    """MurmurApp creates all R-12 menu items including a disabled notes item.

    The notes item must have callback=None (renders as disabled in rumps).
    """
    app, _ = _make_app(tmp_path, mock_rumps)

    # All required items exist
    assert app.menu is not None
    assert app._item_pause_resume is not None
    assert app._item_pause_30 is not None
    assert app._item_pause_1h is not None
    assert app._item_pause_day is not None
    assert app._item_open_window is not None
    assert app._item_open_today is not None
    assert app._item_open_folder is not None
    assert app._item_notes is not None
    assert app._item_quit is not None

    # Notes item must be in the menu list (present but disabled — callback=None)
    assert app._item_notes in app.menu


def test_icon_reflects_snapshot(tmp_path: Path, mock_rumps: MagicMock) -> None:  # [R-11]
    """Timer callback sets app title to snapshot.icon.value."""
    app, status = _make_app(tmp_path, mock_rumps)

    status.set_icon(IconState.RECORDING)
    app._on_tick(None)
    assert app.title == IconState.RECORDING.value

    status.set_icon(IconState.PAUSED)
    app._on_tick(None)
    assert app.title == IconState.PAUSED.value

    status.set_icon(IconState.ERROR)
    app._on_tick(None)
    assert app.title == IconState.ERROR.value


def test_pause_resume_label_flips(tmp_path: Path, mock_rumps: MagicMock) -> None:
    """Pause/Resume label flips based on icon state from status board."""
    app, status = _make_app(tmp_path, mock_rumps)

    status.set_icon(IconState.RECORDING)
    app._on_tick(None)
    assert app._item_pause_resume.title == "Pause"

    status.set_icon(IconState.PAUSED)
    app._on_tick(None)
    assert app._item_pause_resume.title == "Resume"


def test_teardown_stops_runtime_then_quits(tmp_path: Path, mock_rumps: MagicMock) -> None:
    """_teardown stops the runtime and quits the rumps loop (in that order)."""
    app, _ = _make_app(tmp_path, mock_rumps)

    app._teardown()

    app._runtime.stop.assert_called_once_with()
    mock_rumps.quit_application.assert_called_once_with()


def test_teardown_is_idempotent(tmp_path: Path, mock_rumps: MagicMock) -> None:
    """A second teardown (e.g. duplicate signal) is a no-op."""
    app, _ = _make_app(tmp_path, mock_rumps)

    app._teardown()
    app._teardown()

    app._runtime.stop.assert_called_once_with()
    mock_rumps.quit_application.assert_called_once_with()


def test_on_quit_delegates_to_teardown(tmp_path: Path, mock_rumps: MagicMock) -> None:
    """The Quit menu item runs the same teardown as the signal path."""
    app, _ = _make_app(tmp_path, mock_rumps)

    app._on_quit(None)

    app._runtime.stop.assert_called_once_with()
    mock_rumps.quit_application.assert_called_once_with()


def test_teardown_quits_even_if_stop_raises(tmp_path: Path, mock_rumps: MagicMock) -> None:
    """A failure in runtime.stop() must not prevent the loop from quitting."""
    app, _ = _make_app(tmp_path, mock_rumps)
    app._runtime.stop.side_effect = RuntimeError("boom")

    app._teardown()

    mock_rumps.quit_application.assert_called_once_with()


def test_request_shutdown_schedules_teardown_on_main_loop(
    tmp_path: Path, mock_rumps: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """request_shutdown defers teardown to the main run loop via AppHelper."""
    from PyObjCTools import AppHelper

    scheduled: list[object] = []
    monkeypatch.setattr(AppHelper, "callAfter", lambda func, *a: scheduled.append(func))

    app, _ = _make_app(tmp_path, mock_rumps)
    app.request_shutdown()

    assert scheduled == [app._teardown]
    # Nothing torn down until the scheduled callback runs.
    app._runtime.stop.assert_not_called()
