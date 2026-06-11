"""macOS notification adapter using osascript."""

from __future__ import annotations

import contextlib
import subprocess
import threading
from typing import Protocol


class Notifier(Protocol):
    """Protocol for fire-and-forget macOS notifications."""

    def notify(self, title: str, message: str) -> None:
        """Fire a notification. Never raises. Never blocks the caller."""
        ...


class OsascriptNotifier:
    """Send notifications via osascript in a daemon thread.

    Uses ``display notification`` which works from any process without an
    Info.plist (notifications may appear attributed to "Script Editor" until
    the user grants notification permission once — see Risk 5 in the plan).

    Never raises. Never blocks the calling thread.
    """

    def notify(self, title: str, message: str) -> None:
        # Escape embedded quotes to prevent AppleScript injection.
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        script = f'display notification "{safe_msg}" with title "{safe_title}"'

        def _run() -> None:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["osascript", "-e", script],
                    timeout=5,
                    check=False,
                    capture_output=True,
                )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
