"""Tests for murmur.config — T-2."""

from __future__ import annotations

from pathlib import Path

from murmur.config import CaptureConfig, Config, load_config


def _write_toml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# AC-30
def test_values_applied(tmp_path: Path) -> None:  # [AC-30]
    """Configured silence_split_s and max_segment_s are exactly applied."""
    cfg_file = _write_toml(
        tmp_path / "config.toml",
        "[capture]\nsilence_split_s = 1.5\nmax_segment_s = 120.0\n",
    )
    config, warnings = load_config(cfg_file)
    assert config.capture.silence_split_s == 1.5
    assert config.capture.max_segment_s == 120.0
    assert not warnings


# E-16
def test_missing_file_defaults(tmp_path: Path) -> None:  # [E-16]
    """Missing config file → all defaults, warning naming the expected path."""
    missing = tmp_path / "nonexistent.toml"
    config, warnings = load_config(missing)
    assert isinstance(config, Config)
    assert config.capture.silence_split_s == 1.5  # default
    assert len(warnings) == 1
    assert "nonexistent.toml" in warnings[0]


def test_none_path_defaults() -> None:
    """None path → all defaults."""
    config, warnings = load_config(None)
    assert isinstance(config, Config)
    assert len(warnings) == 1


# E-16 (unparseable)
def test_unparseable_file_defaults(tmp_path: Path) -> None:  # [E-16]
    """Unparseable TOML → all defaults + warning."""
    cfg_file = _write_toml(tmp_path / "config.toml", "not valid toml ][[[")
    config, warnings = load_config(cfg_file)
    assert isinstance(config, Config)
    assert any("could not be parsed" in w for w in warnings)


# E-17
def test_unknown_key_warns(tmp_path: Path) -> None:  # [E-17]
    """Unknown top-level key produces a warning."""
    cfg_file = _write_toml(tmp_path / "config.toml", "[mystery_section]\nfoo = 1\n")
    _, warnings = load_config(cfg_file)
    assert any("mystery_section" in w for w in warnings)


def test_unknown_nested_key_warns(tmp_path: Path) -> None:
    """Unknown nested key inside a known section produces a warning."""
    cfg_file = _write_toml(tmp_path / "config.toml", "[capture]\nfoo_unknown = 99\n")
    _, warnings = load_config(cfg_file)
    assert any("foo_unknown" in w for w in warnings)


def test_type_error_on_known_key_uses_default(tmp_path: Path) -> None:
    """Type error on a known key → default + warning (not a crash)."""
    cfg_file = _write_toml(
        tmp_path / "config.toml", "[capture]\nsilence_split_s = 'not_a_number'\n"
    )
    config, warnings = load_config(cfg_file)
    assert config.capture.silence_split_s == CaptureConfig().silence_split_s
    assert any("silence_split_s" in w for w in warnings)


def test_stt_section(tmp_path: Path) -> None:
    """STT section values are applied correctly."""
    cfg_file = _write_toml(
        tmp_path / "config.toml",
        '[stt]\nlocal_model = "large-v3-turbo"\napi_fallback = true\nvoice_hints = []\n',
    )
    config, _ = load_config(cfg_file)
    assert config.stt.local_model == "large-v3-turbo"
    assert config.stt.api_fallback is True


def test_vocab_hints_applied(tmp_path: Path) -> None:
    """vocab_hints list is parsed and exposed as a tuple."""
    cfg_file = _write_toml(
        tmp_path / "config.toml",
        '[stt]\nvocab_hints = ["Rivy", "BigQuery"]\n',
    )
    config, _ = load_config(cfg_file)
    assert "Rivy" in config.stt.vocab_hints
    assert "BigQuery" in config.stt.vocab_hints


def test_storage_paths_expanded(tmp_path: Path) -> None:
    """Storage paths are Path objects and tilde-expanded."""
    cfg_file = _write_toml(
        tmp_path / "config.toml",
        f'[storage]\ntranscripts_dir = "{tmp_path}/Transcripts"\n',
    )
    config, _ = load_config(cfg_file)
    assert isinstance(config.storage.transcripts_dir, Path)
    assert str(tmp_path) in str(config.storage.transcripts_dir)


def test_retention_days_int_coercion(tmp_path: Path) -> None:
    """retention_days = 7 (integer) is correctly parsed."""
    cfg_file = _write_toml(tmp_path / "config.toml", "[storage]\nretention_days = 7\n")
    config, _ = load_config(cfg_file)
    assert config.storage.retention_days == 7


def test_float_from_int_toml(tmp_path: Path) -> None:
    """Integer TOML value is coerced to float for float fields."""
    cfg_file = _write_toml(tmp_path / "config.toml", "[capture]\nsilence_split_s = 2\n")
    config, _ = load_config(cfg_file)
    assert config.capture.silence_split_s == 2.0
