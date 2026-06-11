"""API key retrieval from environment and macOS Keychain.

Order: OPENAI_API_KEY env var → `security find-generic-password -s murmur-openai -w`.
Keys are never logged.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping


def get_openai_api_key(
    env: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Return the OpenAI API key, or None if not available.

    Lookup order:
    1. ``env["OPENAI_API_KEY"]``
    2. macOS Keychain item with service name ``murmur-openai``
       (``security find-generic-password -s murmur-openai -w``)

    The result is NEVER logged. Callers must treat this as opaque.
    """
    # 1. Environment variable
    key_from_env = env.get("OPENAI_API_KEY", "").strip()
    if key_from_env:
        return key_from_env

    # 2. Keychain
    try:
        result = run(
            ["security", "find-generic-password", "-s", "murmur-openai", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            key = result.stdout.strip()
            if key:
                return key
    except Exception:
        pass

    return None
