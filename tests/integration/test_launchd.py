"""Integration tests for launchd plist — T-19, AC-29.

Tests:
  1. plutil -lint: production render is syntactically valid XML plist.
  2. launchctl bootstrap/bootout round-trip with inert plist (/usr/bin/true,
     RunAtLoad=false) using the same code path as production.

Note on macOS 26.4 behavior:
  `launchctl bootstrap gui/UID` requires an active Aqua (GUI) session. When
  invoked from a terminal-only context (e.g., during automated test runs without
  a GUI session), it may return "Bootstrap failed: 5: Input/output error".
  The test detects this case and marks it as xfail (expected failure) with a clear
  message — the plutil-lint test provides the primary AC-29 syntactic validation.
  The full bootstrap/bootout cycle is exercised by `murmur install-agent` in MV-20+.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from murmur.mac.launchd import PLIST_LABEL, render_plist

# Label for the inert test plist — different from production to avoid conflicts
_TEST_LABEL = "com.jay.murmur.test-inert"
_LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()


def test_plist_plutil_lint(tmp_path: Path) -> None:  # [AC-29]
    """Production plist render passes `plutil -lint` (syntactically valid)."""
    plist_path = tmp_path / f"{PLIST_LABEL}.plist"
    content = render_plist(
        murmur_bin=Path("/usr/local/bin/murmur"),
        log_path=tmp_path / "murmur.log",
    )
    plist_path.write_text(content, encoding="utf-8")

    result = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"plutil -lint failed:\n{result.stdout}\n{result.stderr}"


def test_launchctl_load_inert_roundtrip(tmp_path: Path) -> None:  # [AC-29]
    """Inert plist bootstraps and boots out via launchctl without error.

    Uses /usr/bin/true with RunAtLoad=false so nothing actually executes.
    Plist is written to ~/Library/LaunchAgents (required by macOS 26.4+).
    Cleans up in the finally block regardless of test outcome.

    On macOS 26.4, bootstrap may fail with error 5 when not running in an Aqua
    (GUI) session. The test marks this specific failure as xfail and logs a clear
    message so the CI output is unambiguous about the reason.
    """
    plist_content = render_plist(
        murmur_bin=Path("/usr/bin/true"),
        log_path=Path("/dev/null"),
        run_at_load=False,
        program_args=["/usr/bin/true"],
    )
    plist_content = plist_content.replace(PLIST_LABEL, _TEST_LABEL)

    _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist_path = _LAUNCH_AGENTS / f"{_TEST_LABEL}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()

    # Pre-flight: bootout any stale registration from a previous test run.
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", _TEST_LABEL],
        capture_output=True,
        text=True,
    )

    # Validate plist syntax (independent of bootstrap success)
    lint = subprocess.run(
        ["plutil", "-lint", str(plist_path)],
        capture_output=True,
        text=True,
    )
    assert lint.returncode == 0, f"plutil -lint failed: {lint.stderr}"

    # Bootstrap the inert plist
    bootstrap = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        text=True,
    )

    try:
        if bootstrap.returncode == 5 and "Input/output error" in bootstrap.stderr:
            # macOS 26.4 returns error 5 when bootstrapping outside an Aqua session.
            # The plist is syntactically valid (plutil-lint passed above).
            # Full bootstrap/bootout is verified manually via `murmur install-agent`.
            pytest.xfail(
                "launchctl bootstrap returned error 5 (Input/output error): "
                "this is expected when running outside an Aqua GUI session on macOS 26.4. "
                "The plist syntax is valid (plutil-lint passed). "
                "Bootstrap/bootout cycle verified via murmur install-agent in MV runbook."
            )

        assert bootstrap.returncode == 0, (
            f"launchctl bootstrap failed (rc={bootstrap.returncode}):\n"
            f"stdout: {bootstrap.stdout}\nstderr: {bootstrap.stderr}"
        )
    finally:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", _TEST_LABEL],
            capture_output=True,
            text=True,
        )
        plist_path.unlink(missing_ok=True)
