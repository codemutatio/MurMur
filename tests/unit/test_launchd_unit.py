"""Unit tests for mac/launchd.py plist rendering — T-19, AC-29."""

from __future__ import annotations

from pathlib import Path

from murmur.mac.launchd import PLIST_LABEL, render_plist


def test_render_keys() -> None:
    """Rendered plist contains all required keys."""
    content = render_plist(
        Path("/usr/local/bin/murmur"),
        Path("/tmp/murmur.log"),
    )
    assert f"<string>{PLIST_LABEL}</string>" in content
    assert "<key>ProgramArguments</key>" in content
    assert "<key>RunAtLoad</key>" in content
    assert "<true/>" in content  # run_at_load=True by default
    assert "<key>KeepAlive</key>" in content
    assert "<false/>" in content  # KeepAlive=false per checkpoint
    assert "<key>StandardOutPath</key>" in content
    assert "<key>StandardErrorPath</key>" in content
    assert "/tmp/murmur.log" in content
    assert "<key>ProcessType</key>" in content
    assert "<string>Interactive</string>" in content
    assert "<key>LimitLoadToSessionType</key>" in content
    assert "<string>Aqua</string>" in content


def test_render_program_arguments_default() -> None:
    """Default ProgramArguments uses murmur_bin + 'run'."""
    content = render_plist(
        Path("/home/user/.venv/bin/murmur"),
        Path("/tmp/murmur.log"),
    )
    assert "/home/user/.venv/bin/murmur" in content
    assert "<string>run</string>" in content


def test_render_program_arguments_override() -> None:
    """program_args override replaces default ProgramArguments."""
    content = render_plist(
        Path("/usr/bin/true"),
        Path("/tmp/murmur.log"),
        run_at_load=False,
        program_args=["/usr/bin/true"],
    )
    assert "/usr/bin/true" in content
    assert "<false/>" in content  # run_at_load=False


def test_render_run_at_load_false() -> None:
    """run_at_load=False → RunAtLoad is false."""
    content = render_plist(
        Path("/usr/bin/true"),
        Path("/tmp/murmur.log"),
        run_at_load=False,
    )
    # The RunAtLoad value should be <false/>
    import re

    run_at_load_match = re.search(r"<key>RunAtLoad</key>\s*<(true|false)/>", content)
    assert run_at_load_match is not None
    assert run_at_load_match.group(1) == "false"


def test_render_plist_xml_structure() -> None:
    """Rendered plist starts with valid XML/plist header."""
    content = render_plist(Path("/usr/bin/true"), Path("/tmp/test.log"))
    assert content.startswith('<?xml version="1.0"')
    assert "<!DOCTYPE plist" in content
    assert "<plist version" in content
    assert "</plist>" in content
