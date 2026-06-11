"""Tests for murmur.control — StateStore, PauseState, is_pause_active — T-11."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from murmur.control import PauseMode, PauseState, StateStore, is_pause_active
from tests.fakes import FixedClock


def _make_store(tmp_path: Path, clock: FixedClock | None = None) -> tuple[StateStore, FixedClock]:
    c = clock or FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))
    state_file = tmp_path / "state.json"
    return StateStore(state_file, c), c


# AC-16
def test_rest_of_day_persists_before_midnight(tmp_path: Path) -> None:  # [AC-16]
    """REST_OF_DAY pause persists across 'relaunch' before midnight of same day."""
    # Set pause at 9am local (UTC for simplicity)
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))
    store, _ = _make_store(tmp_path, clock)

    # Compute next midnight (start of 2026-06-13)
    until = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc)
    pause = PauseState(mode=PauseMode.REST_OF_DAY, until=until)
    store.save(pause)

    # Reload (simulate relaunch) at 11am same day
    clock.set(datetime(2026, 6, 12, 11, 0, 0, tzinfo=timezone.utc))
    loaded = store.load()
    assert is_pause_active(loaded, clock()), (
        "REST_OF_DAY pause should still be active before midnight"
    )


# AC-17
def test_expires_after_midnight(tmp_path: Path) -> None:  # [AC-17]
    """REST_OF_DAY pause expires after midnight of the paused day."""
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))
    store, _ = _make_store(tmp_path, clock)

    until = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc)
    store.save(PauseState(mode=PauseMode.REST_OF_DAY, until=until))

    # Simulate relaunch after midnight
    clock.set(datetime(2026, 6, 13, 6, 0, 0, tzinfo=timezone.utc))
    loaded = store.load()
    assert not is_pause_active(loaded, clock()), (
        "REST_OF_DAY pause should have expired after midnight"
    )


def test_corrupt_state_defaults_to_recording(tmp_path: Path) -> None:
    """Corrupt state.json → PauseState() → not paused (recording)."""
    state_file = tmp_path / "state.json"
    state_file.write_text("{ invalid json [", encoding="utf-8")
    clock = FixedClock()
    store = StateStore(state_file, clock)
    loaded = store.load()
    assert loaded == PauseState()
    assert not is_pause_active(loaded, clock())


def test_missing_state_file_not_paused(tmp_path: Path) -> None:
    """Missing state.json → not paused."""
    state_file = tmp_path / "nonexistent.json"
    store = StateStore(state_file, FixedClock())
    loaded = store.load()
    assert not is_pause_active(loaded, FixedClock()())


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """Save and load produces the same PauseState."""
    until = datetime(2026, 6, 13, 0, 0, 0, tzinfo=timezone.utc)
    pause = PauseState(mode=PauseMode.TIMED, until=until)
    store, clock = _make_store(tmp_path)
    store.save(pause)
    loaded = store.load()
    assert loaded.mode == PauseMode.TIMED
    assert loaded.until is not None
    assert loaded.until.replace(tzinfo=timezone.utc) == until or loaded.until == until


def test_manual_pause_always_active(tmp_path: Path) -> None:
    """MANUAL pause is active regardless of clock time."""
    store, clock = _make_store(tmp_path)
    store.save(PauseState(mode=PauseMode.MANUAL))
    loaded = store.load()
    assert is_pause_active(loaded, clock())
    clock.advance(86400)  # advance 24 hours
    assert is_pause_active(loaded, clock())


def test_timed_pause_auto_expires(tmp_path: Path) -> None:
    """TIMED pause expires after `until` passes."""
    clock = FixedClock(datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc))
    store, _ = _make_store(tmp_path, clock)
    until = datetime(2026, 6, 12, 9, 30, 0, tzinfo=timezone.utc)
    store.save(PauseState(mode=PauseMode.TIMED, until=until))

    # Before expiry
    assert is_pause_active(store.load(), clock())
    # After expiry
    clock.set(datetime(2026, 6, 12, 10, 0, 0, tzinfo=timezone.utc))
    assert not is_pause_active(store.load(), clock())


def test_state_file_is_atomic(tmp_path: Path) -> None:
    """No .tmp files remain after save."""
    store, _ = _make_store(tmp_path)
    store.save(PauseState(mode=PauseMode.MANUAL))
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []
