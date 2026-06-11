"""Application path resolution for murmur.

All paths are injectable via $MURMUR_HOME for test isolation.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    """All filesystem paths used by murmur, resolved at startup."""

    home: Path
    """App support directory (default: ~/Library/Application Support/Murmur)."""

    config_file: Path
    """config.toml path."""

    state_file: Path
    """state.json for persisted pause state."""

    lock_file: Path
    """fcntl lock file for single-instance enforcement."""

    log_file: Path
    """Log output file."""


def resolve_paths(
    env: Mapping[str, str],
    config_override: Path | None = None,
) -> AppPaths:
    """Resolve all application paths from the environment.

    $MURMUR_HOME overrides the default support directory. *config_override*
    replaces the default config.toml path (--config CLI flag).

    Creates the home directory (and parents) if absent.
    """
    raw_home = env.get("MURMUR_HOME", "")
    if raw_home:
        home = Path(raw_home).expanduser().resolve()
    else:
        home = Path("~/Library/Application Support/Murmur").expanduser()

    home.mkdir(parents=True, exist_ok=True)

    config_file = config_override if config_override is not None else home / "config.toml"

    return AppPaths(
        home=home,
        config_file=config_file,
        state_file=home / "state.json",
        lock_file=home / "murmur.lock",
        log_file=home / "murmur.log",
    )


def _env() -> Mapping[str, str]:
    """Return the real process environment. Indirected so tests can inject."""
    return os.environ
