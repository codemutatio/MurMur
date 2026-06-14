"""launchd user agent management for the Murmur server.

Renders, installs, and uninstalls the com.jay.murmur-server launchd plist.

Key differences from mac/launchd.py (which manages the Mac app agent):
  - Label: com.jay.murmur-server
  - KeepAlive = true  (server must restart on crash — AC-19, R-16)
  - ProgramArguments: [caffeinate, -i, <python>, -m, murmur.server]  (AC-19)
  - Does NOT use RunAtLoad=false or LimitLoadToSessionType=Aqua
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger(__name__)

SERVER_PLIST_LABEL = "com.jay.murmur-server"
_DEFAULT_LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()

# Single-instance PID-file path (OQ-8 belt — launchd is primary; PID file is secondary)
SERVER_PID_FILE = Path("/tmp/murmur-server.pid")


def render_server_plist(
    python_bin: Path,
    log_path: Path,
    *,
    run_at_load: bool = True,
) -> str:
    """Render the server launchd plist as a string.

    Args:
        python_bin: Absolute path to the Python interpreter (or /usr/bin/true for tests).
        log_path:   Path for stdout/stderr log output.
        run_at_load: Set RunAtLoad key (default True for production).

    Returns:
        Plist XML string.

    Contract (AC-19):
      - KeepAlive = true  (server restarts on crash)
      - RunAtLoad = true
      - ProgramArguments = [caffeinate, -i, <python_bin>, -m, murmur.server]
    """
    args = [
        "/usr/bin/caffeinate",
        "-i",
        str(python_bin),
        "-m",
        "murmur.server",
    ]
    args_xml = "\n".join(f"        <string>{a}</string>" for a in args)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SERVER_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <{"true" if run_at_load else "false"}/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def install_server_agent(plist_dir: Path = _DEFAULT_LAUNCH_AGENTS) -> Path:
    """Render the production plist, write it, and bootstrap the server agent.

    Returns the installed plist path.

    Raises:
        subprocess.CalledProcessError: if launchctl bootstrap fails.
    """
    python_bin = Path(sys.executable)
    log_path = Path("~/Library/Logs/murmur-server.log").expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    plist_content = render_server_plist(python_bin, log_path)

    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / f"{SERVER_PLIST_LABEL}.plist"
    plist_path.write_text(plist_content, encoding="utf-8")

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True,
        capture_output=True,
    )
    _log.info("Installed server launchd agent: %s", plist_path)
    return plist_path


def uninstall_server_agent(plist_dir: Path = _DEFAULT_LAUNCH_AGENTS) -> None:
    """Bootout the server agent and remove the plist file.

    Does not raise if the agent is not loaded or the plist does not exist.
    """
    uid = os.getuid()
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}", SERVER_PLIST_LABEL],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        _log.debug(
            "launchctl bootout returned non-zero (may not have been loaded): %s", exc
        )

    plist_path = plist_dir / f"{SERVER_PLIST_LABEL}.plist"
    plist_path.unlink(missing_ok=True)
    _log.info("Uninstalled server launchd agent: %s", plist_path)
