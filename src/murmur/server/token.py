"""Bearer-token retrieval and constant-time comparison for the Murmur server.

Mirrors mac/keychain.py pattern exactly:
  Lookup order:
    1. MURMUR_SERVER_TOKEN environment variable (Assumption #2, R-7)
    2. macOS Keychain item with service name ``murmur-server-token``
       (``security find-generic-password -s murmur-server-token -w``)

The token is NEVER logged (AC-17, R-7).
Comparison uses hmac.compare_digest to prevent timing oracles (AC-15).
"""

from __future__ import annotations

import hmac
import subprocess
from collections.abc import Callable, Mapping


def get_server_token(
    env: Mapping[str, str],
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Return the server bearer token, or None if not available.

    Lookup order:
    1. ``env["MURMUR_SERVER_TOKEN"]``
    2. macOS Keychain item with service name ``murmur-server-token``
       (``security find-generic-password -s murmur-server-token -w``)

    The result is NEVER logged. Callers must treat this as opaque.
    """
    # 1. Environment variable
    token_from_env = env.get("MURMUR_SERVER_TOKEN", "").strip()
    if token_from_env:
        return token_from_env

    # 2. Keychain
    try:
        result = run(
            ["security", "find-generic-password", "-s", "murmur-server-token", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                return token
    except Exception:
        pass

    return None


def verify_token(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison of *provided* against *expected*.

    Uses hmac.compare_digest to prevent timing oracles (AC-15).
    Returns False immediately if either value is None or empty.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())
