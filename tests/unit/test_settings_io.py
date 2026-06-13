"""Tests for murmur.app.settings_io — config serialization round-trips."""

from __future__ import annotations

from pathlib import Path

from murmur.app.settings_io import serialize_config, with_edits, write_config
from murmur.config import Config, load_config


def _reload(tmp_path: Path, config: Config) -> Config:
    """Write *config* via write_config and load it back through load_config."""
    path = tmp_path / "config.toml"
    write_config(path, config)
    loaded, warnings = load_config(path)
    assert warnings == [], f"canonical config produced warnings: {warnings}"
    return loaded


def test_defaults_roundtrip(tmp_path: Path) -> None:
    """Serializing the default Config and reloading yields an equal Config."""
    assert _reload(tmp_path, Config()) == Config()


def test_edited_config_roundtrips(tmp_path: Path) -> None:
    edited = with_edits(
        Config(),
        vocab_hints=("Rivy", "Yassir", "BigQuery"),
        api_fallback=True,
        keep_audio=True,
        retention_days=7,
        hotkey_toggle="ctrl+shift+m",
    )
    reloaded = _reload(tmp_path, edited)
    assert reloaded == edited
    assert reloaded.stt.vocab_hints == ("Rivy", "Yassir", "BigQuery")
    assert reloaded.stt.api_fallback is True
    assert reloaded.storage.keep_audio is True
    assert reloaded.storage.retention_days == 7
    assert reloaded.hotkey.toggle == "ctrl+shift+m"


def test_with_edits_only_changes_given_fields() -> None:
    base = Config()
    out = with_edits(base, keep_audio=True)
    assert out.storage.keep_audio is True
    # Everything else untouched.
    assert out.stt == base.stt
    assert out.storage.retention_days == base.storage.retention_days
    assert out.hotkey == base.hotkey


def test_vocab_hints_with_special_chars_escape(tmp_path: Path) -> None:
    """Quotes and backslashes in vocab hints survive the TOML round-trip."""
    tricky = ('say "hi"', "back\\slash")
    cfg = with_edits(Config(), vocab_hints=tricky)
    assert _reload(tmp_path, cfg).stt.vocab_hints == tricky


def test_write_config_is_atomic(tmp_path: Path) -> None:
    """No stray .tmp file remains after a successful write."""
    path = tmp_path / "config.toml"
    write_config(path, Config())
    assert path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_serialize_is_valid_toml_with_sections() -> None:
    text = serialize_config(Config())
    for section in ("[capture]", "[stt]", "[storage]", "[hotkey]", "[notes]"):
        assert section in text
