"""Configuration loading for murmur.

Uses stdlib tomllib (Python 3.11+). Config file is optional — missing or
unparseable → all defaults + logged warning (E-16). Unknown keys → logged
warning, not error (E-17). Type errors on known keys → use default + warn.
API keys are NEVER stored in config — see murmur.mac.keychain.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

_log = logging.getLogger(__name__)

# Default vocab hints covering Jay's typical domain vocabulary (PRD §5)
_DEFAULT_VOCAB_HINTS: tuple[str, ...] = (
    "Rivy",
    "Yassir",
    "BigQuery",
    "Supabase",
    "LLM",
    "API",
    "OAuth",
    "PostgreSQL",
    "TypeScript",
    "JavaScript",
    "React",
    "Next.js",
    "Vercel",
    "Anthropic",
    "Claude",
)


@dataclass(frozen=True)
class CaptureConfig:
    device: str = "default"
    vad_threshold: float = 0.5
    silence_split_s: float = 1.5
    # Force-split a continuous speech run at this length. Whisper's native
    # window is 30 s, so a longer segment only inflates the per-call working
    # set without improving accuracy — bounded here to keep transcription
    # memory modest (see murmur.transcribe.isolated).
    max_segment_s: float = 30.0


@dataclass(frozen=True)
class SttConfig:
    engine: Literal["local", "api"] = "local"
    # "large-v3-turbo" (~4x faster, smaller decoder, lower memory) is the
    # default; "large-v3" trades speed for a small accuracy gain.
    local_model: str = "large-v3-turbo"
    api_model: str = "gpt-4o-transcribe"
    api_fallback: bool = False
    language: str = "en"
    vocab_hints: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_VOCAB_HINTS)


@dataclass(frozen=True)
class StorageConfig:
    transcripts_dir: Path = field(default_factory=lambda: Path("~/Transcripts").expanduser())
    spool_dir: Path = field(default_factory=lambda: Path("~/Transcripts/spool").expanduser())
    keep_audio: bool = False
    retention_days: int = 365


@dataclass(frozen=True)
class HotkeyConfig:
    toggle: str = "ctrl+alt+cmd+m"


@dataclass(frozen=True)
class NotesConfig:
    """v2 item — parsed but never acted on in v1."""

    enabled: bool = False


@dataclass(frozen=True)
class Config:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    notes: NotesConfig = field(default_factory=NotesConfig)


def load_config(path: Path | None) -> tuple[Config, list[str]]:
    """Load config from *path*.

    Returns:
        A (Config, warnings) tuple. Config contains all applied values.
        Warnings are human-readable strings suitable for logging.
        Never raises — missing/unparseable → defaults + warning (E-16).
    """
    warnings: list[str] = []

    if path is None or not path.exists():
        expected = path or Path("~/Library/Application Support/Murmur/config.toml").expanduser()
        warnings.append(f"Config file not found at {expected}; using all defaults.")
        return Config(), warnings

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"Config file {path} could not be parsed ({exc}); using all defaults.")
        return Config(), warnings

    known_top = {"capture", "stt", "storage", "hotkey", "notes"}
    for key in raw:
        if key not in known_top:
            warnings.append(f"Unknown config key '{key}' ignored.")

    capture = _parse_capture(raw.get("capture", {}), warnings)
    stt = _parse_stt(raw.get("stt", {}), warnings)
    storage = _parse_storage(raw.get("storage", {}), warnings)
    hotkey = _parse_hotkey(raw.get("hotkey", {}), warnings)
    notes = _parse_notes(raw.get("notes", {}), warnings)

    return Config(capture=capture, stt=stt, storage=storage, hotkey=hotkey, notes=notes), warnings


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_capture(raw: Any, warnings: list[str]) -> CaptureConfig:
    if not isinstance(raw, dict):
        warnings.append("'capture' section is not a table; using defaults.")
        return CaptureConfig()
    d = CaptureConfig()
    known = {"device", "vad_threshold", "silence_split_s", "max_segment_s"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown capture key '{k}' ignored.")
    return CaptureConfig(
        device=_coerce(raw, "device", str, d.device, warnings),
        vad_threshold=_coerce(raw, "vad_threshold", float, d.vad_threshold, warnings),
        silence_split_s=_coerce(raw, "silence_split_s", float, d.silence_split_s, warnings),
        max_segment_s=_coerce(raw, "max_segment_s", float, d.max_segment_s, warnings),
    )


def _parse_stt(raw: Any, warnings: list[str]) -> SttConfig:
    if not isinstance(raw, dict):
        warnings.append("'stt' section is not a table; using defaults.")
        return SttConfig()
    d = SttConfig()
    known = {"engine", "local_model", "api_model", "api_fallback", "language", "vocab_hints"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown stt key '{k}' ignored.")

    hints_raw = raw.get("vocab_hints", list(d.vocab_hints))
    if isinstance(hints_raw, list) and all(isinstance(h, str) for h in hints_raw):
        hints: tuple[str, ...] = tuple(hints_raw)
    else:
        warnings.append("stt.vocab_hints is not a list of strings; using defaults.")
        hints = d.vocab_hints

    engine_raw = _coerce(raw, "engine", str, d.engine, warnings)
    engine = cast(
        "Literal['local', 'api']",
        engine_raw if engine_raw in ("local", "api") else d.engine,
    )
    if engine_raw not in ("local", "api"):
        warnings.append(f"stt.engine '{engine_raw}' invalid; using '{d.engine}'.")

    return SttConfig(
        engine=engine,
        local_model=_coerce(raw, "local_model", str, d.local_model, warnings),
        api_model=_coerce(raw, "api_model", str, d.api_model, warnings),
        api_fallback=_coerce(raw, "api_fallback", bool, d.api_fallback, warnings),
        language=_coerce(raw, "language", str, d.language, warnings),
        vocab_hints=hints,
    )


def _parse_storage(raw: Any, warnings: list[str]) -> StorageConfig:
    if not isinstance(raw, dict):
        warnings.append("'storage' section is not a table; using defaults.")
        return StorageConfig()
    d = StorageConfig()
    known = {"transcripts_dir", "spool_dir", "keep_audio", "retention_days"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown storage key '{k}' ignored.")

    transcripts_dir_raw = raw.get("transcripts_dir")
    if transcripts_dir_raw is not None and isinstance(transcripts_dir_raw, str):
        transcripts_dir = Path(transcripts_dir_raw).expanduser()
    else:
        transcripts_dir = d.transcripts_dir

    spool_dir_raw = raw.get("spool_dir")
    if spool_dir_raw is not None and isinstance(spool_dir_raw, str):
        spool_dir = Path(spool_dir_raw).expanduser()
    else:
        spool_dir = d.spool_dir

    return StorageConfig(
        transcripts_dir=transcripts_dir,
        spool_dir=spool_dir,
        keep_audio=_coerce(raw, "keep_audio", bool, d.keep_audio, warnings),
        retention_days=_coerce(raw, "retention_days", int, d.retention_days, warnings),
    )


def _parse_hotkey(raw: Any, warnings: list[str]) -> HotkeyConfig:
    if not isinstance(raw, dict):
        warnings.append("'hotkey' section is not a table; using defaults.")
        return HotkeyConfig()
    d = HotkeyConfig()
    known = {"toggle"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown hotkey key '{k}' ignored.")
    return HotkeyConfig(
        toggle=_coerce(raw, "toggle", str, d.toggle, warnings),
    )


def _parse_notes(raw: Any, warnings: list[str]) -> NotesConfig:
    if not isinstance(raw, dict):
        warnings.append("'notes' section is not a table; using defaults.")
        return NotesConfig()
    d = NotesConfig()
    known = {"enabled"}
    for k in raw:
        if k not in known:
            warnings.append(f"Unknown notes key '{k}' ignored.")
    return NotesConfig(
        enabled=_coerce(raw, "enabled", bool, d.enabled, warnings),
    )


def _coerce(raw: dict[str, Any], key: str, typ: type, default: Any, warnings: list[str]) -> Any:
    """Return raw[key] coerced to *typ*, or *default* with a warning on failure."""
    if key not in raw:
        return default
    val = raw[key]
    if isinstance(val, typ):
        return val
    # Allow int→float coercion (TOML represents 1.0 as float, but user may write 1)
    if typ is float and isinstance(val, int):
        return float(val)
    warnings.append(
        f"Config key '{key}' expected {typ.__name__}, got {type(val).__name__}; "
        f"using default {default!r}."
    )
    return default
