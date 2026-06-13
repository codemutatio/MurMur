"""Serialize the app config back to ``config.toml`` for the Settings pane.

The stdlib only *reads* TOML (``tomllib``), so this module renders a complete,
canonical ``config.toml`` from a :class:`~murmur.config.Config` and writes it
crash-safely. Saving from the Settings pane rewrites the file in canonical form
(values preserved, hand-written comments/unknown keys dropped — those were
ignored at load time anyway). Changes apply on the next launch.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path

from murmur.config import Config


def _toml_str(value: str) -> str:
    """Render *value* as a TOML basic string (quoted, escaped)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_str_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_str(i) for i in items) + "]"


def serialize_config(config: Config) -> str:
    """Render *config* as a complete, re-parseable ``config.toml`` document."""
    c, s, st = config.capture, config.stt, config.storage
    h, n = config.hotkey, config.notes
    lines = [
        "# Murmur configuration — written by the Settings pane.",
        "# Edit and restart Murmur to apply changes.",
        "",
        "[capture]",
        f"device = {_toml_str(c.device)}",
        f"vad_threshold = {c.vad_threshold}",
        f"silence_split_s = {c.silence_split_s}",
        f"max_segment_s = {c.max_segment_s}",
        "",
        "[stt]",
        f"engine = {_toml_str(s.engine)}",
        f"local_model = {_toml_str(s.local_model)}",
        f"api_model = {_toml_str(s.api_model)}",
        f"api_fallback = {_toml_bool(s.api_fallback)}",
        f"language = {_toml_str(s.language)}",
        f"vocab_hints = {_toml_str_list(s.vocab_hints)}",
        "",
        "[storage]",
        f"transcripts_dir = {_toml_str(str(st.transcripts_dir))}",
        f"spool_dir = {_toml_str(str(st.spool_dir))}",
        f"keep_audio = {_toml_bool(st.keep_audio)}",
        f"retention_days = {st.retention_days}",
        "",
        "[hotkey]",
        f"toggle = {_toml_str(h.toggle)}",
        "",
        "[notes]",
        f"enabled = {_toml_bool(n.enabled)}",
        "",
    ]
    return "\n".join(lines)


def with_edits(
    config: Config,
    *,
    vocab_hints: tuple[str, ...] | None = None,
    api_fallback: bool | None = None,
    keep_audio: bool | None = None,
    retention_days: int | None = None,
    hotkey_toggle: str | None = None,
) -> Config:
    """Return a copy of *config* with the Settings-pane-editable fields applied.

    Only non-None arguments override; everything else is left untouched.
    """
    stt = config.stt
    if vocab_hints is not None:
        stt = replace(stt, vocab_hints=vocab_hints)
    if api_fallback is not None:
        stt = replace(stt, api_fallback=api_fallback)

    storage = config.storage
    if keep_audio is not None:
        storage = replace(storage, keep_audio=keep_audio)
    if retention_days is not None:
        storage = replace(storage, retention_days=retention_days)

    hotkey = config.hotkey
    if hotkey_toggle is not None:
        hotkey = replace(hotkey, toggle=hotkey_toggle)

    return replace(config, stt=stt, storage=storage, hotkey=hotkey)


def write_config(path: Path, config: Config) -> None:
    """Write *config* to *path* atomically (tmp + fsync + os.replace).

    Mirrors the crash-safe write pattern used by the transcript writer and
    state store.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = serialize_config(config)
    fd, tmp_str = tempfile.mkstemp(dir=path.parent, suffix=".toml.tmp")
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)
