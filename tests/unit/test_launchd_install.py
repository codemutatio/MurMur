"""Tests for mac/launchd install/uninstall helper functions — T-19."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from murmur.mac.launchd import PLIST_LABEL, install_agent, install_app_agent, uninstall_agent


def test_install_agent_murmur_not_found(tmp_path: Path) -> None:
    """install_agent raises RuntimeError when murmur binary is not in PATH."""
    with (
        patch("shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="murmur binary not found"),
    ):
        install_agent(plist_dir=tmp_path)


def test_install_agent_writes_plist(tmp_path: Path) -> None:
    """install_agent writes the plist file to plist_dir and calls launchctl bootstrap."""
    fake_murmur_bin = "/usr/local/bin/murmur"

    with (
        patch("shutil.which", return_value=fake_murmur_bin),
        patch("subprocess.run") as mock_run,
    ):
        # Make subprocess.run return success
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        plist_path = install_agent(plist_dir=tmp_path)

    assert plist_path == tmp_path / f"{PLIST_LABEL}.plist"
    assert plist_path.exists(), "Plist file should have been written"

    content = plist_path.read_text()
    assert PLIST_LABEL in content
    assert fake_murmur_bin in content

    # Verify launchctl bootstrap was called
    call_args = mock_run.call_args_list
    bootstrap_calls = [c for c in call_args if "bootstrap" in str(c)]
    assert len(bootstrap_calls) >= 1, "launchctl bootstrap should have been called"


def test_install_app_agent_missing_bundle_raises(tmp_path: Path) -> None:
    """install_app_agent raises when the Murmur.app executable is absent."""
    with pytest.raises(RuntimeError, match="Murmur.app executable not found"):
        install_app_agent(app_path=tmp_path / "Nope.app", plist_dir=tmp_path)


def test_install_app_agent_points_plist_at_bundle(tmp_path: Path) -> None:
    """install_app_agent writes a plist whose ProgramArguments is the .app executable."""
    app = tmp_path / "Murmur.app"
    exe = app / "Contents" / "MacOS" / "Murmur"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")  # stand-in executable

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        plist_path = install_app_agent(app_path=app, plist_dir=tmp_path)

    assert plist_path == tmp_path / f"{PLIST_LABEL}.plist"
    content = plist_path.read_text()
    assert str(exe) in content
    # It runs the bundle executable directly — not `murmur run`.
    assert "<string>run</string>" not in content
    bootstrap_calls = [c for c in mock_run.call_args_list if "bootstrap" in str(c)]
    assert len(bootstrap_calls) >= 1


def test_uninstall_agent_removes_plist(tmp_path: Path) -> None:
    """uninstall_agent removes the plist file and calls launchctl bootout."""
    plist_path = tmp_path / f"{PLIST_LABEL}.plist"
    plist_path.write_text("fake plist content")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        uninstall_agent(plist_dir=tmp_path)

    assert not plist_path.exists(), "Plist file should have been removed"

    call_args = mock_run.call_args_list
    bootout_calls = [c for c in call_args if "bootout" in str(c)]
    assert len(bootout_calls) >= 1, "launchctl bootout should have been called"


def test_uninstall_agent_tolerates_not_loaded(tmp_path: Path) -> None:
    """uninstall_agent does not raise if the agent is not currently loaded."""
    plist_path = tmp_path / f"{PLIST_LABEL}.plist"
    plist_path.write_text("fake plist content")

    with patch("subprocess.run") as mock_run:
        # Simulate launchctl bootout returning non-zero (not loaded)
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=113, stdout=b"", stderr=b"Could not find service"
        )
        # Should not raise
        uninstall_agent(plist_dir=tmp_path)


def test_uninstall_agent_no_plist_file(tmp_path: Path) -> None:
    """uninstall_agent does not raise when plist file does not exist."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        # Should not raise when plist does not exist
        uninstall_agent(plist_dir=tmp_path)
