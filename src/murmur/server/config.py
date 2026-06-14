"""Server-specific configuration loading.

Schema (server.toml):
    [server]  host / port / max_upload_mb / busy_wait_s / log_path
    [stt]     model / language / vocab_hints
    [clean]   parsed-and-ignored (OQ-3/OQ-5; Non-Goal #1)

Mirrors murmur/config.py pattern exactly:
  - Never raises.
  - Missing / malformed file → all defaults + warning.
  - Unknown keys → logged warning, ignored.
  - Bearer token is NEVER stored here (R-7).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Default vocab hints for the server (PRD §5.5 / Assumption #1).
# These differ from murmur/config.py's _DEFAULT_VOCAB_HINTS (Mac-app defaults).
_DEFAULT_SERVER_VOCAB_HINTS: tuple[str, ...] = (
    "Rivy",
    "Yassir",
    "Refendr",
    "Chidera",
    "Kemi",
    "Vaasa",
    "GKE",
    "BigQuery",
    "FIN-FSA",
)


@dataclass(frozen=True)
class ServerSettings:
    """Mirrors [server] table."""

    host: str = "127.0.0.1"
    port: int = 8080
    max_upload_mb: int = 25
    busy_wait_s: int = 0
    log_path: str = "~/Library/Logs/murmur-server.log"


@dataclass(frozen=True)
class ServerSttSettings:
    """Mirrors [stt] table."""

    model: str = "large-v3"
    language: str = "en"
    vocab_hints: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_SERVER_VOCAB_HINTS)


@dataclass(frozen=True)
class ServerConfig:
    """Top-level server configuration."""

    server: ServerSettings = field(default_factory=ServerSettings)
    stt: ServerSttSettings = field(default_factory=ServerSttSettings)
    # [clean] is parsed into a raw dict and NEVER acted on (OQ-3, OQ-5, Non-Goal #1).
    clean_raw: dict[str, object] = field(default_factory=dict)


def load_server_config(path: Path | None) -> tuple[ServerConfig, list[str]]:
    """Load server config from *path*.

    Returns:
        A (ServerConfig, warnings) tuple.  Config contains all applied values.
        Warnings are human-readable strings suitable for logging.
        Never raises — missing/unparseable → defaults + warning (AC-16, E-15, E-16).
    """
    warnings: list[str] = []

    if path is None or not path.exists():
        expected = path or Path("~/Library/Application Support/Murmur/server.toml").expanduser()
        warnings.append(f"Server config file not found at {expected}; using all defaults.")
        return ServerConfig(), warnings

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(
            f"Server config file {path} could not be parsed ({exc}); using all defaults."
        )
        return ServerConfig(), warnings

    known_top = {"server", "stt", "clean"}
    for key in raw:
        if key not in known_top:
            warnings.append(f"Unknown server config key '{key}' ignored.")  # E-17

    server = _parse_server(raw.get("server", {}), warnings)
    stt = _parse_stt(raw.get("stt", {}), warnings)
    # [clean] is parsed into a plain dict and stored; never read by any server logic.
    clean_raw = raw.get("clean", {})
    if not isinstance(clean_raw, dict):
        warnings.append("'clean' section is not a table; ignored.")
        clean_raw = {}

    return ServerConfig(server=server, stt=stt, clean_raw=clean_raw), warnings


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_server(raw: Any, warnings: list[str]) -> ServerSettings:
    if not isinstance(raw, dict):
        warnings.append("'server' section is not a table; using defaults.")
        return ServerSettings()
    d = ServerSettings()
    known = {"host", "port", "max_upload_mb", "busy_wait_s", "log_path"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown server key '{k}' ignored.")
    return ServerSettings(
        host=_coerce(raw, "host", str, d.host, warnings),
        port=_coerce(raw, "port", int, d.port, warnings),
        max_upload_mb=_coerce(raw, "max_upload_mb", int, d.max_upload_mb, warnings),
        busy_wait_s=_coerce(raw, "busy_wait_s", int, d.busy_wait_s, warnings),
        log_path=_coerce(raw, "log_path", str, d.log_path, warnings),
    )


def _parse_stt(raw: Any, warnings: list[str]) -> ServerSttSettings:
    if not isinstance(raw, dict):
        warnings.append("'stt' section is not a table; using defaults.")
        return ServerSttSettings()
    d = ServerSttSettings()
    known = {"model", "language", "vocab_hints"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown stt key '{k}' ignored.")

    hints_raw = raw.get("vocab_hints", list(d.vocab_hints))
    if isinstance(hints_raw, list) and all(isinstance(h, str) for h in hints_raw):
        hints: tuple[str, ...] = tuple(hints_raw)
    else:
        warnings.append("stt.vocab_hints is not a list of strings; using defaults.")
        hints = d.vocab_hints

    return ServerSttSettings(
        model=_coerce(raw, "model", str, d.model, warnings),
        language=_coerce(raw, "language", str, d.language, warnings),
        vocab_hints=hints,
    )


def _coerce(
    raw: dict[str, Any], key: str, typ: type, default: Any, warnings: list[str]
) -> Any:
    """Return raw[key] coerced to *typ*, or *default* with a warning on failure."""
    if key not in raw:
        return default
    val = raw[key]
    if isinstance(val, typ):
        return val
    # Allow int→float coercion
    if typ is float and isinstance(val, int):
        return float(val)
    warnings.append(
        f"Server config key '{key}' expected {typ.__name__}, got {type(val).__name__}; "
        f"using default {default!r}."
    )
    return default
