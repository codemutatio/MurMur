"""launchd user agent management for murmur.

Renders, installs, and uninstalls the com.jay.murmur launchd plist.
KeepAlive=false: Quit must stick (checkpoint-approved, plan flag #8).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

PLIST_LABEL = "com.jay.murmur"
_DEFAULT_LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
_DEFAULT_APP_PATH = Path("/Applications/Murmur.app")


def render_plist(
    murmur_bin: Path,
    log_path: Path,
    *,
    run_at_load: bool = True,
    program_args: list[str] | None = None,
) -> str:
    """Render the launchd plist as a string.

    Args:
        murmur_bin: Absolute path to the murmur executable (or /usr/bin/true for tests).
        log_path: Path for stdout/stderr log output.
        run_at_load: Set RunAtLoad key.
        program_args: Override ProgramArguments (default: [murmur_bin, "run"]).

    Returns:
        Plist XML string.
    """
    args = program_args if program_args is not None else [str(murmur_bin), "run"]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <{"true" if run_at_load else "false"}/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
</dict>
</plist>
"""


def install_agent(plist_dir: Path = _DEFAULT_LAUNCH_AGENTS) -> Path:
    """Render the production plist, write it, and bootstrap the agent.

    Returns the installed plist path.

    Raises:
        subprocess.CalledProcessError: if launchctl bootstrap fails.
        RuntimeError: if murmur binary cannot be located.
    """
    import shutil

    murmur_bin = shutil.which("murmur")
    if murmur_bin is None:
        raise RuntimeError(
            "murmur binary not found in PATH. "
            "Install with `uv sync` and ensure the venv is activated."
        )

    home_dir = Path("~/Library/Application Support/Murmur").expanduser()
    home_dir.mkdir(parents=True, exist_ok=True)
    log_path = home_dir / "murmur.log"

    plist_content = render_plist(Path(murmur_bin), log_path)

    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{PLIST_LABEL}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True,
        capture_output=True,
    )
    _log.info("Installed launchd agent: %s", plist_path)
    return plist_path


def install_app_agent(
    app_path: Path = _DEFAULT_APP_PATH,
    plist_dir: Path = _DEFAULT_LAUNCH_AGENTS,
) -> Path:
    """Render the plist pointing at the Murmur.app bundle, write it, and bootstrap.

    Unlike install_agent() (which runs bare ``murmur run``), this points launchd
    at the .app executable. The bundle's Info.plist declares
    NSMicrophoneUsageDescription, so the launchd-spawned instance can obtain
    Microphone permission — the reason this path enables true always-on capture.

    Uses the same PLIST_LABEL, so uninstall_agent() removes either variant.

    Returns the installed plist path.

    Raises:
        RuntimeError: if the Murmur.app executable cannot be located.
        subprocess.CalledProcessError: if launchctl bootstrap fails.
    """
    exe = app_path / "Contents" / "MacOS" / "Murmur"
    if not exe.exists():
        raise RuntimeError(
            f"Murmur.app executable not found at {exe}. "
            "Build and install the app first with packaging/build_app.sh."
        )

    home_dir = Path("~/Library/Application Support/Murmur").expanduser()
    home_dir.mkdir(parents=True, exist_ok=True)
    log_path = home_dir / "murmur.log"

    plist_content = render_plist(exe, log_path, program_args=[str(exe)])

    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{PLIST_LABEL}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True,
        capture_output=True,
    )
    _log.info("Installed launchd app agent: %s -> %s", plist_path, exe)
    return plist_path


def uninstall_agent(plist_dir: Path = _DEFAULT_LAUNCH_AGENTS) -> None:
    """Bootout the agent and remove the plist file.

    Does not raise if the agent is not loaded or the plist does not exist.
    """
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", PLIST_LABEL],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        _log.debug("launchctl bootout returned non-zero (may not have been loaded): %s", exc)

    plist_path = plist_dir / f"{PLIST_LABEL}.plist"
    plist_path.unlink(missing_ok=True)
    _log.info("Uninstalled launchd agent: %s", plist_path)
