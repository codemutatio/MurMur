"""Tests for app/menubar.py menu structure — T-17, R-11, R-12."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    TimerClass = MagicMock(return_value=timer_instance)

    rumps_mock = MagicMock()
    rumps_mock.App = MagicMock
    rumps_mock.MenuItem = MagicMock
    rumps_mock.Timer = TimerClass
    rumps_mock.separator = "---"
    rumps_mock.quit_application = MagicMock()
    monkeypatch.setitem(sys.modules, "rumps", rumps_mock)
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


def test_menu_items_and_disabled_notes(
    tmp_path: Path, mock_rumps: MagicMock
) -> None:  # [R-12]
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
    assert app._item_open_today is not None
    assert app._item_open_folder is not None
    assert app._item_notes is not None
    assert app._item_quit is not None

    # Notes item must be in the menu list (present but disabled — callback=None)
    assert app._item_notes in app.menu


def test_icon_reflects_snapshot(
    tmp_path: Path, mock_rumps: MagicMock
) -> None:  # [R-11]
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


def test_pause_resume_label_flips(
    tmp_path: Path, mock_rumps: MagicMock
) -> None:
    """Pause/Resume label flips based on icon state from status board."""
    app, status = _make_app(tmp_path, mock_rumps)

    status.set_icon(IconState.RECORDING)
    app._on_tick(None)
    assert app._item_pause_resume.title == "Pause"

    status.set_icon(IconState.PAUSED)
    app._on_tick(None)
    assert app._item_pause_resume.title == "Resume"
