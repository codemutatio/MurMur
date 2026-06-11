"""Tests for murmur.paths — T-1."""
from __future__ import annotations

from pathlib import Path

import pytest

from murmur.paths import resolve_paths


def test_murmur_home_env_override(tmp_path: Path) -> None:
    """$MURMUR_HOME overrides the default app-support directory."""
    custom = tmp_path / "custom_home"
    paths = resolve_paths({"MURMUR_HOME": str(custom)})
    assert paths.home == custom
    assert custom.exists(), "resolve_paths must create the home directory"


def test_default_home_is_library_application_support() -> None:
    """Without $MURMUR_HOME, home defaults to ~/Library/Application Support/Murmur."""
    paths = resolve_paths({})
    assert str(paths.home).endswith("Library/Application Support/Murmur")


def test_config_override(tmp_path: Path) -> None:
    """--config PATH replaces the default config.toml path."""
    home = tmp_path / "home"
    custom_cfg = tmp_path / "my.toml"
    paths = resolve_paths({"MURMUR_HOME": str(home)}, config_override=custom_cfg)
    assert paths.config_file == custom_cfg


def test_default_config_inside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = resolve_paths({"MURMUR_HOME": str(home)})
    assert paths.config_file == home / "config.toml"


def test_state_lock_log_inside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = resolve_paths({"MURMUR_HOME": str(home)})
    assert paths.state_file == home / "state.json"
    assert paths.lock_file == home / "murmur.lock"
    assert paths.log_file == home / "murmur.log"


def test_home_created_if_absent(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()
    resolve_paths({"MURMUR_HOME": str(nested)})
    assert nested.exists()
