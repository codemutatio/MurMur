"""Tests for murmur.control.Controller — T-11."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from murmur.control import (
    Controller,
    PauseMode,
    PauseState,
    StateStore,
)
from tests.fakes import FixedClock, RecordingNotifier


def _make_controller(
    tmp_path: Path,
    clock: FixedClock | None = None,
    notifier: RecordingNotifier | None = None,
) -> tuple[Controller, StateStore, FixedClock, RecordingNotifier]:
    c = clock or FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=UTC))
    n = notifier or RecordingNotifier()
    state_file = tmp_path / "state.json"
    store = StateStore(state_file, c)
    ctrl = Controller(store, n, c)
    return ctrl, store, c, n


def test_toggle_fires_notification(tmp_path: Path) -> None:
    """toggle_pause() fires a notification on each toggle."""
    ctrl, store, clock, notifier = _make_controller(tmp_path)
    ctrl.toggle_pause()
    assert len(notifier.notifications) == 1
    assert "Paused" in notifier.notifications[0][1]
    ctrl.toggle_pause()
    assert len(notifier.notifications) == 2
    assert "Recording" in notifier.notifications[1][1]


def test_toggle_returns_new_state(tmp_path: Path) -> None:
    """toggle_pause() returns True when pausing, False when resuming."""
    ctrl, _, _, _ = _make_controller(tmp_path)
    assert ctrl.toggle_pause() is True  # was recording → now paused
    assert ctrl.toggle_pause() is False  # was paused → now recording


def test_timed_pause_auto_expires(tmp_path: Path) -> None:
    """pause_for() creates a TIMED pause that expires automatically."""
    ctrl, store, clock, _ = _make_controller(tmp_path)
    ctrl.pause_for(1800)  # 30 min
    assert ctrl.effective_paused() is True
    clock.advance(3600)  # advance 1 h
    assert ctrl.effective_paused() is False


def test_pause_rest_of_day(tmp_path: Path) -> None:
    """pause_rest_of_day() sets until = next local midnight."""
    ctrl, store, clock, notifier = _make_controller(tmp_path)
    ctrl.pause_rest_of_day()
    assert ctrl.effective_paused() is True
    # Should fire notification
    assert any("rest of day" in msg.lower() for _, msg in notifier.notifications)


def test_resume_clears_pause(tmp_path: Path) -> None:
    """resume() always results in not-paused."""
    ctrl, store, clock, _ = _make_controller(tmp_path)
    ctrl.toggle_pause()
    assert ctrl.effective_paused() is True
    ctrl.resume()
    assert ctrl.effective_paused() is False


def test_effective_paused_reads_from_store(tmp_path: Path) -> None:
    """effective_paused() reads live from StateStore (auto-expiry, no timers)."""
    ctrl, store, clock, _ = _make_controller(tmp_path)
    # Manually inject a TIMED pause that is already expired
    from datetime import timedelta

    until = clock() - timedelta(seconds=60)  # in the past
    store.save(PauseState(mode=PauseMode.TIMED, until=until))
    assert ctrl.effective_paused() is False
