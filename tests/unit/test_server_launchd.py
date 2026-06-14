"""Tests for server launchd plist — T-8, AC-19, R-16, R-17, OQ-8."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from murmur.server.launchd_server import (
    SERVER_PLIST_LABEL,
    install_server_agent,
    render_server_plist,
    uninstall_server_agent,
)

# ---------------------------------------------------------------------------
# AC-19 — plutil -lint passes (plist is syntactically valid)
# ---------------------------------------------------------------------------


def test_plist_lints(tmp_path: Path) -> None:  # [AC-19]
    """Production server plist render passes `plutil -lint`."""
    python_bin = Path("/usr/local/bin/python3")
    log_path = tmp_path / "murmur-server.log"
    plist_path = tmp_path / f"{SERVER_PLIST_LABEL}.plist"

    content = render_server_plist(python_bin, log_path)
    plist_path.write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"plutil -lint failed:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC-19 — KeepAlive = true
# ---------------------------------------------------------------------------


def test_keepalive_true(tmp_path: Path) -> None:  # [AC-19]
    """Rendered plist contains KeepAlive = true (not false)."""
    content = render_server_plist(
        python_bin=Path("/usr/bin/python3"),
        log_path=tmp_path / "server.log",
    )

    # Must contain <true/> after the KeepAlive key
    assert "<key>KeepAlive</key>" in content
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "<key>KeepAlive</key>" in line:
            # The next non-empty line should be <true/>
            remaining = lines[i + 1:]
            for next_line in remaining:
                stripped = next_line.strip()
                if stripped:
                    assert stripped == "<true/>", (
                        f"KeepAlive value is not <true/>; got: {stripped!r}"
                    )
                    break
            break


# ---------------------------------------------------------------------------
# AC-19 — caffeinate -i present in ProgramArguments
# ---------------------------------------------------------------------------


def test_caffeinate_present(tmp_path: Path) -> None:  # [AC-19]
    """ProgramArguments includes caffeinate -i."""
    content = render_server_plist(
        python_bin=Path("/usr/local/bin/python3"),
        log_path=tmp_path / "server.log",
    )

    assert "caffeinate" in content
    assert "-i" in content


# ---------------------------------------------------------------------------
# AC-19 — ProgramArguments points to the correct entrypoint
# ---------------------------------------------------------------------------


def test_program_args_point_to_entrypoint(tmp_path: Path) -> None:  # [AC-19]
    """ProgramArguments includes -m murmur.server as the entry point."""
    python_bin = Path("/opt/homebrew/bin/python3.11")
    content = render_server_plist(python_bin, tmp_path / "server.log")

    assert str(python_bin) in content
    assert "-m" in content
    assert "murmur.server" in content


# ---------------------------------------------------------------------------
# AC-19 — RunAtLoad = true
# ---------------------------------------------------------------------------


def test_run_at_load_true(tmp_path: Path) -> None:  # [AC-19]
    """Rendered plist contains RunAtLoad = true (production default)."""
    content = render_server_plist(
        python_bin=Path("/usr/bin/python3"),
        log_path=tmp_path / "server.log",
    )

    assert "<key>RunAtLoad</key>" in content
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "<key>RunAtLoad</key>" in line:
            remaining = lines[i + 1:]
            for next_line in remaining:
                stripped = next_line.strip()
                if stripped:
                    assert stripped == "<true/>", (
                        f"RunAtLoad value is not <true/>; got: {stripped!r}"
                    )
                    break
            break


# ---------------------------------------------------------------------------
# Label check
# ---------------------------------------------------------------------------


def test_plist_label(tmp_path: Path) -> None:
    """Rendered plist uses the correct label."""
    content = render_server_plist(Path("/usr/bin/python3"), tmp_path / "server.log")

    assert SERVER_PLIST_LABEL in content
    assert "com.jay.murmur-server" in content


# ---------------------------------------------------------------------------
# StandardOutPath / StandardErrorPath
# ---------------------------------------------------------------------------


def test_plist_log_paths(tmp_path: Path) -> None:
    """Rendered plist sets StandardOutPath and StandardErrorPath to the log path."""
    log_path = tmp_path / "murmur-server.log"
    content = render_server_plist(Path("/usr/bin/python3"), log_path)

    assert str(log_path) in content
    assert "StandardOutPath" in content
    assert "StandardErrorPath" in content


def test_run_at_load_false_option(tmp_path: Path) -> None:
    """run_at_load=False is reflected in the plist output."""
    content = render_server_plist(
        python_bin=Path("/usr/bin/python3"),
        log_path=tmp_path / "server.log",
        run_at_load=False,
    )

    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "<key>RunAtLoad</key>" in line:
            remaining = lines[i + 1:]
            for next_line in remaining:
                stripped = next_line.strip()
                if stripped:
                    assert stripped == "<false/>"
                    break
            break


# ---------------------------------------------------------------------------
# install_server_agent / uninstall_server_agent — mocked launchctl
# ---------------------------------------------------------------------------


def test_install_server_agent_writes_plist_and_bootstraps(tmp_path: Path) -> None:
    """install_server_agent writes the plist file and calls launchctl bootstrap."""
    plist_dir = tmp_path / "LaunchAgents"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        plist_path = install_server_agent(plist_dir=plist_dir)

    assert plist_path.exists()
    content = plist_path.read_text()
    assert SERVER_PLIST_LABEL in content
    assert "caffeinate" in content

    # launchctl bootstrap should have been called
    bootstrap_calls = [c for c in mock_run.call_args_list if "bootstrap" in str(c)]
    assert bootstrap_calls, "launchctl bootstrap was not called"


def test_uninstall_server_agent_calls_bootout_and_removes_plist(tmp_path: Path) -> None:
    """uninstall_server_agent calls launchctl bootout and deletes the plist."""
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir(parents=True)
    plist_path = plist_dir / f"{SERVER_PLIST_LABEL}.plist"
    plist_path.write_text("dummy", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        uninstall_server_agent(plist_dir=plist_dir)

    assert not plist_path.exists()
    bootout_calls = [c for c in mock_run.call_args_list if "bootout" in str(c)]
    assert bootout_calls, "launchctl bootout was not called"


def test_uninstall_swallows_bootout_error(tmp_path: Path) -> None:
    """uninstall_server_agent does not raise if launchctl bootout fails."""
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir(parents=True)

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "launchctl bootout")
        # Should not raise
        uninstall_server_agent(plist_dir=plist_dir)


# ---------------------------------------------------------------------------
# deploy fix — Homebrew PATH in EnvironmentVariables (live bug)
# ---------------------------------------------------------------------------


def test_environment_variables_path_includes_homebrew__deploy(tmp_path: Path) -> None:
    """Rendered plist EnvironmentVariables.PATH includes /opt/homebrew/bin.

    Parses the plist with plistlib (not string-match) and verifies:
      - EnvironmentVariables dict is present
      - PATH covers Apple-Silicon Homebrew (/opt/homebrew/bin) and Intel
        Homebrew (/usr/local/bin) so ffmpeg resolves under launchd [deploy]
      - KeepAlive is True
      - ProgramArguments contains caffeinate
      - plutil -lint passes
    """
    python_bin = Path("/usr/local/bin/python3")
    log_path = tmp_path / "murmur-server.log"
    plist_path = tmp_path / f"{SERVER_PLIST_LABEL}.plist"

    content = render_server_plist(python_bin, log_path)
    plist_path.write_text(content, encoding="utf-8")

    # --- plutil lint --------------------------------------------------------
    lint = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, (
        f"plutil -lint failed after adding EnvironmentVariables:\n"
        f"{lint.stdout}\n{lint.stderr}"
    )

    # --- parse with plistlib -----------------------------------------------
    parsed = plistlib.loads(content.encode())

    assert "EnvironmentVariables" in parsed, (
        "EnvironmentVariables key missing from rendered plist"
    )
    env_vars = parsed["EnvironmentVariables"]
    assert "PATH" in env_vars, "PATH key missing from EnvironmentVariables"

    path_value: str = env_vars["PATH"]
    path_dirs = path_value.split(":")

    assert "/opt/homebrew/bin" in path_dirs, (
        f"/opt/homebrew/bin (Apple-Silicon Homebrew) not in PATH: {path_value!r}"
    )
    assert "/usr/local/bin" in path_dirs, (
        f"/usr/local/bin (Intel Homebrew) not in PATH: {path_value!r}"
    )

    # --- KeepAlive still true -----------------------------------------------
    assert parsed.get("KeepAlive") is True, "KeepAlive must remain True"

    # --- caffeinate still in ProgramArguments --------------------------------
    prog_args: list[str] = parsed.get("ProgramArguments", [])
    assert any("caffeinate" in a for a in prog_args), (
        "caffeinate missing from ProgramArguments"
    )
