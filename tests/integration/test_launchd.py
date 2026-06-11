"""Integration tests for launchd plist — T-19, AC-29.

Tests:
  1. plutil -lint: production render is syntactically valid XML plist.
  2. launchctl bootstrap/bootout round-trip with inert plist (/usr/bin/true,
     RunAtLoad=false) using the same code path as production.

The inert plist uses /usr/bin/true and RunAtLoad=false so it does NOT spawn
a real process and does NOT register itself to auto-launch. The test verifies
the plist machinery works end-to-end without affecting the real murmur agent.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from murmur.mac.launchd import PLIST_LABEL, render_plist


# Label for the inert test plist — different from production to avoid conflicts
_TEST_LABEL = "com.jay.murmur.test-inert"


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
    assert result.returncode == 0, (
        f"plutil -lint failed:\n{result.stdout}\n{result.stderr}"
    )


def test_launchctl_load_inert_roundtrip(tmp_path: Path) -> None:  # [AC-29]
    """Inert plist bootstraps and boots out via launchctl without error.

    Uses /usr/bin/true with RunAtLoad=false so nothing actually executes.
    Cleans up in the finally block regardless of test outcome.
    """
    # Render an inert plist with a test-specific label so it never conflicts
    # with the real murmur agent.
    plist_content = render_plist(
        murmur_bin=Path("/usr/bin/true"),
        log_path=tmp_path / "test.log",
        run_at_load=False,
        program_args=["/usr/bin/true"],
    )
    # Replace the production label with our test label in the rendered content
    plist_content = plist_content.replace(PLIST_LABEL, _TEST_LABEL)

    plist_path = tmp_path / f"{_TEST_LABEL}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()

    # Validate plist first
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
        assert bootstrap.returncode == 0, (
            f"launchctl bootstrap failed (rc={bootstrap.returncode}):\n"
            f"stdout: {bootstrap.stdout}\nstderr: {bootstrap.stderr}"
        )
    finally:
        # Always bootout, even if bootstrap failed (prevents plist leaks)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", _TEST_LABEL],
            capture_output=True,
            text=True,
        )
