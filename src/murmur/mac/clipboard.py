"""macOS clipboard adapter using pbcopy."""

from __future__ import annotations

import subprocess
from collections.abc import Callable


def copy_to_clipboard(
    text: str,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> None:
    """Copy *text* to the macOS clipboard via pbcopy.

    Raises:
        subprocess.CalledProcessError: if pbcopy returns a non-zero exit code.
    """
    run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        check=True,
    )
