"""Tests for isolate_app_sys_path: inside Murmur.app, Murmur's env wins."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import murmur.cli as cli


def test_noop_outside_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    before = list(sys.path)
    assert cli.isolate_app_sys_path() == []
    assert sys.path == before


def test_inside_app_puts_own_env_first_and_drops_strays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    own = str(Path(cli.__file__).resolve().parents[1])
    user_site = tmp_path / "Library/Python/3.11/lib/python/site-packages"
    base = tmp_path / "Framework/3.11"
    ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    global_site = base / "lib" / ver / "site-packages"
    for d in (user_site, global_site):
        d.mkdir(parents=True)
    stdlib = str(base / "lib" / ver)

    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr("site.getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(
        sys,
        "path",
        ["/app/Resources", stdlib, str(global_site), str(global_site / "pkg.egg"), str(user_site), own],
    )

    removed = cli.isolate_app_sys_path()

    assert sys.path[0] == own
    assert stdlib in sys.path and "/app/Resources" in sys.path  # stdlib + bundle kept
    assert str(global_site) not in sys.path and str(user_site) not in sys.path
    assert str(global_site / "pkg.egg") not in sys.path  # .pth-added children too
    assert set(removed) == {str(global_site), str(global_site / "pkg.egg"), str(user_site)}
    assert sys.path.count(own) == 1
